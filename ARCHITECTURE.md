# Architecture Walkthrough

A guided tour of the codebase, following how a real request actually
flows through it — not an alphabetical file list. If you can't explain
this back in your own words, that's the part to come back and reread.

## The big picture

This is a server that sits between an AI model and a real database. The
AI never touches the database directly — it can only ask this server to
do things ("what tables exist?", "run this SQL"), and the server decides
whether that's safe before doing it. Every file's job is either (a) part
of that safety decision, (b) bookkeeping around who's allowed to ask, or
(c) tooling for the operator to set things up.

## The spine — what happens when a request comes in

1. **`app/mcp/server.py`** — the front door. This is where the four tools
   (`list_tables`, `describe_table`, `sample_rows`, `run_query`) are
   actually declared using the MCP protocol library. Every tool function
   does the same two things: check who's asking, then hand off to the
   real logic elsewhere. It also has the `_translate_errors` wrapper —
   this turns "the key is wrong" or "that table doesn't exist" into a
   clean message the AI model can actually read, instead of crashing
   with a scary traceback.

2. **`app/auth/service.py`** — "who is this?" Takes the API key the
   request came with, hashes it, looks it up, and either returns the
   `Workspace` it belongs to or raises `AuthenticationError`. Everything
   downstream trusts that if you have a `Workspace` object, the caller is
   legitimately allowed to act as that workspace.

3. **`app/query/validation.py`** — "is this SQL even allowed?" This is
   the actual safety brain of the whole project. Before any SQL touches
   a real database, this file parses it and checks: is it exactly one
   statement? Is it a plain `SELECT` with no hidden `INSERT`/`UPDATE`/
   `DELETE` smuggled inside a sub-query? Does it call any function we
   haven't explicitly allowed? If any check fails, the query never gets
   near the database — it's rejected right here, in pure Python, no DB
   connection even opened.

4. **`app/query/cost_check.py`** — "is this SQL going to be too
   expensive?" Only reached if validation passed. Asks Postgres "how
   expensive would this be?" (via `EXPLAIN`) without actually running
   it, and rejects it if the estimate is too high.

5. **`app/query/service.py`** — "actually run it." This is where
   `run_query` and `list_tables` live for real. It ties the above two
   checks together, adds a timeout so a slow query gets killed rather
   than hanging forever, retries only if the *connection* itself dropped
   (never retries just because a query was slow), and — critically —
   always writes down what happened in the audit log, whether the query
   succeeded, failed, or was rejected. `get_data_source` here is a small
   shared helper (used by several files) that looks up which real
   external database a workspace is allowed to query.

6. **`app/query/schema_cache.py`** — `describe_table` and `sample_rows`
   live here. They answer "what columns does this table have?" and
   "show me a few example rows," using a cache so repeated schema
   questions don't have to re-ask the real database every time. The
   cache is for *convenience only* — if it's wrong or stale, the real
   query in steps 3–5 still runs against the live database and can't be
   fooled by it.

## Supporting cast — the data these steps depend on

- **`app/auth/models.py`** — defines the database tables for the
  bookkeeping side: `Workspace` (one per tenant), `ApiKey` (the
  credential used to run queries — can be revoked), `ConsoleCredential`
  (a *separate* login for the web dashboard, so revoking the API key
  doesn't lock you out of viewing history), `RateLimitCounter` (how many
  requests a workspace has made recently).
- **`app/auth/security.py`** — the actual hashing math: turns a
  plain-text API key or password into something safe to store, and
  checks a plain-text value against a stored hash later.
- **`app/auth/rate_limit.py`** — the logic that actually enforces the
  "only N requests per hour" rule using `RateLimitCounter`.
- **`app/query/models.py`** — the database tables for the query side:
  `DataSourceConnection` (which real external database a workspace
  points at), `QueryExecution` (the audit log row — one per attempt,
  this *is* the audit trail), `SchemaCache` (the cached table/column
  info mentioned above).
- **`app/benchmark/models.py` + `app/benchmark/grading.py`** — only used
  by the Slice E benchmark script, not by the live server. `EvalCase` is
  a hand-written question + correct answer; `BenchmarkResult` records
  whether one benchmark run got it right; `grading.py` is the actual
  "did the AI's answer match?" comparison logic.

## Plumbing — things everything else depends on

