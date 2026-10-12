from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

router = APIRouter(prefix="/console")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


@router.get("/")
async def dashboard(request: Request):
    return templates.TemplateResponse(request, "dashboard.html", {})


@router.get("/keys")
async def keys(request: Request):
    return templates.TemplateResponse(request, "keys.html", {})
