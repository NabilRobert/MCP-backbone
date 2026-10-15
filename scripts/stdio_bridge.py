"""Bridges a local stdio MCP client (Claude Desktop, or anything else
that only knows how to spawn a local command) to this server's real
deployment over HTTP -- a Python-native alternative to the Node-based
`mcp-remote` bridge, for anyone who'd rather not add a Node dependency
just for this one thing.

Claude Desktop spawns this script exactly like any other local stdio
MCP server. This script's only job is forwarding: every message read
from stdin goes to the real HTTP server (with the given API key as a
Bearer header), and every message the server sends back goes to
stdout. Claude Desktop never needs to know the real server isn't
local."""

import argparse
from typing import Any

import anyio
from mcp.client.streamable_http import streamable_http_client
from mcp.server.stdio import stdio_server
from mcp.shared._httpx_utils import create_mcp_http_client


async def _forward(source: Any, destination: Any) -> None:
    async for item in source:
        # ReadStream can yield an Exception (a malformed message on that
        # side) as well as a real message -- WriteStream only accepts
        # real messages, so an Exception here means stop, not "forward
        # the exception object as if it were a message".
        if isinstance(item, Exception):
            raise item
        await destination.send(item)


async def bridge(url: str, api_key: str) -> None:
    http_client = create_mcp_http_client(headers={"Authorization": f"Bearer {api_key}"})
    async with (
        http_client,
        streamable_http_client(url, http_client=http_client) as (remote_read, remote_write),
        stdio_server() as (local_read, local_write),
        anyio.create_task_group() as tg,
    ):
        tg.start_soon(_forward, local_read, remote_write)
        tg.start_soon(_forward, remote_read, local_write)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="The deployed server's /mcp URL.")
    parser.add_argument("--api-key", required=True, help="Your workspace's API key.")
    args = parser.parse_args()
    anyio.run(bridge, args.url, args.api_key)


if __name__ == "__main__":
    main()
