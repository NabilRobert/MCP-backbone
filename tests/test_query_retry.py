"""How run_query's execute step treats a failure: a dropped connection is
retried once, a statement timeout never is (app/query/service.py,
_run_with_retry). A real database can't drop a connection on cue, so these
use a stand-in connection that fails the way SQLAlchemy reports asyncpg's
errors: the driver's exception as the __cause__ of the wrapper's `orig`."""

from typing import Any, cast

import asyncpg  # pyright: ignore[reportMissingTypeStubs] -- asyncpg ships no stubs
import pytest
from sqlalchemy.ext.asyncio import AsyncConnection

from app.query import service
from app.query.service import (
    _run_with_retry,  # pyright: ignore[reportPrivateUsage]
)


class _WrappedDriverError(Exception):
    """Shaped like SQLAlchemy's DBAPIError around an asyncpg error."""

    def __init__(self, cause: BaseException) -> None:
        super().__init__(str(cause))
        self.orig = Exception("driver error")
        self.orig.__cause__ = cause


class _Row:
    def __init__(self, values: dict[str, Any]) -> None:
        self._mapping = values


class _Result:
    def __init__(self, rows: list[_Row]) -> None:
        self._rows = rows

    async def fetchmany(self, size: int) -> list[_Row]:
        return self._rows[:size]

    async def close(self) -> None:
        return None


class _FlakyConnection:
    """Raises each of `failures` in turn, then returns one row."""

    def __init__(self, failures: list[BaseException]) -> None:
        self._failures = failures
        self.calls = 0

    async def stream(self, _statement: object) -> _Result:
        self.calls += 1
        if self._failures:
            raise _WrappedDriverError(self._failures.pop(0))
        return _Result([_Row({"answer": 42})])


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "RETRY_BACKOFF_SECONDS", 0)


async def test_dropped_connection_is_retried_once_and_then_succeeds() -> None:
    conn = _FlakyConnection([asyncpg.exceptions.ConnectionDoesNotExistError("gone")])

    outcome, attempts = await _run_with_retry(cast(AsyncConnection, conn), "select 42", 1.0)

    assert outcome.status == "succeeded"
    assert outcome.rows == [{"answer": 42}]
    assert attempts == 2


async def test_connection_dropped_on_every_attempt_is_reported_as_errored() -> None:
    conn = _FlakyConnection(
        [
            asyncpg.exceptions.ConnectionDoesNotExistError("gone")
            for _ in range(service.MAX_ATTEMPTS)
        ]
    )

    outcome, attempts = await _run_with_retry(cast(AsyncConnection, conn), "select 42", 1.0)

    assert outcome.status == "errored"
    assert attempts == service.MAX_ATTEMPTS
    assert conn.calls == service.MAX_ATTEMPTS


async def test_statement_timeout_is_not_retried() -> None:
    conn = _FlakyConnection([asyncpg.exceptions.QueryCanceledError("too slow")])

    outcome, attempts = await _run_with_retry(cast(AsyncConnection, conn), "select 42", 1.0)

    assert outcome.status == "timed_out"
    assert outcome.cost == 1.0
    assert attempts == 1
    assert conn.calls == 1
