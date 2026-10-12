import datetime
from collections.abc import AsyncGenerator

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import ApiKey, ConsoleCredential, Workspace
from app.auth.security import generate_api_key, hash_api_key, hash_password
from app.db import Base, SessionLocal, engine
from app.query.models import DataSourceConnection


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
