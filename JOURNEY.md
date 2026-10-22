# Journey Log

A running record of what actually happened building this — what was tried,
what broke, what surprised me, and why decisions landed where they did.
Not a changelog (git history already has that); this is the "why" and the
dead ends, the stuff that gets lost if it only lives in commit messages.

## 2026-10-12 — Scaffold, then Slice A (the walking skeleton)

Scaffolded the project structure matching the Phase 5 architecture: `auth`,
`query`, `benchmark`, `console`, `mcp` modules, one FastAPI app, Postgres,
no Redis/Celery (per ADR-002).

First real lesson of the build: **almost nothing in a fresh scaffold works
on the first try, and that's fine as long as you actually run it instead of
trusting it.** Caught six real bugs just by installing dependencies and
importing the code, before writing a single line of business logic:

- `pyproject.toml`'s project name didn't match the `app/` directory, so
  `hatchling` couldn't figure out what to ship. Needed an explicit
  `packages = ["app"]`.
- The MCP SDK installed as v2.x, where `FastMCP` was renamed to
  `MCPServer`. The scaffold was written against the v1 API name from
  memory — a reminder that SDK docs/examples age fast.
- `pyright` strict flagged `pydantic-settings`' `Settings()` call as
  missing arguments — a known false positive, since those fields load from
  the environment at runtime. Suppressed explicitly rather than weakening
  strict mode globally.
- Every `Mapped[datetime]` column defaulted to `TIMESTAMP WITHOUT TIME
  ZONE`, but all the app code uses `datetime.now(UTC)`. This didn't show up
  until actually seeding a workspace against a real database — a real
  `DataError`, not a lint warning. Fixed once, globally, via
  `type_annotation_map` on `Base`, instead of annotating every column by
  hand.
- The async engine used normal connection pooling, which broke the moment
  there was more than one test function: asyncpg connections are bound to
  the event loop they were created on, and pytest-asyncio gives each test
  its own loop by default. Symptom was a cryptic `RuntimeError: Future
  attached to a different loop`. Fixed with `NullPool` — and at this
  project's actual scale, pooling wasn't buying anything anyway.
- The first integration test fixture passed `str(engine.url)` as a
  workspace's data source connection string — which silently masks the
  password as `***` for safety. Took a `password authentication failed`
  error to notice. Fixed with `render_as_string(hide_password=False)`.

None of these were subtle design flaws — they were exactly the kind of
thing that looks fine on paper and breaks the instant you run it. Good
argument for why "build with understanding" means actually executing the
code at every step, not just reading it back.

By the end of the day: API key auth, `list_tables`, and `run_query` work
end to end through the *real* MCP `call_tool` interface (not just the
underlying Python functions), backed by a real Postgres instance, with a
passing integration test suite. That's Slice A done, locally — not yet
deployed.

Also had a process correction: the `.gitignore` picked up a `phase-*.md`
exclusion mid-session, which would have left the README's links to the
planning docs dead for anyone cloning the repo — exactly the audience
(a reviewer) this whole trail exists for. Flagged it, decision reversed,
docs are tracked.

## 2026-10-13 — Phase 6 spikes, before touching Slice B

The plan said: run the spikes before building Slice B's safety layer, not
after. Glad it did — two of the four spikes changed what Slice B actually
needs to be.

**Spike 1 (read-only role airtightness):** confirmed a real gap. A direct
`INSERT` is correctly blocked by table privileges. But a `SELECT`-wrapped
call to a `SECURITY DEFINER` function — one that performs a write — still
succeeds, because Postgres grants `EXECUTE` on new functions to `PUBLIC`
by default, and the call is a syntactically valid single `SELECT`. Tried
the obvious fix (`REVOKE EXECUTE ... FROM PUBLIC` / `ALTER DEFAULT
PRIVILEGES`) and found it only protects functions created *afterward, by
the same role* — not functions belonging to a schema we don't own, which
is exactly the situation for "connect to any external database." Real,
load-bearing finding: the DB role alone isn't sufficient for that case.
Fed forward into Slice B as a new requirement: sqlglot needs an explicit
function-call allowlist.

**Spike 2 (does sqlglot actually catch disguised writes):** also a real
finding. Checking only sqlglot's top-level statement type is not enough —
`WITH x AS (INSERT ... RETURNING id) SELECT * FROM x` and `SELECT * INTO
new_table FROM ...` both parse with a top-level type of `Select`. The fix,
verified against a CTE nested two levels deep: walk the *entire* parsed
tree (`parsed.find_all(exp.Insert, exp.Update, exp.Delete, exp.Into,
exp.Create, exp.Drop, exp.Alter)`), not just the root node. Also verified
— reassuringly — that the DB-level read-only role independently blocks
both of these cases anyway (privilege checks are per-operation, not
per-outer-statement-type), so the layered defense held even where the
naive app-level check wouldn't have. That's the actual point of
defense-in-depth, seen working rather than assumed.

**Spike 4 (TypeScript build step):** worked cleanly on the first real try
— `esbuild main.ts --bundle` produced a static JS file FastAPI serves with
no extra plumbing. Found and fixed an unrelated moderate advisory in the
pinned esbuild version (affects its dev server, which this project never
runs, but the fix was free, so took it).

**Spike 3 (EXPLAIN cost check):** mechanics confirmed — `EXPLAIN (FORMAT
JSON)` gives a clean, parseable `Total Cost` figure, and a scoped
`statement_timeout` applies to the `EXPLAIN` call itself. What's *not*
done yet: picking an actual cost threshold, since that needs the real
public demo dataset, which hasn't been chosen/loaded yet. Logged as a
known gap rather than guessed at.

## 2026-10-13 (continued) — Slice B: the real safety layer

Built the validator exactly as the spikes specified: single-statement
check, a recursive walk for any write-node type (not just the top-level
statement type), and a function-call allowlist that defaults to empty —
sqlglot gives every function it recognizes (`COUNT`, `COALESCE`,
`DATE_TRUNC`, ...) its own node type, so anything that falls back to
`exp.Anonymous` is either user-defined or an unmodeled builtin like
`pg_sleep`, and both get rejected by default. Verified against the full
disguised-write battery from the spikes, plus a set of common analytical
functions to confirm nothing legitimate gets false-rejected.

Wired validation, the `EXPLAIN` cost check, and the statement timeout into
`run_query`, and found two more real bugs by actually running it:

- `EXPLAIN` on a query referencing a nonexistent table raises a plain DB
  error, not `QueryRejected` — the first version let that propagate
  uncaught instead of becoming a normal "errored" audit row.
- Needed to specifically exercise the timeout-classification path (not
  just assume it): temporarily allowed `pg_sleep` through the allowlist,
  confirmed a query gets killed at the configured timeout, classified as
  `timed_out` (not `errored`), and is *not* retried — retrying a slow
  query just repeats the cost, which was the Phase 4 decision.

Built the read-only role provisioning script to match what spike 1 tested
manually by hand, and in doing so found a third real gotcha:
`GRANT ... ON ALL TABLES IN SCHEMA` is a point-in-time grant — a table
created *after* provisioning was completely invisible to the role,
permission-denied on a plain `SELECT`, not just writes. Fixed with
`ALTER DEFAULT PRIVILEGES ... GRANT SELECT ON TABLES`, which extends the
grant to tables created later by the same admin connection — documented,
not silently assumed, that it does *not* cover tables added by a
different role that owns the schema. Confirmed by actually creating a
table after provisioning and checking both the role's read access and
that direct writes were still blocked.

