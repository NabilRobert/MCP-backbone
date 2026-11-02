"""What happens when the target database fails: a dropped connection is
retried on a new one, every attempt is still audited, and nothing the
caller or the audit log sees names the target's host or port. Real
connections throughout: a stand-in can't show whether a dead connection
really recovers."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from mcp.client.session import ClientSession
from mcp.shared.memory import create_client_server_memory_streams
from mcp.types import TextContent
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.auth.models import ApiKey, RateLimitCounter, Workspace
from app.config import settings
from app.db import engine
from app.mcp import server as mcp_server
from app.mcp.server import UNEXPECTED_ERROR, mcp
from app.query import schema_cache, service, target
from app.query.models import DataSourceConnection, QueryExecution, SchemaCache
from app.query.service import (
    _run_with_retry,  # pyright: ignore[reportPrivateUsage]
)
from app.query.target import TARGET_NOT_ALLOWED, TARGET_UNREACHABLE

pytestmark = pytest.mark.asyncio

CLOSED_PORT = 59999
UNREACHABLE_URL = f"postgresql+asyncpg://nobody:pw@127.0.0.1:{CLOSED_PORT}/none"
# What must never reach a caller or the audit log. A user's own URL host
# isn't secret from them, but the default database's is, and the text can't
# tell the two apart.
TELLTALES = ("127.0.0.1", str(CLOSED_PORT), "localhost", "Connect call failed", "nobody")
# Unique, so the query can be found in pg_stat_activity.
SLOW_SQL = "select pg_sleep(1.0137) as slept"


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "RETRY_BACKOFF_SECONDS", 0)


def _target_url() -> str:
    return engine.url.render_as_string(hide_password=False)


async def _kill_slow_query(times: int) -> None:
    """Terminates the backend running SLOW_SQL, `times` times over, the way
    a database restart or a network drop would end it."""
    admin = create_async_engine(_target_url(), isolation_level="AUTOCOMMIT")
    killed = 0
    try:
        async with admin.connect() as conn:
            while killed < times:
                result = await conn.execute(
                    text(
                        "select pg_terminate_backend(pid) from pg_stat_activity "
                        "where query = :sql and state = 'active'"
                    ),
                    {"sql": SLOW_SQL},
                )
                killed += len(result.all())
                await asyncio.sleep(0.02)
    finally:
        await admin.dispose()


async def _point_workspace_at(session: AsyncSession, connection_url: str) -> None:
    data_source = (await session.execute(select(DataSourceConnection))).scalar_one()
    data_source.connection_url = connection_url
    await session.commit()


async def _only_audit_row(session: AsyncSession) -> QueryExecution:
    session.expire_all()
    return (await session.execute(select(QueryExecution))).scalar_one()


def _assert_no_telltales(message: str | None) -> None:
    for telltale in TELLTALES:
        assert telltale not in (message or ""), f"{telltale!r} leaked in: {message}"


@asynccontextmanager
async def _mcp_session() -> AsyncGenerator[ClientSession]:
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        lowlevel = mcp._lowlevel_server  # pyright: ignore[reportPrivateUsage]
        server_task = asyncio.ensure_future(
            lowlevel.run(
                *server_streams, lowlevel.create_initialization_options(), raise_exceptions=False
            )
        )
        try:
            async with ClientSession(*client_streams) as session:
                await session.initialize()
                yield session
        finally:
            server_task.cancel()


async def _call_tool(tool: str, args: dict[str, Any], api_key: str) -> tuple[bool, str]:
    """(is_error, text) of a tool call, as an AI client sees it."""
    async with _mcp_session() as session:
        result = await session.call_tool(tool, args, meta={"api_key": api_key})
    first = result.content[0]
    assert isinstance(first, TextContent)
    return bool(result.is_error), first.text


async def test_really_dropped_connection_is_retried_on_a_new_one(
    db_session: AsyncSession,
) -> None:
    killer = asyncio.create_task(_kill_slow_query(times=1))

    outcome, attempts = await _run_with_retry(_target_url(), SLOW_SQL, 1.0)
    await killer

    assert outcome.status == "succeeded"
    assert attempts == 2


async def test_connection_dropped_on_every_attempt_ends_as_errored_without_the_host(
    db_session: AsyncSession,
) -> None:
    killer = asyncio.create_task(_kill_slow_query(times=service.MAX_ATTEMPTS))

    outcome, attempts = await _run_with_retry(_target_url(), SLOW_SQL, 1.0)
    await killer

    assert outcome.status == "errored"
    assert outcome.error == TARGET_UNREACHABLE
    assert attempts == service.MAX_ATTEMPTS


async def test_a_retried_query_still_has_the_statement_timeout(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SET LOCAL dies with the first attempt's transaction, so without
    setting it again a retried query would run with no time limit."""
    monkeypatch.setattr(settings, "query_timeout_seconds", 1)
    killer = asyncio.create_task(_kill_slow_query(times=1))

    outcome, attempts = await _run_with_retry(_target_url(), SLOW_SQL, 1.0)
    await killer

    assert outcome.status == "timed_out"
    assert attempts == 2


