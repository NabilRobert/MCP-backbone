"""Decides whether the AI agent's answer to a benchmark question was
actually correct -- by comparing what its query returned against the
known-correct answer, not by comparing the SQL text itself."""

from decimal import Decimal, InvalidOperation
from typing import Any

# How close two numbers have to be to count as "the same answer" --
# chosen to absorb an agent rounding a price-style value to 2 decimal
# places (e.g. 290.65 vs the full-precision 290.6512500000000000) for
# readability, which is a reasonable thing for it to do, not a wrong
# answer. A genuinely different answer will be off by far more than
# half a cent.
_NUMERIC_TOLERANCE = Decimal("0.005")


def _normalize_value(value: Any) -> Any:
    """Expected rows come straight from the database, so NUMERIC is a
    Decimal; the agent's rows come through MCP's JSON, where it's the
    string "387684.66". Without normalising both, every numeric answer
    fails regardless of correctness. Tradeoff: a numeric-looking string
    (a zip code with a leading zero) would lose that distinction."""
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        return Decimal(str(value))
    if isinstance(value, str):
        try:
            return Decimal(value)
        except InvalidOperation:
            return value
    return value


def _values_match(expected_value: Any, actual_value: Any) -> bool:
    """Two Decimals match within _NUMERIC_TOLERANCE; everything else
    (strings, after _normalize_value already ruled out a numeric
    parse) still needs to match exactly."""
    if isinstance(expected_value, Decimal) and isinstance(actual_value, Decimal):
        return abs(expected_value - actual_value) < _NUMERIC_TOLERANCE
    return expected_value == actual_value


def _is_subsequence_match(expected_row: tuple[Any, ...], actual_row: tuple[Any, ...]) -> bool:
    """True if every value _normalize_value produced for the expected
    row also shows up in the actual row, in the same relative order --
    found by running a real benchmark: an agent answering "which
    country has the most customers" with (country, customer_count)
    when asked for just the country is giving a MORE informative
    answer than the one-column ground truth, not a wrong one. Still
    order-sensitive on purpose, so this doesn't quietly undo
    test_column_order_matters_documented_limitation: (5, "a") is not a
    subsequence of ("a", 5), since "a" would have to come after 5
    there but doesn't -- an actual column swap stays caught, only
    actual EXTRA columns (anywhere, interleaved or trailing) get
    tolerated."""
    actual_iter = iter(actual_row)
    for expected_value in expected_row:
        if not any(_values_match(expected_value, actual_value) for actual_value in actual_iter):
            return False
    return True


def rows_equal(actual: list[dict[str, Any]], expected: list[dict[str, Any]]) -> bool:
    """Execution-accuracy comparison: row *values*, not SQL text and not
    column names -- many correct SQL formulations exist for the same
    question, including a different (equally valid) column alias, e.g.
    `AS revenue` vs `AS total`. Comparing by key would fail a correct
    answer over a naming choice, so rows are compared as value tuples
    instead. Both row order (GROUP BY and aggregate results don't
    guarantee one unless the query specifies it) and an actual row
    having extra columns beyond what was asked (see
    _is_subsequence_match) are tolerated; an actual column swap within
    a row is not (see test_column_order_matters_documented_limitation).

    Matching rows between the two sides is a greedy one-to-one
    assignment, not a full bipartite-matching algorithm -- knowingly
    simpler than strictly correct in theory, fine in practice at this
    benchmark's scale (a handful of rows, no two of which plausibly
    produce an adversarial matching ambiguity)."""
    actual_rows = [tuple(_normalize_value(v) for v in row.values()) for row in actual]
    expected_rows = [tuple(_normalize_value(v) for v in row.values()) for row in expected]

    if len(actual_rows) != len(expected_rows):
        return False

    unmatched_actual = list(actual_rows)
    for expected_row in expected_rows:
        match_index = next(
            (i for i, a in enumerate(unmatched_actual) if _is_subsequence_match(expected_row, a)),
            None,
        )
        if match_index is None:
            return False
        unmatched_actual.pop(match_index)
    return True


def rows_equal_strict(actual: list[dict[str, Any]], expected: list[dict[str, Any]]) -> bool:
    """The no-tolerance score, reported next to rows_equal so the lenient
    one never has to be taken on trust: the same number of columns, in
    the same order, with exactly equal values (no rounding slack), and
    the same rows in any order. The only normalisation is the
    Decimal-vs-JSON-string one in _normalize_value, which fixes how a
    value is *represented*, not whether it's right."""
    actual_rows = sorted(repr(tuple(_normalize_value(v) for v in row.values())) for row in actual)
    expected_rows = sorted(
        repr(tuple(_normalize_value(v) for v in row.values())) for row in expected
    )
    return actual_rows == expected_rows
