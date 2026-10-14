"""The safety brain of this whole project. Before any SQL an AI client
sends is allowed anywhere near the real database, this file reads it and
decides: is this actually a safe, read-only question, or something that
could change/damage data? If it's not obviously safe, it's rejected here
-- the database connection is never even opened."""

import sqlglot
from sqlglot import exp

# Anything that writes, in any form -- including hidden inside a CTE or a
# SELECT INTO. See Phase 6 spike 2: checking only the top-level statement
# type misses both of those, since they parse with a top-level type of
# Select. Walking the whole tree catches them regardless of nesting depth.
WRITE_NODE_TYPES = (exp.Insert, exp.Update, exp.Delete, exp.Into, exp.Create, exp.Drop, exp.Alter)

# sqlglot gives every function it recognizes (COUNT, COALESCE, DATE_TRUNC,
# ...) its own named node type; anything it doesn't recognize -- a
# user-defined function, or a builtin like pg_sleep -- falls back to
# exp.Anonymous. See Phase 6 spike 1: that's exactly how a SECURITY
# DEFINER write slips past a "single SELECT" check. Default to rejecting
# all of them; add a name here only for a specific, reviewed, known-safe
# case.
ALLOWED_ANONYMOUS_FUNCTIONS: frozenset[str] = frozenset()

# If a query doesn't already say how many rows to return, we cap it at
# this many -- stops one accidental "give me everything" query from
# pulling back millions of rows.
DEFAULT_LIMIT = 1000


class QueryRejected(Exception):
    """Raised whenever a query fails one of the safety checks below.
    reason is a short, human-readable explanation of exactly which check
    failed -- shown back to whoever (or whatever AI) sent the query."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def validate_and_normalize(sql: str) -> str:
    """Runs a piece of SQL text through every safety check, in order.
    If all checks pass, returns a cleaned-up version of the SQL
    (guaranteed to have a row limit) that's actually safe to run. If any
    check fails, raises QueryRejected and never returns -- the caller
    never gets a "maybe safe" answer, only "safe" or "rejected"."""

    # Check 1: can this even be understood as SQL at all? sqlglot is a
    # library that reads SQL text and turns it into a structured,
    # inspectable representation (like a grammar tree for a sentence).
    # If it can't do that, we definitely can't trust it.
    try:
        statements = [s for s in sqlglot.parse(sql, read="postgres") if s is not None]
    except Exception as exc:
        raise QueryRejected(f"could not parse SQL: {exc}") from exc

    # Check 2: is it exactly ONE query? "select 1; drop table x;" would
    # parse as two separate statements -- reject anything that isn't a
    # single one, so there's no way to sneak a second, dangerous
    # statement in after a harmless-looking first one.
    if len(statements) != 1:
        raise QueryRejected("exactly one SQL statement is required")

    statement = statements[0]

    # Check 3: is the statement a plain SELECT (a read)? Anything else
    # (UPDATE, DELETE, DROP, ...) is rejected immediately.
    if not isinstance(statement, exp.Select):
        raise QueryRejected("only SELECT statements are allowed")

    # Check 4: does this SELECT have a write hidden somewhere INSIDE it?
    # This sounds unlikely, but it's a real trick: SQL lets you nest a
    # separate "mini-query" (a CTE, written as "WITH ... AS (...)")
    # inside a SELECT, and that nested part can itself be a write. A
    # query can look exactly like an innocent SELECT at a glance while
    # secretly containing "WITH x AS (DELETE FROM important_table ...)
    # SELECT * FROM x". Walking the entire structure (not just the
    # outermost layer) catches this.
    write_nodes = list(statement.find_all(*WRITE_NODE_TYPES))
    if write_nodes:
        kinds = sorted({type(node).__name__ for node in write_nodes})
        raise QueryRejected(f"query contains a write operation: {', '.join(kinds)}")

    # Check 5: does the query call any function we haven't explicitly
    # allowed? Ordinary, well-known functions (COUNT, LOWER, SUM, ...)
    # are always fine. The danger is a function that LOOKS like a normal
    # read but secretly performs a write when called -- something the
    # database administrator (not us) set up on the target database.
    # Since we can't inspect what every possible function actually
    # does, we default to refusing anything we don't recognize by name.
    for func in statement.find_all(exp.Anonymous):
        name = (func.name or "").lower()
        if name not in ALLOWED_ANONYMOUS_FUNCTIONS:
            raise QueryRejected(f"function '{func.name}' is not on the allowlist")

    # All checks passed. One last adjustment: if the query doesn't
    # already limit how many rows it returns, add that limit ourselves.
    if not statement.args.get("limit"):
        statement = statement.limit(DEFAULT_LIMIT)

    return statement.sql(dialect="postgres")
