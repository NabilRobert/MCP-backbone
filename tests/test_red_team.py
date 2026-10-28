"""The red-team suite (Slice F). Each test here is one adversarial
attempt against the safety layer, organized by the threats listed in
docs/threat-model.md. This file IS the "published" evidence Phase 1 asked
for -- run `pytest tests/test_red_team.py -v` and every test name states
exactly what was attempted and what should have happened.

Two kinds of tests appear here:
- Attacks run through the real run_query path (the validator + cost
  check doing their job).
- Attacks run with the validator deliberately bypassed, straight
  against a REAL provisioned read-only role (readonly_role_workspace),
  to prove the database itself independently blocks what the app-level
  check is designed to catch -- not just trusting that it would.
"""

import datetime
import secrets

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.auth.models import ApiKey, ConsoleCredential, Workspace
from app.auth.security import generate_api_key, hash_api_key, hash_password
from app.config import settings
from app.query.models import DataSourceConnection
from app.query.service import get_data_source, run_query

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# T1: writes disguised as reads
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "with x as (insert into demo_items (label) values ('pwned') returning id) select * from x",
        "with x as (update demo_items set label = 'pwned' returning id) select * from x",
        "with x as (delete from demo_items returning id) select * from x",
        "select * into stolen_copy from demo_items",
        (
            "with a as (select 1), b as (insert into demo_items (label) values ('pwned') "
            "returning id) select * from a, b"
        ),
    ],
)
async def test_disguised_write_rejected_by_validator(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str], sql: str
) -> None:
    """A write hidden inside a CTE, SELECT INTO, or a multi-level CTE --
    all parse with a top-level type of Select, which is exactly the
    naive check Phase 6 spike 2 found insufficient. The validator must
    catch all of these before touching the database."""
    workspace, _ = seeded_workspace
    result = await run_query(db_session, workspace, sql)
    assert result["status"] == "rejected"

    rows = (await db_session.execute(text("select count(*) as c from demo_items"))).mappings().one()
    assert rows["c"] == 2  # unchanged from the seed fixture -- nothing actually wrote

    if "stolen_copy" in sql:
        # The SELECT INTO case specifically: confirm it didn't even
        # create its target table, not just that demo_items is intact.
        table_exists = (
            (
                await db_session.execute(
                    text(
                        "select count(*) as c from information_schema.tables "
                        "where table_name = 'stolen_copy'"
                    )
                )
            )
            .mappings()
            .one()["c"]
        )
        assert table_exists == 0


async def test_disguised_write_also_blocked_by_db_role_independently(
    db_session: AsyncSession, readonly_role_workspace: tuple[Workspace, str]
) -> None:
    """Defense-in-depth, proven, not assumed: bypass our own validator
    entirely and send a CTE-hidden write straight to the database using
    the real provisioned read-only role's own credentials. Postgres
    checks privileges per-operation regardless of nesting, so this
    should fail on its own, independent of any app code."""
    workspace, _ = readonly_role_workspace
    data_source = await get_data_source(db_session, workspace)

    engine = create_async_engine(data_source.connection_url)
    try:
        async with engine.connect() as conn:
            with pytest.raises(Exception, match="permission denied"):
                await conn.execute(
                    text(
                        "with x as (insert into redteam_items (label) values ('pwned') "
                        "returning id) select * from x"
                    )
                )
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# T2: function-call bypass (SECURITY DEFINER)
# ---------------------------------------------------------------------------


