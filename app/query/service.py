import asyncio
import datetime
from typing import Any

import asyncpg  # pyright: ignore[reportMissingTypeStubs] -- asyncpg ships no stubs
from sqlalchemy import inspect, select, text
from sqlalchemy.engine import Inspector
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.auth.models import Workspace
from app.auth.rate_limit import RateLimitExceeded, check_and_increment
from app.config import settings
from app.query.cost_check import check_cost
from app.query.models import DataSourceConnection, QueryExecution
from app.query.validation import QueryRejected, validate_and_normalize

# Transient infra failures get retried with backoff (Phase 4). A query
# that's merely slow/expensive is never retried -- statement_timeout
# surfaces as asyncpg.exceptions.QueryCanceledError, handled separately
# below, and retrying an expensive query just repeats the cost.
TRANSIENT_EXCEPTIONS = (
    asyncpg.exceptions.ConnectionDoesNotExistError,
    asyncpg.exceptions.ConnectionFailureError,
    asyncpg.exceptions.InterfaceError,
    OSError,
)
MAX_ATTEMPTS = 2
RETRY_BACKOFF_SECONDS = 0.5


class QueryExecutionError(Exception):
    pass


def _asyncpg_cause(exc: BaseException) -> BaseException | None:
    orig = getattr(exc, "orig", None)
    return getattr(orig, "__cause__", None)


async def _get_data_source(session: AsyncSession, workspace: Workspace) -> DataSourceConnection:
    result = await session.execute(
        select(DataSourceConnection).where(DataSourceConnection.workspace_id == workspace.id)
    )
    data_source = result.scalar_one_or_none()
    if data_source is None:
        raise QueryExecutionError("workspace has no data source connection configured")
    return data_source


async def _write_audit(
    session: AsyncSession,
    workspace: Workspace,
    data_source: DataSourceConnection,
    sql: str,
    status: str,
    cost_estimate: float | None,
    rejection_reason: str | None,
) -> None:
    # If this write fails, the exception propagates and no result is
    # returned -- a successful query must never go un-audited (Phase 4).
    session.add(
        QueryExecution(
            workspace_id=workspace.id,
            data_source_connection_id=data_source.id,
            query_text=sql,
            status=status,
            cost_estimate=cost_estimate,
            rejection_reason=rejection_reason,
            created_at=datetime.datetime.now(datetime.UTC),
        )
    )
    await session.commit()


async def list_tables(session: AsyncSession, workspace: Workspace) -> list[str]:
    data_source = await _get_data_source(session, workspace)
    engine = create_async_engine(data_source.connection_url)
    try:
        async with engine.connect() as conn:

            def _get_table_names(sync_conn: Any) -> list[str]:
                inspector: Inspector = inspect(sync_conn)
                return inspector.get_table_names()

            return await conn.run_sync(_get_table_names)
    finally:
        await engine.dispose()


async def run_query(session: AsyncSession, workspace: Workspace, sql: str) -> dict[str, Any]:
    data_source = await _get_data_source(session, workspace)

    try:
        await check_and_increment(
            session, workspace, settings.demo_rate_limit, settings.demo_rate_limit_window_seconds
        )
    except RateLimitExceeded as exc:
        await _write_audit(session, workspace, data_source, sql, "rejected", None, exc.reason)
        return {"status": "rejected", "rows": [], "error": exc.reason}

    try:
        normalized_sql = validate_and_normalize(sql)
    except QueryRejected as exc:
        await _write_audit(session, workspace, data_source, sql, "rejected", None, exc.reason)
        return {"status": "rejected", "rows": [], "error": exc.reason}

    status = "succeeded"
    error_message: str | None = None
    cost: float | None = None
    rows: list[dict[str, Any]] = []

    engine = create_async_engine(data_source.connection_url)
    try:
        async with engine.connect() as conn:
            try:
                cost = await check_cost(
                    conn, normalized_sql, settings.cost_threshold, settings.explain_timeout_seconds
                )
            except QueryRejected as exc:
                await _write_audit(session, workspace, data_source, sql, "rejected", None, exc.reason)
                return {"status": "rejected", "rows": [], "error": exc.reason}
            except Exception as exc:  # noqa: BLE001 -- e.g. EXPLAIN on a table that doesn't exist
                await _write_audit(session, workspace, data_source, sql, "errored", None, str(exc))
                return {"status": "errored", "rows": [], "error": str(exc)}

            await conn.execute(
                text(f"SET statement_timeout = {settings.query_timeout_seconds * 1000}")
            )

            for attempt in range(1, MAX_ATTEMPTS + 1):
                try:
                    result = await conn.execute(text(normalized_sql))
                    rows = [
                        dict(row._mapping)  # pyright: ignore[reportPrivateUsage]
                        for row in result
                    ]
                    break
                except Exception as exc:  # noqa: BLE001 -- classified below, never silently swallowed
                    cause = _asyncpg_cause(exc)
                    if isinstance(cause, asyncpg.exceptions.QueryCanceledError):
                        status, error_message = "timed_out", "statement timeout exceeded"
                        break
                    if isinstance(cause, TRANSIENT_EXCEPTIONS) and attempt < MAX_ATTEMPTS:
                        await asyncio.sleep(RETRY_BACKOFF_SECONDS)
                        continue
                    status, error_message = "errored", str(exc)
                    break
    finally:
        await engine.dispose()

    await _write_audit(session, workspace, data_source, sql, status, cost, error_message)
    return {"status": status, "rows": rows, "error": error_message}
