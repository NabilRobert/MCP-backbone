"""This is where list_tables and run_query actually happen -- the two
checks in validation.py and cost_check.py get called from here, in
order, before any real query touches the real database. This file also
guarantees every single attempt gets written to the audit log, no
matter how it turns out."""

import asyncio
import datetime
from dataclasses import dataclass, field
from typing import Any

import asyncpg  # pyright: ignore[reportMissingTypeStubs] -- asyncpg ships no stubs
from opentelemetry import trace
from opentelemetry.trace import SpanKind
from sqlalchemy import inspect, select, text
from sqlalchemy.engine import Inspector
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.auth.models import Workspace
from app.auth.rate_limit import RateLimitExceeded, allowance, check_and_increment
from app.config import settings
from app.db import get_target_engine
from app.query.cost_check import check_cost
from app.query.models import DataSourceConnection, QueryExecution
from app.query.network_guard import UnsafeTargetError, ensure_public_target
from app.query.validation import QueryRejected, validate_and_normalize
from app.tracing import DB_ATTRIBUTES, mark_failed, stage

# If a query fails because the DATABASE CONNECTION itself dropped (a
# network blip, the database restarting, etc.), that's worth retrying --
# it's not the query's fault. These are the specific error types that
# count as "the connection broke," not "the query was bad."
#
# A query that times out for being slow/expensive is a completely
# different situation and is NEVER retried below -- retrying something
# slow just makes the database do the same expensive work twice.
TRANSIENT_EXCEPTIONS = (
    asyncpg.exceptions.ConnectionDoesNotExistError,
    asyncpg.exceptions.ConnectionFailureError,
    asyncpg.exceptions.InterfaceError,
    OSError,
)
MAX_ATTEMPTS = 2
RETRY_BACKOFF_SECONDS = 0.5


class QueryExecutionError(Exception):
    """Raised for problems that aren't about SQL safety (that's
    QueryRejected, in validation.py) -- things like "this workspace
    isn't even set up to query anything yet.\""""


def _asyncpg_cause(exc: BaseException) -> BaseException | None:
    """A small helper for digging out the real, specific database error
    from underneath the generic wrapper exception that SQLAlchemy raises.
    Needed so the code below can tell "the connection dropped" apart from
    "the query timed out" apart from "something else entirely broke.\""""
    orig = getattr(exc, "orig", None)
    return getattr(orig, "__cause__", None)


async def get_data_source(session: AsyncSession, workspace: Workspace) -> DataSourceConnection:
    """Looks up which real, outside database this workspace is allowed
    to query. Used by every function in this file (and in
    schema_cache.py) that needs to actually connect somewhere."""
    result = await session.execute(
        select(DataSourceConnection).where(DataSourceConnection.workspace_id == workspace.id)
    )
    data_source = result.scalar_one_or_none()
    if data_source is None:
        raise QueryExecutionError("workspace has no data source connection configured")
    # Re-checked on every use, not just at registration: the host's DNS
    # could have been changed since to point somewhere private. The
    # server's own default database is the operator's choice, not a
    # user's, so it's exempt (it may legitimately be on localhost).
    if not data_source.uses_default_target:
        try:
            await ensure_public_target(data_source.connection_url)
        except UnsafeTargetError as exc:
            raise QueryExecutionError(str(exc)) from exc
    return data_source


@dataclass(frozen=True)
class QueryOutcome:
    """How one query attempt ended: what the audit log records, and what
    the caller is told. `status` is "succeeded", "rejected", "errored" or
    "timed_out"; `error` is the reason for anything but success."""

    status: str
    error: str | None = None
    cost: float | None = None
    rows: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    is_truncated: bool = False


async def write_audit(
    session: AsyncSession, data_source: DataSourceConnection, sql: str, outcome: QueryOutcome
) -> None:
    """Writes one row to the audit log. This is called no matter how a
    query attempt ends up -- succeeded, rejected, timed out, or errored
    -- so there's always a complete record of what was asked and what
    happened. If this write itself fails for some reason, the error is
    allowed to bubble up and the caller never gets a result back: a
    successful query must never go unrecorded, even if that means
    occasionally failing a perfectly good query because the audit log
    itself had a problem."""
    # The outcome, on the request's own span (this always runs directly
    # under it), so the trace viewer can search calls by it.
    trace.get_current_span().set_attribute("mcp_backbone.query.status", outcome.status)
    with stage(
        "audit",
        kind=SpanKind.CLIENT,
        attributes={
            **DB_ATTRIBUTES,
            "db.operation.name": "INSERT",
            "db.collection.name": QueryExecution.__tablename__,
        },
    ):
        session.add(
            QueryExecution(
                workspace_id=data_source.workspace_id,
                data_source_connection_id=data_source.id,
                query_text=sql,
                status=outcome.status,
                cost_estimate=outcome.cost,
                rejection_reason=outcome.error,
                created_at=datetime.datetime.now(datetime.UTC),
            )
        )
        await session.commit()


