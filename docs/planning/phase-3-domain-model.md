# Phase 3 — Domain Model

## Glossary

- **Workspace** — the tenancy unit. Owns one `ConsoleCredential`, one current
  `ApiKey`, one `DataSourceConnection` (v1 — see below).
- **ApiKey** — credential used *only* to authenticate MCP query execution.
  Revocable/rotatable.
- **ConsoleCredential** — separate, longer-lived login for the web console
  (dashboard viewing, key management). Independent of `ApiKey` state.
- **DataSourceConnection** — the external database a workspace queries.
- **QueryExecution** — a single query attempt. Doubles as the audit record
  (see invariants) — there is no separate `AuditEntry` entity.
- **CostEstimate** — the `EXPLAIN`-derived value used to accept/reject a query.
- **RejectionReason** — why a query was blocked (not read-only, too expensive,
  auth failure, etc.).
- **EvalCase / BenchmarkResult** — for the text-to-SQL benchmark: question,
  expected SQL/result, actual SQL/result, correct flag.
- **RateLimitCounter** — per-workspace request count within a time window.
- **SchemaCache** — per-workspace cache of table/column metadata, with a TTL.
  Advisory only — never consulted during query execution, only for
  `list_tables`/`describe_table`.

## Entities & relationships

- `Workspace` → one `ConsoleCredential`, one current `ApiKey`, one
  `DataSourceConnection` **for v1**. *Revisit if:* multi-connection support
  becomes a real need later — the vision allows for it, v1 deliberately does not.
- `ApiKey` → belongs to a `Workspace`. Status `active`/`revoked`. Revocation
  blocks query execution only.
- `ConsoleCredential` → belongs to a `Workspace`. Grants dashboard
  viewing/key-management regardless of the current `ApiKey`'s state.
- `DataSourceConnection` → belongs to a `Workspace`, holds connection details
  for the one external database it queries.
- `QueryExecution` → belongs to a `Workspace` and a `DataSourceConnection`.
  Fields: query text, status (`succeeded`/`rejected`/`timed_out`/`errored`),
  `CostEstimate`, `RejectionReason` (if applicable), timestamp.
- `EvalCase`/`BenchmarkResult` — standalone, used for the accuracy benchmark,
  not tied to a `Workspace`.
- `RateLimitCounter` → belongs to a `Workspace`.
- `SchemaCache` → belongs to a `Workspace`, holds a TTL'd snapshot of its
  `DataSourceConnection`'s table/column metadata.

## Invariants

- A query is rejected before any DB contact unless it's a single, read-only
  `SELECT`.
- Every execution attempt produces exactly one `QueryExecution` record —
  success, rejection, and error all included. (This record *is* the audit log;
  modeling a separate `AuditEntry` would be redundant since the two are always
  1:1.)
- A query can only run against the `DataSourceConnection` belonging to the
  authenticating workspace.
- A query whose `CostEstimate` exceeds the threshold is rejected, never
  executed.
- A revoked `ApiKey` cannot authenticate query execution; the workspace's
  `ConsoleCredential` is unaffected and can still view the dashboard.
- `RateLimitCounter` increments on every execution attempt, including
  rejected ones.
- `SchemaCache` is advisory only: a stale cache can cause a query to error
  (e.g. referencing a dropped column) but can never cause a query to return
  wrong or stale data silently, since execution always validates live
  against the `DataSourceConnection`.
- A workspace's `DataSourceConnection` (its connection string) is set via
  CLI/config by the operator only — never exposed to the console or any
  client — to remove the SSRF surface of letting arbitrary connection
  strings be submitted.

## Lifecycle

`QueryExecution` is a simple synchronous request/response with a single
terminal status — no persisted intermediate states. Nothing in v1 is
long-running enough to need a real state machine.

## Module boundaries

- **auth/workspace** — owns `ApiKey`, `ConsoleCredential`, `RateLimitCounter`.
- **query safety + execution** — owns parsing/validation, cost-check,
  execution, and the `QueryExecution` record (the audit log).
- **web console** — not owned by either; it's a client that reads from both
  modules (audit records for the dashboard, workspace module for key
  management).
