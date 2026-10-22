import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import Workspace
from app.query.schema_cache import describe_table, sample_rows
from app.query.service import QueryExecutionError, list_tables

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


async def test_partitions_are_hidden_behind_their_parent(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    """A partitioned table is listed once: its partitions are left out,
    since querying the parent already includes them. Listing both led a
    benchmark agent to sum the parent and every partition, double
    counting."""
    workspace, _ = seeded_workspace
    await db_session.execute(
        text("create table rt_sales (id int, sold date) partition by range (sold)")
    )
    await db_session.execute(
        text(
            "create table rt_sales_2025 partition of rt_sales for values from ('2025-01-01') to ('2026-01-01')"
        )
    )
    await db_session.execute(
        text(
            "create table rt_sales_2026 partition of rt_sales for values from ('2026-01-01') to ('2027-01-01')"
        )
    )
    await db_session.commit()
    try:
        tables = await list_tables(db_session, workspace)
        assert "rt_sales" in tables
        assert "rt_sales_2025" not in tables and "rt_sales_2026" not in tables
        assert "demo_items" in tables  # ordinary tables unaffected

        described = await describe_table(db_session, workspace, "rt_sales")
        assert [c["name"] for c in described["columns"]] == ["id", "sold"]
        with pytest.raises(QueryExecutionError):
            await describe_table(db_session, workspace, "rt_sales_2025")
    finally:
        await db_session.execute(text("drop table rt_sales cascade"))
        await db_session.commit()
