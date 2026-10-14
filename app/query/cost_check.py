"""A second safety check, separate from app/query/validation.py. That
file asks "is this query dangerous?" -- this file asks "even if it's
safe, is it going to be slow/expensive to actually run?" and rejects it
before wasting real database time on it."""

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.query.validation import QueryRejected


async def check_cost(
    conn: AsyncConnection, normalized_sql: str, threshold: float, timeout_seconds: int
) -> float:
    """Asks Postgres to estimate how expensive a query would be, WITHOUT
    actually running it -- that's what "EXPLAIN" does: it plans the
    query and reports back a cost number, but never touches real rows.
    If that estimate is higher than the allowed threshold, the query is
    rejected here, before it ever executes for real.

    normalized_sql must already have passed app/query/validation.py --
    EXPLAIN needs the literal SQL text pasted in, which is only safe to
    do once we're sure the SQL itself has already been checked."""
    # A short timeout on the estimate itself -- even figuring out "how
    # expensive would this be" should always be quick. If it isn't,
    # something is unusual enough to bail out rather than wait.
    await conn.execute(text(f"SET statement_timeout = {timeout_seconds * 1000}"))

    result = await conn.execute(text(f"EXPLAIN (FORMAT JSON) {normalized_sql}"))
    plan: list[dict[str, Any]] = result.scalar_one()
    cost: float = plan[0]["Plan"]["Total Cost"]

    if cost > threshold:
        raise QueryRejected(f"estimated cost {cost} exceeds threshold {threshold}")
    return cost
