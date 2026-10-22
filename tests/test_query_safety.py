import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import Workspace
from app.config import settings
from app.query.models import QueryExecution
from app.query.service import run_query

pytestmark = pytest.mark.asyncio


async def _last_execution(session: AsyncSession) -> QueryExecution:
    result = await session.execute(select(QueryExecution).order_by(QueryExecution.created_at))
    return result.scalars().all()[-1]


async def test_write_disguised_as_cte_is_rejected_before_touching_db(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    workspace, _ = seeded_workspace

    result = await run_query(
        db_session,
        workspace,
        "with x as (insert into demo_items (label) values ('x') returning id) select * from x",
    )

    assert result["status"] == "rejected"
    execution = await _last_execution(db_session)
    assert execution.status == "rejected"
    assert execution.cost_estimate is None  # never reached the DB to get a cost


async def test_disallowed_function_is_rejected(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    workspace, _ = seeded_workspace

    result = await run_query(db_session, workspace, "select pg_sleep(0)")

    assert result["status"] == "rejected"
    assert "pg_sleep" in (result["error"] or "")


async def test_multi_statement_is_rejected(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    workspace, _ = seeded_workspace

    result = await run_query(db_session, workspace, "select 1; select 2;")

    assert result["status"] == "rejected"


async def test_successful_query_records_cost_estimate(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    workspace, _ = seeded_workspace

    result = await run_query(db_session, workspace, "select * from demo_items")

    assert result["status"] == "succeeded"
    execution = await _last_execution(db_session)
    assert execution.cost_estimate is not None


async def test_run_query_caps_result_rows(
    db_session: AsyncSession,
    seeded_workspace: tuple[Workspace, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cost check bounds how expensive a query is, not how big its
    result is: a cheap "select *" on a big table must still be cut off
    before it reaches the AI model, and say so."""
    workspace, _ = seeded_workspace
    await db_session.execute(
        text("insert into demo_items (label) select 'x' from generate_series(1, 300)")
    )
    await db_session.commit()
    monkeypatch.setattr(settings, "max_result_rows", 50)

    big = await run_query(db_session, workspace, "select * from demo_items")
    assert big["status"] == "succeeded"
    assert len(big["rows"]) == 50
    assert big["truncated"] is True
    assert "first 50 rows" in big["note"]

    exact = await run_query(db_session, workspace, "select * from demo_items limit 50")
    assert len(exact["rows"]) == 50
    assert "truncated" not in exact
