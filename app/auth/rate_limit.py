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
    session: AsyncSession, workspace: Workspace, limit: int, window_seconds: int
) -> None:
    """"Fixed-window" rate limiting, explained plainly: pick a time
    window (e.g. one hour), count requests within it, and once the
    window's time is up, start counting from zero again. This is
    simpler than more precise alternatives (like a "sliding window"),
    at the cost of being a bit bursty right at the edge of a window --
    an acceptable tradeoff here, since this limit is a rough
    cost-control placeholder, not a hard guarantee (Phase 4).

    Also approximate on purpose for a second reason: two requests
    arriving at the exact same instant could both read the counter
    before either writes it back, letting the count run slightly over
    the limit. Fixing that needs database-level locking, which isn't
    worth the complexity at this project's scale."""
    now = datetime.datetime.now(datetime.UTC)
    result = await session.execute(
        select(RateLimitCounter).where(RateLimitCounter.workspace_id == workspace.id)
    )
    counter = result.scalar_one_or_none()

    if counter is None:
        # First request this workspace has ever made -- start a fresh counter.
        counter = RateLimitCounter(workspace_id=workspace.id, window_started_at=now, count=0)
        session.add(counter)
    elif (now - counter.window_started_at).total_seconds() > window_seconds:
        # The previous window has expired -- reset the clock and the count.
        counter.window_started_at = now
        counter.count = 0

    if counter.count >= limit:
        raise RateLimitExceeded(f"rate limit of {limit} requests per window exceeded")

    counter.count += 1
    await session.commit()
