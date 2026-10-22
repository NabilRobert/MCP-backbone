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

from app.auth.service import AuthenticationError, authenticate
from app.db import SessionLocal
from app.query import schema_cache
from app.query import service as query_service
from app.query.service import QueryExecutionError

mcp = MCPServer("database-mcp")


def _resolve_api_key(ctx: Context, api_key: str | None) -> str:
    """Prefers a real HTTP client's "Authorization: Bearer <key>"
    header over the explicit api_key argument below -- the header is
    the standard way a remote MCP client authenticates, and it means
    the AI model never has to be told to fill in a credential at all.
    Falls back to the argument for stdio/in-memory transport, where
    there's no HTTP request to carry a header on (ctx.headers is None
    there) -- this is exactly what scripts/run_benchmark.py's own
    key-injecting wrapper already relies on, so this stays working
    unchanged for it. Checked case-insensitively by hand rather than
    assuming a specific headers object's own case handling, since
    ctx.headers is typed as a plain Mapping.

    Over HTTP (ctx.headers is not None), ONLY the header is accepted.
    A key passed as a tool argument travels through the AI model's own
    context -- it lands in transcripts and the model can repeat it --
    so a remote client must use the header, which the model never
    sees. The argument stays for local transports, where there's no
    header to use."""
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
        workspace = await authenticate(session, _resolve_api_key(ctx, api_key))
        return await query_service.list_tables(session, workspace)


@mcp.tool()
@_translate_errors
async def describe_table(
    ctx: Context, table_name: str, api_key: str | None = None
) -> dict[str, Any]:
    """Tool: "what columns does this specific table have?\""""
    async with SessionLocal() as session:
        workspace = await authenticate(session, _resolve_api_key(ctx, api_key))
        return await schema_cache.describe_table(session, workspace, table_name)


@mcp.tool()
@_translate_errors
async def sample_rows(
    ctx: Context, table_name: str, limit: int = 10, api_key: str | None = None
) -> list[dict[str, Any]]:
    """Tool: "show me a few example rows from this table.\""""
    async with SessionLocal() as session:
        workspace = await authenticate(session, _resolve_api_key(ctx, api_key))
        return await schema_cache.sample_rows(session, workspace, table_name, limit)


@mcp.tool()
@_translate_errors
async def run_query(ctx: Context, sql: str, api_key: str | None = None) -> dict[str, Any]:
    """Tool: "run this SQL and give me the results." This is the one
    that goes through every safety check (see app/query/validation.py
    and app/query/cost_check.py) before anything actually executes."""
    async with SessionLocal() as session:
        workspace = await authenticate(session, _resolve_api_key(ctx, api_key))
        return await query_service.run_query(session, workspace, sql)
