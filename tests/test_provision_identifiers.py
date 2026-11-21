"""The read-only provisioning script puts role, database and schema names
into DDL, which can't take bind parameters. These check that a name is
either a plain Postgres identifier, quoted, or refused before any
connection is made. No database needed."""

import pytest

from scripts.provision_readonly_role import (
    MAX_IDENTIFIER_LENGTH,
    drop_role,
    provision,
    quote_identifier,
)

# Never reached: every case below must fail before connecting.
UNREACHABLE_DSN = "postgresql://nobody@127.0.0.1:1/none"


def test_a_plain_name_is_quoted() -> None:
    assert quote_identifier("pagila_ro", "role name") == '"pagila_ro"'


def test_a_reserved_word_is_quoted_so_it_works_as_a_name() -> None:
    assert quote_identifier("select", "schema") == '"select"'


@pytest.mark.parametrize(
    "name",
    ['ro"; drop table rental; --', "has space", "1starts_with_digit", "", "naïve"],
)
def test_a_name_that_is_not_a_plain_identifier_is_refused(name: str) -> None:
    with pytest.raises(ValueError, match="isn't a plain Postgres name"):
        quote_identifier(name, "role name")


def test_a_name_longer_than_postgres_allows_is_refused() -> None:
    with pytest.raises(ValueError, match="isn't a plain Postgres name"):
        quote_identifier("r" * (MAX_IDENTIFIER_LENGTH + 1), "role name")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role_name", "database", "schema"),
    [
        ('ro"x', "pagila", "public"),
        ("pagila_ro", 'pagila"x', "public"),
        ("pagila_ro", "pagila", 'public"x'),
    ],
)
async def test_provision_refuses_a_quote_in_any_name_before_connecting(
    role_name: str, database: str, schema: str
) -> None:
    with pytest.raises(ValueError, match="isn't a plain Postgres name"):
        await provision(UNREACHABLE_DSN, role_name, database, schema)


@pytest.mark.asyncio
async def test_drop_role_refuses_a_quote_in_the_name_before_connecting() -> None:
    with pytest.raises(ValueError, match="isn't a plain Postgres name"):
        await drop_role(UNREACHABLE_DSN, 'ro"x')
