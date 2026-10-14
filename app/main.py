"""Starts the web part of this project -- the console dashboard pages
(app/console/) are served from here. This is a SEPARATE thing from the
MCP server in app/mcp/server.py: that one talks to AI clients over the
MCP protocol; this one serves ordinary web pages to a human's browser."""

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import settings
from app.console.routes import router as console_router

app = FastAPI(title="Database MCP Console")

# Lets the console remember who's logged in via a signed cookie (see
# app/console/auth.py). same_site="strict" means the cookie is never
# sent on a request originating from another site -- the project's
# CSRF defense for the console, chosen instead of per-form tokens
# since every form here is a same-origin POST.
app.add_middleware(
    SessionMiddleware, secret_key=settings.console_session_secret, same_site="strict"
)

app.include_router(console_router)
# Serves the compiled TypeScript (see static-src/) and any other static
# files the console's pages need, at /console/static/...
app.mount("/console/static", StaticFiles(directory="app/console/static"), name="console-static")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """A trivial endpoint that just confirms the server is up and
    responding -- useful for monitoring/deployment tools to check."""
    return {"status": "ok"}
