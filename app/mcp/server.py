from typing import Any

from mcp.server.mcpserver import MCPServer

from app.auth.service import authenticate
from app.db import SessionLocal
from app.query import service as query_service

mcp = MCPServer("jarvis-database-mcp")


@mcp.tool()
async def list_tables(api_key: str) -> list[str]:
    async with SessionLocal() as session:
        workspace = await authenticate(session, api_key)
        return await query_service.list_tables(session, workspace)


@mcp.tool()
async def describe_table(api_key: str, table_name: str) -> dict[str, Any]:
    # Slice D: backed by SchemaCache, not part of the Slice A skeleton.
    raise NotImplementedError


@mcp.tool()
async def sample_rows(api_key: str, table_name: str, limit: int = 10) -> list[dict[str, Any]]:
    # Slice D: backed by SchemaCache, not part of the Slice A skeleton.
    raise NotImplementedError


@mcp.tool()
async def run_query(api_key: str, sql: str) -> dict[str, Any]:
    # Slice A only: no sqlglot validation or EXPLAIN cost check yet (Slice B).
    async with SessionLocal() as session:
        workspace = await authenticate(session, api_key)
        return await query_service.run_query(session, workspace, sql)
