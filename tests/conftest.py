import datetime
import os
import secrets
from collections.abc import AsyncGenerator

from cryptography.fernet import Fernet

# Set before anything imports app.config (which reads settings once, at
# import). A throwaway encryption key per test run; and the test target
# database IS localhost, which the address guard (app/query/network_guard.py)
# would otherwise refuse. Real env vars still win, via setdefault.
os.environ.setdefault("APP_CONNECTION_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("APP_ALLOW_PRIVATE_TARGETS", "true")

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine.url import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import ApiKey, ConsoleCredential, Workspace
from app.auth.security import generate_api_key, hash_api_key, hash_password
from app.console.throttle import demo_signup_limiter, login_limiter
from app.db import Base, SessionLocal, engine
from app.query.models import DataSourceConnection


@pytest.fixture(autouse=True)
def _reset_login_limiter() -> None:
    """The login limiter is in-memory and process-wide; without this,
    failed logins in one test would lock out the next."""
    login_limiter.reset()
    demo_signup_limiter.reset()


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession]:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with SessionLocal() as session:
        yield session

    async with engine.begin() as conn:
        await conn.execute(text("drop table if exists demo_items"))
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture
async def seeded_workspace(db_session: AsyncSession) -> tuple[Workspace, str]:
    workspace = Workspace(name="test-workspace", created_at=datetime.datetime.now(datetime.UTC))
    db_session.add(workspace)
    await db_session.flush()

    db_session.add(
        DataSourceConnection(
            workspace_id=workspace.id,
            connection_url=engine.url.render_as_string(hide_password=False),
        )
    )
    db_session.add(
        ConsoleCredential(
            workspace_id=workspace.id,
            username="admin",
            password_hash=hash_password("testpass"),
        )
    )
    api_key = generate_api_key()
    db_session.add(
        ApiKey(
            workspace_id=workspace.id,
            key_hash=hash_api_key(api_key),
            status="active",
            created_at=datetime.datetime.now(datetime.UTC),
        )
    )
    await db_session.commit()

    await db_session.execute(text("create table demo_items (id serial primary key, label text)"))
    await db_session.execute(text("insert into demo_items (label) values ('a'), ('b')"))
    await db_session.commit()

    return workspace, api_key


@pytest_asyncio.fixture
async def readonly_role_workspace(
    db_session: AsyncSession,
) -> AsyncGenerator[tuple[Workspace, str]]:
    """Like seeded_workspace, but the DataSourceConnection points at a
    REAL, actually-provisioned read-only role (via
    scripts/provision_readonly_role.py's own provision() function, not
    a reimplementation of it) instead of the superuser connection. This
    is what lets the red-team suite prove defense-in-depth for real --
    that the database itself independently blocks a write, not just
    that our own validator does."""
    from scripts.provision_readonly_role import drop_role, provision

    role_name = f"redteam_{secrets.token_hex(4)}"
    admin_url = make_url(engine.url.render_as_string(hide_password=False)).set(
        drivername="postgresql"
    )
    admin_dsn = admin_url.render_as_string(hide_password=False)
    database = admin_url.database or "postgres"

    await db_session.execute(text("create table if not exists redteam_items (id serial primary key, label text)"))
    await db_session.execute(text("delete from redteam_items"))
    await db_session.execute(text("insert into redteam_items (label) values ('a'), ('b')"))
    await db_session.commit()

    password = await provision(admin_dsn, role_name, database, "public")

    workspace = Workspace(name="redteam-workspace", created_at=datetime.datetime.now(datetime.UTC))
    db_session.add(workspace)
    await db_session.flush()

    role_url = admin_url.set(username=role_name, password=password, drivername="postgresql+asyncpg")
    db_session.add(
        DataSourceConnection(
            workspace_id=workspace.id,
            connection_url=role_url.render_as_string(hide_password=False),
        )
    )
    api_key = generate_api_key()
    db_session.add(
        ApiKey(
            workspace_id=workspace.id,
            key_hash=hash_api_key(api_key),
            status="active",
            created_at=datetime.datetime.now(datetime.UTC),
        )
    )
    await db_session.commit()

    try:
        yield workspace, api_key
    finally:
        await drop_role(admin_dsn, role_name)
        await db_session.execute(text("drop table if exists redteam_items"))
        await db_session.commit()
