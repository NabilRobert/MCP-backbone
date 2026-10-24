"""The front door. This is the only file an AI client actually talks to
-- it declares the four tools (list_tables, describe_table, sample_rows,
run_query) using the MCP protocol library, and every one of them does
the same two things: check who's asking (app/auth/service.py), then
hand off to the real logic in app/query/."""

import functools
from collections.abc import Awaitable, Callable
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from app.auth.scopes import DESCRIBE_TABLE, LIST_TABLES, RUN_QUERY, SAMPLE_ROWS
from app.auth.service import AuthenticationError, ToolNotPermitted, authenticate
from app.db import SessionLocal
from app.query import schema_cache
from app.query import service as query_service
from app.query.service import QueryExecutionError
from app.version import BUILD_VERSION

mcp = MCPServer("database-mcp", version=BUILD_VERSION)


def _resolve_api_key(ctx: Context, api_key: str | None) -> str:
    """Over HTTP, the key must come in the `Authorization: Bearer` header
    (matched case-insensitively; ctx.headers is a plain Mapping). A key
    passed as a tool argument travels through the AI model's context and
    transcripts, so it's refused there. Local transports (stdio,
    in-memory, as the benchmark uses) have no headers (ctx.headers is
    None) and keep using the api_key argument."""
    headers = ctx.headers
    if headers is not None:
        auth_header = next((v for k, v in headers.items() if k.lower() == "authorization"), None)
        if auth_header:
            return auth_header.removeprefix("Bearer ").strip()
        raise AuthenticationError(
            "missing API key: over HTTP, send it as an 'Authorization: Bearer <key>' header "
            "(the api_key argument is only accepted on local transports)"
        )
    if api_key:
        return api_key
    raise AuthenticationError("missing API key (pass the api_key argument)")


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
async def list_tables(ctx: Context, api_key: str | None = None) -> list[str]:
    """Tool: "what tables exist in my database?\""""
    async with SessionLocal() as session:
        workspace = await authenticate(session, _resolve_api_key(ctx, api_key), LIST_TABLES)
        return await query_service.list_tables(session, workspace)


@mcp.tool()
@_translate_errors
async def describe_table(
    ctx: Context, table_name: str, api_key: str | None = None
) -> dict[str, Any]:
    """Tool: "what columns does this specific table have?\""""
    async with SessionLocal() as session:
        workspace = await authenticate(session, _resolve_api_key(ctx, api_key), DESCRIBE_TABLE)
        return await schema_cache.describe_table(session, workspace, table_name)


@mcp.tool()
@_translate_errors
async def sample_rows(
    ctx: Context, table_name: str, limit: int = 10, api_key: str | None = None
) -> list[dict[str, Any]]:
    """Tool: "show me a few example rows from this table.\""""
    async with SessionLocal() as session:
        try:
            workspace = await authenticate(session, _resolve_api_key(ctx, api_key), SAMPLE_ROWS)
        except ToolNotPermitted as exc:
            # An attempt to read data, so it's audited even when refused.
            await query_service.record_refusal(
                session, exc.workspace, f'SELECT * FROM "{table_name}" LIMIT {limit}', str(exc)
            )
            raise
        return await schema_cache.sample_rows(session, workspace, table_name, limit)


@mcp.tool()
@_translate_errors
async def run_query(ctx: Context, sql: str, api_key: str | None = None) -> dict[str, Any]:
    """Tool: "run this SQL and give me the results." This is the one
    that goes through every safety check (see app/query/validation.py
    and app/query/cost_check.py) before anything actually executes."""
    async with SessionLocal() as session:
        try:
            workspace = await authenticate(session, _resolve_api_key(ctx, api_key), RUN_QUERY)
        except ToolNotPermitted as exc:
            await query_service.record_refusal(session, exc.workspace, sql, str(exc))
            raise
        return await query_service.run_query(session, workspace, sql)
