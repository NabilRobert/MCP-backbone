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
        # A set operation gets the same checks in every branch.
        "select 1 union select pg_sleep(1)",
        "select 1 union (with x as (delete from t returning id) select id from x)",
        "select x from t except select x into new_table from t",
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


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        (
            "select a from x union select b from y",
            "SELECT a FROM x UNION SELECT b FROM y LIMIT 1000",
        ),
        (
            "select a from x union all select b from y",
            "SELECT a FROM x UNION ALL SELECT b FROM y LIMIT 1000",
        ),
        (
            "select a from x intersect select a from y",
            "SELECT a FROM x INTERSECT SELECT a FROM y LIMIT 1000",
        ),
        (
            "select a from x except select a from y",
            "SELECT a FROM x EXCEPT SELECT a FROM y LIMIT 1000",
        ),
    ],
)
def test_a_top_level_set_operation_is_allowed_and_capped(sql: str, expected: str) -> None:
    assert validate_and_normalize(sql) == expected


def test_a_set_operation_keeps_its_own_limit() -> None:
    sql = "select a from x union select b from y limit 5"

    assert validate_and_normalize(sql) == "SELECT a FROM x UNION SELECT b FROM y LIMIT 5"


@pytest.mark.parametrize(
    "sql",
    [
        "select * from t for update",
        "select * from t for no key update",
        "select * from t for share",
        "select * from t for key share",
    ],
)
def test_a_row_lock_is_rejected(sql: str) -> None:
    """A lock is the one "read" that affects other sessions: it makes their
    writes wait. Postgres refuses one in a READ ONLY transaction anyway
    (tests/test_red_team.py), so this only gives a clearer message first."""
    with pytest.raises(QueryRejected, match="row lock"):
        validate_and_normalize(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "set transaction read write",
        "truncate t",
        "commit",
        "grant select on t to public",
        "copy t to stdout",
        "vacuum t",
    ],
)
def test_a_statement_that_isnt_a_query_is_rejected(sql: str) -> None:
    """None of these is a write node, so only the "is it a read?" check
    stops them. The first would undo the READ ONLY transaction."""
    with pytest.raises(QueryRejected, match="only SELECT statements are allowed"):
        validate_and_normalize(sql)


# One call per name in ALLOWED_ANONYMOUS_FUNCTIONS, the way an analyst
# would write it. tests/test_query_safety.py runs the same calls on Postgres.
READ_ONLY_DATE_TIME_CALLS = [
    "select age(returned_at, rented_at) from t",
    "select age(rented_at) from t",
    "select clock_timestamp()",
    "select date_subtract(rented_at, interval '1 day') from t",
    "select isfinite(rented_at) from t",
    "select make_date(2024, 2, 29)",
    "select make_timestamptz(2024, 2, 29, 12, 0, 0)",
    "select statement_timestamp()",
    "select timeofday()",
    "select timezone('UTC', rented_at) from t",
    "select transaction_timestamp()",
]


@pytest.mark.parametrize("sql", READ_ONLY_DATE_TIME_CALLS)
def test_read_only_date_time_builtins_are_allowed(sql: str) -> None:
    """sqlglot doesn't model these, so they parse as unknown functions,
    which the validator used to refuse. age() was the case the v2
    accuracy benchmark hit."""
    validate_and_normalize(sql)  # must not raise


def test_an_allowed_builtin_is_allowed_inside_another_function() -> None:
    sql = "select extract(epoch from age(returned_at, rented_at)) from t"

    assert "AGE(returned_at, rented_at)" in validate_and_normalize(sql)


@pytest.mark.parametrize(
    "sql", ["select public.age(rented_at) from t", "select pg_catalog.age(rented_at) from t"]
)
def test_an_allowed_builtin_called_with_a_schema_is_rejected(sql: str) -> None:
    """The allowlist vouches for the builtin, not for a look-alike someone
    created in another schema."""
    with pytest.raises(QueryRejected, match="schema-qualified"):
        validate_and_normalize(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "select pg_sleep(1)",
        "select set_config('default_transaction_read_only', 'off', false)",
        "select nextval('demo_items_id_seq')",
        "select current_setting('data_directory')",
        "select pg_read_file('/etc/passwd')",
        "select pg_advisory_lock(1)",
        "select pg_terminate_backend(1)",
        "select txid_current()",
        "select lo_import('/etc/passwd')",
        "select dblink('host=elsewhere', 'delete from t')",
    ],
)
def test_builtins_with_side_effects_stay_rejected(sql: str) -> None:
    with pytest.raises(QueryRejected, match="not on the allowlist"):
        validate_and_normalize(sql)
