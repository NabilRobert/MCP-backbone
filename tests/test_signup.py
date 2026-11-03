"""Signup: invite codes, demo accounts without one, and what a new
workspace gets (app/auth/signup.py)."""

import datetime

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import InviteCode, ThrottleEvent, Workspace
from app.auth.service import authenticate
from app.auth.signup import create_invite
from app.config import settings
from app.db import engine
from app.main import app
from app.query.models import DataSourceConnection
from app.query.service import list_tables
from tests.console_client import ConsoleClient

pytestmark = pytest.mark.asyncio

STRONG_PASSWORD = "correct-horse-battery"


async def _clear_throttle(session: AsyncSession) -> None:
    await session.execute(delete(ThrottleEvent))
    await session.commit()


def _signup(client: ConsoleClient, invite: str, username: str = "newuser", **extra: str):
    return client.post(
        "/console/signup",
        data={"invite_code": invite, "username": username, "password": STRONG_PASSWORD, **extra},
    )


async def _invite_used_at(session: AsyncSession, note: str) -> datetime.datetime | None:
    result = await session.execute(select(InviteCode.used_at).where(InviteCode.note == note))
    return result.scalar_one()


# --- Signup -----------------------------------------------------------------


async def test_signup_with_default_database(db_session: AsyncSession) -> None:
    invite = await create_invite(db_session, "for-default-test")
    client = ConsoleClient(app, follow_redirects=False)

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
    again = _signup(ConsoleClient(app, follow_redirects=False), invite, username="someoneelse")
    assert again.status_code == 400
    assert "invalid, expired, or already used" in again.text


async def test_signup_with_own_database_warns_about_write_access(
    db_session: AsyncSession,
) -> None:
    invite = await create_invite(db_session, "for-own-db-test")
    own_url = engine.url.render_as_string(hide_password=False)  # the test superuser

    response = _signup(ConsoleClient(app), invite, connection_url=own_url)
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

    response = ConsoleClient(app).post("/console/signup", data=data)
    assert response.status_code == 400
    assert expected_error in response.text
    assert await _invite_used_at(db_session, "for-failure-test") is None
    assert (await db_session.execute(select(Workspace))).first() is None


async def test_signup_unreachable_database_is_refused(db_session: AsyncSession) -> None:
    invite = await create_invite(db_session, "for-unreachable-test")
    response = _signup(
        ConsoleClient(app), invite, connection_url="postgresql://u:p@127.0.0.1:1/nothing"
    )
    assert response.status_code == 400
    assert "could not connect" in response.text
    assert await _invite_used_at(db_session, "for-unreachable-test") is None


async def test_signup_rejects_taken_username(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    invite = await create_invite(db_session, "for-duplicate-test")
    response = _signup(ConsoleClient(app), invite, username="admin")
    assert response.status_code == 400
    assert "username is taken" in response.text
    assert await _invite_used_at(db_session, "for-duplicate-test") is None


async def test_signup_rejects_expired_and_unknown_invites(db_session: AsyncSession) -> None:
    expired = await create_invite(db_session, "for-expiry-test")
    await db_session.execute(text("UPDATE invite_codes SET expires_at = now() - interval '1 day'"))
    await db_session.commit()

    client = ConsoleClient(app)
    for code in (expired, "inv_made_up"):
        response = _signup(client, code)
        assert response.status_code == 400
        assert "invalid, expired, or already used" in response.text


# --- Demo signup (no invite code) -------------------------------------------


def _demo_signup(client: ConsoleClient, username: str, **extra: str):
    return client.post(
        "/console/signup", data={"username": username, "password": STRONG_PASSWORD, **extra}
    )


async def test_demo_signup_needs_no_invite(db_session: AsyncSession) -> None:
    response = _demo_signup(ConsoleClient(app), "demo-user")
    assert response.status_code == 200
    api_key = response.text.split('class="key-box">')[1].split("<")[0].strip()

    workspace = await authenticate(db_session, api_key)
    assert workspace.created_via == "demo"
    data_source = (await db_session.execute(select(DataSourceConnection))).scalar_one()
    assert data_source.connection_url == settings.target_database_url


async def test_demo_signup_cannot_bring_its_own_database(db_session: AsyncSession) -> None:
    response = _demo_signup(
        ConsoleClient(app), "demo-user", connection_url="postgresql://u:p@8.8.8.8/db"
    )
    assert response.status_code == 400
    assert "needs an invite code" in response.text
    assert (await db_session.execute(select(Workspace))).first() is None


async def test_demo_signups_are_capped_per_ip(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "demo_signups_per_ip_per_day", 2)
    client = ConsoleClient(app)
    assert _demo_signup(client, "demo-one").status_code == 200
    assert _demo_signup(client, "demo-two").status_code == 200
    third = _demo_signup(client, "demo-three")
    assert third.status_code == 400
    assert "Too many demo accounts" in third.text

    # An invite still works from the same address.
    invite = await create_invite(db_session, "after-demo-cap")
    assert _signup(client, invite, username="invited-one").status_code == 200


async def test_demo_signups_are_capped_per_day_overall(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "demo_signups_per_day", 1)
    assert _demo_signup(ConsoleClient(app), "demo-one").status_code == 200
    await _clear_throttle(db_session)

    full = _demo_signup(ConsoleClient(app), "demo-two")
    assert full.status_code == 400
    assert "demo is full for today" in full.text


async def test_demo_signup_can_be_switched_off(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "demo_signups_enabled", False)
    response = _demo_signup(ConsoleClient(app), "demo-user")
    assert response.status_code == 400
    assert "needs an invite code" in response.text


async def test_invite_signup_is_recorded_as_invite(db_session: AsyncSession) -> None:
    invite = await create_invite(db_session, "for-created-via")
    response = _signup(ConsoleClient(app), invite, username="invited")
    api_key = response.text.split('class="key-box">')[1].split("<")[0].strip()
    assert (await authenticate(db_session, api_key)).created_via == "invite"


# --- The default database is referenced, not copied --------------------------


async def test_default_target_is_referenced_not_stored(db_session: AsyncSession) -> None:
    """A demo workspace stores no URL at all: it follows
    APP_TARGET_DATABASE_URL, so the operator can move every default
    workspace by changing one setting, and the default's password is
    never copied into the bookkeeping database."""
    response = _demo_signup(ConsoleClient(app), "demo-user")
    assert response.status_code == 200

    data_source = (await db_session.execute(select(DataSourceConnection))).scalar_one()
    assert data_source.encrypted_connection_url is None
    assert data_source.uses_default_target
    assert data_source.connection_url == settings.target_database_url


async def test_default_target_is_exempt_from_the_private_address_guard(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The operator's default database may live on localhost (the VPS
    hosts the demo database itself), so it skips the guard -- while a
    workspace's own localhost URL is still refused (see
    test_guard_is_rechecked_on_every_use)."""
    monkeypatch.setattr(
        settings, "target_database_url", engine.url.render_as_string(hide_password=False)
    )
    monkeypatch.setattr(settings, "allow_private_targets", False)

    response = _demo_signup(ConsoleClient(app), "demo-user")
    api_key = response.text.split('class="key-box">')[1].split("<")[0].strip()
    workspace = await authenticate(db_session, api_key)

    assert isinstance(await list_tables(db_session, workspace), list)
