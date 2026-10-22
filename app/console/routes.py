"""The web dashboard's pages: log in with a ConsoleCredential, view the
audit trail, and manage the workspace's API key. Everything here reads
from data the rest of the app already produces (QueryExecution rows,
ApiKey status) -- the console adds no new capability, just a way to
see it in a browser."""

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import Workspace
from app.auth.service import get_active_api_key, revoke_api_key, rotate_api_key
from app.auth.signup import MIN_PASSWORD_LENGTH, SignupError, sign_up
from app.console.auth import get_logged_in_workspace, log_in, log_out, verify_console_login
from app.console.throttle import login_limiter
from app.db import get_session
from app.query.models import QueryExecution

router = APIRouter(prefix="/console")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


async def _require_login(
    request: Request, session: AsyncSession
) -> Workspace | RedirectResponse:
    """Used at the top of every protected route below: returns the
    logged-in Workspace if there is one, or a ready-to-return redirect
    to the login page if not. (A plain FastAPI dependency can't short-
    circuit a route by itself, so each route checks this explicitly.)"""
    workspace = await get_logged_in_workspace(request, session)
    if workspace is None:
        return RedirectResponse("/console/login", status_code=303)
    return workspace


def _client_ip(request: Request) -> str:
    # The real visitor's IP, even behind the reverse proxy (Caddy) in
    # front of the deployment: uvicorn runs with --proxy-headers and
    # --forwarded-allow-ips set to the proxy's network only (see
    # deploy/mcp-backbone.service), so it replaces request.client with
    # the X-Forwarded-For address only when the request really came
    # from the proxy. A visitor connecting directly can't spoof it.
    # Without that, every visitor would share the proxy's IP, and one
    # person's failed logins would lock everyone out.
    return request.client.host if request.client else "unknown"


_TOO_MANY_ATTEMPTS = "Too many failed attempts. Try again in a few minutes."


@router.get("/login")
async def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    ip = _client_ip(request)
    # Checked before the password is even looked at, so a locked-out
    # client learns nothing more, not even whether a guess was right.
    if login_limiter.is_blocked(ip):
        return templates.TemplateResponse(
            request, "login.html", {"error": _TOO_MANY_ATTEMPTS}, status_code=429
        )
    workspace = await verify_console_login(session, username, password)
    if workspace is None:
        login_limiter.record_failure(ip)
        return templates.TemplateResponse(
            request, "login.html", {"error": "Incorrect username or password."}
        )
    log_in(request, workspace)
    return RedirectResponse("/console/", status_code=303)


@router.get("/signup")
async def signup_form(request: Request):
    return templates.TemplateResponse(
        request, "signup.html", {"error": None, "min_password": MIN_PASSWORD_LENGTH}
    )


@router.post("/signup")
async def signup_submit(
    request: Request,
    invite_code: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    connection_url: str = Form(""),
    session: AsyncSession = Depends(get_session),
):
    ip = _client_ip(request)
    context = {"min_password": MIN_PASSWORD_LENGTH, "username": username}
    if login_limiter.is_blocked(ip):
        return templates.TemplateResponse(
            request, "signup.html", {**context, "error": _TOO_MANY_ATTEMPTS}, status_code=429
        )
    try:
        result = await sign_up(session, invite_code, username, password, connection_url)
    except SignupError as exc:
        login_limiter.record_failure(ip)
        return templates.TemplateResponse(
            request, "signup.html", {**context, "error": str(exc)}, status_code=400
        )

    log_in(request, result.workspace)
    # Rendered directly, not redirected: like a rotated key, the new
    # plain-text key only ever exists in this one response.
    return templates.TemplateResponse(
        request,
        "signup_done.html",
        {
            "api_key": result.api_key,
            "warnings": result.warnings,
            "mcp_url": f"{str(request.base_url).rstrip('/')}/mcp/",
        },
    )


@router.post("/logout")
async def logout(request: Request):
    log_out(request)
    return RedirectResponse("/console/login", status_code=303)


@router.get("/")
async def dashboard(request: Request, session: AsyncSession = Depends(get_session)):
    workspace_or_redirect = await _require_login(request, session)
    if isinstance(workspace_or_redirect, RedirectResponse):
        return workspace_or_redirect
    return templates.TemplateResponse(request, "dashboard.html", {})


@router.get("/keys")
async def keys(request: Request, session: AsyncSession = Depends(get_session)):
    workspace_or_redirect = await _require_login(request, session)
    if isinstance(workspace_or_redirect, RedirectResponse):
        return workspace_or_redirect
    workspace = workspace_or_redirect

    active_key = await get_active_api_key(session, workspace)
    return templates.TemplateResponse(
        request, "keys.html", {"active_key": active_key, "new_key": None}
    )


@router.post("/keys/revoke")
async def keys_revoke(request: Request, session: AsyncSession = Depends(get_session)):
    workspace_or_redirect = await _require_login(request, session)
    if isinstance(workspace_or_redirect, RedirectResponse):
        return workspace_or_redirect
    workspace = workspace_or_redirect

    await revoke_api_key(session, workspace)
    return templates.TemplateResponse(request, "keys.html", {"active_key": None, "new_key": None})


@router.post("/keys/rotate")
async def keys_rotate(request: Request, session: AsyncSession = Depends(get_session)):
    workspace_or_redirect = await _require_login(request, session)
    if isinstance(workspace_or_redirect, RedirectResponse):
        return workspace_or_redirect
    workspace = workspace_or_redirect

    # Rendered directly here, not redirected -- the new plain-text key
    # only ever exists in this one response and is never stored, so a
    # redirect-after-POST (which would lose it) isn't an option.
    new_key = await rotate_api_key(session, workspace)
    active_key = await get_active_api_key(session, workspace)
    return templates.TemplateResponse(
        request, "keys.html", {"active_key": active_key, "new_key": new_key}
    )


@router.get("/api/audit", response_model=None)
async def api_audit(
    request: Request, session: AsyncSession = Depends(get_session)
) -> list[dict[str, Any]] | RedirectResponse:
    """Returns the most recent audit log rows for the logged-in
    workspace as JSON -- polled periodically by the dashboard page's
    TypeScript to refresh the table without a full page reload."""
    workspace_or_redirect = await _require_login(request, session)
    if isinstance(workspace_or_redirect, RedirectResponse):
        return workspace_or_redirect
    workspace = workspace_or_redirect

    result = await session.execute(
        select(QueryExecution)
        .where(QueryExecution.workspace_id == workspace.id)
        .order_by(QueryExecution.created_at.desc())
        .limit(100)
    )
    return [
        {
            "query_text": row.query_text,
            "status": row.status,
            "cost_estimate": row.cost_estimate,
            "rejection_reason": row.rejection_reason,
            "created_at": row.created_at.isoformat(),
        }
        for row in result.scalars()
    ]
