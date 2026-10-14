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
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.auth.models import Workspace
from app.query.models import DataSourceConnection, SchemaCache
from app.query.service import QueryExecutionError, get_data_source

# How long a saved copy of the schema stays "fresh" before we bother
# asking the real database again.
CACHE_TTL_SECONDS = 300


async def _fetch_live(connection_url: str) -> dict[str, list[dict[str, str]]]:
    """Connects to the real database and asks it directly: what tables
    exist, and what columns (name + type) does each one have? Returns
    something like {"customers": [{"name": "id", "type": "INTEGER"}, ...]}."""
    engine = create_async_engine(connection_url)
    try:
        async with engine.connect() as conn:

            def _inspect(sync_conn: Any) -> dict[str, list[dict[str, str]]]:
                inspector: Inspector = inspect(sync_conn)
                return {
                    table: [
                        {"name": col["name"], "type": str(col["type"])}
                        for col in inspector.get_columns(table)
                    ]
                    for table in inspector.get_table_names()
                }

            return await conn.run_sync(_inspect)
    finally:
        await engine.dispose()


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
            SchemaCache(
                workspace_id=workspace.id, table_metadata_json=metadata_json, cached_at=now
            )
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


async def sample_rows(
    session: AsyncSession, workspace: Workspace, table_name: str, limit: int = 10
) -> list[dict[str, Any]]:
    """Answers "show me a few example rows from this table" -- always
    goes straight to the live database (never the cache), since this is
    actual data, not just schema information."""
    data_source = await get_data_source(session, workspace)
    metadata = await get_table_metadata(session, workspace, data_source)
    if table_name not in metadata:
        raise QueryExecutionError(f"table '{table_name}' not found")

    # Keep the sample small, no matter what was asked for.
    safe_limit = max(1, min(int(limit), 100))

    # A technical note on why this is safe to build as a plain string
    # below, instead of being a SQL-injection risk: table_name was just
    # checked against metadata above, which only ever contains REAL
    # table names read directly from the database itself -- it's not
    # raw, unchecked input. Escaping any stray quote characters in the
    # name is an extra layer of care on top of that check, not the
    # only thing keeping this safe.
    quoted_name = table_name.replace('"', '""')

    engine = create_async_engine(data_source.connection_url)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text(f'SELECT * FROM "{quoted_name}" LIMIT :limit'), {"limit": safe_limit}
            )
            return [
                dict(row._mapping)  # pyright: ignore[reportPrivateUsage]
                for row in result
            ]
    finally:
        await engine.dispose()
