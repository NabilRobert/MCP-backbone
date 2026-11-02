"""The front door. This is the only file an AI client actually talks to
-- it declares the four tools (list_tables, describe_table, sample_rows,
run_query) using the MCP protocol library, and every one of them does
the same two things: check who's asking (app/auth/service.py), then
hand off to the real logic in app/query/."""

import functools
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import Tool
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import Workspace
from app.auth.scopes import DESCRIBE_TABLE, LIST_TABLES, RUN_QUERY, SAMPLE_ROWS
from app.auth.service import AuthenticationError, ToolNotPermitted, authenticate
from app.db import SessionLocal
from app.query import schema_cache
from app.query import service as query_service
from app.query.target import QueryExecutionError
from app.tracing import stage
from app.version import BUILD_VERSION

logger = logging.getLogger(__name__)

# For a failure we didn't expect, whose own text can't be trusted to be
# safe: it could name a database host. The details go to the server log.
UNEXPECTED_ERROR = "the server hit an unexpected error: try again in a moment"


class _StrictSchemaServer(MCPServer):
    """Publishes every tool's input schema with additionalProperties
    false. With every argument required and none defaulted (see the tools
    below), that makes each schema valid in OpenAI's strict mode, which
    some clients switch on for every tool and which rejects the whole
    request otherwise. The server itself still ignores unknown arguments,
    so older clients that send extras keep working."""

    async def list_tools(self) -> list[Tool]:
        tools = await super().list_tools()
        for tool in tools:
            schema = tool.input_schema
            tool.input_schema = {
                **schema,
                "required": schema.get("required", []),
                "additionalProperties": False,
            }
        return tools


mcp = _StrictSchemaServer("database-mcp", version=BUILD_VERSION)


def _resolve_api_key(ctx: Context) -> str:
    """Over HTTP, the key must come in the `Authorization: Bearer` header
    (matched case-insensitively; ctx.headers is a plain Mapping). Local
    transports (stdio, in-memory, as the benchmark uses) have no headers
    (ctx.headers is None) and send it in the request's `_meta` instead.
    Neither is visible to the AI model: a tool argument would travel
    through its context and transcripts, so there is none."""
    headers = ctx.headers
    if headers is not None:
        auth_header = next((v for k, v in headers.items() if k.lower() == "authorization"), None)
        if auth_header:
            return auth_header.removeprefix("Bearer ").strip()
        raise AuthenticationError(
            "missing API key: over HTTP, send it as an 'Authorization: Bearer <key>' header "
            "(local transports send it as _meta.api_key instead)"
        )
    meta = ctx.request_context.meta or {}
    api_key = meta.get("api_key")
    if isinstance(api_key, str) and api_key:
        return api_key
    raise AuthenticationError("missing API key (send it as _meta.api_key on the tool call)")


async def _authenticate(session: AsyncSession, ctx: Context, scope: str) -> Workspace:
    """authenticate() for a tool call, timed as its own "authenticate"
    span in the request's trace (app/tracing.py). A refusal shows up on
    that span as the exception's class name, e.g. ToolNotPermitted."""
    with stage("authenticate"):
        return await authenticate(session, _resolve_api_key(ctx), scope)


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
        except Exception as exc:
            logger.exception("a tool call failed unexpectedly")
            raise ToolError(UNEXPECTED_ERROR) from exc

    return wrapper


@mcp.tool()
@_translate_errors
async def list_tables(ctx: Context) -> list[str]:
    """Tool: "what tables exist in my database?\""""
    async with SessionLocal() as session:
        workspace = await _authenticate(session, ctx, LIST_TABLES)
        return await query_service.list_tables(session, workspace)


@mcp.tool()
@_translate_errors
async def describe_table(ctx: Context, table_name: str) -> dict[str, Any]:
    """Tool: "what columns does this specific table have?\""""
    async with SessionLocal() as session:
        workspace = await _authenticate(session, ctx, DESCRIBE_TABLE)
        return await schema_cache.describe_table(session, workspace, table_name)


# `limit` has no default: strict-mode clients need every argument required.
@mcp.tool()
@_translate_errors
async def sample_rows(ctx: Context, table_name: str, limit: int) -> list[dict[str, Any]]:
    """Tool: "show me a few example rows from this table." `limit` is how
    many rows, 1 to 100; 10 is usually enough."""
    async with SessionLocal() as session:
        try:
            workspace = await _authenticate(session, ctx, SAMPLE_ROWS)
        except ToolNotPermitted as exc:
            # An attempt to read data, so it's audited even when refused.
            await query_service.record_refusal(
                session,
                exc.workspace,
                schema_cache.sample_rows_audit_text(table_name, limit),
                str(exc),
            )
            raise
        return await schema_cache.sample_rows(session, workspace, table_name, limit)


@mcp.tool()
@_translate_errors
async def run_query(ctx: Context, sql: str) -> dict[str, Any]:
    """Tool: "run this SQL and give me the results." This is the one
    that goes through every safety check (see app/query/validation.py
    and app/query/cost_check.py) before anything actually executes."""
    async with SessionLocal() as session:
        try:
            workspace = await _authenticate(session, ctx, RUN_QUERY)
        except ToolNotPermitted as exc:
            await query_service.record_refusal(session, exc.workspace, sql, str(exc))
            raise
        return await query_service.run_query(session, workspace, sql)
