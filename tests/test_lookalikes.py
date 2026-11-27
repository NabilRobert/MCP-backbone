"""A function in another schema with a builtin's name (a "look-alike")
must never be what a query calls. Postgres picks a look-alike over the
builtin whenever its argument types match better, even with pg_catalog
first on the search path, so these tests create real look-alikes on a
real Postgres and check they're refused, never run."""

from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.auth.models import Workspace
from app.db import engine
from app.query import service
from app.query.service import run_query

pytestmark = pytest.mark.asyncio

# What every look-alike below returns, so a test can tell it ran.
LOOKALIKE_RESULT = "LOOKALIKE RAN"
# The "$user" entry of the default search path names a schema called like
# the login, so creating it puts a second schema on the path besides public.
USER_SCHEMA = engine.url.username or "postgres"
DROP_LOOKALIKES = [
    "drop function if exists public.age(text)",
    "drop function if exists public.lower(integer)",
    "drop function if exists public.date_trunc(text, text)",
    "drop function if exists public.lower(varchar)",
    f'drop schema if exists "{USER_SCHEMA}" cascade',
    "drop extension if exists citext",
]


async def _run_ddl(*statements: str) -> None:
    async with engine.begin() as conn:
        for statement in statements:
            await conn.execute(text(statement))


def _lookalike(signature: str, volatility: str = "VOLATILE") -> str:
    return (
        f"create function {signature} returns text language sql {volatility} "
        f"as $$ select '{LOOKALIKE_RESULT}'::text $$"
    )


@pytest_asyncio.fixture
async def clean_functions() -> AsyncGenerator[None]:
    """Look-alikes outlive the per-test table cleanup, so drop them here."""
    yield
    await _run_ddl(*DROP_LOOKALIKES)


@pytest_asyncio.fixture
async def clean_search_path() -> AsyncGenerator[None]:
    """A role setting outlives the test, so put the default back."""
    yield
    await _run_ddl(f'alter role "{USER_SCHEMA}" reset search_path')


async def test_a_lookalike_age_in_public_is_refused_not_called(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str], clean_functions: None
) -> None:
    workspace, _ = seeded_workspace
    await _run_ddl(_lookalike("public.age(text)"))

    result = await run_query(db_session, workspace, "select age('2024-01-01') as a")

    assert result["status"] == "rejected", result["error"]
    assert result["error"] == (
        "function 'age' is also defined in schema 'public' on this database, so the "
        "call might not reach the Postgres builtin: ask the database's owner to "
        "rename or drop that function"
    )


async def test_a_lookalike_in_any_schema_on_the_search_path_is_refused(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str], clean_functions: None
) -> None:
    workspace, _ = seeded_workspace
    await _run_ddl(f'create schema "{USER_SCHEMA}"', _lookalike(f'"{USER_SCHEMA}".age(text)'))

    result = await run_query(db_session, workspace, "select age('2024-01-01') as a")

    assert result["status"] == "rejected", result["error"]
    assert f"schema '{USER_SCHEMA}'" in (result["error"] or "")


async def test_a_lookalike_of_a_name_sqlglot_knows_is_refused(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str], clean_functions: None
) -> None:
    workspace, _ = seeded_workspace
    await _run_ddl(_lookalike("public.lower(integer)"))

    result = await run_query(db_session, workspace, "select lower(5) as l")

    assert result["status"] == "rejected", result["error"]
    assert "function 'lower'" in (result["error"] or "")


async def test_a_lookalike_is_refused_before_explain_could_run_it(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str], clean_functions: None
) -> None:
    """EXPLAIN folds an IMMUTABLE call on constants while planning, so a
    check made only before the real run would come too late."""
    workspace, _ = seeded_workspace
    await _run_ddl(
        "create function public.date_trunc(text, text) returns text language plpgsql "
        f"immutable as $$ begin raise exception '{LOOKALIKE_RESULT}'; end $$"
    )

    result = await run_query(db_session, workspace, "select date_trunc('day', 'x') as d")

    assert result["status"] == "rejected", result["error"]
    assert LOOKALIKE_RESULT not in (result["error"] or "")


async def test_a_lookalike_created_after_the_cost_check_is_still_refused(
    db_session: AsyncSession,
    seeded_workspace: tuple[Workspace, str],
    clean_functions: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """lower(varchar) is an exact match for a varchar argument, so it beats
    the builtin lower(text); before it exists, the query is valid as is."""
    workspace, _ = seeded_workspace
    real_check_cost = service.check_cost

    async def check_cost_then_add_lookalike(conn: AsyncConnection, *args: Any) -> float:
        cost = await real_check_cost(conn, *args)
        await _run_ddl(_lookalike("public.lower(varchar)"))
        return cost

    monkeypatch.setattr(service, "check_cost", check_cost_then_add_lookalike)

    result = await run_query(db_session, workspace, "select lower(cast('ABC' as varchar)) as l")

    assert result["status"] == "rejected", result["error"]
    assert result["rows"] == []


async def test_a_superuser_installed_extension_is_not_a_lookalike(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str], clean_functions: None
) -> None:
    """citext adds replace(citext, ...) and max(citext) to public. Only a
    superuser can add to an extension a superuser owns, so they're trusted."""
    workspace, _ = seeded_workspace
    await _run_ddl("create extension citext")

    result = await run_query(db_session, workspace, "select replace('ABC', 'B', 'x') as r")

    assert result["status"] == "succeeded", result["error"]
    assert result["rows"] == [{"r": "AxC"}]


async def test_queries_on_unqualified_tables_still_run(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    workspace, _ = seeded_workspace

    result = await run_query(
        db_session,
        workspace,
        "select count(*) as n, max(lower(label)) as last, age(now(), now()) as zero "
        "from demo_items",
    )

    assert result["status"] == "succeeded", result["error"]
    assert result["rows"][0]["n"] == 2


async def test_a_search_path_naming_pg_catalog_does_not_make_builtins_lookalikes(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str], clean_search_path: None
) -> None:
    workspace, _ = seeded_workspace
    await _run_ddl(f'alter role "{USER_SCHEMA}" set search_path = pg_catalog, public')

    result = await run_query(db_session, workspace, "select lower(label) as l from demo_items")

    assert result["status"] == "succeeded", result["error"]
