"""A query's result is cut to a size the server and the AI model can
take: by rows, by bytes in all, and by bytes per text value. Each cut
says so (truncated, with a note). Runs real queries on the test Postgres."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import Workspace
from app.config import settings
from app.query.service import run_query

pytestmark = pytest.mark.asyncio


async def test_a_result_over_the_byte_cap_is_cut_off_and_says_so(
    db_session: AsyncSession,
    seeded_workspace: tuple[Workspace, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, _ = seeded_workspace
    monkeypatch.setattr(settings, "max_result_bytes", 5_000)

    # 100 rows of 1,000 bytes: under the row cap, 20 times over the byte cap.
    result = await run_query(
        db_session, workspace, "select repeat('x', 1000) as v from generate_series(1, 100)"
    )

    assert result["status"] == "succeeded", result["error"]
    assert len(result["rows"]) == 5
    assert result["truncated"] is True
    assert "5,000 bytes" in result["note"]


async def test_a_text_value_over_the_value_cap_is_shortened_and_says_so(
    db_session: AsyncSession,
    seeded_workspace: tuple[Workspace, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, _ = seeded_workspace
    monkeypatch.setattr(settings, "max_value_bytes", 101)

    result = await run_query(db_session, workspace, "select repeat('é', 1000) as v")

    assert result["status"] == "succeeded", result["error"]
    # 101 bytes hold 50 two-byte letters and half of one: the half is dropped.
    assert result["rows"] == [{"v": "é" * 50}]
    assert result["truncated"] is True


async def test_a_result_within_every_cap_is_whole(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    workspace, _ = seeded_workspace

    result = await run_query(db_session, workspace, "select repeat('x', 1000) as v")

    assert result["rows"] == [{"v": "x" * 1000}]
    assert "truncated" not in result
