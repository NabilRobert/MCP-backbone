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
