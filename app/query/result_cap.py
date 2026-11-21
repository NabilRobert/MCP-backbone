"""Reads a query's result and cuts it down to what's safe to hand an AI
model: at most max_result_rows rows and max_result_bytes in all, with
any text value over max_value_bytes shortened.

Why a byte cap as well as a row cap: the cost check bounds how expensive
a query is, not how big its result is. One row can be huge, so a row cap
alone let "select repeat('x', 500000000)" build a 500 MB answer.
validation.py refuses that call; this keeps whatever a query does return
to a size the server and the model can take.

Called by app/query/service.py: _run_with_retry."""

from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.config import settings


@dataclass(frozen=True)
class CappedResult:
    rows: list[dict[str, Any]]
    is_truncated: bool


async def fetch_capped(conn: AsyncConnection, normalized_sql: str) -> CappedResult:
    """Streams the result through a server-side cursor and stops at the
    first row that would cross either cap, so a big result is never held
    in full. One row past the row cap is read only to tell "exactly at
    the cap" from "cut off"."""
    result = await conn.stream(text(normalized_sql))
    rows: list[dict[str, Any]] = []
    total_bytes = 0
    is_truncated = False
    try:
        async for row in result:
            values, row_bytes, was_shortened = _shorten_long_values(
                dict(row._mapping)  # pyright: ignore[reportPrivateUsage]
            )
            if len(rows) == settings.max_result_rows or (
                total_bytes + row_bytes > settings.max_result_bytes
            ):
                is_truncated = True
                break
            rows.append(values)
            total_bytes += row_bytes
            is_truncated = is_truncated or was_shortened
    finally:
        await result.close()
    return CappedResult(rows=rows, is_truncated=is_truncated)


def _shorten_long_values(row: dict[str, Any]) -> tuple[dict[str, Any], int, bool]:
    """Returns the row with each text value cut to max_value_bytes, the
    row's size in bytes as text, and whether anything was cut."""
    shortened: dict[str, Any] = {}
    row_bytes = 0
    was_shortened = False
    for column, value in row.items():
        if isinstance(value, str) and len(value.encode()) > settings.max_value_bytes:
            # Cut on a character boundary, so a multi-byte letter isn't split.
            value = value.encode()[: settings.max_value_bytes].decode(errors="ignore")
            was_shortened = True
        shortened[column] = value
        row_bytes += _size_in_bytes(value)
    return shortened, row_bytes, was_shortened


def _size_in_bytes(value: Any) -> int:
    """About what the value takes in the JSON answer. Only text is cut
    (_shorten_long_values); anything else still counts toward the total."""
    if isinstance(value, str):
        return len(value.encode())
    if isinstance(value, bytes | bytearray):
        return len(value)
    return len(str(value))
