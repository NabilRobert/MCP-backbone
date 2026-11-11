"""Refuses a query that calls a builtin whose name is also defined in
another schema on the target's search path: a "look-alike".

Why a catalog check and not search_path: Postgres resolves a function
call by the best argument-type match over every schema on the path.
Putting pg_catalog first only breaks ties, so a look-alike `age(text)`
still wins over the builtin for `age('2024-01-01')` (confirmed on
Postgres 16). A path of pg_catalog alone would close it, but then every
unqualified table name would stop resolving. So the validator
(validation.py) vouches for names, and this module checks, on the target
itself, that each name it vouched for can only mean the builtin.

Called by app/query/service.py inside each target transaction, before
EXPLAIN (which can run an IMMUTABLE function while planning) and again
before the query runs."""

from itertools import pairwise

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY, TEXT
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlglot.dialects.postgres import Postgres
from sqlglot.tokenizer_core import Token, TokenType

from app.query.validation import QueryRejected

# Functions named like a call in the query, in a schema the query's
# unqualified names can resolve to, other than pg_catalog. Postgres never
# looks up functions in pg_temp, so current_schemas(false) is the whole
# risk. Exempt: members of an extension owned by a superuser (citext's
# lower(citext), say). Only that extension's owner can add a member, so
# only a superuser could slip a look-alike in that way, and a superuser
# could do anything anyway. An extension a database owner installed
# themself is not exempt: they could add their own look-alike to it.
# Every name here is schema-qualified so the check can't be shadowed too.
FIND_LOOKALIKE = text(
    """
    SELECT n.nspname, p.proname
    FROM pg_catalog.pg_proc p
    JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname OPERATOR(pg_catalog.=) ANY (pg_catalog.current_schemas(false))
      AND n.nspname OPERATOR(pg_catalog.<>) 'pg_catalog'
      AND p.proname::text OPERATOR(pg_catalog.=) ANY (:names)
      AND NOT EXISTS (
        SELECT FROM pg_catalog.pg_depend d
        JOIN pg_catalog.pg_extension e ON e.oid = d.refobjid
        JOIN pg_catalog.pg_roles r ON r.oid = e.extowner
        WHERE d.classid OPERATOR(pg_catalog.=) 'pg_catalog.pg_proc'::pg_catalog.regclass
          AND d.objid OPERATOR(pg_catalog.=) p.oid
          AND d.deptype OPERATOR(pg_catalog.=) 'e'
          AND r.rolsuper
      )
    ORDER BY 1, 2
    LIMIT 1
    """
).bindparams(bindparam("names", type_=ARRAY(TEXT)))


class LookalikeFunction(QueryRejected):
    """A query refused because a name it calls has a look-alike."""


async def refuse_lookalikes(conn: AsyncConnection, normalized_sql: str) -> None:
    """Raises LookalikeFunction if any function the query calls has a
    look-alike on this target's search path."""
    names = called_names(normalized_sql)
    if not names:
        return
    result = await conn.execute(FIND_LOOKALIKE, {"names": sorted(names)})
    found = result.first()
    if found is None:
        return
    schema, name = found
    raise LookalikeFunction(
        f"function '{name}' is also defined in schema '{schema}' on this database, "
        "so the call might not reach the Postgres builtin: ask the database's "
        "owner to rename or drop that function"
    )


def called_names(sql: str) -> frozenset[str]:
    """Every word directly followed by "(" in the SQL Postgres will run,
    spelled as Postgres will look it up: each function call, plus harmless
    extras like AS or IN. Read from the tokens of the final SQL, not
    sqlglot's tree, because sqlglot can write one node as several calls
    (a date difference, say)."""
    tokens = Postgres().tokenize(sql)
    names: set[str] = set()
    for token, following in pairwise(tokens):
        if following.token_type is TokenType.L_PAREN and _is_name(token):
            names.add(_as_postgres_reads_it(token))
    return frozenset(names)


def _as_postgres_reads_it(token: Token) -> str:
    """Postgres folds a bare name to lower case and keeps a quoted one."""
    if token.token_type is TokenType.IDENTIFIER:
        return token.text
    return token.text.lower()


def _is_name(token: Token) -> bool:
    """A quoted or bare identifier, or a keyword spelled like one (LEFT
    and REPLACE are keywords to sqlglot and functions to Postgres)."""
    if token.token_type is TokenType.IDENTIFIER:
        return True
    if token.token_type.name.endswith("STRING"):
        return False
    return token.text.replace("_", "a").isalnum() and not token.text[0].isdigit()
