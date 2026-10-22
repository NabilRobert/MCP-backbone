"""This is where list_tables and run_query actually happen -- the two
checks in validation.py and cost_check.py get called from here, in
order, before any real query touches the real database. This file also
guarantees every single attempt gets written to the audit log, no
matter how it turns out."""

import asyncio
import datetime
from typing import Any

import asyncpg  # pyright: ignore[reportMissingTypeStubs] -- asyncpg ships no stubs
from sqlalchemy import inspect, select, text
from sqlalchemy.engine import Inspector
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import Workspace
from app.auth.rate_limit import RateLimitExceeded, check_and_increment
from app.config import settings
from app.db import get_target_engine
from app.query.cost_check import check_cost
from app.query.models import DataSourceConnection, QueryExecution
from app.query.network_guard import UnsafeTargetError, ensure_public_target
from app.query.validation import QueryRejected, validate_and_normalize

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


async def write_audit(
    session: AsyncSession,
    workspace: Workspace,
    data_source: DataSourceConnection,
    sql: str,
    status: str,
    cost_estimate: float | None,
    rejection_reason: str | None,
) -> None:
    """Writes one row to the audit log. This is called no matter how a
    query attempt ends up -- succeeded, rejected, timed out, or errored
    -- so there's always a complete record of what was asked and what
    happened. If this write itself fails for some reason, the error is
    allowed to bubble up and the caller never gets a result back: a
    successful query must never go unrecorded, even if that means
    occasionally failing a perfectly good query because the audit log
    itself had a problem."""
    session.add(
        QueryExecution(
            workspace_id=workspace.id,
            data_source_connection_id=data_source.id,
            query_text=sql,
            status=status,
            cost_estimate=cost_estimate,
            rejection_reason=rejection_reason,
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
    await write_audit(session, workspace, data_source, sql, "rejected", None, reason)


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
    at the cap also carries truncated=True and a note saying so."""
    data_source = await get_data_source(session, workspace)

    # Step 1: has this workspace made too many requests recently? This
    # check happens before anything else, so it still counts even for a
    # request that would've been rejected anyway.
    try:
        await check_and_increment(
            session, workspace, settings.demo_rate_limit, settings.demo_rate_limit_window_seconds
        )
    except RateLimitExceeded as exc:
        await write_audit(session, workspace, data_source, sql, "rejected", None, exc.reason)
        return {"status": "rejected", "rows": [], "error": exc.reason}

    # Step 2: is this SQL even safe? (see app/query/validation.py for
    # exactly what "safe" means here.) Nothing below this point ever
    # runs for a query that fails this check -- it's rejected before any
    # database connection to the real target is even opened.
    try:
        normalized_sql = validate_and_normalize(sql)
    except QueryRejected as exc:
        await write_audit(session, workspace, data_source, sql, "rejected", None, exc.reason)
        return {"status": "rejected", "rows": [], "error": exc.reason}

    status = "succeeded"
    error_message: str | None = None
    cost: float | None = None
    rows: list[dict[str, Any]] = []
    truncated = False

    async with get_target_engine(data_source.connection_url).connect() as conn:
        # Step 3: how expensive would this query be? (see
        # app/query/cost_check.py.) Only reached once the SQL has
        # already passed the safety check above.
        try:
            cost = await check_cost(
                conn, normalized_sql, settings.cost_threshold, settings.explain_timeout_seconds
            )
        except QueryRejected as exc:
            await write_audit(session, workspace, data_source, sql, "rejected", None, exc.reason)
            return {"status": "rejected", "rows": [], "error": exc.reason}
        except Exception as exc:  # noqa: BLE001 -- e.g. EXPLAIN on a table that doesn't exist
            await write_audit(session, workspace, data_source, sql, "errored", None, str(exc))
            return {"status": "errored", "rows": [], "error": str(exc)}

        # Step 4: actually run the query, but with a safety net -- if
        # it takes longer than this timeout, Postgres kills it
        # itself rather than letting it run forever.
        await conn.execute(
            text(f"SET LOCAL statement_timeout = {settings.query_timeout_seconds * 1000}")
        )

        # Try running the query, up to MAX_ATTEMPTS times. Most
        # queries succeed on the very first try; the loop only
        # exists to retry the rare case where the database
        # CONNECTION itself dropped (see TRANSIENT_EXCEPTIONS above).
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                # Streamed through a server-side cursor and cut off at
                # the cap: the cost check limits how expensive a query
                # is, not how big its result is, and a cheap
                # "select * from rental" is 16,000 rows -- far too many
                # to hand an AI model. One extra row is fetched only to
                # tell "exactly at the cap" from "cut off".
                result = await conn.stream(text(normalized_sql))
                fetched = await result.fetchmany(settings.max_result_rows + 1)
                await result.close()
                truncated = len(fetched) > settings.max_result_rows
                rows = [
                    dict(row._mapping)  # pyright: ignore[reportPrivateUsage]
                    for row in fetched[: settings.max_result_rows]
                ]
                break  # success -- stop trying
            except Exception as exc:  # noqa: BLE001 -- classified below, never silently swallowed
                cause = _asyncpg_cause(exc)
                if isinstance(cause, asyncpg.exceptions.QueryCanceledError):
                    # The timeout from step 4 kicked in. Never retry
                    # this -- the query was just too slow, and
                    # trying again would just be slow again.
                    status, error_message = "timed_out", "statement timeout exceeded"
                    break
                if isinstance(cause, TRANSIENT_EXCEPTIONS) and attempt < MAX_ATTEMPTS:
                    # The connection itself broke, not the query --
                    # worth a short pause and one more try.
                    await asyncio.sleep(RETRY_BACKOFF_SECONDS)
                    continue
                # Anything else: give up and report it as an error.
                status, error_message = "errored", str(exc)
                break

    # Step 5: no matter what happened above, write it to the audit log
    # before telling the caller the result.
    await write_audit(session, workspace, data_source, sql, status, cost, error_message)
    response: dict[str, Any] = {"status": status, "rows": rows, "error": error_message}
    if truncated:
        response["truncated"] = True
        response["note"] = (
            f"Only the first {settings.max_result_rows} rows are returned. "
            "Aggregate, filter, or add a LIMIT to get a complete answer."
        )
    return response
