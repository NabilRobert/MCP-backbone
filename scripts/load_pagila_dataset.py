# pyright: basic
# (asyncpg ships no type stubs; this file's data-loading path is a
# handful of raw asyncpg calls, so strict mode here is mostly noise.)
"""Loads Pagila -- a real, well-known public Postgres sample database
(a DVD rental store: actors, films, customers, rentals, payments,
stores, 16+ tables) -- into the target database, as a richer
alternative to load_demo_dataset.py's hand-generated synthetic data.

Source: https://github.com/devrimgunduz/pagila (the standard Postgres
port of MySQL's "Sakila" sample database). Used here only as public
sample data -- it was never real customer data, even in the original
project, and this script makes no other claim about it.

Needs `psql` on PATH (or pass --psql with a full path) to apply the
schema file: it defines several PL/pgSQL trigger functions with
semicolons inside their own bodies, which a naive "split on semicolons
and run each piece" approach -- the trick load_demo_dataset.py's much
simpler schema gets away with -- would corrupt. Only a real SQL client
parses that correctly. The (much larger) data file is loaded directly
through asyncpg's own COPY support instead, since it doesn't need a
general SQL parser, just the Postgres COPY FROM stdin protocol.

Three compatibility patches are applied to the upstream schema before
it's run, each found by actually trying to load it, not anticipated in
advance -- see the comments next to each one:
  1. uuidv7() -> gen_random_uuid() (Postgres 18+ builtin -> core since v13)
  2. VIRTUAL generated columns -> STORED (same version gap)
  3. The optional film_embedding table and its "vector" extension are
     dropped entirely -- nothing in this project does embeddings/vector
     search, so it's not worth requiring pgvector just to load sample
     data we'd never use."""

import argparse
import asyncio
import io
import re
import subprocess
import urllib.request
from pathlib import Path

import asyncpg

SCHEMA_URL = "https://raw.githubusercontent.com/devrimgunduz/pagila/master/pagila-schema.sql"
DATA_URL = "https://raw.githubusercontent.com/devrimgunduz/pagila/master/pagila-data.sql"

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


def _run_schema_file(psql_path: str, connection_url: str, schema_file: Path) -> None:
    schema_sql = schema_file.read_text()
    patched_sql = schema_sql.replace("DEFAULT uuidv7()", "DEFAULT gen_random_uuid()")
    patched_sql = patched_sql.replace(") VIRTUAL", ") STORED")

    statements = _split_sql_statements(patched_sql)
    kept = [s for s in statements if not _mentions_pgvector(s)]
    final_sql = "\n".join(kept)

    result = subprocess.run(
        [psql_path, connection_url, "-v", "ON_ERROR_STOP=1"],
        input=final_sql,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SystemExit(f"psql failed applying {schema_file}:\n{result.stderr}")


def _iter_copy_blocks(data_sql: str):
    """Walks the data file's lines looking for "COPY ... FROM stdin;"
    headers, then collects every line after it up to the lone "\\."
    terminator pg_dump always writes to end a block -- that collected
    text is exactly the tab-separated data COPY expects, same format
    whether it came from pg_dump or a hand-typed COPY session. Skips
    the film_embedding table -- dropped from the schema above, so
    there's nowhere for its rows to go."""
    lines = data_sql.splitlines()
    i = 0
    while i < len(lines):
        match = _COPY_HEADER.match(lines[i])
        if not match:
            i += 1
            continue
        table = match.group(1)
        columns = [c.strip() for c in match.group(2).split(",")]
        i += 1
        body_lines: list[str] = []
        while lines[i] != "\\.":
            body_lines.append(lines[i])
            i += 1
        i += 1  # skip the "\." terminator itself
        if table == "film_embedding":
            continue
        body = ("\n".join(body_lines) + "\n").encode("utf-8")
        yield table, columns, body


async def _load_data_file(connection_url: str, data_file: Path) -> None:
    conn = await asyncpg.connect(connection_url)
    try:
        data_sql = data_file.read_text()
        for table, columns, body in _iter_copy_blocks(data_sql):
            # asyncpg treats a plain `bytes` source as a file *path* to
            # open, not as the data itself -- it only reads data in-memory
            # from something with a .read() method, hence the BytesIO.
            await conn.copy_to_table(table, source=io.BytesIO(body), columns=columns, format="text")
    finally:
        await conn.close()


def load(psql_path: str, connection_url: str, cache_dir: Path) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    schema_file = cache_dir / "pagila-schema.sql"
    data_file = cache_dir / "pagila-data.sql"
    _download_if_missing(SCHEMA_URL, schema_file)
    _download_if_missing(DATA_URL, data_file)

    _run_schema_file(psql_path, connection_url, schema_file)
    asyncio.run(_load_data_file(connection_url, data_file))


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
    args = parser.parse_args()

    load(args.psql, args.connection_url, Path(args.cache_dir))
    print("Pagila loaded.")


if __name__ == "__main__":
    main()
