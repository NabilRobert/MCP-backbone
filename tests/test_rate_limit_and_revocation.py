import datetime

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import ApiKey, Workspace
from app.auth.service import AuthenticationError, authenticate
from app.config import settings
from app.query.models import DataSourceConnection
from app.query.schema_cache import sample_rows
from app.query.service import run_query
from app.query.target import QueryExecutionError, get_data_source

pytestmark = pytest.mark.asyncio


async def test_rate_limit_rejects_after_threshold(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    workspace, _ = seeded_workspace

    statuses = [
        (await run_query(db_session, workspace, "select * from demo_items"))["status"]
        for _ in range(7)
    ]

    assert statuses[:5] == ["succeeded"] * 5
    assert statuses[5:] == ["rejected"] * 2


async def test_workspace_query_limit_overrides_the_default(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    """A workspace with its own allowance gets exactly that many queries,
    not the server default -- above it here, so the test fails if the
    override is ignored and the default still applies."""
    workspace, _ = seeded_workspace
    workspace.query_rate_limit = settings.demo_rate_limit + 3
    await db_session.commit()

    statuses = [
        (await run_query(db_session, workspace, "select * from demo_items"))["status"]
        for _ in range(settings.demo_rate_limit + 4)
    ]

    assert statuses[:-1] == ["succeeded"] * (settings.demo_rate_limit + 3)
    assert statuses[-1] == "rejected"


async def test_workspace_sample_limit_overrides_the_default(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    """Same for sample_rows, set below the default this time: the limit
    can be tightened as well as raised, and the two buckets are read from
    their own columns (a mixed-up column would allow the default here)."""
    workspace, _ = seeded_workspace
    workspace.sample_rate_limit = 2
    await db_session.commit()

    for _ in range(2):
        await sample_rows(db_session, workspace, "demo_items")
    with pytest.raises(QueryExecutionError, match="rate limit"):
        await sample_rows(db_session, workspace, "demo_items")


async def test_one_workspace_override_leaves_others_on_the_default(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    """Raising one workspace's allowance (the eval harness's, say) must
    not raise anyone else's: a second workspace with no override is still
    cut off at the server default."""
    workspace, _ = seeded_workspace
    workspace.query_rate_limit = 1000

    other = Workspace(name="other-workspace", created_at=datetime.datetime.now(datetime.UTC))
    db_session.add(other)
    await db_session.flush()
    db_session.add(
        DataSourceConnection(
            workspace_id=other.id,
            connection_url=(await get_data_source(db_session, workspace)).connection_url,
        )
    )
    await db_session.commit()

    statuses = [
        (await run_query(db_session, other, "select * from demo_items"))["status"]
        for _ in range(settings.demo_rate_limit + 1)
    ]

    assert statuses[:-1] == ["succeeded"] * settings.demo_rate_limit
    assert statuses[-1] == "rejected"


async def test_revoked_key_fails_authentication(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    workspace, api_key = seeded_workspace

    await db_session.execute(
        update(ApiKey)
        .where(ApiKey.workspace_id == workspace.id)
        .values(status="revoked", revoked_at=datetime.datetime.now(datetime.UTC))
    )
    await db_session.commit()

    with pytest.raises(AuthenticationError):
        await authenticate(db_session, api_key)
