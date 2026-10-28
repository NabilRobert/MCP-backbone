"""Loads Pagila -- a real, well-known public Postgres sample database
(a DVD rental store: actors, films, customers, rentals, payments,
stores, 16+ tables) -- into the target database, as a richer
alternative to load_demo_dataset.py's hand-generated synthetic data.

Source: https://github.com/devrimgunduz/pagila (the standard Postgres
port of MySQL's "Sakila" sample database). Used here only as public
sample data -- it was never real customer data, even in the original
project, and this script makes no other claim about it.

Needs `psql` on PATH (or pass --psql with a full path). The schema
file defines several PL/pgSQL trigger functions with semicolons inside
their own bodies, which a naive "split on semicolons and run each
piece" approach -- the trick load_demo_dataset.py's much simpler
schema gets away with -- would corrupt. Only a real SQL client parses
that correctly. Schema and data are fed to ONE continuous `psql`
session (not a separate connection per file): pg_dump's "COPY ...
FROM stdin" data format is designed for exactly this -- psql reads the
follow-on lines as the row data itself -- and a single session also
sidesteps a real problem hit running this against a pooled/managed
Postgres host: a second connection opened moments after the first one
created the schema saw "relation does not exist", almost certainly a
pooler routing it to a different backend that hadn't caught up yet.
One session the whole way through can't have that problem.

Three compatibility patches are applied to the upstream schema before
it's run, each found by actually trying to load it, not anticipated in
advance -- see the comments next to each one:
  1. uuidv7() -> gen_random_uuid() (Postgres 18+ builtin -> core since v13)
  2. VIRTUAL generated columns -> STORED (same version gap)
  3. The optional film_embedding table and its "vector" extension are
     dropped entirely -- nothing in this project does embeddings/vector
     search, so it's not worth requiring pgvector just to load sample
     data we'd never use. Every object's "OWNER TO postgres" is also
     dropped: whoever runs the CREATE already owns the object, so this
     is a no-op on hosts where that happens to be "postgres" too (the
     local test container) and an outright failure wherever it isn't --
     reassigning ownership needs permission to SET ROLE to the target
     role, which an ordinary (non-superuser) connection on a managed
     host doesn't have.
  4. The dump's "GRANT ALL ON SCHEMA public TO PUBLIC" is dropped. It
     re-opens what Postgres 15+ closed by default: every login, even a
     read-only one, could CREATE tables in the schema. Found when the
     VPS demo setup's own write test (deploy/setup_demo_database.sh)
     managed to create a table as the read-only role.

After loading, every table's row count is checked against the dump
file itself (not a guessed number) -- "psql didn't error" was never
proof the data actually landed correctly, and on one real run it
didn't (see docs/journey.md for the pooler bug that caused that). On a
mismatch the target is scrubbed and the whole load retried from
scratch, up to MAX_ATTEMPTS times, since the one realistic cause of a
clean-looking run producing wrong counts is a transient hiccup
(dropped connection mid-COPY, pooler weirdness) rather than something a
retry can't fix. Pass --no-scrub to load into a database you know is
already empty without wiping it first."""

import argparse
import re
import subprocess
import urllib.request
from pathlib import Path

SCHEMA_URL = "https://raw.githubusercontent.com/devrimgunduz/pagila/master/pagila-schema.sql"
DATA_URL = "https://raw.githubusercontent.com/devrimgunduz/pagila/master/pagila-data.sql"
MAX_ATTEMPTS = 5

# Matches a pg_dump COPY header like:
#   COPY public.actor (actor_id, first_name, last_name, last_update) FROM stdin;
_COPY_HEADER = re.compile(r"^COPY\s+(?:public\.)?(\S+)\s*\(([^)]*)\)\s+FROM stdin;$")

_DOLLAR_TAG = re.compile(r"\$[A-Za-z_]*\$")