async def record_refusal(
    session: AsyncSession, workspace: Workspace, sql: str, reason: str
) -> None:
    """Audits a data-reading call that was refused before it ran (e.g.
    by the key's scopes), so the log still shows every attempt. Skipped
    only if the workspace has no data source to attribute it to."""
    try:
        data_source = await get_data_source(session, workspace)
    except QueryExecutionError:
        return
    await write_audit(session, data_source, sql, QueryOutcome(status="rejected", error=reason))


def visible_table_names(sync_conn: Any) -> list[str]:
    """Table names an AI client should see: every table in the current
    schema except the partitions of a partitioned table. Querying the
    partitioned parent already covers all of its partitions, so listing
    them too invites double counting. Found by the benchmark: with
    `payment` and its 55 monthly partitions all listed, an agent summed
    the parent plus every partition and counted each payment twice."""
    partitions = set(
        sync_conn.execute(
            text(
                "SELECT c.relname FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE c.relispartition AND n.nspname = current_schema()"
            )
        ).scalars()
    )
    inspector: Inspector = inspect(sync_conn)
    return [name for name in inspector.get_table_names() if name not in partitions]


async def list_tables(session: AsyncSession, workspace: Workspace) -> list[str]:
    """Connects to the workspace's real database just long enough to ask
    "what tables exist here?" and returns their names."""
    data_source = await get_data_source(session, workspace)
    async with get_target_engine(data_source.connection_url).connect() as conn:
        return await conn.run_sync(visible_table_names)


async def run_query(session: AsyncSession, workspace: Workspace, sql: str) -> dict[str, Any]:
    """The main event: takes whatever SQL text an AI client sent, runs it
    through every safety check in order, actually executes it if it
    passes, and always writes down what happened. Returns a dict with
    status ("succeeded"/"rejected"/"errored"/"timed_out"), the resulting
    rows (empty unless it succeeded; at most APP_MAX_RESULT_ROWS), and an
    error message (empty unless something went wrong). A result cut off
    at the cap also carries truncated=True and a note saying so.

    Each step below is its own function, timed as its own span in the
    request's trace (app/tracing.py): rate_limit, validate, cost_check,
    execute, then audit. A step that ends the request returns the
    outcome, and the audit is written outside the steps' spans, so it
    shows as their sibling."""
    data_source = await get_data_source(session, workspace)

    refusal = await _check_rate_limit(session, workspace)
    if refusal is not None:
        return await _finish(session, data_source, sql, refusal)

    validated = _validate(sql)
    if isinstance(validated, QueryOutcome):
        return await _finish(session, data_source, sql, validated)

    async with get_target_engine(data_source.connection_url).connect() as conn:
        cost = await _estimate_cost(conn, validated)
        if isinstance(cost, QueryOutcome):
            return await _finish(session, data_source, sql, cost)
        outcome = await _execute(conn, validated, cost)

    return await _finish(session, data_source, sql, outcome)


async def _check_rate_limit(session: AsyncSession, workspace: Workspace) -> QueryOutcome | None:
    """Step 1: has this workspace made too many requests recently? This
    runs before anything else, so it still counts a request that
    would've been rejected anyway. Returns the refusal, or None."""
    with stage("rate_limit") as span:
        try:
            await check_and_increment(
                session,
                workspace,
                allowance(workspace.query_rate_limit, settings.demo_rate_limit),
                settings.demo_rate_limit_window_seconds,
            )
        except RateLimitExceeded as exc:
            mark_failed(span, "rate_limited", exc.reason)
            return QueryOutcome(status="rejected", error=exc.reason)
    return None


def _validate(sql: str) -> str | QueryOutcome:
    """Step 2: is this SQL even safe? (see app/query/validation.py for
    exactly what "safe" means here.) Returns the normalized SQL, or the
    refusal. A refused query never opens a connection to the target."""
    with stage("validate") as span:
        try:
            return validate_and_normalize(sql)
        except QueryRejected as exc:
            mark_failed(span, "rejected_by_validation", exc.reason)
            return QueryOutcome(status="rejected", error=exc.reason)


