import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import Workspace
from app.query.schema_cache import describe_table, sample_rows
from app.query.service import QueryExecutionError

pytestmark = pytest.mark.asyncio


async def test_describe_table_returns_columns(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    workspace, _ = seeded_workspace

    result = await describe_table(db_session, workspace, "demo_items")

    assert result["table"] == "demo_items"
    names = {col["name"] for col in result["columns"]}
    assert {"id", "label"} <= names


async def test_describe_table_unknown_table_raises(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    workspace, _ = seeded_workspace

    with pytest.raises(QueryExecutionError):
        await describe_table(db_session, workspace, "does_not_exist")


async def test_sample_rows_respects_limit(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    workspace, _ = seeded_workspace

    rows = await sample_rows(db_session, workspace, "demo_items", limit=1)

    assert len(rows) == 1


async def test_sample_rows_unknown_table_raises(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    workspace, _ = seeded_workspace

    with pytest.raises(QueryExecutionError):
        await sample_rows(db_session, workspace, "does_not_exist")