async def test_statement_timeout_is_not_retried(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "query_timeout_seconds", 1)

    outcome, attempts = await _run_with_retry(_target_url(), "select pg_sleep(5)", 1.0)

    assert outcome.status == "timed_out"
    assert outcome.cost == 1.0
    assert attempts == 1


async def test_a_bad_query_is_not_retried_and_keeps_its_own_error(
    db_session: AsyncSession,
) -> None:
    outcome, attempts = await _run_with_retry(_target_url(), "select * from no_such_table", 1.0)

    assert outcome.status == "errored"
    assert "no_such_table" in (outcome.error or "")
    assert attempts == 1


async def test_run_query_on_an_unreachable_target_is_audited_without_the_host(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    _, api_key = seeded_workspace
    await _point_workspace_at(db_session, UNREACHABLE_URL)

    is_error, reply = await _call_tool("run_query", {"sql": "select 1"}, api_key)

    assert not is_error
    assert TARGET_UNREACHABLE in reply
    _assert_no_telltales(reply)
    row = await _only_audit_row(db_session)
    assert row.status == "errored"
    assert row.rejection_reason == TARGET_UNREACHABLE


async def test_target_lost_after_the_cost_check_is_audited_with_the_cost(
    db_session: AsyncSession,
    seeded_workspace: tuple[Workspace, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, _ = seeded_workspace
    real_get_engine = target.get_target_engine
    calls = 0

    def engine_lost_after_first_use(_url: str) -> Any:
        nonlocal calls
        calls += 1
        return real_get_engine(_target_url() if calls == 1 else UNREACHABLE_URL)

    monkeypatch.setattr(target, "get_target_engine", engine_lost_after_first_use)

    response = await service.run_query(db_session, workspace, "select 1")

    assert response["status"] == "errored"
    assert response["error"] == TARGET_UNREACHABLE
    row = await _only_audit_row(db_session)
    assert row.status == "errored"
    assert row.cost_estimate is not None
    _assert_no_telltales(row.rejection_reason)


async def test_run_query_on_a_refused_address_is_audited_without_the_host(
    db_session: AsyncSession,
    seeded_workspace: tuple[Workspace, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, api_key = seeded_workspace
    monkeypatch.setattr(settings, "allow_private_targets", False)

    is_error, reply = await _call_tool("run_query", {"sql": "select 1"}, api_key)

    assert not is_error
    assert TARGET_NOT_ALLOWED in reply
    _assert_no_telltales(reply)
    row = await _only_audit_row(db_session)
    assert row.status == "rejected"
    assert row.rejection_reason == TARGET_NOT_ALLOWED


async def test_a_refused_address_uses_up_no_rate_limit(
    db_session: AsyncSession,
    seeded_workspace: tuple[Workspace, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, _ = seeded_workspace
    monkeypatch.setattr(settings, "allow_private_targets", False)

    await service.run_query(db_session, workspace, "select 1")

    counters = (await db_session.execute(select(RateLimitCounter))).scalars().all()
    assert counters == []


async def test_a_key_refusal_is_audited_even_when_the_address_is_refused_too(
    db_session: AsyncSession,
    seeded_workspace: tuple[Workspace, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, api_key = seeded_workspace
    key_row = (await db_session.execute(select(ApiKey))).scalar_one()
    key_row.scopes = ["list_tables"]
    await db_session.commit()
    monkeypatch.setattr(settings, "allow_private_targets", False)

    is_error, _ = await _call_tool("run_query", {"sql": "select 1"}, api_key)

    assert is_error
    assert (await _only_audit_row(db_session)).status == "rejected"


async def test_sample_rows_on_an_unreachable_target_is_audited_without_the_host(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    """With no cached schema, the failure is in looking up the table."""
    _, api_key = seeded_workspace
    await _point_workspace_at(db_session, UNREACHABLE_URL)

    is_error, reply = await _call_tool(
        "sample_rows", {"table_name": "workspaces", "limit": 1}, api_key
    )

    assert is_error
    _assert_no_telltales(reply)
    row = await _only_audit_row(db_session)
    assert row.status == "errored"
    assert row.rejection_reason == TARGET_UNREACHABLE


async def test_sample_rows_losing_the_target_after_a_cached_schema_is_audited(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    """With a cached schema, the failure is in reading the rows."""
    workspace, api_key = seeded_workspace
    await schema_cache.describe_table(db_session, workspace, "workspaces")
    assert (await db_session.execute(select(SchemaCache))).scalar_one() is not None
    await _point_workspace_at(db_session, UNREACHABLE_URL)

    is_error, reply = await _call_tool(
        "sample_rows", {"table_name": "workspaces", "limit": 1}, api_key
    )

    assert is_error
    _assert_no_telltales(reply)
    row = await _only_audit_row(db_session)
    assert row.status == "errored"
    assert row.rejection_reason == TARGET_UNREACHABLE


async def test_sample_rows_on_a_refused_address_is_audited_without_the_host(
    db_session: AsyncSession,
    seeded_workspace: tuple[Workspace, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, api_key = seeded_workspace
    monkeypatch.setattr(settings, "allow_private_targets", False)

    is_error, reply = await _call_tool(
        "sample_rows", {"table_name": "workspaces", "limit": 1}, api_key
    )

    assert is_error
    assert TARGET_NOT_ALLOWED in reply
    _assert_no_telltales(reply)
    assert (await _only_audit_row(db_session)).status == "rejected"


@pytest.mark.parametrize(
    ("tool", "args"),
    [("list_tables", {}), ("describe_table", {"table_name": "workspaces"})],
)
async def test_schema_tools_on_an_unreachable_target_fail_without_the_host(
    db_session: AsyncSession,
    seeded_workspace: tuple[Workspace, str],
    tool: str,
    args: dict[str, Any],
) -> None:
    _, api_key = seeded_workspace
    await _point_workspace_at(db_session, UNREACHABLE_URL)

    is_error, reply = await _call_tool(tool, args, api_key)

    assert is_error
    assert TARGET_UNREACHABLE in reply
    _assert_no_telltales(reply)


async def test_an_unexpected_failure_reaches_the_caller_without_its_own_text(
    db_session: AsyncSession,
    seeded_workspace: tuple[Workspace, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, api_key = seeded_workspace

    async def fail_with_a_host(*_args: object) -> None:
        raise RuntimeError(f"Connect call failed ('127.0.0.1', {CLOSED_PORT})")

    monkeypatch.setattr(mcp_server.query_service, "list_tables", fail_with_a_host)

    is_error, reply = await _call_tool("list_tables", {}, api_key)

    assert is_error
    assert UNEXPECTED_ERROR in reply
    _assert_no_telltales(reply)
