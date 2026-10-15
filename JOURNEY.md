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

