import datetime

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import ApiKey, Workspace
from app.auth.service import AuthenticationError, authenticate
from app.query.service import run_query

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
