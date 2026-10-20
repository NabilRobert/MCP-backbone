"""Sets up the ONE shared connection to this project's own database (the
app's internal bookkeeping database -- see APP_DATABASE_URL in
.env.example) and the base class every table model inherits from.
Everything else in app/ that reads or writes workspaces, API keys, the
audit log, etc. goes through what's defined here."""

import re
from collections.abc import AsyncIterator
from datetime import datetime
from typing import ClassVar

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

# NullPool means "don't keep connections open and reuse them, open a
# fresh one each time instead." Normally that would hurt performance,
# but at this project's small scale it costs nothing, and it sidesteps
# a real bug that shows up otherwise: a reused connection can end up
# tied to a different async "event loop" than the one currently running
# (this bit a test run during development), which pooling would have
# caused but NullPool avoids entirely.
#
# statement_cache_size=0 disables asyncpg's own server-side prepared
# statement cache -- necessary if APP_DATABASE_URL ever points through
# PgBouncer in "transaction" or "statement" pooling mode (confirmed by
# actually hitting it: a real deploy pointed this at a PgBouncer-fronted
# Postgres and every query failed with "prepared statement ... already
# exists", exactly the failure mode asyncpg's own error message
# documents for this situation). Plain Postgres, no pooler in front,
# works identically either way -- this isn't a tradeoff being made, it
# just makes the one case that was actually broken work too.
engine = create_async_engine(
    settings.database_url,
    poolclass=NullPool,
    connect_args={"statement_cache_size": 0},
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


_POSTGRES_SCHEME = re.compile(r"^postgresql(\+\w+)?://")


def create_target_engine(connection_url: str) -> AsyncEngine:
    """Every place that connects to a workspace's real TARGET database
    (app/query/service.py, app/query/schema_cache.py -- never this
    project's own bookkeeping one, that's the module-level `engine`
    above) needs the exact same statement_cache_size=0 fix, for the
    exact same reason: a target is just as likely to be PgBouncer-
    fronted as the bookkeeping database was. Confirmed directly, not
    assumed from the bookkeeping fix alone -- the real deployed target
    database hit the identical DuplicatePreparedStatementError once a
    real client actually called a tool against it. One helper here
    instead of repeating connect_args in four separate call sites.

    Also forces the +asyncpg driver regardless of what scheme a stored
    connection_url actually has. This project has only ever tested
    asyncpg; a bare "postgresql://" (no driver suffix at all -- exactly
    what a workspace gets if its connection_url is copied straight from
    APP_TARGET_DATABASE_URL without converting the scheme first, a real
    mistake made more than once while setting this project up)
    silently resolves to a DIFFERENT driver (psycopg) under this
    SQLAlchemy version, which isn't even installed -- confirmed by
    actually hitting ModuleNotFoundError: No module named 'psycopg' on
    a real deployment's first real tool call, not a hypothetical."""
    if _POSTGRES_SCHEME.match(connection_url):
        connection_url = _POSTGRES_SCHEME.sub("postgresql+asyncpg://", connection_url, count=1)
    return create_async_engine(connection_url, connect_args={"statement_cache_size": 0})


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
