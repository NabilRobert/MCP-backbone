"""Sets up the ONE shared connection to this project's own database (the
app's internal bookkeeping database -- see APP_DATABASE_URL in
.env.example) and the base class every table model inherits from.
Everything else in app/ that reads or writes workspaces, API keys, the
audit log, etc. goes through what's defined here."""

from collections.abc import AsyncIterator
from datetime import datetime
from typing import ClassVar

from sqlalchemy import DateTime
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
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
engine = create_async_engine(settings.database_url, poolclass=NullPool)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


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