async def _estimate_cost(conn: AsyncConnection, normalized_sql: str) -> float | QueryOutcome:
    """Step 3: how expensive would this query be? (see
    app/query/cost_check.py.) Returns the estimate, or the outcome that
    ends the request: a rejection over the threshold, or an error (e.g.
    EXPLAIN on a table that doesn't exist)."""
    with stage(
        "cost_check",
        kind=SpanKind.CLIENT,
        attributes={
            **DB_ATTRIBUTES,
            "db.operation.name": "EXPLAIN",
            "mcp_backbone.cost.threshold": settings.cost_threshold,
        },
    ) as span:
        try:
            cost = await check_cost(
                conn, normalized_sql, settings.cost_threshold, settings.explain_timeout_seconds
            )
        except QueryRejected as exc:
            mark_failed(span, "rejected_by_cost", exc.reason)
            return QueryOutcome(status="rejected", error=exc.reason)
        except Exception as exc:  # noqa: BLE001 -- e.g. EXPLAIN on a table that doesn't exist
            mark_failed(span, type(exc).__qualname__, str(exc))
            return QueryOutcome(status="errored", error=str(exc))
        span.set_attribute("mcp_backbone.cost.estimate", cost)
        return cost


async def _execute(conn: AsyncConnection, normalized_sql: str, cost: float) -> QueryOutcome:
    """Step 4: actually run the query, with a safety net -- if it takes
    longer than the timeout, Postgres kills it itself rather than
    letting it run forever."""
    with stage(
        "execute",
        kind=SpanKind.CLIENT,
        attributes={**DB_ATTRIBUTES, "db.operation.name": "SELECT"},
    ) as span:
        if settings.otel_capture_content:
            span.set_attribute("db.query.text", normalized_sql)
        await conn.execute(
            text(f"SET LOCAL statement_timeout = {settings.query_timeout_seconds * 1000}")
        )

        outcome, attempts = await _run_with_retry(conn, normalized_sql, cost)

        span.set_attribute("mcp_backbone.execute.attempts", attempts)
        if outcome.status == "succeeded":
            span.set_attribute("db.response.returned_rows", len(outcome.rows))
            span.set_attribute("mcp_backbone.result.truncated", outcome.is_truncated)
        else:
            mark_failed(span, outcome.status, outcome.error or "")
    return outcome


async def _run_with_retry(
    conn: AsyncConnection, normalized_sql: str, cost: float
) -> tuple[QueryOutcome, int]:
    """Runs the query, up to MAX_ATTEMPTS times. Most queries succeed on
    the first try; a retry happens only when the database CONNECTION
    itself dropped (see TRANSIENT_EXCEPTIONS above). Returns the outcome
    and how many attempts it took."""
    attempt = 1
    while True:
        try:
            rows, is_truncated = await _fetch_capped(conn, normalized_sql)
        except Exception as exc:  # noqa: BLE001 -- classified below, never silently swallowed
            cause = _asyncpg_cause(exc)
            if isinstance(cause, asyncpg.exceptions.QueryCanceledError):
                # The statement timeout kicked in. Never retried: the query
                # was just too slow, and trying again would be slow again.
                return QueryOutcome(
                    status="timed_out", error="statement timeout exceeded", cost=cost
                ), attempt
            if isinstance(cause, TRANSIENT_EXCEPTIONS) and attempt < MAX_ATTEMPTS:
                # The connection broke, not the query: pause, then try again.
                attempt += 1
                await asyncio.sleep(RETRY_BACKOFF_SECONDS)
                continue
            return QueryOutcome(status="errored", error=str(exc), cost=cost), attempt
        succeeded = QueryOutcome(
            status="succeeded", cost=cost, rows=rows, is_truncated=is_truncated
        )
        return succeeded, attempt


async def _fetch_capped(
    conn: AsyncConnection, normalized_sql: str
) -> tuple[list[dict[str, Any]], bool]:
    """Streams the result through a server-side cursor and cuts it off at
    the cap: the cost check limits how expensive a query is, not how big
    its result is, and a cheap "select * from rental" is 16,000 rows --
    far too many to hand an AI model. One extra row is fetched only to
    tell "exactly at the cap" from "cut off". Returns the rows and
    whether they were cut off."""
    result = await conn.stream(text(normalized_sql))
    fetched = await result.fetchmany(settings.max_result_rows + 1)
    await result.close()
    rows = [
        dict(row._mapping)  # pyright: ignore[reportPrivateUsage]
        for row in fetched[: settings.max_result_rows]
    ]
    return rows, len(fetched) > settings.max_result_rows


async def _finish(
    session: AsyncSession, data_source: DataSourceConnection, sql: str, outcome: QueryOutcome
) -> dict[str, Any]:
    """Step 5: whatever happened, write it to the audit log before telling
    the caller the result."""
    await write_audit(session, data_source, sql, outcome)
    response: dict[str, Any] = {
        "status": outcome.status,
        "rows": outcome.rows,
        "error": outcome.error,
    }
    if outcome.is_truncated:
        response["truncated"] = True
        response["note"] = (
            f"Only the first {settings.max_result_rows} rows are returned. "
            "Aggregate, filter, or add a LIMIT to get a complete answer."
        )
    return response