def _split_sql_statements(sql: str) -> list[str]:
    """Splits a block of SQL on top-level semicolons only -- "top-level"
    meaning not inside a quoted string, a "--" line comment, or a
    dollar-quoted function body (e.g. $$ ... $$ or $tag$ ... $tag$).
    Function/trigger bodies can contain semicolons of their own, and
    pg_dump's own "-- Name: public; Type: SCHEMA; ..." comment headers
    contain several more -- none of those are statement separators. A
    plain `sql.split(";")` (fine for load_demo_dataset.py's much
    simpler schema, which has neither) would corrupt both."""
    statements: list[str] = []
    current: list[str] = []
    i = 0
    n = len(sql)
    in_single_quote = False
    dollar_tag: str | None = None

    while i < n:
        char = sql[i]

        if dollar_tag is not None:
            if sql.startswith(dollar_tag, i):
                current.append(dollar_tag)
                i += len(dollar_tag)
                dollar_tag = None
                continue
            current.append(char)
            i += 1
            continue

        if in_single_quote:
            current.append(char)
            if char == "'" and sql[i + 1 : i + 2] == "'":
                current.append("'")
                i += 2
                continue
            if char == "'":
                in_single_quote = False
            i += 1
            continue

        if char == "-" and sql[i + 1 : i + 2] == "-":
            end = sql.find("\n", i)
            end = n if end == -1 else end + 1
            current.append(sql[i:end])
            i = end
            continue

        if char == "'":
            in_single_quote = True
            current.append(char)
            i += 1
            continue

        if char == "$":
            match = _DOLLAR_TAG.match(sql, i)
            if match:
                dollar_tag = match.group(0)
                current.append(dollar_tag)
                i += len(dollar_tag)
                continue

        if char == ";":
            current.append(char)
            statements.append("".join(current))
            current = []
            i += 1
            continue

        current.append(char)
        i += 1

    tail = "".join(current).strip()
    if tail:
        statements.append(tail)
    return statements


def _download_if_missing(url: str, dest: Path) -> None:
    if dest.exists():
        return
    print(f"Downloading {url} -> {dest}")
    urllib.request.urlretrieve(url, dest)


_VECTOR_WORD = re.compile(r"\bvector\b", re.IGNORECASE)


def _mentions_pgvector(statement: str) -> bool:
    """True if a statement touches the dropped pgvector extension or
    its film_embedding table. "vector" is matched as a whole word, not
    a plain substring -- the real film table has a legitimate
    `tsvector` (full-text search) column and a `tsvector_update_trigger`
    trigger, and a substring check on "vector" would wrongly match
    those too, silently dropping the whole CREATE TABLE film statement
    (caught by actually running this against a real database)."""
    return bool(_VECTOR_WORD.search(statement)) or "film_embedding" in statement.lower()


_OWNER_TO_POSTGRES = re.compile(r"\bOWNER\s+TO\s+postgres\b", re.IGNORECASE)
# Patch 4 (module docstring): would let every role create objects here.
_GRANT_ALL_SCHEMA_TO_PUBLIC = re.compile(
    r"\bGRANT\s+ALL\s+ON\s+SCHEMA\s+public\s+TO\s+PUBLIC\b", re.IGNORECASE
)


def _patch_schema(schema_sql: str) -> str:
    patched_sql = schema_sql.replace("DEFAULT uuidv7()", "DEFAULT gen_random_uuid()")
    patched_sql = patched_sql.replace(") VIRTUAL", ") STORED")

    statements = _split_sql_statements(patched_sql)
    kept = [
        s
        for s in statements
        if not _mentions_pgvector(s)
        and not _OWNER_TO_POSTGRES.search(s)
        and not _GRANT_ALL_SCHEMA_TO_PUBLIC.search(s)
    ]
    return "\n".join(kept)


def _patch_data(data_sql: str) -> str:
    """Removes the one "COPY public.film_embedding (...) FROM stdin;
    ... \\." block from the data file -- that table was dropped from
    the schema above, so there's nowhere for its rows to go. Every
    other COPY block (and everything else in the file) is left
    untouched, byte for byte, since psql consumes pg_dump's COPY
    format natively.

    Also strips the file's own "SET transaction_timeout = 0;" preamble
    line -- it was dumped with a newer pg_dump (Postgres 17+) than the
    Postgres 16 this project targets, and that GUC doesn't exist yet
    there, which is a hard error, not a warning."""
    data_sql = data_sql.replace("SET transaction_timeout = 0;\n", "")
    lines = data_sql.splitlines(keepends=True)
    kept_lines: list[str] = []
    i = 0
    while i < len(lines):
        match = _COPY_HEADER.match(lines[i].rstrip("\n"))
        if match and match.group(1) == "film_embedding":
            i += 1
            while lines[i].rstrip("\n") != "\\.":
                i += 1
            i += 1  # skip the "\." terminator itself
            continue
        kept_lines.append(lines[i])
        i += 1
    return "".join(kept_lines)