async def test_security_definer_function_rejected_by_validator(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    """A SECURITY DEFINER function that performs a write, called as a
    plain SELECT, is exactly the Phase 6 spike 1 bypass -- it runs with
    the FUNCTION OWNER's privileges, not the caller's, so the database
    role can't save you here. The function allowlist is the real
    defense: reject anything sqlglot doesn't recognize as a named
    builtin."""
    workspace, _ = seeded_workspace
    await db_session.execute(
        text(
            "create or replace function sneaky_write() returns void "
            "language plpgsql security definer as $$ "
            "begin insert into demo_items (label) values ('pwned'); end; $$"
        )
    )
    await db_session.commit()
    try:
        result = await run_query(db_session, workspace, "select sneaky_write()")
        assert result["status"] == "rejected"
        assert "sneaky_write" in (result["error"] or "")
    finally:
        await db_session.execute(text("drop function if exists sneaky_write()"))
        await db_session.commit()


async def test_security_definer_function_succeeds_if_validator_is_bypassed(
    db_session: AsyncSession, readonly_role_workspace: tuple[Workspace, str]
) -> None:
    """The documented residual risk from docs/threat-model.md, demonstrated
    rather than just claimed: if a target database the operator doesn't
    own already has a SECURITY DEFINER function, calling it directly
    (bypassing our validator) DOES succeed in writing data, even
    through the read-only role. This is exactly why the test above --
    the validator's function allowlist -- is the real defense, and the
    database role is not. A passing test here is expected and correct;
    it would be dishonest to claim the role alone is airtight.

    This connects with a plain engine on purpose, to test the ROLE alone.
    The app's own target connections are now also READ ONLY
    transactions, which do refuse this write -- see
    test_target_connections_refuse_writes_even_past_the_validator."""
    workspace, _ = readonly_role_workspace
    data_source = await get_data_source(db_session, workspace)

    await db_session.execute(
        text(
            "create or replace function sneaky_write_redteam() returns void "
            "language plpgsql security definer as $$ "
            "begin insert into redteam_items (label) values ('pwned'); end; $$"
        )
    )
    await db_session.commit()

    engine = create_async_engine(data_source.connection_url)
    try:
        async with engine.connect() as conn:
            # Deliberately calling the function directly -- this is the
            # validator-bypassed path, not going through run_query.
            await conn.execute(text("select sneaky_write_redteam()"))
            result = await conn.execute(
                text("select count(*) as c from redteam_items where label = 'pwned'")
            )
            assert result.mappings().one()["c"] == 1
    finally:
        await engine.dispose()
        await db_session.execute(text("drop function if exists sneaky_write_redteam()"))
        await db_session.commit()


# ---------------------------------------------------------------------------
# T3: resource exhaustion
# ---------------------------------------------------------------------------


async def test_unrecognized_resource_exhaustion_function_rejected(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    """pg_sleep doesn't write anything, but calling it with a large
    argument is a real way to tie up a database connection. It's not a
    named builtin sqlglot recognizes, so the function allowlist catches
    it the same way it catches a SECURITY DEFINER write."""
    workspace, _ = seeded_workspace
    result = await run_query(db_session, workspace, "select pg_sleep(30)")
    assert result["status"] == "rejected"


async def test_expensive_cross_join_rejected_by_cost_check(
    db_session: AsyncSession,
    seeded_workspace: tuple[Workspace, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cross join against a table with itself blows up the row count
    multiplicatively -- a plain, syntactically valid SELECT, so the
    validator alone wouldn't catch it. The EXPLAIN-based cost check is
    what's actually responsible for stopping it before it runs. The
    threshold is lowered here rather than inserting a huge amount of
    test data -- this test is about the mechanism rejecting an
    expensive plan, not about reproducing production-scale cost units
    against a two-row demo table."""
    monkeypatch.setattr(settings, "cost_threshold", 1)
    workspace, _ = seeded_workspace

    result = await run_query(
        db_session, workspace, "select * from demo_items a, demo_items b, demo_items c"
    )
    assert result["status"] == "rejected"
    assert "cost" in (result["error"] or "").lower()


async def test_read_only_role_has_its_own_statement_timeout(
    db_session: AsyncSession, readonly_role_workspace: tuple[Workspace, str]
) -> None:
    """The app sets a timeout on every query it runs, but a connection
    made with the role's password outside the app (psql, a leaked URL)
    gets none of that. The role itself must carry one: checked here on
    the real provisioned role, connecting directly, with no app code in
    between."""
    from scripts.provision_readonly_role import DEFAULT_STATEMENT_TIMEOUT_SECONDS

    workspace, _ = readonly_role_workspace
    data_source = await get_data_source(db_session, workspace)

    engine = create_async_engine(data_source.connection_url)
    try:
        async with engine.connect() as conn:
            timeout = (await conn.execute(text("show statement_timeout"))).scalar_one()
    finally:
        await engine.dispose()

    assert timeout == f"{DEFAULT_STATEMENT_TIMEOUT_SECONDS}s"


async def test_read_only_role_timeout_kills_a_slow_query_outside_the_app(
    db_session: AsyncSession,
) -> None:
    """And that the role's timeout actually stops a statement, rather than
    just being stored: a role provisioned with a 1-second limit runs a
    2-second sleep directly and gets cancelled by Postgres."""
    from sqlalchemy.engine.url import make_url

    from app.db import engine as admin_engine
    from scripts.provision_readonly_role import drop_role, provision

    role_name = f"redteam_{secrets.token_hex(4)}"
    admin_url = make_url(admin_engine.url.render_as_string(hide_password=False)).set(
        drivername="postgresql"
    )
    admin_dsn = admin_url.render_as_string(hide_password=False)
    password = await provision(
        admin_dsn, role_name, admin_url.database or "postgres", "public", 1
    )
    role_url = admin_url.set(username=role_name, password=password, drivername="postgresql+asyncpg")

    engine = create_async_engine(role_url)
    try:
        async with engine.connect() as conn:
            with pytest.raises(Exception, match="statement timeout"):
                await conn.execute(text("select pg_sleep(2)"))
    finally:
        await engine.dispose()
        await drop_role(admin_dsn, role_name)


# ---------------------------------------------------------------------------
# T4: privilege escalation / session tampering
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "set role postgres",
        "set session authorization postgres",
        "reset role",
    ],
)
async def test_session_tampering_rejected(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str], sql: str
) -> None:
    """None of these are SELECT statements, so they're rejected at the
    "only SELECT statements are allowed" check -- the same gate that
    blocks UPDATE/DELETE also blocks attempts to change who the
    connection is acting as."""
    workspace, _ = seeded_workspace
    result = await run_query(db_session, workspace, sql)
    assert result["status"] == "rejected"


# ---------------------------------------------------------------------------
# T5: statement stacking / injection via comments
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "select * from demo_items; drop table demo_items;",
        "select 1; -- ) ; drop table demo_items; --",
    ],
)
async def test_statement_stacking_rejected(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str], sql: str
) -> None:
    """A second, dangerous statement riding along after a harmless-
    looking first one -- the "exactly one statement" check rejects all
    of these regardless of how the extra statement is disguised."""
    workspace, _ = seeded_workspace
    result = await run_query(db_session, workspace, sql)
    assert result["status"] == "rejected"

    rows = (await db_session.execute(text("select count(*) as c from demo_items"))).mappings().one()
    assert rows["c"] == 2  # table still exists and is untouched


# ---------------------------------------------------------------------------
# T6: workspace isolation
# ---------------------------------------------------------------------------


async def test_rate_limit_exhaustion_does_not_affect_other_workspaces(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    """One workspace hammering its rate limit shouldn't be able to
    affect a different workspace's ability to make requests --
    RateLimitCounter is scoped per-workspace, confirmed here rather
    than just assumed from reading the model."""
    workspace, _ = seeded_workspace

    # Exhaust this workspace's default rate limit (5 per window).
    results = [await run_query(db_session, workspace, "select * from demo_items") for _ in range(6)]
    assert results[-1]["status"] == "rejected"

    # A second, unrelated workspace should be unaffected.
    other = Workspace(name="other-workspace", created_at=datetime.datetime.now(datetime.UTC))
    db_session.add(other)
    await db_session.flush()
    db_session.add(
        DataSourceConnection(
            workspace_id=other.id,
            connection_url=(await get_data_source(db_session, workspace)).connection_url,
        )
    )
    db_session.add(
        ConsoleCredential(workspace_id=other.id, username="other", password_hash=hash_password("x"))
    )
    other_key = generate_api_key()
    db_session.add(
        ApiKey(
            workspace_id=other.id,
            key_hash=hash_api_key(other_key),
            status="active",
            created_at=datetime.datetime.now(datetime.UTC),
        )
    )
    await db_session.commit()

    other_result = await run_query(db_session, other, "select * from demo_items")
    assert other_result["status"] == "succeeded"


async def test_provisioning_refuses_a_schema_everyone_can_create_in(
    db_session: AsyncSession,
) -> None:
    """Pagila's dump grants CREATE on its schema to PUBLIC, which made a
    "read-only" role able to create tables (found by the VPS demo
    setup's own write test). A grant to PUBLIC covers every role and
    can't be revoked per role, so provisioning must refuse to hand out
    a role on such a schema rather than report success."""
    from sqlalchemy.engine.url import make_url

    from app.db import engine
    from scripts.provision_readonly_role import drop_role, provision

    admin_dsn = (
        make_url(engine.url.render_as_string(hide_password=False))
        .set(drivername="postgresql")
        .render_as_string(hide_password=False)
    )
    await db_session.execute(text("create schema if not exists leaky"))
    await db_session.execute(text("grant create on schema leaky to public"))
    await db_session.commit()
    try:
        with pytest.raises(RuntimeError, match="can still CREATE"):
            await provision(admin_dsn, "redteam_leaky", make_url(admin_dsn).database or "", "leaky")
    finally:
        await drop_role(admin_dsn, "redteam_leaky")
        await db_session.execute(text("drop schema leaky cascade"))
        await db_session.commit()


async def test_pagila_loader_drops_the_grant_that_opens_its_schema() -> None:
    from scripts.load_pagila_dataset import (
        _patch_schema,  # pyright: ignore[reportPrivateUsage]
    )

    patched = _patch_schema(
        "CREATE TABLE public.actor (id int);\n"
        "REVOKE USAGE ON SCHEMA public FROM PUBLIC;\n"
        "GRANT ALL ON SCHEMA public TO PUBLIC;\n"
    )
    assert "CREATE TABLE public.actor" in patched
    assert "GRANT ALL ON SCHEMA public TO PUBLIC" not in patched


@pytest.mark.parametrize(
    "write",
    [
        "with x as (insert into rt_ro_probe values (2) returning id) select * from x",
        "select * into rt_ro_probe_copy from rt_ro_probe",
        "create temp table rt_ro_tmp (x int)",
        "select rt_ro_probe_write()",  # SECURITY DEFINER: a read-only ROLE can't stop this one
        "select set_config('transaction_read_only', 'off', true), (select 1)",
    ],
)
async def test_target_connections_refuse_writes_even_past_the_validator(
    db_session: AsyncSession, write: str
) -> None:
    """Every connection to a target database runs in a READ ONLY
    transaction (app/db.py's create_target_engine), so Postgres itself
    refuses writes -- tested here by sending each one STRAIGHT to the
    database, bypassing our validator entirely, over a superuser
    connection that has every privilege. Needs no CREATE ROLE, so it
    also protects targets where no read-only role can be made. Includes
    the SECURITY DEFINER write that a read-only role lets through (see
    test_security_definer_function_succeeds_if_validator_is_bypassed)."""
    from app.db import create_target_engine, engine

    await db_session.execute(text("create table if not exists rt_ro_probe (id int)"))
    await db_session.execute(
        text(
            "create or replace function rt_ro_probe_write() returns int language sql "
            "security definer as $$ insert into rt_ro_probe values (1) returning id $$"
        )
    )
    await db_session.commit()

    target = create_target_engine(engine.url.render_as_string(hide_password=False))
    try:
        async with target.connect() as conn:
            assert (await conn.execute(text("show transaction_read_only"))).scalar() == "on"
        with pytest.raises(Exception, match="read-only transaction|must be set before any query"):
            async with target.connect() as conn:
                await conn.execute(text(write))
    finally:
        await target.dispose()

    written = (await db_session.execute(text("select count(*) from rt_ro_probe"))).scalar()
    await db_session.execute(text("drop function rt_ro_probe_write()"))
    await db_session.execute(text("drop table rt_ro_probe"))
    await db_session.commit()
    assert written == 0
