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
[THREAT_MODEL.md](THREAT_MODEL.md).

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

Remaining: Slice F (red-team suite, shrink-not-cut if time is short).

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
