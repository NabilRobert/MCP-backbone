"""Everything about reaching a workspace's target database: which one it
is, whether its address is still allowed, opening a connection, and what
a failure may say. Every path that reads a target goes through here, so
none of them can leak a driver's error text, which can name the host.
app/query/service.py and schema_cache.py build on this."""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import asyncpg  # pyright: ignore[reportMissingTypeStubs] -- asyncpg ships no stubs
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.auth.models import Workspace
from app.crypto import EncryptionKeyError
from app.db import get_target_engine
from app.query.models import DataSourceConnection
from app.query.network_guard import UnsafeTargetError, ensure_public_target

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

logger = logging.getLogger(__name__)

# What a caller and the audit log see when the target can't be used. Fixed
# text on purpose: a driver's own message names the host, port or login,
# and the default database is the operator's, not the caller's. The details
# go to the server log.
TARGET_UNREACHABLE = "could not connect to this workspace's database: try again in a moment"
TARGET_NOT_ALLOWED = (
    "this workspace's database address is no longer allowed: "
    "it must resolve to a public internet address"
)
# For a failure that isn't Postgres's (a bug here, say): its text could be
# anything, so the caller gets this and the server log gets the type.
QUERY_FAILED_OUTSIDE_DATABASE = (
    "the query failed inside this server, not in the database: "
    "try again, and report it if it keeps happening"
)


class QueryExecutionError(Exception):
    """Raised for problems that aren't about SQL safety (that's
    QueryRejected, in validation.py) -- things like "this workspace
    isn't even set up to query anything yet.\""""


class TargetUnreachable(QueryExecutionError):
    """A connection to the target database couldn't be opened."""

    def __init__(self) -> None:
        super().__init__(TARGET_UNREACHABLE)


def asyncpg_cause(exc: BaseException) -> BaseException:
    """The real, specific database error. SQLAlchemy usually wraps it, but
    fetching from a stream raises the driver's own error unwrapped, so
    this handles both. It's how the code below tells "the connection
    dropped" apart from "the query timed out" apart from anything else."""
    orig = getattr(exc, "orig", None)
    return getattr(orig, "__cause__", None) or exc


def is_connection_failure(exc: BaseException) -> bool:
    """True if the connection broke, as opposed to the query being bad."""
    return isinstance(exc, TargetUnreachable) or isinstance(
        asyncpg_cause(exc), TRANSIENT_EXCEPTIONS
    )


def caller_safe_error(exc: BaseException) -> str:
    """The text to audit and return for a failed query. A connection
    failure's text can name the host, so it becomes fixed text. Anything
    else keeps only what Postgres itself said (see postgres_message)."""
    if is_connection_failure(exc):
        logger.warning("target database connection failed: %s", exc)
        return TARGET_UNREACHABLE
    if isinstance(exc, QueryExecutionError):
        return str(exc)  # this module's own errors are written to be shown
    # Debug only: SQLAlchemy's text quotes the query, which can hold
    # whatever the caller typed, and the audit log already keeps the SQL.
    logger.debug("a query failed", exc_info=exc)
    return postgres_message(exc)


def postgres_message(exc: BaseException) -> str:
    """Postgres's own message, plus its hint when it gives one ("Perhaps
    you meant to reference the column ..."): enough for the caller to fix
    its SQL. SQLAlchemy's str(exc) would add the driver's class name, the
    SQL as this server rewrote it (EXPLAIN included) and a link to
    SQLAlchemy's docs, none of which help the caller. The detail field is
    left out: it can quote row values, and a hint covers what a read needs."""
    cause = asyncpg_cause(exc)
    if not isinstance(cause, asyncpg.exceptions.PostgresError):
        logger.warning("a query failed outside the database: %s", type(cause).__qualname__)
        return QUERY_FAILED_OUTSIDE_DATABASE
    # asyncpg sets these per error from the server's reply, so its classes
    # don't declare them; getattr keeps the types honest.
    message: str = getattr(cause, "message", "") or str(cause)
    hint: str | None = getattr(cause, "hint", None)
    if hint:
        return f"{message} (hint: {hint})"
    return message


@asynccontextmanager
async def connect_to_target(connection_url: str) -> AsyncGenerator[AsyncConnection]:
    """A connection to a target database, for every path that reads one.
    Raises TargetUnreachable, which is safe to show, if none can be opened."""
    try:
        conn = await get_target_engine(connection_url).connect()
    except Exception as exc:  # any failure here means "can't connect"
        logger.warning("could not connect to a target database: %s", exc)
        raise TargetUnreachable from exc
    try:
        yield conn
    finally:
        await conn.close()


async def find_data_source(session: AsyncSession, workspace: Workspace) -> DataSourceConnection:
    """Looks up which real, outside database this workspace is allowed
    to query. Raises only if it has none, the one case with nothing to
    attribute an audit row to."""
    result = await session.execute(
        select(DataSourceConnection).where(DataSourceConnection.workspace_id == workspace.id)
    )
    data_source = result.scalar_one_or_none()
    if data_source is None:
        raise QueryExecutionError("workspace has no data source connection configured")
    return data_source


async def ensure_target_allowed(data_source: DataSourceConnection) -> None:
    """Re-checked on every use, not just at registration: the host's DNS
    could have been changed since to point somewhere private. The
    server's own default database is the operator's choice, not a
    user's, so it's exempt (it may legitimately be on localhost)."""
    if data_source.uses_default_target:
        return
    try:
        await ensure_public_target(data_source.connection_url)
    except UnsafeTargetError as exc:
        logger.warning("refused a target database: %s", exc)
        raise QueryExecutionError(TARGET_NOT_ALLOWED) from exc
    except EncryptionKeyError as exc:
        logger.warning("could not read a target database URL: %s", exc)
        raise TargetUnreachable from exc


async def get_data_source(session: AsyncSession, workspace: Workspace) -> DataSourceConnection:
    """find_data_source plus the address check, for callers that write no
    audit row (data-reading calls do the two steps apart, to audit a
    refusal)."""
    data_source = await find_data_source(session, workspace)
    await ensure_target_allowed(data_source)
    return data_source
