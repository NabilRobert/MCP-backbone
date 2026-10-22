"""Exercises the web console (Slice G) through actual HTTP requests --
login, protected-route redirects, the dashboard, key management, and
the audit JSON API the dashboard polls."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.sessions import SessionMiddleware

from app.auth.models import Workspace
from app.main import app
from app.query.service import run_query

pytestmark = pytest.mark.asyncio


def _set_cookie_https_only(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    """Flips the session middleware's https_only for one test. The
    middleware's settings are fixed when app.main is imported, so this
    edits its registered kwargs and clears the cached middleware stack
    so Starlette rebuilds it on the next request; monkeypatch restores
    both afterwards."""
    middleware = next(m for m in app.user_middleware if m.cls is SessionMiddleware)
    monkeypatch.setitem(middleware.kwargs, "https_only", value)
    monkeypatch.setattr(app, "middleware_stack", None)


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


async def test_session_cookie_secure_flag_follows_setting(
    db_session: AsyncSession,
    seeded_workspace: tuple[Workspace, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credentials = {"username": "admin", "password": "testpass"}

    _set_cookie_https_only(monkeypatch, False)
    plain = TestClient(app, follow_redirects=False).post("/console/login", data=credentials)
    assert "secure" not in plain.headers["set-cookie"].lower()

    _set_cookie_https_only(monkeypatch, True)
    login = TestClient(app, follow_redirects=False).post("/console/login", data=credentials)
    assert "secure" in login.headers["set-cookie"].lower()


async def test_https_only_cookie_keeps_session_over_https_but_not_http(
    db_session: AsyncSession,
    seeded_workspace: tuple[Workspace, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Why the setting has to stay off for local plain-HTTP development:
    # with it on, the client never sends the cookie back over http://.
    _set_cookie_https_only(monkeypatch, True)
    credentials = {"username": "admin", "password": "testpass"}

    https_client = TestClient(app, base_url="https://testserver", follow_redirects=False)
    assert https_client.post("/console/login", data=credentials).status_code == 303
    assert https_client.get("/console/").status_code == 200

    http_client = TestClient(app, base_url="http://testserver", follow_redirects=False)
    assert http_client.post("/console/login", data=credentials).status_code == 303
    dashboard = http_client.get("/console/")
    assert dashboard.status_code == 303
    assert dashboard.headers["location"] == "/console/login"


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


async def test_root_redirects_to_signup_or_dashboard(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    client = TestClient(app, follow_redirects=False)
    assert client.get("/").headers["location"] == "/console/signup"

    client.post("/console/login", data={"username": "admin", "password": "testpass"})
    assert client.get("/").headers["location"] == "/console/"


async def test_allowed_hosts_match_with_and_without_port() -> None:
    """Behind the reverse proxy, requests arrive as plain "mcp.my.id"
    (standard port, no ":port" in the Host header); direct ones on
    8443 as "mcp.my.id:8443". The SDK's "host:*" pattern only matches
    the second, so both forms must be listed."""
    from mcp.server.transport_security import TransportSecurityMiddleware

    from app.main import transport_security_for

    guard = TransportSecurityMiddleware(transport_security_for(["mcp.my.id"]))
    assert guard._validate_host("mcp.my.id")  # pyright: ignore[reportPrivateUsage]
    assert guard._validate_host("mcp.my.id:8443")  # pyright: ignore[reportPrivateUsage]
    assert not guard._validate_host("evil.example")  # pyright: ignore[reportPrivateUsage]
    assert guard._validate_origin("https://mcp.my.id")  # pyright: ignore[reportPrivateUsage]


async def test_log_out_everywhere_revokes_other_sessions(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    """A signed cookie can't be recalled by itself; the session_version
    stamp lets "log out everywhere" end sessions on other browsers too."""
    credentials = {"username": "admin", "password": "testpass"}
    laptop = TestClient(app, follow_redirects=False)
    phone = TestClient(app, follow_redirects=False)
    laptop.post("/console/login", data=credentials)
    phone.post("/console/login", data=credentials)
    assert phone.get("/console/").status_code == 200

    assert laptop.post("/console/sessions/revoke").status_code == 303

    for browser in (laptop, phone):
        after = browser.get("/console/")
        assert after.status_code == 303
        assert after.headers["location"] == "/console/login"

    # Logging in again works normally.
    phone.post("/console/login", data=credentials)
    assert phone.get("/console/").status_code == 200


async def test_healthz_reports_database_reachability(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An uptime monitor needs a red signal when the server can't do its
    job, not just when the process is down."""
    client = TestClient(app)
    healthy = client.get("/healthz")
    assert healthy.status_code == 200
    assert healthy.json() == {"status": "ok", "database": "ok"}

    import app.main as main_module

    def unreachable() -> None:
        raise OSError("connection refused")

    monkeypatch.setattr(main_module, "SessionLocal", unreachable)
    down = client.get("/healthz")
    assert down.status_code == 503
    assert down.json()["database"] == "unreachable"


async def test_audit_api_filters_by_status_and_text(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    workspace, _ = seeded_workspace
    await run_query(db_session, workspace, "select * from demo_items")
    await run_query(db_session, workspace, "select label from demo_items where label = '100%'")
    await run_query(db_session, workspace, "drop table demo_items")  # rejected

    client = TestClient(app, follow_redirects=False)
    client.post("/console/login", data={"username": "admin", "password": "testpass"})

    rejected = client.get("/console/api/audit", params={"status": "rejected"}).json()
    assert [r["status"] for r in rejected] == ["rejected"]

    by_text = client.get("/console/api/audit", params={"q": "LABEL ="}).json()
    assert len(by_text) == 1 and "label = '100%'" in by_text[0]["query_text"]

    # "%" is matched literally, not as a wildcard that matches everything.
    literal = client.get("/console/api/audit", params={"q": "%"}).json()
    assert len(literal) == 1

    assert client.get("/console/api/audit", params={"status": "bogus"}).status_code == 400


async def test_audit_daily_counts(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    workspace, _ = seeded_workspace
    await run_query(db_session, workspace, "select * from demo_items")
    await run_query(db_session, workspace, "drop table demo_items")

    client = TestClient(app, follow_redirects=False)
    client.post("/console/login", data={"username": "admin", "password": "testpass"})
    days = client.get("/console/api/audit/daily").json()

    assert len(days) == 14
    assert days[-1]["succeeded"] == 1 and days[-1]["rejected"] == 1
    assert sum(d["succeeded"] + d["rejected"] for d in days[:-1]) == 0
    assert set(days[0]) == {"date", "succeeded", "rejected", "errored", "timed_out"}
