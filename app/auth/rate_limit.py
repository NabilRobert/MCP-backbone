import datetime
import uuid

from sqlalchemy import case
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import RateLimitCounter, Workspace


class RateLimitExceeded(Exception):
    """Raised when a workspace has made too many requests recently.
    Carries a human-readable reason that gets shown back to the caller."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def allowance(override: int | None, default: int) -> int:
    """A workspace's own limit if the operator set one, else the
    server-wide default. Checked against None, not truthiness: an
    override of 0 means "no requests", not "use the default"."""
    return default if override is None else override


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

    One atomic upsert: the first request inserts the counter, later ones
    reset or increment it, and a request over the limit changes nothing.
    A read-then-write here let two concurrent first requests insert two
    counters, after which every lookup failed and the workspace was cut
    off for good (reported by sql-analyst-env, 2026-11-07). Still bursty
    at a window's edge, which is fine for a cost-control limit."""
    over_limit = RateLimitExceeded(f"rate limit of {limit} requests per window exceeded")
    if limit <= 0:
        raise over_limit

    now = datetime.datetime.now(datetime.UTC)
    window_cutoff = now - datetime.timedelta(seconds=window_seconds)
    # In ON CONFLICT DO UPDATE, the model's columns are the existing row.
    window_has_expired = RateLimitCounter.window_started_at < window_cutoff
    statement = (
        insert(RateLimitCounter)
        .values(
            id=uuid.uuid4(),
            workspace_id=workspace.id,
            bucket=bucket,
            window_started_at=now,
            count=1,
        )
        .on_conflict_do_update(
            index_elements=["workspace_id", "bucket"],
            set_={
                "window_started_at": case(
                    (window_has_expired, now), else_=RateLimitCounter.window_started_at
                ),
                "count": case((window_has_expired, 1), else_=RateLimitCounter.count + 1),
            },
            # No row comes back when this is false: the request is over the limit.
            where=window_has_expired | (RateLimitCounter.count < limit),
        )
        .returning(RateLimitCounter.count)
    )
    new_count = (await session.execute(statement)).scalar_one_or_none()
    await session.commit()
    if new_count is None:
        raise over_limit
