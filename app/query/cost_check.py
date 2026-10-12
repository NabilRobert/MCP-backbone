from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.query.validation import QueryRejected


async def check_cost(
    conn: AsyncConnection, normalized_sql: str, threshold: float, timeout_seconds: int
) -> float:
    """Raises QueryRejected if the estimated cost exceeds the threshold.
    normalized_sql must already be validated -- EXPLAIN takes a literal
    statement, not something that can go through a bind parameter."""
    await conn.execute(text(f"SET statement_timeout = {timeout_seconds * 1000}"))
    result = await conn.execute(text(f"EXPLAIN (FORMAT JSON) {normalized_sql}"))
    plan: list[dict[str, Any]] = result.scalar_one()
    cost: float = plan[0]["Plan"]["Total Cost"]
    if cost > threshold:
        raise QueryRejected(f"estimated cost {cost} exceeds threshold {threshold}")
    return cost
