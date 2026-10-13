from typing import Any


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
    in a genuinely different column order would be marked wrong."""

    def normalize(rows: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
        return sorted((tuple(row.values()) for row in rows), key=str)

    return normalize(actual) == normalize(expected)