Running theme across both build days: every non-trivial claim in this
project ("the role is read-only," "the validator catches disguised
writes," "the grant covers future tables") turned out to have a real edge
where the naive version was wrong, and every one of them was caught by
actually executing something against a real database, never by reasoning
about it on paper. That's probably the single most honest thing to say
about this project if asked "what did you learn building it."

## 2026-10-13 (continued) — Repositioning, then Slice C

Reopened Phase 1. A separate planning pass (`portfolio-plan.md`) concluded
that a broad "Jarvis" assistant platform is the wrong portfolio piece — it
reads as the most common AI project, breadth is hard to evaluate, and it's
indistinguishable from a weekend demo. The stronger move: three small,
*finished* things (this MCP server, an eval harness, a real-system case
study) instead of one sprawling platform. This project was never actually
built as part of a bigger platform's code — "stands alone, imports
nothing" was decided before this build started — so the change was almost
entirely about framing, not architecture: dropped the "backbone for
Jarvis" language from the brief and the README, removed the long-term
web/mobile/WhatsApp vision section outright. Reusing this later is now a
possible side-benefit, never a stated roadmap.

Closed the two gaps that repositioning's own checklist surfaced: a CI
pipeline and an explicit threat model doc. Verified the CI workflow
properly before trusting it — ran the exact same install/lint/typecheck/
test sequence locally against a fresh venv and a clean Postgres container
first, rather than writing YAML and hoping.

Then Slice C turned up two more real gaps, this time by reading the code
against the plan rather than by running it: `RateLimitCounter` had existed
as a model since Phase 3 but was never actually checked or incremented
anywhere, and there was a script to issue API keys but no way to revoke
one. Both fixed and verified — a low rate limit correctly cuts off
requests mid-run, and a revoked key immediately fails authentication.

## 2026-10-13 (continued) — Slice D, and the most interesting bug yet

Wired `describe_table`/`sample_rows` to the `SchemaCache`, then found the
most instructive bug of the whole build while testing them. Calling the
tools directly via `MCPServer.call_tool()` — the SDK's convenience Python
method — a bad table name or a bad API key crashed with a raw traceback.
Read the SDK's own exception docstring before assuming that was the real
behavior: `call_tool()` is explicitly documented to *raise* `ToolError`
rather than convert it, because it's a shortcut for in-process callers,
not the actual client-facing path. The real path is the low-level
server's request handler, which does catch `ToolError` and convert it to
a graceful `is_error=True` result.

Proved that distinction rather than taking the docstring's word for it:
wired an in-memory `ClientSession` to the server's actual `_lowlevel_server.run()`
over real (if in-process) streams — the same code path a real AI client
exercises — and confirmed the difference firsthand. Through that real
path, an unhandled `AuthenticationError`/`QueryExecutionError` became a
generic `"Error executing tool X"` with no detail reaching the model;
after wrapping them as `ToolError`, the same failure came through as
`"invalid or revoked API key"` or `"table 'x' not found"` — the actual
reason, not a crash. That's a meaningful difference for a real AI client
trying to recover from a mistake (try a different key vs. give up), and
it would have shipped silently wrong if the first test had only used the
convenience method and called it a day.

## 2026-10-13 (continued) — framework choice, then Slice E

Arthur pushed back on hand-rolling the benchmark's agent loop: "seems
mighty unwise to not use a framework." Picked PydanticAI specifically for
its native MCP client support, but spiked it before committing rather
than taking the fit on faith — connected `fastmcp.Client` (PydanticAI's
underlying MCP client) directly to the in-process `MCPServer` object, no
subprocess needed, and it listed and called all four tools correctly, a
bad key surfacing the exact `ToolError` message from Slice D through yet
another independent client implementation.

Built the rest of Slice E: a small synthetic storefront dataset
(hand-generated, not sourced from anywhere real — honesty constraint)
because a real public dataset needs downloading and this session has no
internet access; ten hand-written eval questions; an execution-accuracy
grader; the benchmark runner itself.

Two real findings while building the runner, both the kind that only show
up by actually running the thing:

1. **Grading by column name is wrong.** First version compared result
   rows as key-value pairs, which would fail a correct answer purely for
   using a different (equally valid) column alias — `AS revenue` vs
   `AS total`. Fixed to compare by value in column order instead, and
   wrote down the real limitation that creates (column *order* still
   matters) rather than quietly shipping a grader that looked stricter
   than it should be.
2. **Putting the API key in the prompt and hoping the model copies it
   into every tool call is fragile and a bad pattern.** Found this by
   testing with PydanticAI's `Agent('test')` — a free, no-API-key stub
   model — which doesn't read the prompt at all, so every tool call
   failed with "invalid or revoked API key." The real fix wasn't "tell
   the model more clearly," it was to stop asking the model to handle a
   credential at all: `MCPToolset`'s `process_tool_call` hook injects the
   real key into every call before it reaches the server, so the model
   never sees or needs to touch it.

`Agent('test')` turned out to be the right tool for verifying everything
*except* real model intelligence without spending a cent or needing
Arthur's key yet: confirmed the full `run()` orchestration — auth
injection, tool error handling, SQL/result extraction from the message
history, grading, and `BenchmarkResult` persistence — all execute
correctly end to end. The only thing not yet verified is whether a real
model actually answers the questions right, which needs Arthur's own
Anthropic key and is explicitly his cost to incur, not something to spend
without asking.

## 2026-10-13 (continued) — the CI failure that wasn't about the API key

Arthur got a GitHub email: run failed. First instinct to check was the
obvious one (missing `ANTHROPIC_API_KEY`), but that's a runtime concern —
CI only lints, type-checks, and runs the test suite, none of which calls
an LLM. The real cause was underneath the whole CI setup the entire time:
`pyproject.toml`'s `[tool.pyright]` pins `venvPath="." venv=".venv"` for
local dev, but the CI workflow installed dependencies straight into the
runner's system Python — no `.venv` folder ever existed there. Pyright
couldn't find the configured venv, fell back to nothing resolvable, and
failed with 160 errors, "Import could not be resolved" on everything
including `pytest` itself.

The uncomfortable part: this had almost certainly been broken since the
very first CI run, and my own "verification" of the workflow back when I
built it never caught it. I'd tested by running `pyright` from inside
this project directory — which *always* finds the real `./.venv` and
resolves against it, regardless of which pyright binary or throwaway
venv I thought I was testing with, because pyright's venv lookup goes by
configured path, not by which interpreter launched it. The bug was
invisible to any test run from this directory, by construction.

Found and fixed it properly: exported a clean `git archive` checkout
with deliberately no `.venv` anywhere, reproduced the exact 160-error
failure there, fixed the workflow to create `.venv` at the path the
config expects, and reproduced clean again in the same disposable
checkout before pushing. Also rolled the `benchmark` extra into CI's
install step while at it, so `scripts/run_benchmark.py`'s pydantic_ai/
fastmcp imports get checked too, not just the core app.

The lesson that'll stick: "I tested it locally" is only as good as
whether the local test can actually fail the way the real environment
would. A local check that structurally can't reproduce the failure mode
isn't verification, it's theater.

## 2026-10-14 — Switching to Arthur's real provider, then Slice F

Arthur's AI provider turned out to be Sumopod (an OpenAI-compatible
gateway), not direct Anthropic, with the actual env var names
(`SUMOPOD_API_KEY`, `SUMOPOD_BASE_URL`, `AI_MODEL`) chosen on his end.
Rewired `_build_model()` to match, verified both the gateway path and
the direct-Anthropic fallback construct correctly using fake stand-in
values -- never touching the real secrets, per his explicit ask. Caught
one more real gap right after: once he filled in the real `.env`,
`os.environ.get("SUMOPOD_API_KEY")` still came back empty, because
nothing was loading the `.env` file into the process for that script --
`app/config.py`'s `Settings` reads `.env` for its own fields, but that
mechanism doesn't touch `os.environ` for anything else. Fixed with an
explicit `load_dotenv()` call, verified by checking only *presence*,
never the actual values.

Also found and fixed a self-inflicted CI failure the same way as the
venv bug before it: a fix made on disk, verified locally, but never
actually staged into the commit that was supposed to contain it. Caught
by reproducing the exact failing commit with `git archive` rather than
trusting "it passed on my machine a few commits ago."

Then built Slice F, the red-team suite. The interesting design decision
wasn't the attack catalog itself -- it was recognizing that most of the
existing safety tests only prove the *validator* catches something, not
that the *database* would catch it independently if the validator had a
bug. Added a second fixture (`readonly_role_workspace`) that provisions
a REAL restricted role via `scripts/provision_readonly_role.py`'s own
`provision()` function (not a reimplementation of its SQL), so a subset
of the red-team tests bypass the validator entirely and hit the
database directly -- proving defense-in-depth instead of assuming it.

