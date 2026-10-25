"""Exercises the actual MCP protocol path (client session <-> low-level
server over in-memory streams), not MCPServer.call_tool() directly -- that
convenience method is documented to raise on error rather than convert to
a graceful result, which is a different (and less interesting) behavior
than what a real client actually sees."""

import asyncio

import httpx2
import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.memory import create_client_server_memory_streams

from app.auth.models import Workspace
from app.main import app
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

                good = await session.call_tool("list_tables", {}, meta={"api_key": api_key})
                assert good.is_error is False

                bad_table = await session.call_tool(
                    "describe_table", {"table_name": "does_not_exist"}, meta={"api_key": api_key}
                )
                assert bad_table.is_error is True
                assert "does_not_exist" in str(bad_table.content)

                bad_key = await session.call_tool(
                    "list_tables", {}, meta={"api_key": "jarvis_totally_bogus"}
                )
                assert bad_key.is_error is True
                assert "invalid or revoked" in str(bad_key.content)

                # The old way, a key as a tool argument, is ignored now:
                # the argument no longer exists, and extras are dropped.
                via_argument = await session.call_tool("list_tables", {"api_key": api_key})
                assert via_argument.is_error is True
                assert "missing API key" in str(via_argument.content)
        finally:
            server_task.cancel()


async def test_tool_calls_over_real_http_transport_with_header_auth(
    seeded_workspace: tuple[Workspace, str],
) -> None:
    """The in-memory-streams test above exercises the MCP protocol
    itself, but never the actual HTTP mount (app/main.py) or the new
    Authorization-header auth path -- ctx.headers is always None on
    that in-memory transport, so a regression there would pass that
    test while silently breaking every real HTTP client. Drives this
    through httpx2's ASGI transport (real ASGI HTTP semantics, no real
    socket needed) against the app's actual mounted /mcp route, with
    the API key set as a header and never passed as a tool argument at
    all -- proving the header path works on its own, not just as a
    fallback alongside the argument."""
    _, api_key = seeded_workspace

    # app.mount() doesn't run a mounted sub-app's lifespan automatically
    # (see app/main.py's own comment on this) -- entering it by hand
    # here is what actually starts the MCP session manager background
    # task that /mcp's requests depend on.
    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        # base_url must be an actual host:port the SDK's DNS-rebinding
        # protection allows for a loopback-bound server -- confirmed by
        # actually running this: "http://testserver" (httpx's usual
        # placeholder) was rejected outright, and even "http://localhost"
        # alone still was, since the allowed-hosts check requires a
        # ":<port>" suffix to match its "localhost:*" pattern, not just
        # the bare hostname. The port number itself doesn't matter --
        # nothing is actually listening on it here.
        async with (
            httpx2.AsyncClient(
                transport=transport,
                base_url="http://localhost:8000",
                headers={"Authorization": f"Bearer {api_key}"},
            ) as http_client,
            streamable_http_client("http://localhost:8000/mcp", http_client=http_client) as (
                read_stream,
                write_stream,
            ),
            ClientSession(read_stream, write_stream) as session,
        ):
            await session.initialize()

            good = await session.call_tool("list_tables", {})
            assert good.is_error is False

            bad_table = await session.call_tool("describe_table", {"table_name": "does_not_exist"})
            assert bad_table.is_error is True
            assert "does_not_exist" in str(bad_table.content)

        # Over HTTP the key must come in the header, and nowhere else:
        # not as a tool argument (which travels through the AI model's
        # own context and transcripts), and not in _meta (the local
        # transports' route), even when it's valid. Same
        # server lifetime as above -- the SDK's session manager can only
        # be started once per process.
        async with (
            httpx2.AsyncClient(transport=transport, base_url="http://localhost:8000") as bare,
            streamable_http_client("http://localhost:8000/mcp", http_client=bare) as (
                read_stream,
                write_stream,
            ),
            ClientSession(read_stream, write_stream) as session,
        ):
            await session.initialize()
            via_argument = await session.call_tool("list_tables", {"api_key": api_key})
            assert via_argument.is_error is True
            assert "Authorization" in str(via_argument.content)
            via_meta = await session.call_tool("list_tables", {}, meta={"api_key": api_key})
            assert via_meta.is_error is True
            assert "Authorization" in str(via_meta.content)

