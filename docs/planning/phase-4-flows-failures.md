# Phase 4 — Trace the Flows

## Happy path

1. AI client sends a request to the MCP server with the workspace's `ApiKey`.
2. Server authenticates the key against the `Workspace`; rejects immediately
   if invalid/revoked.
3. `RateLimitCounter` is checked/incremented; rejects if over the (placeholder)
   limit.
4. Client calls `list_tables` / `describe_table` — server checks `SchemaCache`
   first (TTL'd); on a cache miss or expiry, fetches live from the
   `DataSourceConnection` and refreshes the cache. `sample_rows` always goes
   live (row data is never cached).
5. Client (the AI model) writes a SQL query and calls the query tool.
6. Server parses the query with `sqlglot`: rejects if not a single `SELECT`;
   injects a `LIMIT` if missing.
7. Server runs `EXPLAIN` (own, shorter timeout) to get a `CostEstimate`;
   rejects if it exceeds the threshold.
8. Server executes the query against the `DataSourceConnection`, via a
   read-only DB role, with a `statement_timeout`.
9. Server writes the `QueryExecution` record (status `succeeded`, query text,
   cost estimate, timestamp). If this write fails, the whole operation is
   treated as failed — see below.
10. Server returns rows to the client.
11. Separately, someone opens the web console with a `ConsoleCredential`,
    views the (polling-refreshed) audit trail and/or manages the `ApiKey`.

## Source of truth

- **External database:** source of truth for schema and row data.
- **Own Postgres:** source of truth for everything invented by this system —
  `QueryExecution` records, `ApiKey`/`ConsoleCredential` state, rate-limit
  counts.
- **`SchemaCache`:** a copy, not a source of truth — advisory only, TTL'd,
  never consulted during actual query execution (see Phase 3 invariants).
  Caching is scoped to schema/column metadata only; row data and query
  results are never cached, since that would let the AI confidently answer
  with stale data — the one thing this project exists to prevent.

## Failure interrogation (query-execution flow, steps 5–10)

| Step | Fail | Slow | Twice | Partial |
|---|---|---|---|---|
| Parse/validate (sqlglot) | Malformed SQL → reject with `RejectionReason`, write `QueryExecution` as rejected, return a clear error | Parsing is local/fast — not a concern | Each attempt validated independently; harmless duplicates | N/A — validation is atomic |
| EXPLAIN cost check | `EXPLAIN` errors (driver/permission issue) → reject, log reason, never run the real query | Needs its own, shorter timeout than the main query, so a pathological query can't hang even the cost-check step | N/A | N/A |
| Execute with timeout | DB connection drops mid-query → transient failure, auto-retry with backoff, capped at 1–2 attempts | `statement_timeout` kills it; error includes the cost estimate so the client knows why | Safe to retry — read-only, no side effects to duplicate | Connection drops mid-fetch → **no partial rows returned**, treated as a full failure with a clean error |
| Write `QueryExecution`/audit record | **Decision: fails the whole operation.** No un-audited successful query ever reaches the client, even if that means occasionally failing a good query because logging broke. Protects the project's core audit-trail claim. | — | — | — |
| Return rows to client | Network drop after the audit record is written but before the client receives the response → fine by design, record reflects what the server did; client can safely retry (read-only) | — | Harmless duplicate audit rows — accepted as a "should fix later," not urgent | — |

## Concurrency

`RateLimitCounter` does not need strict atomicity for v1 — the limit is a
placeholder (5 requests) for demo cost control, not a hard guarantee.
Approximate/non-atomic increments are acceptable.

## Trust boundaries

- **AI-authored SQL** is untrusted input. Defense in depth: sqlglot
  validation + EXPLAIN cost check at the application layer, **and** a
  read-only DB role at the database level, so a bug in validation code can't
  escalate to a write.
- **Console browser session** is untrusted input. Standard web hygiene:
  CSRF protection, secure/httpOnly session cookies, no unescaped user input
  rendered back (XSS), rate-limited login attempts.
- **Connection strings** are operator-set via CLI/config only — never
  exposed to the console or any client. This removes the SSRF-via-connection-
  string surface for v1 entirely (no workspace can point the server at an
  internal address or metadata endpoint), at the cost of not yet supporting
  self-serve "any database" setup — deferred to a later phase.