- **`app/db.py`** — sets up the one shared database connection pool and
  the base class every table model inherits from. Also where the
  "always store timestamps with timezone info" rule is enforced
  globally, so nobody has to remember it per-column.
- **`app/config.py`** — reads all the `APP_*` settings (timeouts, rate
  limit, which databases to use) from environment variables into one
  typed object the rest of the code uses.
- **`app/main.py`** — wires the web console's routes into an actual
  running web server (FastAPI), plus the session-cookie middleware the
  console's login depends on.
- **`app/console/auth.py`** — the console's login check, against
  `ConsoleCredential` (deliberately separate from `ApiKey` -- see
  app/auth/models.py -- so revoking a compromised API key never locks
  the operator out of viewing that workspace's own audit history).
- **`app/console/routes.py`** — the web dashboard's pages: login,
  logout, a dashboard showing the real audit trail, and a keys page
  that can rotate or revoke the workspace's API key.
- **`migrations/`** — Alembic's job: turning the Python model
  definitions above into actual `CREATE TABLE` statements run against
  Postgres, and tracking that history so the database schema can be
  upgraded safely over time.

## `scripts/` — tools the operator runs by hand, never run automatically

These aren't part of the live server — they're one-off commands for
setting things up:
- **`seed_workspace.py`** — create a new workspace + its first API key.
- **`revoke_key.py`** — kill an existing API key.
- **`provision_readonly_role.py`** — set up the actual restricted
  database user on the *target* database (the one being queried),
  separately from the app's own bookkeeping database.
- **`load_demo_dataset.py`** — fills a target database with
  fake-but-realistic sample data (customers/orders/products) to test and
  demo against.
- **`load_pagila_dataset.py`** — an alternative, richer target: loads
  Pagila, a real public Postgres sample database, instead of the
  hand-written data above. See JOURNEY.md for why a plain substring
  filter almost deleted the real `film` table while stripping out the
  (unused) pgvector extension this schema optionally ships with.
- **`seed_eval_cases.py`** / **`run_benchmark.py`** — the Slice E
  benchmark: write the test questions, then run them against a real AI
  model and grade the results.

## `tests/` mirrors `app/`

Each test file matches the thing it's testing — `test_validation.py`
tests `app/query/validation.py`, `test_rate_limit_and_revocation.py`
tests the auth pieces, and so on. `test_mcp_protocol.py` is special — it
tests the *whole chain* together through the actual MCP protocol, not
just one file at a time. `test_red_team.py` is the adversarial suite —
see RED_TEAM.md for what it covers and why.

## The non-code files

`phase-1` through `phase-7` are the planning trail (problem →
requirements → domain model → ... → work plan). `THREAT_MODEL.md` says
what this defends against and what it doesn't. `RED_TEAM.md` is the
published evidence for that -- the adversarial test suite's results.
`JOURNEY.md` is the day-by-day log of what broke and what was learned
fixing it. `README.md` is the front door to all of that.

## Open gaps, as of this writing

- `APP_COST_THRESHOLD` is now calibrated against real Pagila data
  (40,000 -- see JOURNEY.md for the full measurements), not guessed.
  One real, accepted gap surfaced doing that: a cross join confined to
  small dimension tables (hundreds of rows each) costs *less* than the
  heaviest legitimate analytical query, so no single threshold can
  admit the one and reject the other -- empirically proven harmless at
  Pagila's current scale (ran in 0.2s), not just assumed, but worth
  re-checking if the target dataset's tables grow by an order of
  magnitude.
- The Slice E benchmark is built and verified end to end except for an
  actual AI model call, which needs a real API key in `.env`.
- The console's pages are verified via FastAPI's `TestClient` (the real
  HTTP layer), but never visually confirmed in an actual browser — a
  preview-sandbox limitation hit while building it, see JOURNEY.md.
- The real target database (`APP_TARGET_DATABASE_URL`, now loaded with
  Pagila) has no DB-level read-only role yet — the connecting account
  on that managed host can't `CREATE ROLE`, so
  `scripts/provision_readonly_role.py` can't run against it. That
  database currently relies on the app-level validator alone, not the
  defense-in-depth role-level block THREAT_MODEL.md describes. See
  JOURNEY.md for why, and for a real session-state leak this same
  target host's connection pooler caused and how the loader now avoids
  it.
