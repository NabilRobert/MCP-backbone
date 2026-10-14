"""Exercises the web console (Slice G) through actual HTTP requests --
login, protected-route redirects, the dashboard, key management, and
the audit JSON API the dashboard polls."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import Workspace
from app.main import app
from app.query.service import run_query

pytestmark = pytest.mark.asyncio


async def test_protected_routes_redirect_when_not_logged_in(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    client = TestClient(app, follow_redirects=False)

    for path in ("/console/", "/console/keys", "/console/api/audit"):
        response = client.get(path)
        assert response.status_code == 303
        assert response.headers["location"] == "/console/login"


async def test_login_rejects_wrong_password(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    client = TestClient(app, follow_redirects=False)

    response = client.post("/console/login", data={"username": "admin", "password": "wrong"})
    assert response.status_code == 200
    assert "Incorrect" in response.text


async def test_login_rejects_unknown_username(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    client = TestClient(app, follow_redirects=False)

    response = client.post(
        "/console/login", data={"username": "does-not-exist", "password": "anything"}
    )
    assert response.status_code == 200
    assert "Incorrect" in response.text


async def test_full_login_session_and_logout(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    client = TestClient(app, follow_redirects=False)

    login = client.post("/console/login", data={"username": "admin", "password": "testpass"})
    assert login.status_code == 303
    assert login.headers["location"] == "/console/"

    dashboard = client.get("/console/")
    assert dashboard.status_code == 200
    assert "audit-rows" in dashboard.text

    logout = client.post("/console/logout")
    assert logout.status_code == 303

    after_logout = client.get("/console/")
    assert after_logout.status_code == 303
    assert after_logout.headers["location"] == "/console/login"


async def test_audit_api_returns_real_rows(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    workspace, _ = seeded_workspace
    await run_query(db_session, workspace, "select * from demo_items")
    await run_query(db_session, workspace, "drop table demo_items")  # rejected

    client = TestClient(app, follow_redirects=False)
    client.post("/console/login", data={"username": "admin", "password": "testpass"})

    response = client.get("/console/api/audit")
    assert response.status_code == 200
    rows = response.json()
    statuses = {row["status"] for row in rows}
    assert "succeeded" in statuses
    assert "rejected" in statuses


async def test_key_rotate_then_revoke(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    client = TestClient(app, follow_redirects=False)
    client.post("/console/login", data={"username": "admin", "password": "testpass"})

    keys_page = client.get("/console/keys")
    assert "active" in keys_page.text

    rotated = client.post("/console/keys/rotate")
    assert rotated.status_code == 200
    assert "key-box" in rotated.text  # the new plain-text key is shown once

    revoked = client.post("/console/keys/revoke")
    assert revoked.status_code == 200
    assert "no active key" in revoked.text
