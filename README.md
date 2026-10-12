# Jarvis — Database MCP (v1)

A read-only database MCP server that lets an AI client explore a database's
schema and run safe, read-only SQL queries against it — with every attempt
validated, cost-checked, and logged. This is the backbone proof for a larger
umbrella assistant (Jarvis); it stands alone, imports nothing from any future
Jarvis codebase, and is reachable only over its own protocol/API.

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

## Status

**Slice A (walking skeleton)** done locally: API key auth, `list_tables`,
and `run_query` work end to end through the real MCP protocol, verified
against a real Postgres instance with an integration test suite. No
sqlglot validation, cost check, or read-only DB role yet -- that's Slice B,
which also needs the Phase 6 spikes run first. Not yet deployed to the VPS.

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
