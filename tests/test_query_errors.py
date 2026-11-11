"""What a caller is told when its query fails in the database: Postgres's
own message and hint, which are what it needs to fix its SQL, and none of
the server's internals (the driver's class, the SQL as rewritten here, a
link to SQLAlchemy's docs). Real Postgres throughout: the point is the
text Postgres and the driver actually produce."""

import logging

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import Workspace
from app.query.models import QueryExecution
from app.query.service import run_query
from app.query.target import (
    QUERY_FAILED_OUTSIDE_DATABASE,
    QueryExecutionError,
    caller_safe_error,
)

pytestmark = pytest.mark.asyncio

# Fails in the cost check, which runs the query under EXPLAIN.
UNKNOWN_TABLE_SQL = "select * from no_such_table"
# Fails in the cost check too, and Postgres suggests the right column.
MISSPELT_COLUMN_SQL = "select labl from demo_items"
# Passes the cost check and fails only while running.
DIVIDES_BY_ZERO_SQL = "select id / (id - id) as ratio from demo_items"
# Pieces of SQLAlchemy's own error text that must not reach a caller.
INTERNAL_DETAILS = ("sqlalchemy", "asyncpg", "[SQL:", "EXPLAIN", "sqlalche.me")


async def _last_execution(session: AsyncSession) -> QueryExecution:
    result = await session.execute(select(QueryExecution).order_by(QueryExecution.created_at))
    return result.scalars().all()[-1]


async def test_a_query_failing_in_the_cost_check_returns_only_the_postgres_message(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    workspace, _ = seeded_workspace

    result = await run_query(db_session, workspace, UNKNOWN_TABLE_SQL)

    assert result["status"] == "errored"
    assert result["error"] == 'relation "no_such_table" does not exist'


async def test_a_query_failing_while_running_returns_only_the_postgres_message(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    workspace, _ = seeded_workspace

    result = await run_query(db_session, workspace, DIVIDES_BY_ZERO_SQL)

    assert result["status"] == "errored"
    assert result["error"] == "division by zero"


async def test_the_postgres_hint_is_kept_so_the_caller_can_fix_its_sql(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    workspace, _ = seeded_workspace

    result = await run_query(db_session, workspace, MISSPELT_COLUMN_SQL)

    assert result["error"] == (
        'column "labl" does not exist '
        '(hint: Perhaps you meant to reference the column "demo_items.label".)'
    )


async def test_the_audit_log_records_the_same_clean_message(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    workspace, _ = seeded_workspace

    result = await run_query(db_session, workspace, MISSPELT_COLUMN_SQL)

    execution = await _last_execution(db_session)
    assert execution.rejection_reason == result["error"]
    for detail in INTERNAL_DETAILS:
        assert detail not in (execution.rejection_reason or "")


async def test_the_full_error_reaches_the_server_log_at_debug_level_only(
    db_session: AsyncSession,
    seeded_workspace: tuple[Workspace, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    workspace, _ = seeded_workspace
    caplog.set_level(logging.DEBUG)

    await run_query(db_session, workspace, MISSPELT_COLUMN_SQL)

    quoting_the_sql = [record for record in caplog.records if _quotes_sql(record)]
    assert [record.levelno for record in quoting_the_sql] == [logging.DEBUG]


def _quotes_sql(record: logging.LogRecord) -> bool:
    exception_text = str(record.exc_info[1]) if record.exc_info else ""
    return "labl" in record.getMessage() or "labl" in exception_text


async def test_a_failure_outside_the_database_returns_fixed_text() -> None:
    internal = RuntimeError("KeyError in /srv/app/query/service.py line 12")

    assert caller_safe_error(internal) == QUERY_FAILED_OUTSIDE_DATABASE


async def test_this_servers_own_query_errors_are_shown_as_written() -> None:
    own_error = QueryExecutionError("table 'rentals' not found")

    assert caller_safe_error(own_error) == "table 'rentals' not found"
