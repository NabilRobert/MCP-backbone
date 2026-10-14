"""Starts the web part of this project -- the console dashboard pages
(app/console/) are served from here. This is a SEPARATE thing from the
MCP server in app/mcp/server.py: that one talks to AI clients over the
MCP protocol; this one serves ordinary web pages to a human's browser."""

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.console.routes import router as console_router

app = FastAPI(title="Database MCP Console")

app.include_router(console_router)
# Serves the compiled TypeScript (see static-src/) and any other static
# files the console's pages need, at /console/static/...
app.mount("/console/static", StaticFiles(directory="app/console/static"), name="console-static")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """A trivial endpoint that just confirms the server is up and
    responding -- useful for monitoring/deployment tools to check."""
    return {"status": "ok"}
