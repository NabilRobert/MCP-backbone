"""Starts the web part of this project AND the MCP server, as one
process on one port. The console dashboard pages (app/console/) and
the MCP server's tools (app/mcp/server.py) are still two separate
things -- one serves ordinary web pages to a human's browser, the
other talks to AI clients over the MCP protocol -- they just now share
a single FastAPI app, mounted at different paths, instead of needing
two separate processes to run and expose."""

import contextlib
from collections.abc import AsyncGenerator

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import settings
from app.console.routes import router as console_router
from app.mcp.server import mcp

# mcp.streamable_http_app() returns its own little Starlette app with
# its own lifespan (it starts a background session manager task on
# startup). Mounting a sub-app with app.mount() does NOT run that
# sub-app's lifespan automatically -- Starlette only sends the ASGI
# "lifespan" protocol to the root app, never to anything mounted under
# it. Confirmed by reading Starlette's own Mount implementation, not
# assumed: without the combined lifespan below, /mcp would accept
# connections but every request would hang, since the session manager
# that actually handles them would never have started.
#
# streamable_http_path="/" because app.mount("/mcp", ...) below already
# adds the "/mcp" prefix -- leaving this at its own default of "/mcp"
# made the real path "/mcp/mcp" (confirmed by actually requesting it:
# the first attempt 404'd).
mcp_app = mcp.streamable_http_app(streamable_http_path="/")


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    async with mcp.session_manager.run():
        yield


app = FastAPI(title="Database MCP Console", lifespan=lifespan)

# Lets the console remember who's logged in via a signed cookie (see
# app/console/auth.py). same_site="strict" means the cookie is never
# sent on a request originating from another site -- the project's
# CSRF defense for the console, chosen instead of per-form tokens
# since every form here is a same-origin POST. Harmless for MCP
# requests under /mcp: this middleware only ever ADDS a `request.session`
# capability, it never requires or rejects anything for a request that
# doesn't use it.
app.add_middleware(
    SessionMiddleware, secret_key=settings.console_session_secret, same_site="strict"
)

app.include_router(console_router)
# Serves the compiled TypeScript (see static-src/) and any other static
# files the console's pages need, at /console/static/...
app.mount("/console/static", StaticFiles(directory="app/console/static"), name="console-static")
# The actual MCP server, reachable over a real network connection for
# the first time -- see JOURNEY.md for why that wasn't true before.
app.mount("/mcp", mcp_app)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """A trivial endpoint that just confirms the server is up and
    responding -- useful for monitoring/deployment tools to check."""
    return {"status": "ok"}
