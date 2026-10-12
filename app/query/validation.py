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

DEFAULT_LIMIT = 1000


class QueryRejected(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def validate_and_normalize(sql: str) -> str:
    """Raises QueryRejected without ever touching the database. Returns the
    normalized SQL (LIMIT injected if missing) to actually execute."""
    try:
        statements = [s for s in sqlglot.parse(sql, read="postgres") if s is not None]
    except Exception as exc:
        raise QueryRejected(f"could not parse SQL: {exc}") from exc

    if len(statements) != 1:
        raise QueryRejected("exactly one SQL statement is required")

    statement = statements[0]
    if not isinstance(statement, exp.Select):
        raise QueryRejected("only SELECT statements are allowed")

    write_nodes = list(statement.find_all(*WRITE_NODE_TYPES))
    if write_nodes:
        kinds = sorted({type(node).__name__ for node in write_nodes})
        raise QueryRejected(f"query contains a write operation: {', '.join(kinds)}")

    for func in statement.find_all(exp.Anonymous):
        name = (func.name or "").lower()
        if name not in ALLOWED_ANONYMOUS_FUNCTIONS:
            raise QueryRejected(f"function '{func.name}' is not on the allowlist")

    if not statement.args.get("limit"):
        statement = statement.limit(DEFAULT_LIMIT)

    return statement.sql(dialect="postgres")
