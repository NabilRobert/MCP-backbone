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

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import ThrottleEvent

LOGIN = "login"  # failed console logins and failed signups
DEMO_SIGNUP = "demo_signup"  # successful demo signups

_RETENTION = datetime.timedelta(days=1)  # the longest window any bucket uses


def _key(client_ip: str) -> str:
    return hashlib.sha256(client_ip.encode()).hexdigest()


async def is_blocked(
    session: AsyncSession, bucket: str, client_ip: str, max_events: int, window_seconds: int
) -> bool:
    since = datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=window_seconds)
    count = (
        await session.execute(
            select(func.count(ThrottleEvent.id)).where(
                ThrottleEvent.bucket == bucket,
                ThrottleEvent.key == _key(client_ip),
                ThrottleEvent.created_at > since,
            )
        )
    ).scalar_one()
    return count >= max_events


async def record(session: AsyncSession, bucket: str, client_ip: str) -> None:
    """Records one event and prunes everything past retention, so the
    table never holds more than a day of (hashed) addresses."""
    now = datetime.datetime.now(datetime.UTC)
    await session.execute(delete(ThrottleEvent).where(ThrottleEvent.created_at < now - _RETENTION))
    session.add(ThrottleEvent(bucket=bucket, key=_key(client_ip), created_at=now))
    await session.commit()
