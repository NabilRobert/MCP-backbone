"""Limits failed console logins (and failed signups) per client IP, so
the internet-facing login page can't be used to guess passwords
without limit.

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


class FailureLimiter:
    def __init__(self, max_failures: int, window_seconds: int) -> None:
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self._failures: defaultdict[str, deque[float]] = defaultdict(deque)

    def _prune(self, key: str, now: float) -> deque[float]:
        failures = self._failures[key]
        while failures and now - failures[0] > self.window_seconds:
            failures.popleft()
        if not failures:
            # Don't let one-off visitors accumulate forever.
            del self._failures[key]
            return deque()
        return failures

    def is_blocked(self, key: str) -> bool:
        return len(self._prune(key, time.monotonic())) >= self.max_failures

    def record_failure(self, key: str) -> None:
        now = time.monotonic()
        self._prune(key, now)
        self._failures[key].append(now)

    def reset(self, key: str | None = None) -> None:
        if key is None:
            self._failures.clear()
        else:
            self._failures.pop(key, None)


login_limiter = FailureLimiter(settings.login_max_failures, settings.login_failure_window_seconds)
