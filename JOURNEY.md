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

