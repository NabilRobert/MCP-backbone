import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import RateLimitCounter, Workspace


class RateLimitExceeded(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


async def check_and_increment(
    session: AsyncSession, workspace: Workspace, limit: int, window_seconds: int
) -> None:
    """Fixed-window counter. Approximate/non-atomic is an accepted tradeoff
    at this scale (Phase 4) -- the limit is a cost-control placeholder, not
    a hard guarantee."""
    now = datetime.datetime.now(datetime.UTC)
    result = await session.execute(
        select(RateLimitCounter).where(RateLimitCounter.workspace_id == workspace.id)
    )
    counter = result.scalar_one_or_none()

    if counter is None:
        counter = RateLimitCounter(workspace_id=workspace.id, window_started_at=now, count=0)
        session.add(counter)
    elif (now - counter.window_started_at).total_seconds() > window_seconds:
        counter.window_started_at = now
        counter.count = 0

    if counter.count >= limit:
        raise RateLimitExceeded(f"rate limit of {limit} requests per window exceeded")

    counter.count += 1
    await session.commit()
