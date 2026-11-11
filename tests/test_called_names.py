"""Which names app/query/lookalikes.py checks for look-alikes: every
word followed by "(" in the final SQL, spelled the way Postgres will look
it up. No database needed."""

from app.query.lookalikes import called_names


def test_called_names_folds_bare_names_and_keeps_quoted_ones() -> None:
    assert called_names('SELECT LOWER(x), "AGE"(y) FROM t') == {"lower", "AGE"}


def test_called_names_ignores_text_inside_strings() -> None:
    assert called_names("SELECT 'lower(' AS s FROM t") == set()


def test_called_names_includes_functions_sqlglot_treats_as_keywords() -> None:
    assert "left" in called_names("SELECT LEFT(name, 2) FROM t")
