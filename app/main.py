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
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware

from app.config import settings
from app.console.routes import LoginRequired, redirect_to_login, templates
from app.console.routes import router as console_router
from app.crypto import check_encryption_key
from app.db import SessionLocal, dispose_target_engines
from app.mcp.server import mcp
from app.tracing import configure_tracing, shutdown_tracing
from app.version import BUILD_VERSION


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
    # None unless OTEL_EXPORTER_OTLP_ENDPOINT is set (see app/tracing.py).
    tracer_provider = configure_tracing()
    try:
        # Started here because Starlette never runs a mounted sub-app's own
        # lifespan; without it every /mcp request would hang.
        async with mcp.session_manager.run():
            yield
    finally:
        await dispose_target_engines()
        if tracer_provider is not None:
            # Sends what's still queued, for 2 s at most, so a dead viewer
            # can't hold up a restart.
            shutdown_tracing(tracer_provider)


app = FastAPI(title="Database MCP Console", lifespan=lifespan)

STATIC_DIR = Path(__file__).parent / "console" / "static"

# The console's signed session cookie. same_site="strict" is the first
# CSRF defense (every form is a same-origin POST); the per-session form
# token in app/console/csrf.py is the second. https_only is on for the
# deployment, off for local plain-HTTP development.
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.console_session_secret,
    same_site="strict",
    https_only=settings.console_cookie_https_only,
)

app.include_router(console_router)
# A console route that needs a login sends the browser to the login page.
app.add_exception_handler(LoginRequired, redirect_to_login)
# Serves the compiled TypeScript (see static-src/) and any other static
# files the console's pages need, at /console/static/...
app.mount("/console/static", StaticFiles(directory=STATIC_DIR), name="console-static")
# The actual MCP server, reachable over a real network connection for
# the first time -- see docs/journey.md for why that wasn't true before.
app.mount("/mcp", mcp_app)


# The landing page's three demo steps. Each shows its file from DEMO_DIR
# once it exists (with an optional still for prefers-reduced-motion), and a
# same-sized placeholder until then. Width/height reserve the space so the
# page doesn't jump while a GIF loads.
DEMO_DIR = STATIC_DIR / "demo"
DEMO_STEPS = [
    {
        "title": "Ask a question.",
        "text": "Claude explores the schema, then runs one read-only query.",
        "file": "step1.gif",
        "still": "step1-still.png",
        "width": 1000,
        "height": 644,
        "alt": "Claude Code loads the query-database skill, calls list_tables and "
        "describe_table, then answers with the three most rented films.",
    },
    {
        "title": "Try to change data.",
        "text": "The server refuses anything but a single SELECT.",
        "file": "step2.gif",
        "still": "step2-still.png",
        "width": 1000,
        "height": 439,
        "alt": "Asked to run DELETE FROM rental, the server rejects it: only SELECT "
        "statements are allowed, so nothing was deleted.",
    },
    {
        "title": "Check what ran.",
        "text": "Every query, including refused ones, shows up in your audit trail.",
        "file": "step3.png",
        "still": None,
        "width": 1000,
        "height": 613,
        "alt": "The console's audit trail: the refused DELETE in red with its reason, "
        "the successful query with its cost, and a 14-day chart.",
    },
]


def _demo_steps() -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for step in DEMO_STEPS:
        present = (DEMO_DIR / str(step["file"])).exists()
        still = step["still"]
        steps.append(
            {
                **step,
                "src": f"/console/static/demo/{step['file']}" if present else None,
                "still": f"/console/static/demo/{still}"
                if present and still and (DEMO_DIR / str(still)).exists()
                else None,
            }
        )
    return steps


@app.get("/", include_in_schema=False)
async def landing(request: Request):
    """The public front page: what this is, the demo, and a way in."""
    steps = _demo_steps()
    return templates.TemplateResponse(
        request,
        "landing.html",
        {
            "logged_in": bool(request.session.get("workspace_id")),
            "steps": steps,
            # A still image makes the better link preview; the dashboard is ideal.
            "og_image": steps[-1]["src"],
            "base_url": str(request.base_url).rstrip("/"),
        },
    )


@app.get("/healthz", response_model=None)
async def healthz() -> dict[str, str] | JSONResponse:
    """For uptime monitors: 200 only if the server can actually reach its
    bookkeeping database (every tool call needs it), 503 otherwise. A
    check that only proves the process is up would stay green through a
    database outage. Also reports which build is running (app/version.py),
    so a deploy can be confirmed from outside."""
    try:
        async with asyncio.timeout(3), SessionLocal() as session:
            await session.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001 -- any failure to reach the database is "unhealthy"
        return JSONResponse(
            {"status": "unhealthy", "database": "unreachable", "version": BUILD_VERSION},
            status_code=503,
        )
    return {"status": "ok", "database": "ok", "version": BUILD_VERSION}
