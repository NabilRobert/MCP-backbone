"""Per-client-IP limits for the console: failed logins (and failed
signups), so the internet-facing login page can't be used to guess
passwords without limit; and demo signups, so nobody can mass-create
demo accounts to multiply the per-workspace query limits.

Kept in memory, deliberately: this server runs as one process, so one
in-memory counter sees every attempt, and a database table would add
a write to every failed login for no real gain. The costs, accepted at
this scale: counts reset when the server restarts, and it would need
to move to the database (or Redis) if the server ever ran as several
processes. Keyed by IP only, not username -- locking by username would
let anyone lock a real user out just by failing logins in their name."""

import time
from collections import defaultdict, deque

from app.config import settings


class EventLimiter:
    """Counts events per key in a sliding window; blocked once the count
    reaches max_events."""

    def __init__(self, max_events: int, window_seconds: int) -> None:
        self.max_events = max_events
        self.window_seconds = window_seconds
        self._events: defaultdict[str, deque[float]] = defaultdict(deque)

    def _prune(self, key: str, now: float) -> deque[float]:
        events = self._events[key]
        while events and now - events[0] > self.window_seconds:
            events.popleft()
        if not events:
            # Don't let one-off visitors accumulate forever.
            del self._events[key]
            return deque()
        return events

    def is_blocked(self, key: str) -> bool:
        return len(self._prune(key, time.monotonic())) >= self.max_events

    def record(self, key: str) -> None:
        now = time.monotonic()
        self._prune(key, now)
        self._events[key].append(now)

    def reset(self, key: str | None = None) -> None:
        if key is None:
            self._events.clear()
        else:
            self._events.pop(key, None)


login_limiter = EventLimiter(settings.login_max_failures, settings.login_failure_window_seconds)
demo_signup_limiter = EventLimiter(settings.demo_signups_per_ip_per_day, 24 * 60 * 60)