That fixture surfaced two more real bugs on its own: the same
password-masking issue from Slice A (`str(engine.url)` hides the
password; needed `render_as_string(hide_password=False)` again), and a
teardown failure -- `DROP ROLE` refuses to run while the role still
holds any granted privileges, which it always will right after
`provision()` runs. Fixed with `DROP OWNED BY` before the drop.

The red-team suite itself caught one dishonest test before it shipped:
a "comment-based injection" case where the `/* ... */` actually just
swallowed the rest of the string as a real SQL comment, leaving a
genuinely harmless query. The test's *expectation* was wrong, not the
code -- worth catching, since shipping a red-team test that asserts the
wrong thing would be worse than not having it, it would look like
coverage that isn't real.

The most honest result in the whole suite is probably
`test_security_definer_function_succeeds_if_validator_is_bypassed` --
a test that's *supposed* to pass by demonstrating an attack succeeding,
specifically to prove the documented residual risk in THREAT_MODEL.md
is real and not just a hedge. A red-team suite that only ever shows
green checkmarks for things blocked isn't telling the whole story.

## 2026-10-14 (continued) — Slice G, the console for real

Went from an empty scaffold (two routes rendering blank pages) to an
actually working console: login against ConsoleCredential (separate
from the API key on purpose, so revoking a compromised key never locks
anyone out of the audit history), a dashboard polling the real audit
log, and a keys page that can rotate or revoke the workspace's key.

Centralized key issuance/revocation into app/auth/service.py first --
scripts/revoke_key.py already had working revoke logic, and the
console needed the exact same thing plus a new "rotate" operation.
Duplicating that between a CLI script and a web route would have been
exactly the kind of drift this project has been careful to avoid
elsewhere, so both now call the same functions.

Caught a real timing side-channel while writing the login check: the
first version returned early for an unknown username without ever
running the password hash, meaning a login attempt could be used to
discover which usernames exist just by how fast the response came
back. Fixed by always running the slow hash, even against a dummy
value, so the timing is the same either way -- worth catching before
it shipped, not after.

Caught a second real bug immediately on first import: the audit API's
return type (`list[dict] | RedirectResponse`) isn't something FastAPI
can turn into a Pydantic response model, which crashed the app at
startup. Neither ruff nor pyright catches this kind of thing --  only
actually running the app does.

A third, smaller bug: an env var present in `.env` but left blank
(exactly what `.env.example` ships) silently overrode the random
session-secret default with an actual empty string, which would have
meant an unsigned, forgeable login cookie. Found by testing the exact
case Arthur would actually hit, not just the happy path.

Tried to verify all of this visually in the browser preview and hit a
real environment limit instead: the preview tool resolves `launch.json`
relative to a parent directory one level up from this repo (which
contains a space in its name, "MCP project"), and spawning a process
rooted there was denied by the sandbox -- `getcwd(): Operation not
permitted` -- regardless of how the command was quoted. Didn't fight it
further once the actual functional behavior was already verified more
rigorously a different way: every route, redirect, form, and the
real-data audit API were all tested through FastAPI's `TestClient`,
in-process, hitting the actual HTTP layer rather than just the
underlying functions. A screenshot would have been nice to have, not
proof of anything the tests didn't already cover.

## 2026-10-15 — Loading Pagila as a richer demo dataset

`load_demo_dataset.py`'s synthetic data was never meant to be a
credible demo or a basis for calibrating `APP_COST_THRESHOLD` -- it's
a handful of hand-written rows. Arthur agreed to use Pagila instead (a
real, widely-known public Postgres sample database: a DVD-rental
store with 16+ tables, ~1,000 films, ~1,000 customers, ~50,000
rentals), so `scripts/load_pagila_dataset.py` downloads the upstream
schema and data dumps and loads them into a target database. Three
real bugs, each found only by actually running it against a live
Postgres container, not by reading the dump files first:

1. The upstream schema defaults UUID columns to `uuidv7()`, a Postgres
   18+ builtin. The project's test database is Postgres 16. Patched to
   `gen_random_uuid()` (core since Postgres 13) -- same shape of UUID,
   just without `uuidv7`'s time-sortable ordering, which nothing here
   depends on.
2. One generated column is declared `VIRTUAL` (also Postgres 18+
   only). Patched to `STORED` (supported since Postgres 12) -- a
   storage/performance tradeoff, not a semantic one.
3. The schema also defines an optional `film_embedding` table (a
   20-dim vector column plus an HNSW index) behind `CREATE EXTENSION
   vector`. `pgvector` isn't installed on the plain `postgres:16-alpine`
   image this project uses, and nothing in the MCP server does
   embeddings or vector search, so rather than add an extension purely
   to load sample data that would never be used, the loader parses the
   schema into individual statements (a small hand-written splitter
   that's aware of dollar-quoted function bodies, `--` comments, and
   string literals, since a naive split on `;` would corrupt both
   pg_dump's own multi-semicolon comment headers and every PL/pgSQL
   trigger body) and drops any statement that mentions the extension
   or the table.

That filter's first version matched the plain substring `"vector"`,
which also matched `tsvector` -- the real `film` table's full-text
search column type -- and silently deleted the entire `CREATE TABLE
film` statement along with it, surfacing one migration-batch later as
a confusing `relation "public.film" does not exist` error on an
unrelated `ALTER TABLE`. Fixed by matching `vector` as a whole word
(`\bvector\b`) instead, which doesn't match inside `tsvector` or
`tsvector_update_trigger` but still catches `CREATE EXTENSION vector`
and the `vector(20)` column type. `film_embedding` stays a plain
substring match on purpose -- constraint and index names like
`film_embedding_pkey` and `film_embedding_embedding_hnsw_idx` need it.

A fourth bug, in the data-loading half: asyncpg's `copy_to_table`
treats its `source` argument as a file *path* unless it has a `.read()`
method -- passing the already-downloaded row data as plain `bytes`
made it try to `open()` the data itself as a filename, which failed
with "File name too long" (not a permissions or missing-extension
error, so it took a moment to place). Fixed by wrapping the bytes in
`io.BytesIO()` first.

Verified end to end against the disposable `jarvis-mcp-test-db`
container: full schema applies cleanly, `film_embedding` is
confirmed absent afterward, row counts land where the upstream dump
says they should (1,000 films, 999 customers, ~51,800 rentals, ~51,000
payments), and a real multi-table join (film → category → inventory →
rental) returns sensible results. `film_embedding`'s own data rows are
skipped the same way on the COPY side, so nothing is left trying to
load into a table that no longer exists.

## 2026-10-15 (continued) — Pointing it at the real target DB, scrubbed first

Arthur's real `APP_TARGET_DATABASE_URL` turned out not to be empty --
42 leftover tables from an old, no-longer-used retail/POS schema
(Indonesian table names: `Barang`, `Pelanggan`, `Penjualan`, etc).
Confirmed unused, confirmed fine to scrub. `DROP SCHEMA public CASCADE;
CREATE SCHEMA public;` cleared it in one step.

It's a real managed/pooled Postgres host (behind what's almost
certainly PgBouncer, on port 6432), not a plain local container, and
that surfaced three more real bugs the local Docker test never could:

1. The connecting role has `rolcreaterole = false` -- a normal
   restriction on a managed, non-superuser account. This means
   `scripts/provision_readonly_role.py` can't run against this host
   with the current credential: there's no dedicated read-only role
   here yet, just the one account that owns everything. Flagged, not
   fixed -- needs either a second admin-ish credential from whoever
   manages the host, or accepting this target has only the app-level
   validator as its defense for now, not the DB-level role too.
2. pg_dump bakes `ALTER ... OWNER TO postgres;` onto every single
   object (106 of them) -- fine on the local test container, where the
   connecting superuser genuinely is named `postgres`, but a hard
   failure here (`must be able to SET ROLE "postgres"`) since this
   account can't reassign ownership to a role it doesn't control.
   Dropped those statements outright: whoever runs the `CREATE` already
   owns the object, so they were never doing anything useful anyway.
