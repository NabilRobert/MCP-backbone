"""scripts/reset_console_password.py: an operator sets a new console
password, which replaces an old scrypt hash and logs every session out."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import ConsoleCredential, Workspace
from app.auth.security import verify_password
from app.main import app
from scripts.reset_console_password import read_new_password, reset
from tests.console_client import ConsoleClient
from tests.test_password_hashing import LEGACY_HASH, LEGACY_PASSWORD

NEW_PASSWORD = "a-brand-new-password"


async def _stored_hash(session: AsyncSession) -> str:
    session.expire_all()
    result = await session.execute(
        select(ConsoleCredential.password_hash).where(ConsoleCredential.username == "admin")
    )
    return result.scalar_one()


async def _give_admin_a_legacy_hash(session: AsyncSession) -> None:
    credential = (await session.execute(select(ConsoleCredential))).scalar_one()
    credential.password_hash = LEGACY_HASH
    await session.commit()


async def test_a_reset_replaces_a_legacy_hash_with_argon2(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    await _give_admin_a_legacy_hash(db_session)

    await reset("admin", NEW_PASSWORD)

    stored = await _stored_hash(db_session)
    assert stored.startswith("$argon2id$")
    assert verify_password(NEW_PASSWORD, stored)
    assert not verify_password(LEGACY_PASSWORD, stored)


async def test_the_new_password_logs_in_and_the_old_one_doesnt(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    await _give_admin_a_legacy_hash(db_session)
    await reset("admin", NEW_PASSWORD)
    client = ConsoleClient(app, follow_redirects=False)

    old = client.post("/console/login", data={"username": "admin", "password": LEGACY_PASSWORD})
    new = client.post("/console/login", data={"username": "admin", "password": NEW_PASSWORD})

    assert old.status_code == 200  # the form again, with an error
    assert new.status_code == 303


async def test_a_reset_logs_out_every_existing_session(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    client = ConsoleClient(app, follow_redirects=False)
    login = client.post("/console/login", data={"username": "admin", "password": "testpass"})
    assert login.status_code == 303
    assert client.get("/console/").status_code == 200

    await reset("admin", NEW_PASSWORD)

    after = client.get("/console/")
    assert after.status_code == 303
    assert after.headers["location"] == "/console/login"


async def test_a_short_password_is_refused_and_nothing_changes(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    before = await _stored_hash(db_session)

    with pytest.raises(SystemExit, match="at least 12 characters"):
        await reset("admin", "too-short")

    assert await _stored_hash(db_session) == before


async def test_an_unknown_login_is_refused(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    with pytest.raises(SystemExit, match="no console login named 'nobody'"):
        await reset("nobody", NEW_PASSWORD)


def test_two_different_entries_are_refused() -> None:
    answers = iter(["first-password-1", "second-password-2"])

    with pytest.raises(SystemExit, match="don't match"):
        read_new_password(lambda _prompt: next(answers))


def test_two_matching_entries_are_used() -> None:
    answers = iter([NEW_PASSWORD, NEW_PASSWORD])

    assert read_new_password(lambda _prompt: next(answers)) == NEW_PASSWORD
