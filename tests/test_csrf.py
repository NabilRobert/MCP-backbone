"""The console's CSRF check (app/console/csrf.py): every POST needs the
session's token, so a page on another site can't submit a console form
with the visitor's cookie."""

import re

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import Workspace
from app.auth.service import get_active_api_key
from app.console.routes import router
from app.main import app
from tests.console_client import ConsoleClient

pytestmark = pytest.mark.asyncio

CREDENTIALS = {"username": "admin", "password": "testpass"}
_POST_FORM = re.compile(r'<form[^>]*method="post"[^>]*>(.*?)</form>', re.DOTALL)


def _logged_in() -> ConsoleClient:
    client = ConsoleClient(app, follow_redirects=False)
    assert client.post("/console/login", data=CREDENTIALS).status_code == 303
    # Like a browser following the redirect: the dashboard's logout form
    # gives the session a token, so a wrong one is refused for being
    # wrong, not merely because the session has none.
    assert client.get("/console/").status_code == 200
    return client


async def test_every_post_route_refuses_a_missing_token(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    client = _logged_in()
    paths = [
        route.path
        for route in router.routes
        if isinstance(route, APIRoute) and "POST" in (route.methods or set[str]())
    ]
    assert len(paths) >= 6  # login, signup, logout, sessions/revoke, keys/revoke, keys/rotate
    for path in paths:
        response = TestClient.post(client, path, data={})  # bypasses ConsoleClient's token
        assert response.status_code == 403, path


async def test_every_console_form_carries_the_token(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    anonymous = TestClient(app)
    client = _logged_in()
    pages = [anonymous.get("/console/login"), anonymous.get("/console/signup")]
    pages += [client.get("/console/"), client.get("/console/keys")]
    forms = [form for page in pages for form in _POST_FORM.findall(page.text)]
    assert len(forms) >= 6
    for form in forms:
        assert 'name="csrf_token"' in form


async def test_forged_revoke_leaves_the_key_active(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    """The attack itself: a logged-in browser made to submit the revoke
    form by another site, which can't know the token."""
    workspace, _ = seeded_workspace
    client = _logged_in()

    for forged in ({}, {"csrf_token": "guessed"}, {"csrf_token": ""}):
        response = TestClient.post(client, "/console/keys/revoke", data=forged)
        assert response.status_code == 403

    # Filtered on status in SQL, so this sees what the app committed.
    assert await get_active_api_key(db_session, workspace) is not None


async def test_a_token_from_another_session_is_refused(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    """An attacker can get a valid token for their own session; it must
    not work in the victim's."""
    victim = _logged_in()
    attackers_token = ConsoleClient(app).csrf_token()
    response = TestClient.post(victim, "/console/logout", data={"csrf_token": attackers_token})
    assert response.status_code == 403
    assert victim.get("/console/").status_code == 200  # still logged in


async def test_login_without_token_logs_nobody_in(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    client = TestClient(app, follow_redirects=False)
    assert client.post("/console/login", data=CREDENTIALS).status_code == 403
    assert client.get("/console/").headers["location"] == "/console/login"


async def test_token_changes_at_login(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    client = ConsoleClient(app, follow_redirects=False)
    before = client.csrf_token()
    assert client.post("/console/login", data=CREDENTIALS).status_code == 303

    assert client.csrf_token() != before
    stale = TestClient.post(client, "/console/logout", data={"csrf_token": before})
    assert stale.status_code == 403
