from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.pool import AsyncAdaptedQueuePool

from app.auth.models import Workspace
from app.db import create_target_engine, get_target_engine
from app.query.service import get_data_source, list_tables


def test_bare_postgres_scheme_gets_asyncpg_driver() -> None:
    """Regression test for a real bug: a workspace's connection_url
    with no driver suffix at all silently resolved to a different,
    uninstalled driver (psycopg) and broke every tool call on a real
    deployment."""
    engine = create_target_engine("postgresql://user:pass@host/db")
    assert engine.url.drivername == "postgresql+asyncpg"


def test_wrong_driver_gets_forced_to_asyncpg() -> None:
    """A workspace that somehow ended up with an explicit, different
    driver (seen for real: +psycopg) gets corrected too -- this
    project has only ever tested asyncpg."""
    engine = create_target_engine("postgresql+psycopg://user:pass@host/db")
    assert engine.url.drivername == "postgresql+asyncpg"


def test_correct_driver_is_left_alone() -> None:
    engine = create_target_engine("postgresql+asyncpg://user:pass@host/db")
    assert engine.url.drivername == "postgresql+asyncpg"


async def test_target_engines_are_pooled_and_reused(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    """Tool calls share one pooled engine per target URL instead of
    opening (and, for a remote host, TLS-handshaking) a new connection
    every time."""
    workspace, _ = seeded_workspace
    await list_tables(db_session, workspace)
    await list_tables(db_session, workspace)

    url = (await get_data_source(db_session, workspace)).connection_url
    engine = get_target_engine(url)
    assert engine is get_target_engine(url)
    assert isinstance(engine.pool, AsyncAdaptedQueuePool)
    assert engine.pool.checkedin() == 1  # both calls used the same connection

