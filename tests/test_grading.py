from decimal import Decimal

from app.benchmark.grading import rows_equal


def test_different_alias_same_value_is_equal() -> None:
    actual = [{"total": Decimal("10.50")}]
    expected = [{"revenue": Decimal("10.50")}]
    assert rows_equal(actual, expected)


def test_different_row_order_same_set_is_equal() -> None:
    actual = [{"status": "a", "c": 1}, {"status": "b", "c": 2}]
    expected = [{"status": "b", "c": 2}, {"status": "a", "c": 1}]
    assert rows_equal(actual, expected)


def test_genuinely_different_values_is_not_equal() -> None:
    actual = [{"c": 5}]
    expected = [{"c": 3}]
    assert not rows_equal(actual, expected)


def test_different_row_count_is_not_equal() -> None:
    actual = [{"c": 1}]
    expected = [{"c": 1}, {"c": 2}]
    assert not rows_equal(actual, expected)


def test_column_order_matters_documented_limitation() -> None:
    actual = [{"name": "a", "price": 5}]
    expected = [{"price": 5, "name": "a"}]
    assert not rows_equal(actual, expected)


def test_decimal_serialized_as_string_still_matches() -> None:
    """Regression test for a real bug: run_benchmark.py's "actual" rows
    come back through a real MCP tool call, which JSON-serializes a
    Decimal to a string -- while "expected" rows come from a direct DB
    connection, keeping the real Decimal. A numerically-correct answer
    was marked wrong for exactly this reason on an actual benchmark run
    before _normalize_value existed."""
    actual = [{"total_revenue": "387684.66"}]
    expected = [{"total": Decimal("387684.66")}]
    assert rows_equal(actual, expected)


def test_genuinely_different_decimal_values_still_not_equal() -> None:
    actual = [{"total_revenue": "387684.66"}]
    expected = [{"total": Decimal("1.00")}]
    assert not rows_equal(actual, expected)
