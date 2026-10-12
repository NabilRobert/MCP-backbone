import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import Workspace
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
