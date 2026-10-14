"""The front door. This is the only file an AI client actually talks to
-- it declares the four tools (list_tables, describe_table, sample_rows,
run_query) using the MCP protocol library, and every one of them does
the same two things: check who's asking (app/auth/service.py), then
hand off to the real logic in app/query/."""

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
    """A wrapper placed around every tool function below. Its job: turn
    our own "expected" errors (a bad API key, a table that doesn't
    exist) into a clean message the AI model can actually read and
    react to -- instead of letting them crash as unhandled exceptions,
    which the MCP protocol would otherwise report back as a generic,
    unhelpful "something went wrong" with no detail. Written once here
    and reused on every tool, instead of repeating the same
    try/except in each one."""

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
    """Tool: "what tables exist in my database?\""""
    async with SessionLocal() as session:
        workspace = await authenticate(session, api_key)
        return await query_service.list_tables(session, workspace)


@mcp.tool()
@_translate_errors
async def describe_table(api_key: str, table_name: str) -> dict[str, Any]:
    """Tool: "what columns does this specific table have?\""""
    async with SessionLocal() as session:
        workspace = await authenticate(session, api_key)
        return await schema_cache.describe_table(session, workspace, table_name)


@mcp.tool()
@_translate_errors
async def sample_rows(api_key: str, table_name: str, limit: int = 10) -> list[dict[str, Any]]:
    """Tool: "show me a few example rows from this table.\""""
    async with SessionLocal() as session:
        workspace = await authenticate(session, api_key)
        return await schema_cache.sample_rows(session, workspace, table_name, limit)


@mcp.tool()
@_translate_errors
async def run_query(api_key: str, sql: str) -> dict[str, Any]:
    """Tool: "run this SQL and give me the results." This is the one
    that goes through every safety check (see app/query/validation.py
    and app/query/cost_check.py) before anything actually executes."""
    async with SessionLocal() as session:
        workspace = await authenticate(session, api_key)
        return await query_service.run_query(session, workspace, sql)
