import datetime
from typing import Any

from sqlalchemy import inspect, select, text
from sqlalchemy.engine import Inspector
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.auth.models import Workspace
from app.query.models import DataSourceConnection, QueryExecution


class QueryExecutionError(Exception):
    pass


async def _get_connection_url(session: AsyncSession, workspace: Workspace) -> str:
    result = await session.execute(
        select(DataSourceConnection).where(DataSourceConnection.workspace_id == workspace.id)
    )
    connection = result.scalar_one_or_none()
    if connection is None:
        raise QueryExecutionError("workspace has no data source connection configured")
    return connection.connection_url


async def list_tables(session: AsyncSession, workspace: Workspace) -> list[str]:
    connection_url = await _get_connection_url(session, workspace)
    engine = create_async_engine(connection_url)
    try:
        async with engine.connect() as conn:

            def _get_table_names(sync_conn: Any) -> list[str]:
                inspector: Inspector = inspect(sync_conn)
                return inspector.get_table_names()

            return await conn.run_sync(_get_table_names)
    finally:
        await engine.dispose()


async def run_query(session: AsyncSession, workspace: Workspace, sql: str) -> dict[str, Any]:
    """Slice A (walking skeleton) only: runs the given SQL as-is against the
    workspace's data source, and always writes a QueryExecution record. No
    sqlglot validation or EXPLAIN cost check yet -- that's Slice B."""
    connection_url = await _get_connection_url(session, workspace)
    data_source = (
        await session.execute(
            select(DataSourceConnection).where(DataSourceConnection.workspace_id == workspace.id)
        )
    ).scalar_one()

    engine = create_async_engine(connection_url)
    status = "succeeded"
    error_message: str | None = None
    rows: list[dict[str, Any]] = []
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text(sql))
            rows = [dict(row._mapping) for row in result]  # pyright: ignore[reportPrivateUsage]
    except Exception as exc:  # noqa: BLE001 -- any DB error becomes an "errored" audit row
        status = "errored"
        error_message = str(exc)
    finally:
        await engine.dispose()

    # If this write fails, the exception propagates and no result is
    # returned -- a successful query must never go un-audited (Phase 4).
    session.add(
        QueryExecution(
            workspace_id=workspace.id,
            data_source_connection_id=data_source.id,
            query_text=sql,
            status=status,
            cost_estimate=None,
            rejection_reason=error_message,
            created_at=datetime.datetime.now(datetime.UTC),
        )
    )
    await session.commit()

    return {"status": status, "rows": rows, "error": error_message}
