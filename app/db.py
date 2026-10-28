"""Sets up the ONE shared connection to this project's own database (the
app's internal bookkeeping database -- see APP_DATABASE_URL in
.env.example) and the base class every table model inherits from.
Everything else in app/ that reads or writes workspaces, API keys, the
audit log, etc. goes through what's defined here."""

import asyncio
import re
import weakref
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any, ClassVar

from sqlalchemy import DateTime
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

from app.config import settings

# The bookkeeping database. NullPool (a fresh connection each time):
# negligible cost at this scale, and a pooled connection can end up bound
# to a different event loop than the one running (tests use one loop
# each). statement_cache_size=0: asyncpg's prepared-statement cache breaks
# behind PgBouncer's transaction pooling ("prepared statement already
# exists"); plain Postgres works the same either way.
engine = create_async_engine(
    settings.database_url,
    poolclass=NullPool,
    connect_args={"statement_cache_size": 0},
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


_POSTGRES_SCHEME = re.compile(r"^postgresql(\+\w+)?://")


def create_target_engine(connection_url: str, *, pooled: bool = False) -> AsyncEngine:
    """An engine for a workspace's TARGET database (never the bookkeeping
    one above). Use get_target_engine() below for queries; call this
    directly only for a one-off connection, like checking a URL at signup.

    Every target connection gets:
    - statement_cache_size=0, for the same PgBouncer reason as above.
    - the +asyncpg driver, whatever the URL says: a bare "postgresql://"
      resolves to psycopg under this SQLAlchemy, which isn't installed.
    - READ ONLY transactions (postgresql_readonly, i.e. BEGIN READ ONLY,
      transaction-scoped so PgBouncer-safe). Postgres then refuses writes
      the validator might miss, including inside a SECURITY DEFINER
      function, and needs no CREATE ROLE, so it covers targets where no
      read-only role can be made. A query can't switch it off
      mid-statement; a separate SET TRANSACTION READ WRITE could, which
      needs a way past the single-SELECT validator first. See
      docs/threat-model.md T1."""
    if _POSTGRES_SCHEME.match(connection_url):
        connection_url = _POSTGRES_SCHEME.sub("postgresql+asyncpg://", connection_url, count=1)
    options: dict[str, Any] = (
        # Kept small: one server process, and a target may be a shared,
        # connection-limited managed host. pre_ping drops connections the
        # server or a pooler closed; recycle bounds how long one lives.
        {"pool_size": 2, "max_overflow": 3, "pool_pre_ping": True, "pool_recycle": 300}
        if pooled
        else {"poolclass": NullPool}
    )
    return create_async_engine(
        connection_url,
        connect_args={"statement_cache_size": 0},
        execution_options={"postgresql_readonly": True},
        **options,
    )


# One pooled engine per target URL, so tool calls reuse connections
# instead of opening (and TLS-handshaking) a new one every time. Kept per
# event loop, because an engine's connections belong to the loop that made
# them: the server has one loop, but each test gets its own. A
# WeakKeyDictionary forgets a loop's engines once that loop is gone.
_target_engines: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, AsyncEngine]] = (
    weakref.WeakKeyDictionary()
)


def get_target_engine(connection_url: str) -> AsyncEngine:
    """The shared, pooled engine for a target database. Use this for
    every query path; create_target_engine() directly only for a one-off
    connection, like testing a URL a user just typed in at signup."""
    engines = _target_engines.setdefault(asyncio.get_running_loop(), {})
    engine = engines.get(connection_url)
    if engine is None:
        engine = engines[connection_url] = create_target_engine(connection_url, pooled=True)
    return engine


async def dispose_target_engines() -> None:
    """Closes every pooled target connection made on this event loop
    (server shutdown, and after each test)."""
    engines = _target_engines.pop(asyncio.get_running_loop(), {})
    for engine in engines.values():
        await engine.dispose()


class Base(DeclarativeBase):
    """Every table model in this project (Workspace, ApiKey,
    QueryExecution, ...) inherits from this. Its only real job here:
    make sure every date/time column is always stored WITH timezone
    information attached, everywhere, automatically -- without this,
    it's easy to accidentally mix "timezone-aware" and "timezone-naive"
    timestamps, which Python and the database then disagree about and
    crash on (this happened once during development, which is why it's
    now enforced centrally instead of trusted to be done right in every
    single model file)."""

    type_annotation_map: ClassVar = {datetime: DateTime(timezone=True)}


async def get_session() -> AsyncIterator[AsyncSession]:
    """A small helper that hands out one database session and makes
    sure it gets cleaned up afterward, even if something goes wrong
    while using it."""
    async with SessionLocal() as session:
        yield session
