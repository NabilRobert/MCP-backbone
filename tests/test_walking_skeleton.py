import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import Workspace
from app.auth.service import AuthenticationError, authenticate
from app.query.models import QueryExecution
from app.query.service import list_tables, run_query

pytestmark = pytest.mark.asyncio


async def test_authenticate_rejects_unknown_key(db_session: AsyncSession) -> None:
    with pytest.raises(AuthenticationError):
        await authenticate(db_session, "jarvis_not_a_real_key")


async def test_list_tables_sees_demo_table(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    workspace, api_key = seeded_workspace
    authenticated = await authenticate(db_session, api_key)
    assert authenticated.id == workspace.id

    tables = await list_tables(db_session, workspace)
    assert "demo_items" in tables


async def test_run_query_success_writes_audit_row(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    workspace, _ = seeded_workspace

    result = await run_query(db_session, workspace, "select * from demo_items order by id")

    assert result["status"] == "succeeded"
    assert result["rows"] == [{"id": 1, "label": "a"}, {"id": 2, "label": "b"}]

    executions = (await db_session.execute(select(QueryExecution))).scalars().all()
    assert len(executions) == 1
    assert executions[0].status == "succeeded"


async def test_run_query_failure_still_writes_audit_row(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    workspace, _ = seeded_workspace

    result = await run_query(db_session, workspace, "select * from does_not_exist")

    assert result["status"] == "errored"
    assert result["error"] is not None

    executions = (await db_session.execute(select(QueryExecution))).scalars().all()
    assert len(executions) == 1
    assert executions[0].status == "errored"
    assert executions[0].rejection_reason is not None