def _run_psql(psql_path: str, connection_url: str, sql: str, description: str) -> None:
    result = subprocess.run(
        [psql_path, connection_url, "-v", "ON_ERROR_STOP=1"],
        input=sql,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SystemExit(f"psql failed {description}:\n{result.stderr}")


def _scrub(psql_path: str, connection_url: str) -> None:
    """Wipes every object in the public schema before (re)loading --
    Pagila's CREATE statements fail outright against a schema that
    already has tables in it, including its own from a previous
    attempt, so a retry needs a clean slate, not just a repeat."""
    _run_psql(
        psql_path,
        connection_url,
        "DROP SCHEMA public CASCADE;\nCREATE SCHEMA public;\n",
        "scrubbing the public schema",
    )


def _expected_row_counts(patched_data_sql: str) -> dict[str, int]:
    """Counts rows per table directly from the (already film_embedding-
    and transaction_timeout-patched) data file's own COPY blocks -- the
    actual ground truth, not a number anyone guessed or remembered."""
    lines = patched_data_sql.splitlines()
    counts: dict[str, int] = {}
    i = 0
    while i < len(lines):
        match = _COPY_HEADER.match(lines[i])
        if not match:
            i += 1
            continue
        table = match.group(1)
        i += 1
        n = 0
        while lines[i] != "\\.":
            n += 1
            i += 1
        i += 1
        counts[table] = n
    return counts


def _actual_row_counts(psql_path: str, connection_url: str, tables: list[str]) -> dict[str, int]:
    query = "\nUNION ALL\n".join(f"SELECT '{t}' AS t, count(*) AS c FROM \"{t}\"" for t in tables)
    result = subprocess.run(
        [psql_path, connection_url, "-v", "ON_ERROR_STOP=1", "-At", "-F", "|", "-c", query],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f"psql failed counting rows after loading:\n{result.stderr}")
    counts: dict[str, int] = {}
    for line in result.stdout.strip().splitlines():
        table, _, count = line.partition("|")
        counts[table] = int(count)
    return counts


def load(psql_path: str, connection_url: str, cache_dir: Path, scrub: bool = True) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    schema_file = cache_dir / "pagila-schema.sql"
    data_file = cache_dir / "pagila-data.sql"
    _download_if_missing(SCHEMA_URL, schema_file)
    _download_if_missing(DATA_URL, data_file)

    patched_schema = _patch_schema(schema_file.read_text())
    patched_data = _patch_data(data_file.read_text())
    # pg_dump's own preamble (inside patched_schema) runs
    # `SELECT pg_catalog.set_config('search_path', '', false);`, a
    # deliberate pg_dump safety practice -- harmless for *this* session
    # since every CREATE in the dump is already schema-qualified. But
    # on a host whose connection pooler doesn't reset session state
    # between reused connections (confirmed by testing against one),
    # that empty search_path otherwise leaks to the *next* unrelated
    # connection -- including, eventually, the live app's own. Setting
    # it back explicitly, as the session's last act, is what actually
    # matters here, not just leaving it to each caller to remember.
    combined_sql = patched_schema + "\n" + patched_data + "\nSET search_path TO public;\n"
    expected_counts = _expected_row_counts(patched_data)

    for attempt in range(1, MAX_ATTEMPTS + 1):
        if scrub:
            _scrub(psql_path, connection_url)
        _run_psql(
            psql_path,
            connection_url,
            combined_sql,
            f"applying {schema_file} and {data_file} (attempt {attempt}/{MAX_ATTEMPTS})",
        )

        actual_counts = _actual_row_counts(psql_path, connection_url, list(expected_counts))
        mismatches = [
            f"  {table}: expected {expected}, got {actual_counts.get(table, -1)}"
            for table, expected in expected_counts.items()
            if actual_counts.get(table, -1) != expected
        ]
        if not mismatches:
            print(
                f"Row counts verified against the dump file: all {len(expected_counts)} tables match."
            )
            return

        print(f"Attempt {attempt}/{MAX_ATTEMPTS}: row count mismatch after loading --")
        print("\n".join(mismatches))
        if attempt < MAX_ATTEMPTS:
            print("Scrubbing and retrying..." if scrub else "Retrying...")

    raise SystemExit(
        f"Giving up after {MAX_ATTEMPTS} attempts -- row counts never matched the dump file."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--connection-url",
        required=True,
        help="Plain postgresql:// URL (psql's own format, not SQLAlchemy's +asyncpg).",
    )
    parser.add_argument("--psql", default="psql", help="Path to the psql executable.")
    parser.add_argument(
        "--cache-dir",
        default=".pagila-cache",
        help="Where the downloaded dump files are kept (reused on repeat runs).",
    )
    parser.add_argument(
        "--no-scrub",
        action="store_true",
        help="Don't wipe the public schema first -- only safe against a database you know is already empty.",
    )
    args = parser.parse_args()

    load(args.psql, args.connection_url, Path(args.cache_dir), scrub=not args.no_scrub)
    print("Pagila loaded.")


if __name__ == "__main__":
    main()
