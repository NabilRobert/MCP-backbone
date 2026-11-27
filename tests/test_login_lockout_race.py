"""The console's failed-login lockout holds when the guesses arrive all
at once, and checking a password never blocks the event loop."""

import asyncio
import re
import threading
import time

import httpx2
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import security
from app.auth.models import Workspace
from app.config import settings
from app.main import app
from tests.console_client import ConsoleClient

pytestmark = pytest.mark.asyncio

PARALLEL_GUESSES = 12
_TOKEN_FIELD = re.compile(r'name="csrf_token" value="([^"]+)"')


async def _post_wrong_passwords_at_once(count: int) -> list[int]:
    """Sends `count` wrong-password logins concurrently, from one browser
    session and one IP, and returns their status codes."""
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://testserver") as client:
        page = await client.get("/console/login")
        match = _TOKEN_FIELD.search(page.text)
        assert match, "the login page has no CSRF token field"
        form = {"csrf_token": match.group(1), "username": "admin", "password": "wrong"}
        responses = await asyncio.gather(
            *(client.post("/console/login", data=form) for _ in range(count))
        )
    return [response.status_code for response in responses]


async def test_parallel_wrong_passwords_get_no_more_guesses_than_the_limit(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    statuses = await _post_wrong_passwords_at_once(PARALLEL_GUESSES)

    checked = [status for status in statuses if status == 200]
    assert len(checked) <= settings.login_max_failures, statuses
    assert statuses.count(429) >= PARALLEL_GUESSES - settings.login_max_failures


async def test_a_password_is_checked_off_the_event_loop(
    db_session: AsyncSession,
    seeded_workspace: tuple[Workspace, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checked_on: list[threading.Thread] = []
    real_verify = security.verify_password

    def recording_verify(password: str, stored: str) -> bool:
        checked_on.append(threading.current_thread())
        return real_verify(password, stored)

    monkeypatch.setattr(security, "verify_password", recording_verify)

    await _post_wrong_passwords_at_once(1)

    assert checked_on, "the password was never checked"
    assert threading.main_thread() not in checked_on


async def test_successful_logins_never_count_toward_the_lockout(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    """Each attempt is counted before it's checked, so a success must
    take its count back, or a user logging in often would lock themself out."""
    client = ConsoleClient(app, follow_redirects=False)
    for _ in range(settings.login_max_failures + 1):
        login = client.post("/console/login", data={"username": "admin", "password": "testpass"})
        assert login.status_code == 303


async def test_no_more_than_two_passwords_are_hashed_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each Argon2 hash takes 64 MiB, so a burst of logins must queue
    rather than all hash at once."""
    running = 0
    most_at_once = 0
    lock = threading.Lock()

    def slow_verify(password: str, stored: str) -> bool:
        nonlocal running, most_at_once
        with lock:
            running += 1
            most_at_once = max(most_at_once, running)
        time.sleep(0.05)
        with lock:
            running -= 1
        return False

    monkeypatch.setattr(security, "verify_password", slow_verify)

    await asyncio.gather(*(security.verify_password_in_thread("x", "y") for _ in range(8)))

    assert most_at_once == security.MAX_CONCURRENT_HASHES
