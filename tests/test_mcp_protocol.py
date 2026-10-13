"""Exercises the actual MCP protocol path (client session <-> low-level
server over in-memory streams), not MCPServer.call_tool() directly -- that
convenience method is documented to raise on error rather than convert to
a graceful result, which is a different (and less interesting) behavior
than what a real client actually sees."""

import asyncio

import pytest
from mcp.client.session import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from app.auth.models import Workspace
from app.mcp.server import mcp

pytestmark = pytest.mark.asyncio


async def test_tool_calls_through_the_real_protocol(
    seeded_workspace: tuple[Workspace, str],
) -> None:
    _, api_key = seeded_workspace

    async with create_client_server_memory_streams() as (client_streams, server_streams):
        client_read, client_write = client_streams
        server_read, server_write = server_streams

        init_opts = mcp._lowlevel_server.create_initialization_options()  # pyright: ignore[reportPrivateUsage]
        lowlevel = mcp._lowlevel_server  # pyright: ignore[reportPrivateUsage]
        server_task = asyncio.ensure_future(
            lowlevel.run(server_read, server_write, init_opts, raise_exceptions=False)
        )

        try:
            async with ClientSession(client_read, client_write) as session:
                await session.initialize()

                good = await session.call_tool("list_tables", {"api_key": api_key})
                assert good.is_error is False

                bad_table = await session.call_tool(
                    "describe_table", {"api_key": api_key, "table_name": "does_not_exist"}
                )
                assert bad_table.is_error is True
                assert "does_not_exist" in str(bad_table.content)

                bad_key = await session.call_tool(
                    "list_tables", {"api_key": "jarvis_totally_bogus"}
                )
                assert bad_key.is_error is True
                assert "invalid or revoked" in str(bad_key.content)
        finally:
            server_task.cancel()
