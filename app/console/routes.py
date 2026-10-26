"""The web dashboard's pages: log in with a ConsoleCredential, view the
audit trail, and manage the workspace's API key. Everything here reads
from data the rest of the app already produces (QueryExecution rows,
ApiKey status) -- the console adds no new capability, just a way to
see it in a browser."""

import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import Workspace
from app.auth.scopes import PRESETS, describe_scopes
from app.auth.service import get_active_api_key, revoke_api_key, rotate_api_key
from app.auth.signup import MIN_PASSWORD_LENGTH, SignupError, sign_up
from app.config import settings
from app.console import throttle
from app.console.auth import (
    get_logged_in_workspace,
    log_in,
    log_out,
    revoke_all_sessions,
    verify_console_login,
)
from app.console.csrf import csrf_token, verify_csrf
from app.db import get_session
from app.query.models import QueryExecution

# verify_csrf runs on every route here and checks every POST, so a new
# form can't forget the check.
router = APIRouter(prefix="/console", dependencies=[Depends(verify_csrf)])
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
# Templates call csrf_token(request) for each form's hidden field.
templates.env.globals["csrf_token"] = csrf_token  # pyright: ignore[reportArgumentType] -- Jinja's globals type is too narrow

# Every status an audit row can have (app/query/service.py), in display order.
AUDIT_STATUSES = ("succeeded", "rejected", "errored", "timed_out")
DAILY_CHART_DAYS = 14


async def _require_login(request: Request, session: AsyncSession) -> Workspace | RedirectResponse:
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


async def _login_blocked(session: AsyncSession, ip: str) -> bool:
    return await throttle.is_blocked(
        session,
        throttle.LOGIN,
        ip,
        settings.login_max_failures,
        settings.login_failure_window_seconds,
    )


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
    if await _login_blocked(session, ip):
        return templates.TemplateResponse(
            request, "login.html", {"error": _TOO_MANY_ATTEMPTS}, status_code=429
        )
    workspace = await verify_console_login(session, username, password)
    if workspace is None:
        await throttle.record(session, throttle.LOGIN, ip)
        return templates.TemplateResponse(
            request, "login.html", {"error": "Incorrect username or password."}
        )
    await log_in(request, session, workspace)
    return RedirectResponse("/console/", status_code=303)


@router.get("/signup")
async def signup_form(request: Request):
    return templates.TemplateResponse(
        request, "signup.html", {"error": None, "min_password": MIN_PASSWORD_LENGTH}
    )


@router.post("/signup")
async def signup_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    invite_code: str = Form(""),
    connection_url: str = Form(""),
    session: AsyncSession = Depends(get_session),
):
    ip = _client_ip(request)
    context = {"min_password": MIN_PASSWORD_LENGTH, "username": username}
    if await _login_blocked(session, ip):
        return templates.TemplateResponse(
            request, "signup.html", {**context, "error": _TOO_MANY_ATTEMPTS}, status_code=429
        )
    try:
        result = await sign_up(session, invite_code, username, password, connection_url, ip)
    except SignupError as exc:
        await throttle.record(session, throttle.LOGIN, ip)
        return templates.TemplateResponse(
            request, "signup.html", {**context, "error": str(exc)}, status_code=400
        )

    await log_in(request, session, result.workspace)
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


@router.post("/sessions/revoke")
async def sessions_revoke(request: Request, session: AsyncSession = Depends(get_session)):
    """ "Log out everywhere": every console session for this workspace,
    on every browser, stops working, including this one."""
    workspace_or_redirect = await _require_login(request, session)
    if isinstance(workspace_or_redirect, RedirectResponse):
        return workspace_or_redirect
    await revoke_all_sessions(session, workspace_or_redirect)
    log_out(request)
    return RedirectResponse("/console/login", status_code=303)


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

    return await _keys_page(request, session, workspace, new_key=None)


@router.post("/keys/revoke")
async def keys_revoke(request: Request, session: AsyncSession = Depends(get_session)):
    workspace_or_redirect = await _require_login(request, session)
    if isinstance(workspace_or_redirect, RedirectResponse):
        return workspace_or_redirect
    workspace = workspace_or_redirect

    await revoke_api_key(session, workspace)
    return await _keys_page(request, session, workspace, new_key=None)


