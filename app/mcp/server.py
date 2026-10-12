from typing import Any

from mcp.server.mcpserver import MCPServer

mcp = MCPServer("jarvis-database-mcp")


@mcp.tool()
async def list_tables(api_key: str) -> list[str]:
    raise NotImplementedError


@mcp.tool()
async def describe_table(api_key: str, table_name: str) -> dict[str, Any]:
    raise NotImplementedError


@mcp.tool()
async def sample_rows(api_key: str, table_name: str, limit: int = 10) -> list[dict[str, Any]]:
    raise NotImplementedError


@mcp.tool()
async def run_query(api_key: str, sql: str) -> dict[str, Any]:
    raise NotImplementedError
