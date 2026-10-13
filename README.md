# Database MCP Server

A standalone, read-only database MCP server that lets an AI client explore
a database's schema and run safe, read-only SQL queries against it — with
every attempt validated, cost-checked, and logged. Built to be small enough
to read end to end, not as a piece of a larger platform.

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

**Slices A–C** done locally: API key auth (issuance + revocation), rate
limiting, `list_tables`, and a fully validated `run_query` (sqlglot
checks, `EXPLAIN` cost check, statement timeout, read-only role
provisioning) all work end to end through the real MCP protocol, verified
against a real Postgres instance with 24 passing tests, CI running on
every push. Not yet deployed to the VPS -- Arthur deploys by pulling from
this repo.

Next: Slice D (full tool surface -- `describe_table`/`sample_rows` backed
by `SchemaCache` -- against a real AI client, not just a script).

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
