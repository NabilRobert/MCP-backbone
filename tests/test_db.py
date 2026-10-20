from app.db import create_target_engine


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
