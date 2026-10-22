import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import RateLimitCounter, Workspace


class RateLimitExceeded(Exception):
    """Raised when a workspace has made too many requests recently.
    Carries a human-readable reason that gets shown back to the caller."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


async def check_and_increment(
    session: AsyncSession,
    workspace: Workspace,
    limit: int,
    window_seconds: int,
    bucket: str = "query",
) -> None:
    """Fixed-window rate limit: count requests per window, reset when the
    window ends. `bucket` selects the allowance ("query" for run_query,
    "sample" for sample_rows), each with its own counter.

    Approximate by design: bursty at a window's edge, and two requests at
    the same instant can both read the counter before either writes it,
    slightly overshooting. It's a cost-control limit, not a security
    boundary, so neither is worth database-level locking."""
    now = datetime.datetime.now(datetime.UTC)
    result = await session.execute(
        select(RateLimitCounter).where(
            RateLimitCounter.workspace_id == workspace.id, RateLimitCounter.bucket == bucket
        )
    )
    counter = result.scalar_one_or_none()

    if counter is None:
        # First request this workspace has made in this bucket -- start a fresh counter.
        counter = RateLimitCounter(
            workspace_id=workspace.id, bucket=bucket, window_started_at=now, count=0
        )
        session.add(counter)
    elif (now - counter.window_started_at).total_seconds() > window_seconds:
        # The previous window has expired -- reset the clock and the count.
        counter.window_started_at = now
        counter.count = 0

    if counter.count >= limit:
        raise RateLimitExceeded(f"rate limit of {limit} requests per window exceeded")

    counter.count += 1
    await session.commit()
