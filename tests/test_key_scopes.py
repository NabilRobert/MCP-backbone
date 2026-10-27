"""Per-tool permissions on API keys (app/auth/scopes.py), exercised through
the real MCP protocol, the way an AI client hits them."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import pytest
from mcp.client.session import ClientSession
from mcp.shared.memory import create_client_server_memory_streams
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import ApiKey, Workspace
from app.auth.scopes import ALL_TOOLS, PRESETS
from app.auth.service import rotate_api_key
from app.main import app
from app.mcp.server import mcp
from app.query.models import QueryExecution
from tests.console_client import ConsoleClient

pytestmark = pytest.mark.asyncio


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


async def test_existing_keys_keep_full_access(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    key_row = (await db_session.execute(select(ApiKey))).scalar_one()
    assert sorted(key_row.scopes) == sorted(ALL_TOOLS)


@pytest.mark.parametrize(
    ("preset", "allowed"),
    [
        ("schema", {"list_tables", "describe_table"}),
        ("explore", {"list_tables", "describe_table", "sample_rows"}),
        ("full", set(ALL_TOOLS)),
    ],
)
async def test_each_preset_allows_exactly_its_tools(
    db_session: AsyncSession,
    seeded_workspace: tuple[Workspace, str],
    preset: str,
    allowed: set[str],
) -> None:
    workspace, _ = seeded_workspace
    key = await rotate_api_key(db_session, workspace, PRESETS[preset][1])
    calls = {
        "list_tables": {},
        "describe_table": {"table_name": "demo_items"},
        "sample_rows": {"table_name": "demo_items", "limit": 10},
        "run_query": {"sql": "select count(*) from demo_items"},
    }
    async with _mcp_session() as session:
        for tool, args in calls.items():
            result = await session.call_tool(tool, args, meta={"api_key": key})
            if tool in allowed:
                assert result.is_error is False, (tool, result.content)
            else:
                assert result.is_error is True, tool
                assert f"isn't allowed to use {tool}" in str(result.content)


async def test_refused_data_reads_are_still_audited(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    """A refused run_query or sample_rows is still an attempt to read data,
    so it's in the audit log, as rejected, with the reason."""
    workspace, _ = seeded_workspace
    key = await rotate_api_key(db_session, workspace, PRESETS["schema"][1])
    async with _mcp_session() as session:
        await session.call_tool("run_query", {"sql": "select 1"}, meta={"api_key": key})
        await session.call_tool(
            "sample_rows", {"table_name": "demo_items", "limit": 10}, meta={"api_key": key}
        )

    rows = (
        await db_session.execute(
            select(
                QueryExecution.query_text, QueryExecution.status, QueryExecution.rejection_reason
            )
        )
    ).all()
    assert {r[0] for r in rows} == {"select 1", 'SELECT * FROM "demo_items" LIMIT 10'}
    assert all(r[1] == "rejected" and "isn't allowed" in (r[2] or "") for r in rows)


async def test_console_issues_a_key_with_the_chosen_access(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    client = ConsoleClient(app, follow_redirects=False)
    client.post("/console/login", data={"username": "admin", "password": "testpass"})

    page = client.post("/console/keys/rotate", data={"access": "explore"})
    assert page.status_code == 200
    assert "Explore only" in page.text
    key_row = (
        await db_session.execute(select(ApiKey).where(ApiKey.status == "active"))
    ).scalar_one()
    assert set(key_row.scopes) == {"list_tables", "describe_table", "sample_rows"}

    unknown = client.post("/console/keys/rotate", data={"access": "superuser"})
    assert unknown.status_code == 400
