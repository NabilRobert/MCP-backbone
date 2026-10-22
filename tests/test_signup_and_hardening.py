"""Invite-only signup, and the hardening that came with letting users
register their own database: encrypted connection URLs, the
private-address guard, sample_rows auditing/rate limiting, and console
login throttling."""

import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import InviteCode, Workspace
from app.auth.service import authenticate
from app.auth.signup import create_invite
from app.config import settings
from app.console.throttle import login_limiter
from app.db import engine
from app.main import app
from app.query import schema_cache
from app.query.models import DataSourceConnection, QueryExecution
from app.query.network_guard import UnsafeTargetError, ensure_public_target
from app.query.service import QueryExecutionError, list_tables, run_query

pytestmark = pytest.mark.asyncio

STRONG_PASSWORD = "correct-horse-battery"


def _signup(client: TestClient, invite: str, username: str = "newuser", **extra: str):
    return client.post(
        "/console/signup",
        data={"invite_code": invite, "username": username, "password": STRONG_PASSWORD, **extra},
    )


async def _invite_used_at(session: AsyncSession, note: str) -> datetime.datetime | None:
    result = await session.execute(select(InviteCode.used_at).where(InviteCode.note == note))
    return result.scalar_one()


# --- Encryption at rest -----------------------------------------------------


async def test_connection_url_is_stored_encrypted(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    plaintext = engine.url.render_as_string(hide_password=False)
    stored = (
        await db_session.execute(
            text("SELECT encrypted_connection_url FROM data_source_connections")
        )
    ).scalar_one()

    assert engine.url.host and engine.url.host not in stored
    assert engine.url.password and str(engine.url.password) not in stored
    data_source = (await db_session.execute(select(DataSourceConnection))).scalar_one()
    assert data_source.connection_url == plaintext


# --- Private-address guard --------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://u:p@127.0.0.1:5432/db",
        "postgresql://u:p@localhost/db",
        "postgresql://u:p@10.0.0.5/db",
        "postgresql://u:p@192.168.1.10/db",
        "postgresql://u:p@169.254.169.254/db",  # cloud metadata endpoint
        "postgresql://u:p@100.64.1.1/db",  # carrier-grade NAT
        "postgresql://u:p@[::1]/db",
        "postgresql://u:p@[::ffff:127.0.0.1]/db",  # loopback hidden in IPv6
    ],
)
async def test_guard_rejects_private_addresses(url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "allow_private_targets", False)
    with pytest.raises(UnsafeTargetError):
        await ensure_public_target(url)


@pytest.mark.parametrize(
    "url",
    [
        "mysql://u:p@8.8.8.8/db",
        "postgresql://u:p@8.8.8.8/db?host=127.0.0.1",  # a second host smuggled in
        "postgresql://u:p@8.8.8.8/db?passfile=/etc/passwd",
        "postgresql://u:p@/db",
        "not a url",
    ],
)
async def test_guard_rejects_malformed_urls_even_when_private_allowed(
    url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "allow_private_targets", True)
    with pytest.raises(UnsafeTargetError):
        await ensure_public_target(url)


async def test_guard_accepts_a_public_address(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "allow_private_targets", False)
    await ensure_public_target("postgresql+asyncpg://u:p@8.8.8.8:5432/db?sslmode=require")


