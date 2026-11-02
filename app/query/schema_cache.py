"""Powers the describe_table and sample_rows tools -- the "let me look
around before I write SQL" tools an AI client uses. Unlike run_query,
these answer from a saved copy (a cache) of the schema when they can, to
avoid asking the real database the same "what tables/columns exist?"
question over and over."""

import datetime
import json
from typing import Any

from sqlalchemy import inspect, select, text
from sqlalchemy.engine import Inspector
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import Workspace
from app.auth.rate_limit import RateLimitExceeded, allowance, check_and_increment
from app.config import settings
from app.query.models import DataSourceConnection, SchemaCache
from app.query.service import QueryOutcome, visible_table_names, write_audit
from app.query.target import (
    QueryExecutionError,
    caller_safe_error,
    connect_to_target,
    ensure_target_allowed,
    find_data_source,
    get_data_source,
)

# How long a saved copy of the schema stays "fresh" before we bother
# asking the real database again.
CACHE_TTL_SECONDS = 300
# The most rows sample_rows returns, whatever the caller asks for.
MAX_SAMPLE_ROWS = 100


def sample_rows_audit_text(table_name: str, limit: int) -> str:
    """How a sample_rows call reads in the audit log: as the query it
    stands for. Shared with app/mcp/server.py, which audits a refused call."""
    return f'SELECT * FROM "{table_name}" LIMIT {limit}'


async def _fetch_live(connection_url: str) -> dict[str, list[dict[str, str]]]:
    """Connects to the real database and asks it directly: what tables
    exist, and what columns (name + type) does each one have? Returns
    something like {"customers": [{"name": "id", "type": "INTEGER"}, ...]}."""
    async with connect_to_target(connection_url) as conn:

        def _inspect(sync_conn: Any) -> dict[str, list[dict[str, str]]]:
            inspector: Inspector = inspect(sync_conn)
            return {
                table: [
                    {"name": col["name"], "type": str(col["type"])}
                    for col in inspector.get_columns(table)
                ]
                for table in visible_table_names(sync_conn)
            }

        return await conn.run_sync(_inspect)


async def get_table_metadata(
    session: AsyncSession, workspace: Workspace, data_source: DataSourceConnection
) -> dict[str, list[dict[str, str]]]:
    """Returns the saved copy of the schema if it's still fresh enough
    (see CACHE_TTL_SECONDS), otherwise fetches a new one from the real
    database and saves that instead.

    Important: this cache is a convenience only, never a safety
    decision. If it's out of date for a few minutes, the worst that
    happens is describe_table/sample_rows show slightly stale
    information -- the actual run_query tool always goes straight to
    the live database regardless of what this cache says, so a stale
    cache can never cause a real query to return wrong data."""
    now = datetime.datetime.now(datetime.UTC)
    result = await session.execute(
        select(SchemaCache).where(SchemaCache.workspace_id == workspace.id)
    )
    cache = result.scalar_one_or_none()

    if cache is not None and (now - cache.cached_at).total_seconds() < CACHE_TTL_SECONDS:
        # Still fresh -- use the saved copy instead of asking the
        # database again.
        metadata: dict[str, list[dict[str, str]]] = json.loads(cache.table_metadata_json)
        return metadata

    # Either there's no saved copy yet, or it's too old -- get a fresh
    # one and save it for next time.
    metadata = await _fetch_live(data_source.connection_url)
    metadata_json = json.dumps(metadata)
    if cache is None:
        session.add(
            SchemaCache(workspace_id=workspace.id, table_metadata_json=metadata_json, cached_at=now)
        )
    else:
        cache.table_metadata_json = metadata_json
        cache.cached_at = now
    await session.commit()
    return metadata


async def describe_table(
    session: AsyncSession, workspace: Workspace, table_name: str
) -> dict[str, Any]:
    """Answers "what columns does this table have?" using the cache
    above. Rejects the request if the table doesn't actually exist."""
    data_source = await get_data_source(session, workspace)
    metadata = await get_table_metadata(session, workspace, data_source)
    if table_name not in metadata:
        raise QueryExecutionError(f"table '{table_name}' not found")
    return {"table": table_name, "columns": metadata[table_name]}


async def _check_sample_allowed(
    session: AsyncSession, workspace: Workspace, data_source: DataSourceConnection, audit_sql: str
) -> None:
    """The address check and the rate limit. A refusal by either is
    audited, then raised."""
    try:
        await ensure_target_allowed(data_source)
    except QueryExecutionError as exc:
        refusal = QueryOutcome(status="rejected", error=str(exc))
        await write_audit(session, data_source, audit_sql, refusal)
        raise

    try:
        await check_and_increment(
            session,
            workspace,
            allowance(workspace.sample_rate_limit, settings.sample_rate_limit),
            settings.demo_rate_limit_window_seconds,
            bucket="sample",
        )
    except RateLimitExceeded as exc:
        refusal = QueryOutcome(status="rejected", error=exc.reason)
        await write_audit(session, data_source, audit_sql, refusal)
        raise QueryExecutionError(f"sample_rows {exc.reason}") from exc


async def _read_rows(connection_url: str, table_name: str, limit: int) -> list[dict[str, Any]]:
    # A technical note on why this is safe to build as a plain string
    # below, instead of being a SQL-injection risk: the caller just
    # checked table_name against metadata, which only ever contains REAL
    # table names read directly from the database itself -- it's not
    # raw, unchecked input. Escaping any stray quote characters in the
    # name is an extra layer of care on top of that check, not the
    # only thing keeping this safe.
    quoted_name = table_name.replace('"', '""')
    async with connect_to_target(connection_url) as conn:
        result = await conn.execute(
            text(f'SELECT * FROM "{quoted_name}" LIMIT :limit'), {"limit": limit}
        )
        return [
            dict(row._mapping)  # pyright: ignore[reportPrivateUsage]
            for row in result
        ]


async def sample_rows(
    session: AsyncSession, workspace: Workspace, table_name: str, limit: int = 10
) -> list[dict[str, Any]]:
    """Answers "show me a few example rows from this table" -- always
    goes straight to the live database (never the cache), since this is
    actual data, not just schema information.

    Because it returns real data, every call is written to the audit
    log and counted against its own rate-limit allowance, the same way
    run_query is. Before this, a client could read the first 100 rows
    of every table, as often as it liked, without leaving a trace."""
    data_source = await find_data_source(session, workspace)

    # Keep the sample small, no matter what was asked for.
    safe_limit = max(1, min(int(limit), MAX_SAMPLE_ROWS))
    audit_sql = sample_rows_audit_text(table_name, safe_limit)

    await _check_sample_allowed(session, workspace, data_source, audit_sql)

    rows: list[dict[str, Any]] | None = None
    try:
        metadata = await get_table_metadata(session, workspace, data_source)
        if table_name in metadata:
            rows = await _read_rows(data_source.connection_url, table_name, safe_limit)
    except Exception as exc:  # recorded, then re-raised as a clean tool error
        reason = caller_safe_error(exc)
        failure = QueryOutcome(status="errored", error=reason)
        await write_audit(session, data_source, audit_sql, failure)
        raise QueryExecutionError(f"sample_rows failed: {reason}") from exc

    if rows is None:
        reason = f"table '{table_name}' not found"
        refusal = QueryOutcome(status="rejected", error=reason)
        await write_audit(session, data_source, audit_sql, refusal)
        raise QueryExecutionError(reason)

    await write_audit(session, data_source, audit_sql, QueryOutcome(status="succeeded"))
    return rows
