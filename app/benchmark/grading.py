"""Decides whether the AI agent's answer to a benchmark question was
actually correct -- by comparing what its query returned against the
known-correct answer, not by comparing the SQL text itself."""

from decimal import Decimal, InvalidOperation
from typing import Any


def _normalize_value(value: Any) -> Any:
    """The "expected" side comes from a direct, in-process DB connection
    (app/query/service.py's own row-to-dict conversion), so a NUMERIC
    column arrives as a real Decimal. The "actual" side comes back from
    a real AI agent through the actual MCP tool call -- the same JSON
    round-trip a real client goes through -- and Decimal isn't a JSON
    type, so it arrives as the string "387684.66" instead. Comparing
    those directly (confirmed by actually running the benchmark: a
    numerically-correct revenue answer was marked wrong for exactly this
    reason) fails Decimal("387684.66") == "387684.66" every time, for
    every numeric answer, regardless of whether the agent got it right.
    Parsing anything that looks like a number into a Decimal on both
    sides fixes that. A string that happens to look numeric but isn't
    (a zip code, a SKU) would lose a meaningful distinction like a
    leading zero here -- not a concern for this project's current eval
    questions, but a real tradeoff if that ever changes."""
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        return Decimal(str(value))
    if isinstance(value, str):
        try:
            return Decimal(value)
        except InvalidOperation:
            return value
    return value


def rows_equal(actual: list[dict[str, Any]], expected: list[dict[str, Any]]) -> bool:
    """Execution-accuracy comparison: row *values*, not SQL text and not
    column names -- many correct SQL formulations exist for the same
    question, including a different (equally valid) column alias, e.g.
    `AS revenue` vs `AS total`. Comparing by key would fail a correct
    answer over a naming choice, so rows are compared as value tuples in
    column order instead. Row order itself is insensitive -- GROUP BY and
    aggregate results don't guarantee an order unless the query specifies
    one, and ties inside ORDER BY ... LIMIT aren't uniquely defined either.
    Documented here, not silently assumed, since it's a real methodology
    choice with a real failure mode: an actual query with the right values
    in a genuinely different column order would be marked wrong.

    A second real failure mode, found the same way (running a real
    benchmark, not reasoning about it in the abstract): no rounding
    tolerance exists. An agent answering "what's the average price"
    with a sensibly-rounded 290.65 is marked wrong against an
    `expected_sql` that computes the full-precision 290.6512500000000000
    -- a human would call that answer correct, this function won't.
    Not fixed here, since "how much rounding tolerance" is a real
    judgment call (how many decimal places? relative or absolute?) that
    should be made deliberately, not guessed into a bug fix."""

    def normalize(rows: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
        return sorted((tuple(_normalize_value(v) for v in row.values()) for row in rows), key=str)

    return normalize(actual) == normalize(expected)