async def test_guard_is_rechecked_on_every_use(
    db_session: AsyncSession,
    seeded_workspace: tuple[Workspace, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A URL that was allowed when registered must still be refused
    later: the workspace here points at localhost."""
    workspace, _ = seeded_workspace
    monkeypatch.setattr(settings, "allow_private_targets", False)

    with pytest.raises(QueryExecutionError, match="private or reserved"):
        await list_tables(db_session, workspace)
    with pytest.raises(QueryExecutionError, match="private or reserved"):
        await run_query(db_session, workspace, "select 1")


# --- sample_rows: audited and rate-limited ----------------------------------


async def _audit_rows(session: AsyncSession) -> list[tuple[str, str]]:
    result = await session.execute(
        select(QueryExecution.query_text, QueryExecution.status).order_by(QueryExecution.created_at)
    )
    return [(row[0], row[1]) for row in result]


async def test_sample_rows_is_audited(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    workspace, _ = seeded_workspace

    rows = await schema_cache.sample_rows(db_session, workspace, "demo_items", 5)
    assert len(rows) == 2
    with pytest.raises(QueryExecutionError):
        await schema_cache.sample_rows(db_session, workspace, "no_such_table")

    assert await _audit_rows(db_session) == [
        ('SELECT * FROM "demo_items" LIMIT 5', "succeeded"),
        ('SELECT * FROM "no_such_table" LIMIT 10', "rejected"),
    ]


async def test_sample_rows_has_its_own_rate_limit(
    db_session: AsyncSession,
    seeded_workspace: tuple[Workspace, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, _ = seeded_workspace
    monkeypatch.setattr(settings, "sample_rate_limit", 2)

    await schema_cache.sample_rows(db_session, workspace, "demo_items")
    await schema_cache.sample_rows(db_session, workspace, "demo_items")
    with pytest.raises(QueryExecutionError, match="rate limit"):
        await schema_cache.sample_rows(db_session, workspace, "demo_items")

    # The exhausted sample allowance doesn't touch run_query's.
    result = await run_query(db_session, workspace, "select * from demo_items")
    assert result["status"] == "succeeded"
    assert (await _audit_rows(db_session))[2][1] == "rejected"


# --- Console login throttling -----------------------------------------------


async def test_login_locks_out_after_repeated_failures(
    db_session: AsyncSession,
    seeded_workspace: tuple[Workspace, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = TestClient(app, follow_redirects=False)
    for _ in range(settings.login_max_failures):
        bad = client.post("/console/login", data={"username": "admin", "password": "wrong"})
        assert bad.status_code == 200

    # Locked out: even the right password is refused, without being checked.
    locked = client.post("/console/login", data={"username": "admin", "password": "testpass"})
    assert locked.status_code == 429
    assert "Too many failed attempts" in locked.text


# --- Signup -----------------------------------------------------------------


async def test_signup_with_default_database(db_session: AsyncSession) -> None:
    invite = await create_invite(db_session, "for-default-test")
    client = TestClient(app, follow_redirects=False)

    response = _signup(client, invite)
    assert response.status_code == 200
    api_key = response.text.split('class="key-box">')[1].split("<")[0].strip()
    assert api_key.startswith("dbmcp_")

    # The key works, and points at the server's default target database.
    workspace = await authenticate(db_session, api_key)
    assert workspace.name == "newuser"
    data_source = (
        await db_session.execute(
            select(DataSourceConnection).where(DataSourceConnection.workspace_id == workspace.id)
        )
    ).scalar_one()
    assert data_source.connection_url == settings.target_database_url

    # The invite is spent and records who used it.
    invite_row = (
        await db_session.execute(select(InviteCode).where(InviteCode.note == "for-default-test"))
    ).scalar_one()
    assert invite_row.used_at is not None
    assert invite_row.used_by_workspace_id == workspace.id

    # Signing up logged them straight in.
    assert client.get("/console/").status_code == 200

    # And the code can't be used twice.
    again = _signup(TestClient(app, follow_redirects=False), invite, username="someoneelse")
    assert again.status_code == 400
    assert "invalid, expired, or already used" in again.text


async def test_signup_with_own_database_warns_about_write_access(
    db_session: AsyncSession,
) -> None:
    invite = await create_invite(db_session, "for-own-db-test")
    own_url = engine.url.render_as_string(hide_password=False)  # the test superuser

    response = _signup(TestClient(app), invite, connection_url=own_url)
    assert response.status_code == 200
    assert "superuser" in response.text

    data_source = (await db_session.execute(select(DataSourceConnection))).scalar_one()
    assert data_source.connection_url == own_url


@pytest.mark.parametrize(
    ("form", "expected_error"),
    [
        ({"connection_url": "postgresql://u:p@127.0.0.1:1/db"}, "private or reserved"),
        ({"password": "short"}, "at least 12 characters"),
        ({"username": "x"}, "Username must be"),
    ],
)
async def test_failed_signup_leaves_invite_unused(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    form: dict[str, str],
    expected_error: str,
) -> None:
    monkeypatch.setattr(settings, "allow_private_targets", False)
    invite = await create_invite(db_session, "for-failure-test")
    data = {"invite_code": invite, "username": "newuser", "password": STRONG_PASSWORD, **form}

    response = TestClient(app).post("/console/signup", data=data)
    assert response.status_code == 400
    assert expected_error in response.text
    assert await _invite_used_at(db_session, "for-failure-test") is None
    assert (await db_session.execute(select(Workspace))).first() is None


async def test_signup_unreachable_database_is_refused(db_session: AsyncSession) -> None:
    invite = await create_invite(db_session, "for-unreachable-test")
    response = _signup(
        TestClient(app), invite, connection_url="postgresql://u:p@127.0.0.1:1/nothing"
    )
    assert response.status_code == 400
    assert "could not connect" in response.text
    assert await _invite_used_at(db_session, "for-unreachable-test") is None


async def test_signup_rejects_taken_username(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    invite = await create_invite(db_session, "for-duplicate-test")
    response = _signup(TestClient(app), invite, username="admin")
    assert response.status_code == 400
    assert "username is taken" in response.text
    assert await _invite_used_at(db_session, "for-duplicate-test") is None


async def test_signup_rejects_expired_and_unknown_invites(db_session: AsyncSession) -> None:
    expired = await create_invite(db_session, "for-expiry-test")
    await db_session.execute(text("UPDATE invite_codes SET expires_at = now() - interval '1 day'"))
    await db_session.commit()

    client = TestClient(app)
    for code in (expired, "inv_made_up"):
        response = _signup(client, code)
        assert response.status_code == 400
        assert "invalid, expired, or already used" in response.text


async def test_lockout_uses_forwarded_ip_only_from_trusted_proxy(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    """Behind Caddy, uvicorn's proxy-headers handling (the deployed
    --forwarded-allow-ips flag) must give each visitor their own lockout
    -- otherwise everyone shares the proxy's IP and one person's typos
    lock out all users. And a client NOT coming from the proxy must not
    be able to dodge the lockout by sending its own X-Forwarded-For."""
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    wrong = {"username": "admin", "password": "wrong"}

    def fail_until_locked(client: TestClient, forwarded_for: str) -> None:
        for _ in range(settings.login_max_failures):
            client.post("/console/login", data=wrong, headers={"X-Forwarded-For": forwarded_for})

    # TestClient's peer address is "testclient": trusted here, like Caddy.
    via_proxy = TestClient(ProxyHeadersMiddleware(app, trusted_hosts="testclient"))
    fail_until_locked(via_proxy, "203.0.113.1")
    locked = via_proxy.post(
        "/console/login", data=wrong, headers={"X-Forwarded-For": "203.0.113.1"}
    )
    assert locked.status_code == 429
    other_visitor = via_proxy.post(
        "/console/login", data=wrong, headers={"X-Forwarded-For": "203.0.113.2"}
    )
    assert other_visitor.status_code == 200

    # Untrusted peer: its X-Forwarded-For is ignored, so rotating it
    # doesn't reset the count.
    login_limiter.reset()
    direct = TestClient(ProxyHeadersMiddleware(app, trusted_hosts="172.18.0.0/16"))
    for i in range(settings.login_max_failures):
        direct.post("/console/login", data=wrong, headers={"X-Forwarded-For": f"198.51.100.{i}"})
    spoofed = direct.post(
        "/console/login", data=wrong, headers={"X-Forwarded-For": "198.51.100.99"}
    )
    assert spoofed.status_code == 429