@router.post("/keys/rotate")
async def keys_rotate(
    request: Request,
    access: str = Form("full"),
    session: AsyncSession = Depends(get_session),
):
    workspace_or_redirect = await _require_login(request, session)
    if isinstance(workspace_or_redirect, RedirectResponse):
        return workspace_or_redirect
    workspace = workspace_or_redirect
    if access not in PRESETS:
        return templates.TemplateResponse(
            request,
            "keys.html",
            await _keys_context(session, workspace, new_key=None, error="Unknown access level."),
            status_code=400,
        )

    # Rendered directly here, not redirected -- the new plain-text key
    # only ever exists in this one response and is never stored, so a
    # redirect-after-POST (which would lose it) isn't an option.
    new_key = await rotate_api_key(session, workspace, PRESETS[access][1])
    return await _keys_page(request, session, workspace, new_key=new_key)


async def _keys_context(
    session: AsyncSession, workspace: Workspace, new_key: str | None, error: str | None = None
) -> dict[str, Any]:
    active_key = await get_active_api_key(session, workspace)
    return {
        "active_key": active_key,
        "access_label": describe_scopes(active_key.scopes) if active_key else None,
        "presets": {name: label for name, (label, _) in PRESETS.items()},
        "new_key": new_key,
        "error": error,
    }


async def _keys_page(
    request: Request, session: AsyncSession, workspace: Workspace, new_key: str | None
):
    return templates.TemplateResponse(
        request, "keys.html", await _keys_context(session, workspace, new_key)
    )


@router.get("/api/audit", response_model=None)
async def api_audit(
    request: Request,
    status: str = "",
    q: str = "",
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]] | RedirectResponse | JSONResponse:
    """Returns the most recent audit log rows for the logged-in
    workspace as JSON -- polled periodically by the dashboard page's
    TypeScript to refresh the table without a full page reload.
    Optional filters: `status` (one of AUDIT_STATUSES) and `q`, a
    case-insensitive substring of the query text."""
    workspace_or_redirect = await _require_login(request, session)
    if isinstance(workspace_or_redirect, RedirectResponse):
        return workspace_or_redirect
    workspace = workspace_or_redirect
    if status and status not in AUDIT_STATUSES:
        return JSONResponse({"error": f"unknown status {status!r}"}, status_code=400)

    query = select(QueryExecution).where(QueryExecution.workspace_id == workspace.id)
    if status:
        query = query.where(QueryExecution.status == status)
    if q:
        # Escaped, so a "%" or "_" the user types matches literally.
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        query = query.where(QueryExecution.query_text.ilike(f"%{escaped}%", escape="\\"))
    result = await session.execute(query.order_by(QueryExecution.created_at.desc()).limit(100))
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


@router.get("/api/audit/daily", response_model=None)
async def api_audit_daily(
    request: Request, session: AsyncSession = Depends(get_session)
) -> list[dict[str, Any]] | RedirectResponse:
    """Per-day counts by status for the last DAILY_CHART_DAYS days (UTC),
    zero-filled, for the dashboard's chart."""
    workspace_or_redirect = await _require_login(request, session)
    if isinstance(workspace_or_redirect, RedirectResponse):
        return workspace_or_redirect
    workspace = workspace_or_redirect

    today = datetime.datetime.now(datetime.UTC).date()
    first_day = today - datetime.timedelta(days=DAILY_CHART_DAYS - 1)
    day = func.date(func.timezone("UTC", QueryExecution.created_at))
    result = await session.execute(
        select(day, QueryExecution.status, func.count())
        .where(
            QueryExecution.workspace_id == workspace.id,
            QueryExecution.created_at
            >= datetime.datetime.combine(first_day, datetime.time(), datetime.UTC),
        )
        .group_by(day, QueryExecution.status)
    )
    counts: dict[tuple[datetime.date, str], int] = {
        (row[0], row[1]): row[2] for row in result.all()
    }
    days = [first_day + datetime.timedelta(days=i) for i in range(DAILY_CHART_DAYS)]
    return [
        {"date": d.isoformat(), **{s: counts.get((d, s), 0) for s in AUDIT_STATUSES}} for d in days
    ]
