# Database MCP Server

A standalone, read-only database MCP server that lets an AI client explore
a database's schema and run safe, read-only SQL queries against it — with
every attempt validated, cost-checked, and logged. Built to be small enough
to read end to end, not as a piece of a larger platform.

A guided tour of the codebase -- what each file does and why, following
how a real request actually flows through it -- is in
[ARCHITECTURE.md](ARCHITECTURE.md). Start there if you're new to this repo.

## Planning

The full phase-by-phase planning trail (problem, requirements, domain model,
flows/failures, architecture/ADRs, risks/skeleton, sliced work plan) lives
alongside this README:

- [phase-1-problem-brief.md](phase-1-problem-brief.md)
- [phase-2-requirements-constraints.md](phase-2-requirements-constraints.md)
- [phase-3-domain-model.md](phase-3-domain-model.md)
- [phase-4-flows-failures.md](phase-4-flows-failures.md)
- [phase-5-architecture-decisions.md](phase-5-architecture-decisions.md)
- [phase-6-risks-skeleton.md](phase-6-risks-skeleton.md)
- [phase-7-slices-plan.md](phase-7-slices-plan.md)

What this server defends against, how, and what it doesn't is in
[THREAT_MODEL.md](THREAT_MODEL.md). The adversarial test suite proving
those defenses, with published pass/fail results, is
[RED_TEAM.md](RED_TEAM.md).

The day-by-day build log — what broke, what surprised me, why decisions
landed where they did — is in [JOURNEY.md](JOURNEY.md).

## Status

**Slices A–E** built. Auth (issuance + revocation), rate limiting, and
all four MCP tools (`list_tables`, `describe_table`, `sample_rows`,
`run_query`) work end to end through the real MCP protocol -- verified
via an in-memory client/server session, not just the SDK's raise-on-error
convenience method, which caught a real bug in how tool errors reached
the model. 34 passing tests against a real Postgres instance, CI running
on every push. Not yet deployed to the VPS -- Arthur deploys by pulling
from this repo. Not yet tested against a real AI client (e.g. Claude
Desktop) over actual stdio -- only the in-memory protocol path so far.

**Slice E (benchmark) is built and verified short of an actual model
call**: `scripts/run_benchmark.py` runs a PydanticAI agent against this
server as a real MCP client, grading by execution accuracy against ten
hand-written questions over a synthetic demo dataset
(`scripts/load_demo_dataset.py`). Every piece short of real model
intelligence is confirmed working (see JOURNEY.md) using PydanticAI's
free `TestModel` stub. Running it for real needs an `ANTHROPIC_API_KEY`
in `.env` -- Arthur's own key, his own cost, not something to spend
without asking.

**Slice F (red-team suite) done** -- 16 adversarial tests against the
safety layer, including genuine defense-in-depth against a real
provisioned read-only role, not just the app-level validator. See
[RED_TEAM.md](RED_TEAM.md) for the full catalog and results.

**Slice G (web console) done** -- login (a separate credential from the
API key, so revoking a compromised key never locks you out of the
audit history), a dashboard polling the real audit log every 5s, and a
keys page that can rotate or revoke the workspace's key. Verified
through FastAPI's `TestClient` hitting the actual HTTP layer (login,
redirects, sessions, forms, the real-data audit API) -- see JOURNEY.md
for a real environment limit hit trying to also verify it visually in
a browser, and why the TestClient coverage made that non-blocking.

All seven slices from `phase-7-slices-plan.md` are now built.

**Pagila, a richer dataset.** `scripts/load_demo_dataset.py`'s
hand-written rows are enough to exercise the code paths but too thin
to demo convincingly or to calibrate `APP_COST_THRESHOLD` against.
`scripts/load_pagila_dataset.py` loads [Pagila](https://github.com/devrimgunduz/pagila)
instead -- a real, widely-known public Postgres sample database (a
DVD-rental store, 16+ tables, ~1,000 films, ~50,000 rentals) -- as an
optional, more credible alternative target. See JOURNEY.md for the
version-compatibility bugs real runs against a live Postgres 16
instance turned up getting it working.

## Layout

```
app/
  auth/       workspace, api keys, console credentials, rate limiting
  query/      data source connections, validation+execution, audit records
  benchmark/  text-to-SQL eval cases and results
  console/    server-rendered web console (Jinja2 + TS)
  mcp/        the MCP protocol layer (thin adapter, no business logic)
static-src/   TypeScript source for the console, compiled to app/console/static
```

## Running (once dependencies are installed)

```
uv sync
cp .env.example .env   # fill in real values
uv run uvicorn app.main:app --reload
```

The console's TypeScript is compiled separately:

```
cd static-src && npm install && npm run build
```