3. The data file's own pg_dump preamble sets
   `transaction_timeout = 0`, a GUC that doesn't exist before Postgres
   17 -- it was dumped from a newer server (17.10) than the schema file
   (12.11) and this project's own target (16). Stripped that one line.

Fixing bug 2 meant rethinking the original design: the schema and data
files were being run through two *separate* connections (`psql` for
the schema, a raw `asyncpg` connection for the data's `COPY` blocks).
Right after the schema succeeded, the very next `asyncpg` connection
failed with `relation "actor" does not exist` -- on a database that
provably had that table, seconds old. Chased this as a pooler
replica-lag theory first and wasted real time on it before checking
the obvious thing: `SHOW search_path;` on a fresh connection came back
*empty*. pg_dump's own preamble runs
`SELECT pg_catalog.set_config('search_path', '', false);` -- completely
harmless inside the schema's own session, since every one of its
`CREATE` statements is already schema-qualified -- but this host's
connection pooler isn't resetting session state between reused
connections, so that empty search_path leaked straight onto the next
unrelated connection. Confirmed by running `RESET search_path;` across
a batch of fresh connections and watching `SHOW search_path` settle
back to normal.

That's a real, load-bearing bug, not just an inconvenience: left
alone, it would eventually have leaked onto the *live app's own*
connection to this same target database, breaking every unqualified
query (`SELECT * FROM film`, exactly what an AI client would actually
send) with "relation does not exist" at some unpredictable point
after a deploy, for a reason that would've looked nothing like its
actual cause. Fixed two ways: merged the schema and data loading into
one continuous `psql` session instead of two separate connections
(pg_dump's COPY format is designed to be fed straight to `psql`
alongside the schema -- the separate `asyncpg` COPY path was never
necessary), and appended an explicit `SET search_path TO public;` as
the session's last statement, so the connection is handed back to the
pool clean instead of trusting every future caller to remember to
reset it themselves.

Re-verified the complete fix against the real target: scrubbed,
reloaded, then checked `SHOW search_path` across eight fresh
connections with no manual cleanup in between -- all came back
`public`. Re-ran the same loader against the local disposable
container too, to confirm the single-session rewrite didn't regress
the case that already worked.

## 2026-10-15 (continued) — Calibrating APP_COST_THRESHOLD against real data

`APP_COST_THRESHOLD` (100,000) had been a round, made-up number since
Phase 6 -- the whole reason to load Pagila instead of
`load_demo_dataset.py`'s thin synthetic data was to finally have
something real to measure against. Ran `EXPLAIN (FORMAT JSON)` (no
execution, just the planner's own cost estimate) against the real
Pagila-loaded target for two groups of queries: realistic exploratory
ones an AI client would actually send, and deliberately wasteful ones
meant to represent what the check exists to catch.

Legitimate queries measured (Postgres planner cost units, not seconds
or dollars):

| query | cost |
|---|---|
| `SELECT * FROM film LIMIT 10` | 0.8 |
| `SELECT COUNT(*) FROM customer` | 44.5 |
| filter film by rating | 80.5 |
| top customers by rental count (2-join + GROUP BY) | 1,603.7 |
| revenue per customer, all partitions, no LIMIT | 1,664.1 |
| `SELECT *` the entire 51,805-row rental table, no LIMIT | 1,122.0 |
| top films by category (5-table join + GROUP BY), LIMIT 10 | 21,169.9 |
| same 5-table join + GROUP BY, no LIMIT at all | 18,520.1 |

Deliberately wasteful queries measured:

| query | cost |
|---|---|
| `address` x `city` cross join (~600K rows) | 7,557.0 |
| `film` x `customer` cross join (~999K rows) | 12,610.0 |
| `customer` self cross join (~998K rows) | 12,561.5 |
| `inventory` x `customer` cross join (~4.6M rows) | 57,325.5 |
| `inventory` x `inventory` cross join (~21M rows) | 262,482.6 |
| `film` x `film` x `film` cross join (~1B rows) | 12,512,739.0 |
| `rental` x `payment` cross join (~2.6B rows) | 33,067,840.4 |
| `rental` self cross join (~2.68B rows) | 33,549,348.9 |

The real finding, not anticipated going in: **cost doesn't track
"wastefulness" the way intuition suggests.** The heaviest *legitimate*
query (the 5-table join with a GROUP BY, 21,169.9) costs more than
several of the *wasteful* cross joins (7,557–12,610), because its cost
comes from touching Pagila's actual larger tables through several real
joins plus a sort for GROUP BY, while a cross join of two small
dimension tables (film: 1,000 rows, customer: 999 rows) is cheap in
planner terms almost by definition -- not enough base rows on either
side for the multiplication to get expensive yet. No single threshold
can both admit the legitimate query and reject those three cross
joins; one of those has to give.

Checked whether that gap is an actual availability risk before
accepting it: timed the worst offender in the blind spot (`film` x
`customer`, ~999,000 output rows) for real. It ran in **0.2 seconds**
against the real remote target, over the network. At Pagila's scale, a
cross join confined to small dimension tables isn't slow -- it's just
more rows than useful, which is a nuisance, not a denial-of-service
vector. `APP_QUERY_TIMEOUT_SECONDS` (the actual execution-time backstop,
separate from this pre-execution cost estimate) would never even
trigger for it.

Set `APP_COST_THRESHOLD=40,000`: a little under 2x headroom above the
real legitimate ceiling (21,169.9), and comfortably below the point
where cross joins start actually mattering (57,325.5, the first one
touching a table with thousands of rows rather than hundreds) --
everything from there up to the catastrophic cases (millions) is
caught cleanly. The accepted, documented gap: a cross join confined to
tables in the hundreds-of-rows range slips under 40,000 -- proven
harmless at Pagila's current scale by the timing test above, not just
assumed. Worth re-measuring if the target dataset's dimension tables
ever grow by an order of magnitude or more.

## 2026-10-15 (continued) — Running Slice E's benchmark for real

Everything up to this point had only run against PydanticAI's free
`TestModel` stub. First real run, against `gpt-5-mini` via Sumopod:
**3/10 (30%)**. Didn't take that number at face value -- half the
failures came back with `actual SQL: None`, meaning the harness never
even captured a query, not that the model got one wrong. That's a
different kind of failure and worth chasing before reporting an
accuracy number built partly out of it.

Set up for the run first: a dedicated local demo database
(`benchmark_demo`, loaded via `load_demo_dataset.py` -- the toy
storefront schema these eval questions target, not Pagila), a seeded
workspace pointing at it, and `alembic upgrade head` against the new
local bookkeeping DB (`jarvis-mcp-app-db` from the pytest fix earlier)
-- its tables never existed outside of pytest's own
create-then-drop-per-test fixture until now.

Dug into one `None` case directly (dumped the agent's full message
history, not just the final summary) and found the real cause:
`APP_DEMO_RATE_LIMIT` (5 requests per workspace per hour) is sized for
occasional real usage, not an automated loop that burns 2-4+ tool
calls (`list_tables`, `describe_table`, `run_query`, sometimes retried)
per question. By question 2 or 3 of 10, every further `run_query` call
came back `{"status": "rejected", "error": "rate limit ... exceeded"}`
-- and the model, correctly, reported that it couldn't get a real
answer rather than making one up. Good model behavior, bad benchmark
setup: the 30% number was mostly measuring the rate limiter, not the
model. Fixed by temporarily raising `APP_DEMO_RATE_LIMIT` in `.env` for
the duration of this run only, then reverting it -- this is a real
production anti-abuse setting, not something to weaken permanently for
a benchmark's convenience.

With that cleared, two of the remaining questions still failed with a
real `actual_sql` captured, which pointed at a second, genuine bug
rather than a rate-limit artifact. One of them -- "total revenue" --
had an answer that LOOKED numerically identical to the ground truth
(387684.66 both ways, confirmed by hand in psql) but still graded as
wrong. Chased it by dumping the tool call's raw return value directly
and found: `value='387684.66' type=str`. The "expected" side comes
from a direct DB connection and keeps Decimal as Decimal; the "actual"
side comes back through a real MCP tool call, and Decimal isn't a
JSON type, so it arrives as a string. `Decimal("387684.66") ==
"387684.66"` is `False` in Python -- this would have failed EVERY
numeric-answer question, independent of whether the agent's SQL was
right, and the existing grading unit tests never caught it because
their fixtures matched types on both sides by construction, never
exercising a real MCP round-trip. Fixed in
`app/benchmark/grading.py` (`_normalize_value`, parses anything
number-shaped into a `Decimal` on both sides before comparing) with a
regression test built from this exact failure
(`test_decimal_serialized_as_string_still_matches`).

Re-ran the full benchmark with both fixes in place: **7/10 (70%)** --
a real, credible number this time, not one partly measuring
infrastructure. The three remaining misses, checked individually, are
grading-methodology limits, not the model being wrong:

- "Which country has the most customers?" and "What is the name of the
  most expensive product?" -- the agent's SQL was correct both times,
  but it included one extra, genuinely useful column (the count, the
  price) alongside the answer the question asked for. `rows_equal`
  compares full row shape, not just the asked-for column, so a more
  informative answer than the ground truth's grades as wrong. Already
  a documented tradeoff (`test_column_order_matters_documented_limitation`
  covers the same underlying behavior), not a new bug.
- "What is the average price of products in the 'books' category?" --
  the agent rounded to 290.65 for readability; `expected_sql` doesn't
  round, giving 290.6512500000000000. Numerically different, humanly
  the same answer. No rounding tolerance exists in `rows_equal` --
  documented as a known, deliberately-unfixed gap in
  `app/benchmark/grading.py`'s docstring, since "how much tolerance"
  is a real judgment call, not something to guess into a quiet fix.

## 2026-10-15 (continued) — Pushing the benchmark to 100%, for real

Arthur asked whether the 3 remaining misses could become a true
10/10, not just an explained 70%. All three were grading-methodology
false negatives, not the model being wrong -- fixing a methodology to
actually measure what it claims to measure is a legitimate
improvement, not gaming the number. Two changes to
`app/benchmark/grading.py`:

1. **Row matching became subsequence-based, not exact-tuple.** An
   actual row is now allowed extra columns beyond what the expected
   row asked for, as long as every expected value still appears, IN
   ORDER. Deliberately still order-sensitive within that check --
   `(5, "a")` is still not a match inside `("a", 5)` -- so this
   doesn't quietly undo `test_column_order_matters_documented_limitation`,
   which still passes unchanged. Verified with a new negative test
   (`test_extra_column_with_wrong_answer_still_fails`) that this isn't
   a license to ignore an actually-wrong answer.
2. **A numeric tolerance (0.005) for Decimal comparisons.** Covers an
   agent rounding "290.6512500000000000" to "290.65" for a human to
   read -- the right thing for it to do, previously punished for it.
   Verified with a new negative test
   (`test_large_numeric_difference_still_fails`) that this doesn't
   swallow a genuinely wrong number.

Re-ran the real benchmark against the real model: **10/10 (100%)**.
Every one of the three previously-failing questions passed this time,
each for exactly the reason predicted -- no new, different failures
appeared.

A real environment gotcha surfaced setting up the re-run, worth
knowing about if any of this gets repeated: running the full `pytest`
suite against the SAME local bookkeeping database used to manually
seed the benchmark workspace silently wiped that workspace.
`tests/conftest.py`'s `db_session` fixture does
`Base.metadata.drop_all()` as teardown -- correct and intentional for
an isolated test database, but `Base.metadata` only contains tables
for model classes actually imported during that pytest run. Since
nothing in the main test suite imports `app.benchmark.models`,
`eval_cases` and `benchmark_results` survived the drop while
`workspaces`, `api_keys`, and everything else vanished -- and because
`alembic`'s own version table (never part of `Base.metadata`, so also
untouched) still said the schema was "up to date", a follow-up
`alembic upgrade head` did nothing, silently leaving a half-gone
schema until manually dropping everything and reapplying from a clean
slate. Not a bug in the fixture -- its teardown behavior is correct
for its actual job -- but a real trap for using that same local
database for anything else in between test runs.

## 2026-10-15 (continued) — The MCP server can finally be reached over a network

Found, while assessing what's left before this is deployable, that
there was no way to actually run the MCP server at all -- no `mcp.run()`,
no entrypoint anywhere, only an importable object exercised through
in-memory streams in tests. Decided on Streamable HTTP (not stdio,
which can't be reached remotely; not SSE, the older transport),
mounted into the same FastAPI app as the console at `/mcp` rather than
a second process, since this project's whole ethos is staying small
enough to read end to end.

Also moved auth to a real `Authorization: Bearer <key>` header,
additively -- each tool now takes an optional `ctx: Context`
parameter and prefers `ctx.headers`'s header when present, falling
back to the existing `api_key` argument otherwise. `ctx.headers` is
`None` on stdio/in-memory transport, so this doesn't disturb anything
that already worked: `test_mcp_protocol.py`'s in-memory test and
`scripts/run_benchmark.py`'s own key-injecting wrapper both keep
passing `api_key` exactly as before. The point of the header path is
that a real HTTP client (Claude Desktop, etc.) can now authenticate at
the connection level instead of needing a custom wrapper to stuff a
credential into every tool call.

Getting this actually working took four real, sequential bugs, each
found by actually running it, not by reading the SDK's source and
assuming:

1. **Wrong lifespan.** `mcp.streamable_http_app()` returns its own
   Starlette app with its own lifespan (a background session-manager
   task it needs started). `app.mount()` does NOT run a mounted
   sub-app's lifespan -- Starlette only sends the ASGI "lifespan"
   protocol to the root app. Without fixing this, `/mcp` would accept
   a connection and then hang forever, since nothing was ever running
   to handle it. Fixed with an explicit combined lifespan on the
   parent FastAPI app that does `async with mcp.session_manager.run():`.
2. **Double-mounted path.** `mcp.streamable_http_app()` defaults its
   own internal route to `/mcp`, and mounting that sub-app at `/mcp`
   again made the real path `/mcp/mcp` -- a plain 404 on the path
   anyone would actually try first. Fixed by passing
   `streamable_http_path="/"` so the FastAPI mount prefix is the only
   `/mcp` in the final path.
3. **DNS-rebinding protection rejected the test's own Host header.**
   The SDK auto-enables this for a loopback-bound server (the default
   `host="127.0.0.1"`), restricting the allowed `Host` header to
   `127.0.0.1`/`localhost`/`::1` -- *with an explicit port*, confirmed
   by reading `transport_security.py`'s actual match rule
   (`host.startswith(base_host + ":")`, so a bare `localhost` with no
   port doesn't match `"localhost:*"` either). Not a bug -- real DNS
   rebinding protection working as intended -- just something the
   test's own base URL had to actually satisfy (`http://localhost:8000`,
   not `http://testserver`, httpx's usual placeholder).
4. **A genuinely pre-existing, unrelated process already on the first
   port tried for the real-socket smoke test** (port 8123, running
   since August, nothing to do with this project). Not touched;
   picked a different port instead.

Verified three separate ways, each proving something the last one
didn't: an in-memory-streams test (the protocol itself, pre-existing);
a new test using httpx2's ASGI transport against the app's actual
mounted route with a real `Authorization` header and zero `api_key`
argument (`test_tool_calls_over_real_http_transport_with_header_auth`
in `tests/test_mcp_protocol.py`) -- no real socket, but real ASGI HTTP
semantics; and a real `uvicorn` process on a real TCP port, hit with
the MCP SDK's own HTTP client over an actual socket -- `list_tables`,
a rejected bad key, and a real `run_query` all confirmed working, not
assumed. Cleaned up every throwaway workspace/database this created
afterward.

## 2026-10-15 (continued) — A Node-free way to point Claude Desktop at it

Walking through how Arthur would actually connect Claude Desktop to a
deployed instance, the standard answer is `mcp-remote`, a Node-based
CLI bridge that forwards a local stdio client to a remote HTTP server
with custom headers. Asked for an alternative -- not everyone wants a
Node dependency for one thing in an otherwise all-Python project.

Checked first whether the `mcp` SDK already ships something like this
(`pip show -f mcp`'s entry point, `mcp.cli`) -- it doesn't; `dev`,
`run`, and `install` are all about running/registering a *local*
server, nothing about bridging to a remote one.

Wrote `scripts/stdio_bridge.py` instead: a ~45-line script using the
SDK's own `streamable_http_client` (outbound leg, with the API key as
a Bearer header) and `stdio_server` (inbound leg, what Claude Desktop
actually talks to), joined by two concurrent forwarding loops. The one
real wrinkle: `ReadStream` can yield either a real message or an
`Exception` (a malformed message on that side), while `WriteStream`
only accepts real messages -- blindly forwarding whatever comes out of
read into write would try to `.send()` an Exception object. Raises
instead of forwarding it in that case.

Verified by actually spawning it as a subprocess and driving it with
the MCP SDK's own `stdio_client` -- exactly what Claude Desktop does,
not an approximation of it -- against a real running server: a
`list_tables` call succeeded and a `describe_table` call for a
nonexistent table correctly came back as an error, both carried
correctly across the bridge. Cleaned up the throwaway workspace and
database afterward.

## 2026-10-19 — PgBouncer broke the bookkeeping engine on first real VPS use

First attempt to point `APP_DATABASE_URL` at a real pgbouncer-fronted
Postgres (reusing the Sumopod account that already hosts the Pagila
target, for the VPS deploy) failed immediately, on literally the first
query `app/db.py`'s engine ever runs (`select pg_catalog.version()`,
part of SQLAlchemy's own connection setup):

```
asyncpg.exceptions.DuplicatePreparedStatementError: prepared statement
"__asyncpg_stmt_1__" already exists
HINT: pgbouncer with pool_mode set to "transaction" or "statement"
does not support prepared statements properly... you can set
statement_cache_size to 0 when creating the asyncpg connection object.
```

This had never shown up before because every prior real use of the
Sumopod pgbouncer connection (`load_pagila_dataset.py`, the ad hoc
verification queries throughout this project) either used `psql`
directly or a single short-lived `asyncpg.connect()` call -- never
`app/db.py`'s actual engine, which is `NullPool` (opens a genuinely
new physical connection on every checkout, by design -- see its own
comment) kept alive across a whole running process. Reproduced the
exact failure directly, before trusting the asyncpg hint text alone: 5
separate connection checkouts against the real Sumopod database, no
fix, 4th one failed with the identical error; same 5 checkouts, with
`connect_args={"statement_cache_size": 0}` added, all 5 succeeded.
Root cause, concretely: PgBouncer's transaction/statement pooling
routes different client connections to different backend server
connections, asyncpg names its first prepared statement
`__asyncpg_stmt_1__` on every connection it opens, and when PgBouncer
happens to reuse the same backend for two different client
connections, the second one collides with a prepared statement the
backend still remembers from the first. Fixed in `app/db.py` by
disabling asyncpg's prepared statement cache -- confirmed via the same
reproduction that it's a real fix, not a guess at the hint text.

This would have affected ANY deploy pointing `APP_DATABASE_URL` at a
pgbouncer-fronted Postgres, not just this one -- the bug was latent in
`app/db.py` since the very first commit, just never exercised until
now because the bookkeeping database had only ever been plain,
unpooled Postgres (local Docker, both on this machine and anywhere
else this project has run) until this deploy attempt.

## 2026-10-19 (continued) — "421 Misdirected Request" on the first real external request

With the PgBouncer fix deployed, the server on the VPS actually ran
and `http://mcp.my.id:8000/healthz` worked from outside. The MCP
endpoint itself didn't: every external request to `/mcp/` came back
`421 Misdirected Request`, where a local request to the identical code
on `127.0.0.1` worked fine.

Root cause: `mcp.streamable_http_app()`'s own DNS-rebinding protection
auto-enables whenever `host` isn't explicitly set, and defaults its
`allowed_hosts` to `127.0.0.1`/`localhost`/`::1` only (confirmed
directly in the SDK's own source,
`mcp/server/lowlevel/server.py:streamable_http_app`). `app/main.py`
never passed a `host=` or `transport_security=` of its own, so every
request carrying `Host: mcp.my.id` -- which is every real external
request once DNS actually points there -- got rejected by a security
feature that was silently scoped to loopback-only and never told
otherwise.

Added `APP_MCP_ALLOWED_HOSTS` (`app/config.py`) and wired it into an
explicit `TransportSecuritySettings` in `app/main.py`, so a real
deployment can name its actual domain. Verified both directions
directly, not just reasoned about: a request with `Host: mcp.my.id`
and `APP_MCP_ALLOWED_HOSTS=mcp.my.id` set now gets the correct
JSON-RPC "Missing session ID" response (what a real client actually
expects); the same request with `APP_MCP_ALLOWED_HOSTS` unset still
gets `421`, confirming the fix is opt-in and doesn't quietly weaken
the default (loopback-only) protection for anyone who hasn't
configured it.

## 2026-10-20 — The PgBouncer bug was never just in app/db.py

First real tool call through the fully-deployed server (both earlier
fixes live, Claude Code genuinely connected over the real domain) --
`list_tables` failed with a generic, detail-free "Error executing tool
list_tables". Reproduced it directly rather than guess from the vague
message: same error, calling the tool the exact same way Claude Code
does (header auth, real deployed URL).

The PgBouncer fix from two days ago (`a0a0193`) only touched
`app/db.py`'s *bookkeeping* engine -- but `app/query/service.py` and
`app/query/schema_cache.py` each independently call
`create_async_engine(data_source.connection_url)` to reach the
*target* database, with no `connect_args` at all, four call sites
total. The target database is the exact same PgBouncer-fronted Sumopod
account as the one that broke the bookkeeping engine -- so the
identical `DuplicatePreparedStatementError` was always latent here
too, just never exercised by a real tool call against the real target
until today. (It also explains why nothing caught this earlier:
`test_mcp_protocol.py`'s in-memory test and `run_benchmark.py` both
run against a plain local Postgres, never a PgBouncer-fronted one.)

Fixed once, centrally, instead of patching four call sites with the
same `connect_args` literal: added `create_target_engine()` to
`app/db.py`, and pointed all four at it. Verified with the same
reproduction technique as the original bug -- 5 separate connection
checkouts against the real target database, all succeeding -- not just
assumed that mirroring the earlier fix would obviously work.

Side note from diagnosing this with help from another session running
directly on the VPS: it initially concluded the *other* fix
(`APP_MCP_ALLOWED_HOSTS`, from earlier the same day) was "dead
configuration that nothing read" and rewrote it independently. The
real explanation was simpler -- that VPS checkout was 5 commits behind
`origin/main`, sitting on code from before that fix existed at all.
Caught before it was committed, by asking for the actual `git diff`
instead of accepting a plausible-sounding root-cause story at face
value -- the diff immediately showed a field type (`list[str]`) that
didn't match what was actually pushed, and the commit-distance gap
explained the whole discrepancy. Discarded the divergent reimplementation
and pulled the real one instead of ending up with two different fixes
for the same bug in two places.

## 2026-10-20 (continued) — Bookkeeping and target were the same database

Fixing the target-side PgBouncer bug above surfaced something the
error message itself didn't mention, buried in an aside about the
`list_tables` output: the fixed call returned the real Pagila tables
*and* this project's own bookkeeping tables (`workspaces`, `api_keys`,
`query_executions`, ...) side by side. `APP_DATABASE_URL` and
`APP_TARGET_DATABASE_URL` on the VPS had been pointing at the exact
same Sumopod database the whole time -- the "create a new database for
bookkeeping" step from the original deploy walkthrough never actually
happened there.

This is exactly the failure THREAT_MODEL.md's separation between
these two databases exists to prevent: any AI client calling
`run_query` could read API key hashes, console password hashes, and
the complete audit log of every query anyone has ever run through this
server, through the very tool meant to be sandboxed to Pagila alone.
Not a theoretical gap -- confirmed directly, the bookkeeping tables
were right there in a real `list_tables` response.

Fixing it took a real pivot partway through: the planned fix (create a
second database on the same Sumopod account) failed -- that hosting
role has no `CREATEDB` privilege, confirmed directly rather than
assumed. Installed a local Postgres 16 on the VPS instead, which is
arguably the better architecture anyway (the app's own bookkeeping
data living on the machine it actually runs on, not a second database
on the same remote shared host as the data it's protecting against).
Migrated the real data across with `pg_dump --data-only` (table list
caught two gaps on review before running it -- `eval_cases` and
`benchmark_results` were missing from both the copy and the planned
`DROP TABLE` list, which would have left the drop looking "incomplete"
afterward even though the real security-sensitive tables were handled
correctly) rather than discarding and reseeding, so the already-
configured API key kept working with zero reconfiguration needed.
Verified `list_tables` against the live server afterward -- Pagila
only, 70 tables, nothing from the bookkeeping schema -- before dropping
the bookkeeping tables from the shared database, not before.

Two more real things found and fixed in the same pass, neither of them
guessed at:

- **A genuinely duplicate workspace.** Re-running `seed_workspace.py`
  with the same `--name` (not idempotent -- see the Slice E section
  much earlier in this file for the first time that bit) had created a
  second "vps-deployment" workspace, both with console username
  "arthur". `verify_console_login()`'s `scalar_one_or_none()` doesn't
  tolerate more than one match -- it was raising `MultipleResultsFound`
  on every single console login attempt, for either workspace, not
  returning a clean wrong-password response. Fixed two ways: deleted
  the stale duplicate (unused, still had the broken bare-scheme
  connection URL, zero real queries against it), and added a real
  database-level unique constraint on `console_credentials.username`
  (migration `1dc23f88f669`) so this specific failure mode can't come
  back regardless of which script creates a credential row next time.
  Verified the constraint actually rejects a real duplicate insert, not
  just that the migration applies cleanly.
- **The "stale process" confusion from earlier today, actually
  explained.** Every restart today used `nohup uv run uvicorn ... &`,
  the same pattern that kept appearing to leave old processes running
  even after a `kill`. Root cause: a process can close its listening
  port on SIGTERM while still not actually exiting, if it's stuck
  waiting on something long-lived (here, probably an open SSE
  connection) -- `ss -tlnp` would show the port as free while the old
  process was still technically alive. Fixed properly, not by trying
  to out-guess that race by hand again: the server now runs under a
  real systemd unit (`mcp-backbone`, `Restart=on-failure`, enabled at
  boot), so a restart means `systemctl restart mcp-backbone`, not a
  manually-tracked PID and a background shell job.

Also set up: forced `+asyncpg` for any target connection URL
(`create_target_engine`, app/db.py), closing off the bare-scheme bug
at the source instead of only at the one data row that happened to hit
it (new tests: `tests/test_db.py`); a nightly `pg_dump` backup of the
new bookkeeping database (cron, 14 dumps kept); `.env.bak-*` added to
`.gitignore` (a real backup file this exact migration created, holding
real credentials, must never be committed).

Open gap, not addressed yet: no off-server copy of the bookkeeping
backups exists -- if the VPS itself is lost, the local `~/backups/`
directory goes with it.

## 2026-10-21 — Off-server backups for the bookkeeping database

Closed that gap. The nightly backup script on the VPS now encrypts each
dump with `age` (to a public key; the matching private key is kept only
off the VPS, so a lost VPS can't take the means of decrypting with it)
and uploads it to an S3 bucket. It keeps the last 14 locally and the
last 14 in S3.

The first draft of the script (written for Backblaze B2, before
switching to S3) was blocked by the VPS session's safety check as a
"mass delete" risk. That was a real bug, not a false alarm: its remote
cleanup listed *every* file in the bucket and deleted all but the
newest 14, so anything else stored there, or a typo pointing at a
different bucket, would have been wiped. Fixed by matching only this
script's own filename pattern under its own `bookkeeping/` prefix, and
deleting one named file at a time. The same review caught two smaller
issues worth keeping: credentials passed inline in the rclone remote
string were readable by any user via `ps` (moved to exported
`RCLONE_S3_*` env vars), and a missing `.env` value would only have
failed partway through (now checked up front, before dumping
anything).

Getting the first upload through took two more real fixes, each found
from the actual AWS error, not guessed: a 41-character secret key
(AWS secrets are exactly 40; one extra character from the paste)
causing `SignatureDoesNotMatch`, and an IAM policy missing
`s3:ListBucket` on the bucket ARN itself (separate from the object
ARN). Verified by listing the bucket afterward: the encrypted dump was
there, same size as the local copy.

Still open: no test restore yet. Until someone decrypts one of these
with the off-VPS private key and runs `pg_restore --list` on it, it's
unproven that the backups can actually be restored.

## 2026-10-21 (continued) — Test restore passed, and found an unapplied migration

Downloaded the first S3 copy to the Mac, decrypted it with the private
key kept off the VPS, and did a full restore into a throwaway local
Postgres, not just a `--list`. All 10 bookkeeping tables came back with
their data. The strongest check: the restored `api_keys.key_hash`
matches the hash of the live API key, so a VPS rebuilt from this backup
would accept the existing key, and Claude Code's config wouldn't need
touching.

Two things the restore turned up:

- `pg_restore` 18 (on the Mac) sends `SET transaction_timeout = 0`,
  which a Postgres 16 server rejects. It's the same version gap as the
  Pagila load. `pg_restore` skips that one statement and carries on,
  so it's harmless, but it makes the exit code 1 on an otherwise good
  restore. Use a matching client version, or ignore that one error.
- **The restored `alembic_version` was `072ff1f9b9d6`, not
  `1dc23f88f669`.** The unique constraint on
  `console_credentials.username` had been committed, pushed and pulled
  to the VPS, but `alembic upgrade head` was never run there, so the
  production database never got it. Pulling code doesn't apply
  migrations. A restore test checking the actual schema, not just "did
  the file decrypt", is what caught it.

Small operational note: `age` refuses an identity file containing
anything besides comments and the key, so the key file should hold the
key and nothing else, not other credentials alongside it.

## 2026-10-21 (continued) — HTTPS

Until today the deployed server ran over plain HTTP, so the bearer API
key crossed the internet unencrypted. The obstacle to fixing it:
Let's Encrypt normally proves domain ownership over port 80 or 443, and
on this VPS both belong to an unrelated site's Caddy container. Four
options were weighed: renewing a manual DNS-01 cert by hand every ~60
days, routing through that other site's Caddy (breaking the separation
chosen earlier), moving onto a private network (ruling out anyone else
ever connecting), or moving the domain's DNS to Cloudflare. Cloudflare
won: its DNS API lets certbot pass a DNS-01 challenge and renew itself
forever, with no manual steps and nothing shared with the other site.

What actually happened, mistakes included:

- **The moved DNS record defaulted to "Proxied".** As soon as the
  nameservers switched, `mcp.my.id` resolved to Cloudflare's own IPs,
  whose proxy doesn't forward port 8000, so the live server became
  unreachable through its domain while working fine by IP. Caught by
  checking Cloudflare's authoritative nameserver directly rather than
  trusting a cache; fixed by setting the record to "DNS only". It needs
  to stay that way: a proxied record would let Cloudflare read the
  bearer key and would cut long-lived MCP connections.
- **No proxy in front at all.** uvicorn terminates TLS itself on port
  8443. The cert lives in `/etc/letsencrypt/live`, which the service
  (running as `ubuntu`) can't read, so a certbot deploy hook copies it
  to `/etc/mcp-backbone/tls/` and restarts the service on each
  renewal (`deploy/certbot-deploy-hook.sh`).
- **ufw was a red herring.** The plan included "open 8443 in ufw";
  checking first showed ufw was inactive all along. The firewall that
  matters is the cloud provider's security group, and an outside probe
  (instant "connection refused" on 8443 rather than a timeout) showed
  it already allows the port. It also showed that security group is
  wide open, now recorded as an open gap.
- **A copy-pasted `!` silently skipped the restart.** The unit edit
  went in, but the reload/restart never ran: the command had been
  pasted with a leading `! ` (Claude Code's shell-escape prefix), which
  in plain bash negates the exit status, so `&&` skipped everything
  after it. Found by reading the VPS's sudo log and shell history, not
  by guessing.
- **Restarts hung for 90 seconds.** uvicorn waits for open MCP
  streaming connections to close before stopping; systemd only kills
  it at its 90s timeout. `--timeout-graceful-shutdown 5` added to the
  unit in the repo.

Verified from outside, not just from the server: a publicly trusted
Let's Encrypt cert for `mcp.my.id`, port 8000 closed, plain HTTP to 8443
refused, real `list_tables`/`run_query` calls over HTTPS with the live
key, and Claude Code re-pointed to `https://mcp.my.id:8443/mcp` and
connected.


## 2026-10-21 (continued) — The deployed console had no JavaScript

Checked the deployed web console for the first time: the login page
loaded, but `/console/static/main.js`, which the dashboard needs for its
live audit-log feed, returned 404. The compiled TypeScript was
gitignored and the VPS has no Node, so nothing had ever built it there.
This was flagged as a deploy risk early on, and had never been tested
until now.

Fixed by committing the compiled file instead of building it on the
server: it's one small esbuild bundle, and committing it means a plain
`git pull` deploys it with no extra toolchain. Before relying on that,
confirmed the build is deterministic (two clean `npm ci` builds were
byte-identical) and that the lockfile includes esbuild's Linux binary,
which CI needs. A new CI job typechecks the TypeScript, rebuilds it, and
fails if the committed `main.js` differs. That check was tested both
ways locally: it passes on matching source, and catches a deliberately
edited `main.ts` with a stale `main.js`.

## 2026-10-21 (continued) — Secure console cookie

The console's session cookie is now marked Secure on the deployment,
via a new `APP_CONSOLE_COOKIE_HTTPS_ONLY` setting (default off). It has
to be a setting, not hardcoded, because a Secure cookie is never sent
back over plain `http://`: turned on locally, login "succeeds" and the
very next page bounces back to the login screen. One new test pins
exactly that behavior (session survives over https, not over http), so
that requirement is documented by the tests, not just here. Another
checks the flag appears on the cookie only when the setting is on.

## 2026-10-22 — Packaged as a Claude Code plugin

Until now, using this from Claude Code meant a hand-typed `claude mcp add`
with the key pasted into a local config, which can't be shared. `plugin/`
is now a real plugin and the repo is its own marketplace. The manifest
declares the server URL and API key as `userConfig`, so Claude Code prompts
for them at install, and the key (marked `sensitive`) goes to the OS
credential store instead of `settings.json`. `.mcp.json` turns those two
values into the HTTP server registration and its Bearer header. The
header auth added for the HTTP transport is what makes this possible
without a wrapper.

Tested by actually installing it, not just validating the manifest. Both
manifests pass `claude plugin validate --strict`. The plugin was installed
from a local marketplace and configured, with the key piped in from `.env`
on stdin so it never appeared in a command line or output. From a folder
where the old hand-added registration isn't loaded, `claude mcp list` showed
`plugin:mcp-backbone:database` connected. That alone proves little,
because the server only checks the key when a tool is called. So a
headless session then called `list_tables` with no `api_key` argument and
got the real Pagila table list back.

The plugin also ships a `query-database` skill. In an end-to-end run ("which
film category has the most films?") it triggered on its own, explored the
schema first, and answered with a single `run_query`. The same run
surfaced a real gap: Claude guessed `describe_table`'s argument as `table`
instead of `table_name` and wasted two calls before correcting itself. The
skill now lists each tool's real argument names.

## 2026-10-22 (continued) — Invite-only signup, and the holes it would have opened

With the plugin installable by anyone, every user would have shared one
workspace's key: no way to cut off one person, one shared rate limit,
and an audit log that can't say who asked what. The fix is a workspace
per person, created through invite-only signup. A new workspace queries
the demo database by default, or the user's own Postgres if they supply
a connection URL.

Letting users supply that URL is exactly what ADR-004 had ruled out for
v1, and its reason still held. The bookkeeping database (audit log, key
hashes) listens only on the VPS's loopback interface, which is where this
server connects from. A signup pointing at `127.0.0.1:5432` would have
aimed the server at its own internals. So a user-supplied URL now goes
through an address guard before it's accepted, and again on every use.
The guard refuses loopback, private, link-local (incl. cloud metadata),
carrier-grade NAT and reserved addresses, IPv4 hidden inside IPv6
(`::ffff:127.0.0.1`), and URL parameters that could quietly add a second
host (`?host=`) or read a local file (`?passfile=`). The invite is
checked before the URL is even looked at, so an uninvited visitor can't
use the signup form to make the server probe hosts. ADR-007 records the
reversal. THREAT_MODEL T7 states the one residual gap (DNS rebinding
between the check and the driver's own lookup) and what bounds it.

Storing other people's database passwords was the second problem.
Connection URLs can't be hashed like API keys, because the server needs
the real password, so they're now encrypted with a key that lives only
in the server's environment. A leaked database or S3 backup holds
ciphertext. The migration encrypts existing rows in place and was run
for real against a scratch database: plaintext in, encrypted out,
decrypts back to the original, downgrade restores it. Run without the
key, it fails with a clear error and changes nothing, not even the
tables created earlier in the same migration. That matters for the VPS:
a missed step there can't leave a half-migrated database.

Reviewing the auth surface for this turned up three gaps that existed
already:

- **`sample_rows` was invisible.** It returns real rows (up to 100 from
  any table) but was neither audited nor rate-limited, so the "every
  attempt is logged" claim was false for it. It now writes an audit row
  per call and has its own allowance (30/hour by default, separate from
  run_query's 5, so exploring doesn't eat the query budget).
- **The console login had no attempt limit**, on a page facing the
  internet. Failed logins and signups now lock out the client IP for 15
  minutes after 5 failures. It's keyed by IP, not username, so nobody can
  lock a real user out by failing logins in their name.
- **A key passed as a tool argument goes through the AI model's
  context** (transcripts, and the model can repeat it). Over HTTP only
  the header is accepted now; the argument still works on local
  transports, which have no headers.

96 tests, up from 69, including each address class the guard must
refuse, a signup through each failure path (each must leave the invite
unused and create nothing), and an end-to-end signup whose key then
authenticates. One test design note: the MCP SDK's session manager can
only be started once per process, so the new "argument refused over
HTTP" check had to live inside the existing HTTP test's server
lifetime. A separate test failed with `run() can only be called once
per instance`.

## 2026-10-22 (continued) — https://mcp.my.id, no port

The VPS's ports 80/443 belong to the unrelated Tamara site's Caddy
container, so getting a portless address meant adding one site block to
that Caddy, reverse-proxying `mcp.my.id` to uvicorn on the host. The
Tamara Caddyfile turned out to be built into its Docker image, not
mounted. Rather than rebuild (which would also redeploy whatever is
currently in the Tamara source tree), the block is appended to the host
file, copied into the running container, validated from a temp path,
and only then hot-reloaded. Tamara never restarted, and its two
domains returned the same 200/301 before and after. Caddy got its own
Let's Encrypt certificate for `mcp.my.id` on the first reload.

The first real deploy: pages loaded through the proxy, but every MCP
request got `421 Invalid Host header`. uvicorn's log showed the rejected
value: `172.18.0.1:8443`. For an HTTPS upstream, Caddy sends the
upstream's own address as the Host header by default, not the visitor's.
The console pages don't check the host, so they hid this; only the MCP
endpoint's DNS-rebinding check caught it. It would also have made the
signup page tell new users to connect to `https://172.18.0.1:8443/mcp/`.
`header_up Host {host}` fixed it. Also verified live afterwards: the
same uvicorn log shows real visitor IPs, not the Caddy container's. So
the `--forwarded-allow-ips` setting works, and login lockouts are per
visitor rather than shared by everyone behind the proxy.

Verified from outside after the fix: `list_tables` works through both
`https://mcp.my.id/mcp/` and the old `:8443` address. A key sent as a
tool argument instead of the header is refused over the real internet,
not just in tests. The laptop's plugin now points at the new address
and connects.
