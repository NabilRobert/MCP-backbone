"""The web dashboard's pages. Note: this is currently a SCAFFOLD, not a
finished feature (see ARCHITECTURE.md's "Open Gaps" section) -- these
routes render a page, but nothing real is wired up yet. There's no
actual list of API keys shown, and the "dashboard" doesn't yet pull any
real audit-log rows from the database."""

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

router = APIRouter(prefix="/console")
# Jinja2Templates knows how to fill in the .html files in the
# neighboring templates/ folder with real data (once that's built).
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


@router.get("/")
async def dashboard(request: Request):
    """The main dashboard page -- meant to eventually show the audit
    trail (every query this server has run). Currently just renders an
    empty page."""
    return templates.TemplateResponse(request, "dashboard.html", {})


@router.get("/keys")
async def keys(request: Request):
    """The key-management page -- meant to eventually let you view and
    revoke a workspace's API key from the browser. Currently just
    renders an empty page."""
    return templates.TemplateResponse(request, "keys.html", {})
