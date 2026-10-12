import pytest

from app.query.validation import QueryRejected, validate_and_normalize


def test_allows_plain_select() -> None:
    assert validate_and_normalize("select * from t") == "SELECT * FROM t LIMIT 1000"


def test_preserves_existing_limit() -> None:
    assert validate_and_normalize("select * from t limit 5") == "SELECT * FROM t LIMIT 5"


@pytest.mark.parametrize(
    "sql",
    [
        "select * from t; drop table t;",
        "update t set x = 1",
        "delete from t",
        "with x as (insert into t (x) values (1) returning id) select * from x",
        "with x as (update t set x = 1 returning id) select * from x",
        "select * into new_table from t",
        "select public.some_function()",
        "select pg_sleep(1)",
    ],
)
def test_rejects_unsafe_queries(sql: str) -> None:
    with pytest.raises(QueryRejected):
        validate_and_normalize(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "select count(*) from t",
        "select coalesce(x, 1) from t",
        "select date_trunc('day', now()) from t",
        "select lower(x), upper(x) from t",
    ],
)
def test_allows_common_analytical_functions(sql: str) -> None:
    validate_and_normalize(sql)  # must not raise
