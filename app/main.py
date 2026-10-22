"""Starts the web part of this project AND the MCP server, as one
process on one port. The console dashboard pages (app/console/) and
the MCP server's tools (app/mcp/server.py) are still two separate
things -- one serves ordinary web pages to a human's browser, the
other talks to AI clients over the MCP protocol -- they just now share
a single FastAPI app, mounted at different paths, instead of needing
two separate processes to run and expose."""

import asyncio
import contextlib
from collections.abc import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware

from app.config import settings
from app.console.routes import router as console_router
from app.crypto import check_encryption_key
from app.db import SessionLocal, dispose_target_engines
from app.mcp.server import mcp


# The MCP SDK's DNS-rebinding protection only accepts localhost Host
# headers by default, so APP_MCP_ALLOWED_HOSTS names the real domain. Each
# host is allowed bare and with any port: "host:*" only matches a Host
# header that has a port (mcp.my.id:8443), but a request through the
# reverse proxy on 443 arrives as plain "mcp.my.id".
def transport_security_for(extra_hosts: list[str]) -> TransportSecuritySettings:
    return TransportSecuritySettings(
        allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*"]
        + [pattern for host in extra_hosts for pattern in (host, f"{host}:*")],
        allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
        + [
            origin
            for host in extra_hosts
            for scheme in ("http", "https")
            for origin in (f"{scheme}://{host}", f"{scheme}://{host}:*")
        ],
    )


_extra_hosts = [h.strip() for h in settings.mcp_allowed_hosts.split(",") if h.strip()]
# "/" because app.mount("/mcp", ...) below already adds the prefix.
mcp_app = mcp.streamable_http_app(
    streamable_http_path="/", transport_security=transport_security_for(_extra_hosts)
)


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    # Refuse to start without a usable encryption key, rather than
    # starting fine and failing every tool call (see app/crypto.py).
    check_encryption_key()
    try:
        # Started here because Starlette never runs a mounted sub-app's own
        # lifespan; without it every /mcp request would hang.
        async with mcp.session_manager.run():
            yield
    finally:
        await dispose_target_engines()


app = FastAPI(title="Database MCP Console", lifespan=lifespan)

# The console's signed session cookie. same_site="strict" is the console's
# CSRF defense (every form is a same-origin POST). https_only is on for the
# deployment, off for local plain-HTTP development.
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.console_session_secret,
    same_site="strict",
    https_only=settings.console_cookie_https_only,
)

app.include_router(console_router)
# Serves the compiled TypeScript (see static-src/) and any other static
# files the console's pages need, at /console/static/...
app.mount("/console/static", StaticFiles(directory="app/console/static"), name="console-static")
# The actual MCP server, reachable over a real network connection for
# the first time -- see JOURNEY.md for why that wasn't true before.
app.mount("/mcp", mcp_app)


@app.get("/", include_in_schema=False)
async def root(request: Request) -> RedirectResponse:
    """The bare domain is the front door: logged-in users go to their
    dashboard, everyone else to signup (which links to login)."""
    if request.session.get("workspace_id"):
        return RedirectResponse("/console/", status_code=307)
    return RedirectResponse("/console/signup", status_code=307)


@app.get("/healthz", response_model=None)
async def healthz() -> dict[str, str] | JSONResponse:
    """For uptime monitors: 200 only if the server can actually reach its
    bookkeeping database (every tool call needs it), 503 otherwise. A
    check that only proves the process is up would stay green through a
    database outage."""
    try:
        async with asyncio.timeout(3), SessionLocal() as session:
            await session.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001 -- any failure to reach the database is "unhealthy"
        return JSONResponse({"status": "unhealthy", "database": "unreachable"}, status_code=503)
    return {"status": "ok", "database": "ok"}
