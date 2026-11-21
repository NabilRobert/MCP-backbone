"""Per-client-IP limits for the console: failed logins (and failed
signups), so the internet-facing login page can't be used to guess
passwords without limit; and demo signups, so nobody can mass-create
demo accounts to multiply the per-workspace query limits.

Stored in the database (ThrottleEvent), so limits survive a restart and
would hold across several server processes. Keyed by IP only, not
username: locking by username would let anyone lock a real user out
just by failing logins in their name.

The IP itself isn't stored, only a SHA-256 of it, and rows are deleted
once they're older than the longest window (a day). That's
pseudonymous, not anonymous: there are only about 4 billion IPv4
addresses, so a hash can be reversed by brute force. What actually
limits exposure is the short retention."""

import datetime
import hashlib
import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import ThrottleEvent

LOGIN = "login"  # failed console logins and failed signups
DEMO_SIGNUP = "demo_signup"  # successful demo signups

_RETENTION = datetime.timedelta(days=1)  # the longest window any bucket uses


def _key(client_ip: str) -> str:
    return hashlib.sha256(client_ip.encode()).hexdigest()


async def _count(session: AsyncSession, bucket: str, client_ip: str, window_seconds: int) -> int:
    since = datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=window_seconds)
    return (
        await session.execute(
            select(func.count(ThrottleEvent.id)).where(
                ThrottleEvent.bucket == bucket,
                ThrottleEvent.key == _key(client_ip),
                ThrottleEvent.created_at > since,
            )
        )
    ).scalar_one()


async def is_blocked(
    session: AsyncSession, bucket: str, client_ip: str, max_events: int, window_seconds: int
) -> bool:
    return await _count(session, bucket, client_ip, window_seconds) >= max_events


async def claim_attempt(
    session: AsyncSession, bucket: str, client_ip: str, max_events: int, window_seconds: int
) -> uuid.UUID | None:
    """Counts an attempt BEFORE it's checked, for a limit that parallel
    requests mustn't slip past. Checking first and recording after a
    failure let every request sent at once pass the check before any
    failure was recorded. Here each attempt is committed, then counted:
    the k-th to commit sees at least k, so at most max_events get through.

    Returns the attempt's id, to forget() if it succeeds, or None if over
    the limit (that attempt is forgotten at once, so blocked tries don't
    extend the lockout)."""
    attempt_id = await record(session, bucket, client_ip)
    if await _count(session, bucket, client_ip, window_seconds) <= max_events:
        return attempt_id
    await forget(session, attempt_id)
    return None


async def forget(session: AsyncSession, attempt_id: uuid.UUID) -> None:
    """Removes one claimed attempt: it succeeded, so it isn't a failure."""
    await session.execute(delete(ThrottleEvent).where(ThrottleEvent.id == attempt_id))
    await session.commit()


async def record(session: AsyncSession, bucket: str, client_ip: str) -> uuid.UUID:
    """Records one event and prunes everything past retention, so the
    table never holds more than a day of (hashed) addresses."""
    now = datetime.datetime.now(datetime.UTC)
    await session.execute(delete(ThrottleEvent).where(ThrottleEvent.created_at < now - _RETENTION))
    event = ThrottleEvent(id=uuid.uuid4(), bucket=bucket, key=_key(client_ip), created_at=now)
    session.add(event)
    await session.commit()
    return event.id
