"""Which MCP tools an API key may call. Least privilege for API keys: a
key handed to something that only needs to browse the schema shouldn't
also be able to read every row. Enforced in app/auth/service.py on every
tool call; chosen from a preset when a key is issued in the console."""

LIST_TABLES = "list_tables"
DESCRIBE_TABLE = "describe_table"
SAMPLE_ROWS = "sample_rows"
RUN_QUERY = "run_query"

ALL_TOOLS: tuple[str, ...] = (LIST_TABLES, DESCRIBE_TABLE, SAMPLE_ROWS, RUN_QUERY)

# preset name -> (label shown in the console, tools it allows)
PRESETS: dict[str, tuple[str, frozenset[str]]] = {
    "full": ("Full access", frozenset(ALL_TOOLS)),
    "explore": (
        "Explore only: schema and sample rows, no queries",
        frozenset({LIST_TABLES, DESCRIBE_TABLE, SAMPLE_ROWS}),
    ),
    "schema": (
        "Schema only: table and column names, no data",
        frozenset({LIST_TABLES, DESCRIBE_TABLE}),
    ),
}


def describe_scopes(scopes: list[str]) -> str:
    """The console label for a key's tools: its preset's, or the raw list."""
    for label, tools in PRESETS.values():
        if frozenset(scopes) == tools:
            return label
    return ", ".join(sorted(scopes)) or "No tools"
