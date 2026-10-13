import datetime
import json
from typing import Any

from sqlalchemy import inspect, select, text
from sqlalchemy.engine import Inspector
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.auth.models import Workspace
from app.query.models import DataSourceConnection, SchemaCache
from app.query.service import QueryExecutionError, get_data_source

CACHE_TTL_SECONDS = 300


async def _fetch_live(connection_url: str) -> dict[str, list[dict[str, str]]]:
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
    """Advisory cache only (Phase 3/4): never consulted during query
    execution, only here for schema exploration. A stale entry can cause
    describe_table/sample_rows to miss a brand-new table until the TTL
    expires -- never wrong data, since run_query always goes live."""
    now = datetime.datetime.now(datetime.UTC)
    result = await session.execute(
        select(SchemaCache).where(SchemaCache.workspace_id == workspace.id)
    )
    cache = result.scalar_one_or_none()

    if cache is not None and (now - cache.cached_at).total_seconds() < CACHE_TTL_SECONDS:
        metadata: dict[str, list[dict[str, str]]] = json.loads(cache.table_metadata_json)
        return metadata

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
    data_source = await get_data_source(session, workspace)
    metadata = await get_table_metadata(session, workspace, data_source)
    if table_name not in metadata:
        raise QueryExecutionError(f"table '{table_name}' not found")
    return {"table": table_name, "columns": metadata[table_name]}


async def sample_rows(
    session: AsyncSession, workspace: Workspace, table_name: str, limit: int = 10
) -> list[dict[str, Any]]:
    data_source = await get_data_source(session, workspace)
    metadata = await get_table_metadata(session, workspace, data_source)
    if table_name not in metadata:
        raise QueryExecutionError(f"table '{table_name}' not found")

    safe_limit = max(1, min(int(limit), 100))
    # table_name is validated against the real, live table list above --
    # never used before that check -- so this isn't blind string
    # interpolation; escaping embedded quotes is extra defense, not the
    # only defense.
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
