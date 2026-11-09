"""A workspace's first rate-limit count and first schema lookup, made by
two requests at once. Both used to read "nothing yet" and insert a row
each, after which every lookup failed and the workspace was cut off
(reported by sql-analyst-env, 2026-11-07). Also pins the window logic the
fix rewrote as one upsert."""

import asyncio
import datetime

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import RateLimitCounter, Workspace
from app.auth.rate_limit import RateLimitExceeded, check_and_increment
from app.db import SessionLocal
from app.query.models import SchemaCache
from app.query.schema_cache import get_table_metadata
from app.query.target import get_data_source

pytestmark = pytest.mark.asyncio

LIMIT = 5
WINDOW_SECONDS = 60


async def _count_in_own_session(workspace: Workspace) -> None:
    async with SessionLocal() as session:
        await check_and_increment(session, workspace, LIMIT, WINDOW_SECONDS)


async def _counters(session: AsyncSession) -> list[RateLimitCounter]:
    session.expire_all()
    return list((await session.execute(select(RateLimitCounter))).scalars().all())


async def test_concurrent_first_requests_share_one_counter(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    workspace, _ = seeded_workspace

    await asyncio.gather(_count_in_own_session(workspace), _count_in_own_session(workspace))

    counters = await _counters(db_session)
    assert [counter.count for counter in counters] == [2]


async def test_a_refused_request_does_not_raise_the_count(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    workspace, _ = seeded_workspace
    for _ in range(LIMIT):
        await check_and_increment(db_session, workspace, LIMIT, WINDOW_SECONDS)

    with pytest.raises(RateLimitExceeded):
        await check_and_increment(db_session, workspace, LIMIT, WINDOW_SECONDS)

    assert [counter.count for counter in await _counters(db_session)] == [LIMIT]


async def test_an_expired_window_starts_counting_again(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    workspace, _ = seeded_workspace
    for _ in range(LIMIT):
        await check_and_increment(db_session, workspace, LIMIT, WINDOW_SECONDS)
    long_ago = datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=WINDOW_SECONDS + 1)
    await db_session.execute(update(RateLimitCounter).values(window_started_at=long_ago))
    await db_session.commit()

    await check_and_increment(db_session, workspace, LIMIT, WINDOW_SECONDS)

    assert [counter.count for counter in await _counters(db_session)] == [1]


async def test_a_limit_of_zero_refuses_the_first_request(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    workspace, _ = seeded_workspace

    with pytest.raises(RateLimitExceeded):
        await check_and_increment(db_session, workspace, 0, WINDOW_SECONDS)


async def _look_up_schema_in_own_session(workspace: Workspace) -> None:
    async with SessionLocal() as session:
        data_source = await get_data_source(session, workspace)
        await get_table_metadata(session, workspace, data_source)


async def test_concurrent_first_schema_lookups_save_one_cache(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    workspace, _ = seeded_workspace

    await asyncio.gather(
        _look_up_schema_in_own_session(workspace), _look_up_schema_in_own_session(workspace)
    )

    caches = (await db_session.execute(select(SchemaCache))).scalars().all()
    assert len(caches) == 1
