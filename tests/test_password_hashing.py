"""Console passwords: Argon2id for new hashes, and legacy scrypt hashes
still accepted and upgraded at the next successful login."""

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import ConsoleCredential, Workspace
from app.auth.security import hash_password, needs_rehash, verify_password
from app.main import app

# Made by the scrypt hash_password this project used before Argon2, for
# the password below. A real stored value, not one rebuilt by the test,
# so it pins the format existing databases actually hold.
LEGACY_PASSWORD = "legacy-password-123"
LEGACY_HASH = (
    "2e3b4a9b8ddbe9a20970b929d0227cb3$18c5d108614d97d23813ec8b58de5a60439106853d80341ec5b0fbe262"
    "fcc71b2efc766ee7ced5881f1fdb89f3ab9e3cbc4cc0e7f210eb44ef88249f6d9e3cb3"
)


def test_new_hashes_are_argon2id_and_verify() -> None:
    stored = hash_password("correct horse")
    assert stored.startswith("$argon2id$")
    assert verify_password("correct horse", stored)
    assert not verify_password("wrong horse", stored)
    assert not needs_rehash(stored)


def test_legacy_scrypt_hash_still_verifies_and_needs_rehash() -> None:
    assert verify_password(LEGACY_PASSWORD, LEGACY_HASH)
    assert not verify_password("wrong-password", LEGACY_HASH)
    assert needs_rehash(LEGACY_HASH)


def test_weaker_argon2_parameters_need_rehash() -> None:
    weak = PasswordHasher(time_cost=1, memory_cost=8192).hash("correct horse")
    assert verify_password("correct horse", weak)
    assert needs_rehash(weak)


def test_malformed_argon2_hash_is_rejected_not_raised() -> None:
    assert not verify_password("anything", "$argon2id$not-a-real-hash")


async def _stored_hash(session: AsyncSession) -> str:
    session.expire_all()  # read what the app committed, not this session's cached copy
    return (
        await session.execute(
            select(ConsoleCredential.password_hash).where(ConsoleCredential.username == "admin")
        )
    ).scalar_one()


@pytest.mark.asyncio
async def test_login_upgrades_legacy_hash(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    credential = (
        await db_session.execute(
            select(ConsoleCredential).where(ConsoleCredential.username == "admin")
        )
    ).scalar_one()
    credential.password_hash = LEGACY_HASH
    await db_session.commit()
    client = TestClient(app, follow_redirects=False)

    wrong = client.post("/console/login", data={"username": "admin", "password": "wrong"})
    assert wrong.status_code == 200
    assert await _stored_hash(db_session) == LEGACY_HASH  # a failed login changes nothing

    login = client.post("/console/login", data={"username": "admin", "password": LEGACY_PASSWORD})
    assert login.status_code == 303
    upgraded = await _stored_hash(db_session)
    assert upgraded.startswith("$argon2id$")
    assert verify_password(LEGACY_PASSWORD, upgraded)

    # And the upgraded hash keeps working.
    client.post("/console/logout")
    again = client.post("/console/login", data={"username": "admin", "password": LEGACY_PASSWORD})
    assert again.status_code == 303
    assert await _stored_hash(db_session) == upgraded
