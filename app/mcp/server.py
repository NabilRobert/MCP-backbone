import functools
from collections.abc import Awaitable, Callable
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from app.auth.service import AuthenticationError, authenticate
from app.db import SessionLocal
from app.query import schema_cache
from app.query import service as query_service
from app.query.service import QueryExecutionError

mcp = MCPServer("database-mcp")


def _translate_errors[T](fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
    """AuthenticationError and QueryExecutionError are anticipated failures
    (bad key, missing table) -- ToolError tells the SDK to return
    is_error=True with the message, not crash with a traceback. Every tool
    below hits these via authenticate() at minimum, so wiring this once
    here beats repeating try/except in each tool body."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> T:
        try:
            return await fn(*args, **kwargs)
        except (AuthenticationError, QueryExecutionError) as exc:
            raise ToolError(str(exc)) from exc

    return wrapper


@mcp.tool()
@_translate_errors
async def list_tables(api_key: str) -> list[str]:
    async with SessionLocal() as session:
        workspace = await authenticate(session, api_key)
        return await query_service.list_tables(session, workspace)


@mcp.tool()
@_translate_errors
async def describe_table(api_key: str, table_name: str) -> dict[str, Any]:
    async with SessionLocal() as session:
        workspace = await authenticate(session, api_key)
        return await schema_cache.describe_table(session, workspace, table_name)


@mcp.tool()
@_translate_errors
async def sample_rows(api_key: str, table_name: str, limit: int = 10) -> list[dict[str, Any]]:
    async with SessionLocal() as session:
        workspace = await authenticate(session, api_key)
        return await schema_cache.sample_rows(session, workspace, table_name, limit)


@mcp.tool()
@_translate_errors
async def run_query(api_key: str, sql: str) -> dict[str, Any]:
    async with SessionLocal() as session:
        workspace = await authenticate(session, api_key)
        return await query_service.run_query(session, workspace, sql)
