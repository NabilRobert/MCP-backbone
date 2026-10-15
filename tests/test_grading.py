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


def test_extra_informative_column_still_matches() -> None:
    """Regression test for a real benchmark run: an agent asked "which
    country has the most customers" answered with (country,
    customer_count) -- more informative than the one-column ground
    truth, not wrong. The extra column should be tolerated."""
    actual = [{"country": "Japan", "customer_count": 15}]
    expected = [{"country": "Japan"}]
    assert rows_equal(actual, expected)


def test_extra_column_with_wrong_answer_still_fails() -> None:
    """The tolerance above is for extra columns, not a license to
    ignore a wrong answer just because other columns happen to be
    present."""
    actual = [{"country": "Indonesia", "customer_count": 15}]
    expected = [{"country": "Japan"}]
    assert not rows_equal(actual, expected)


def test_small_rounding_difference_still_matches() -> None:
    """Regression test for a real benchmark run: an agent answered
    "average price" with 290.65 (sensibly rounded); expected_sql
    computes the full-precision 290.6512500000000000. Same answer to
    a human, now the same answer here too."""
    actual = [{"average_price": "290.65"}]
    expected = [{"avg": Decimal("290.6512500000000000")}]
    assert rows_equal(actual, expected)


def test_large_numeric_difference_still_fails() -> None:
    """The rounding tolerance is for sub-cent differences, not a
    license to ignore a genuinely wrong number."""
    actual = [{"average_price": "250.00"}]
    expected = [{"avg": Decimal("290.6512500000000000")}]
    assert not rows_equal(actual, expected)


def test_extra_column_and_rounding_together_still_matches() -> None:
    """The two tolerances compose: an agent can round AND add an extra
    informative column in the same answer without being marked wrong."""
    actual = [{"customer_id": 42, "total_spent": "1234.56"}]
    expected = [{"total_spent": Decimal("1234.5600000000000000")}]
    assert rows_equal(actual, expected)
