"""Two ways a query can end without an answer: the caller cancels it, or
the target database never answers. Either way the attempt is audited,
and neither holds the request open for long."""

import asyncio
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import anyio
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import Workspace
from app.config import settings
from app.db import SessionLocal, engine
from app.query import schema_cache
from app.query.models import DataSourceConnection, QueryExecution
from app.query.service import QUERY_CANCELLED, run_query
from app.query.target import TARGET_UNREACHABLE

pytestmark = pytest.mark.asyncio

# Passes the validator and the cost check (Postgres guesses 1,000 rows for a
# function), then counts for several seconds.
SLOW_SQL = "select count(*) from generate_series(1, 100000000) as slow_series"
SILENT_TIMEOUT_SECONDS = 1


async def _wait_until_running(fragment: str) -> None:
    """Returns once a query containing `fragment` is running on the target."""
    for _ in range(200):
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    "select count(*) from pg_stat_activity "
                    "where state = 'active' and query ilike :pattern and pid <> pg_backend_pid()"
                ),
                {"pattern": f"%{fragment}%"},
            )
            if result.scalar_one() > 0:
                return
        await asyncio.sleep(0.02)
    raise AssertionError(f"no running query matching {fragment!r}")


async def _audit_rows(session: AsyncSession) -> list[QueryExecution]:
    session.expire_all()
    return list((await session.execute(select(QueryExecution))).scalars())


async def _cancel_while_running(call: object, fragment: str) -> None:
    """Runs `call` and cancels it once its query is running, the way the MCP
    SDK cancels a request: through an anyio cancel scope, which cancels
    every later await in that scope too, not just the current one."""
    async with anyio.create_task_group() as group:
        group.start_soon(call)  # pyright: ignore[reportArgumentType]
        await _wait_until_running(fragment)
        group.cancel_scope.cancel()


async def test_a_cancelled_query_is_still_audited(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    workspace, _ = seeded_workspace

    async def query() -> None:
        async with SessionLocal() as session:
            await run_query(session, workspace, SLOW_SQL)

    await _cancel_while_running(query, "slow_series")

    rows = await _audit_rows(db_session)
    assert [(row.status, row.rejection_reason) for row in rows] == [("errored", QUERY_CANCELLED)]


async def test_a_cancelled_sample_is_still_audited(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    """A sample is a quick read, so another session holds a lock on the
    table to keep it waiting until it's cancelled."""
    workspace, _ = seeded_workspace
    await db_session.execute(text("create table locked_sample (id int)"))
    await db_session.commit()

    async def sample() -> None:
        async with SessionLocal() as session:
            await schema_cache.sample_rows(session, workspace, "locked_sample", 1)

    async with engine.connect() as locker:
        await locker.execute(text("lock table locked_sample in access exclusive mode"))
        await _cancel_while_running(sample, 'from "locked_sample"')
        await locker.rollback()
    rows = await _audit_rows(db_session)
    await db_session.execute(text("drop table locked_sample"))
    await db_session.commit()

    assert [(row.status, row.rejection_reason) for row in rows] == [("errored", QUERY_CANCELLED)]


@asynccontextmanager
async def _silent_server() -> AsyncGenerator[int]:
    """A TCP port that accepts connections and then says nothing, like a
    host behind a firewall that drops packets. Yields the port."""
    stop = asyncio.Event()

    async def hold_open(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await stop.wait()
        writer.close()

    server = await asyncio.start_server(hold_open, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        stop.set()
        server.close()


async def test_a_silent_target_fails_fast_and_is_audited(
    db_session: AsyncSession,
    seeded_workspace: tuple[Workspace, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, _ = seeded_workspace
    monkeypatch.setattr(settings, "target_connect_timeout_seconds", SILENT_TIMEOUT_SECONDS)

    async with _silent_server() as port:
        data_source = (await db_session.execute(select(DataSourceConnection))).scalar_one()
        data_source.connection_url = f"postgresql+asyncpg://nobody:pw@127.0.0.1:{port}/none"
        await db_session.commit()

        started = time.monotonic()
        response = await asyncio.wait_for(run_query(db_session, workspace, "select 1"), 15)
        elapsed = time.monotonic() - started

    assert response["error"] == TARGET_UNREACHABLE
    assert elapsed < SILENT_TIMEOUT_SECONDS + 3
    assert [row.status for row in await _audit_rows(db_session)] == ["errored"]
