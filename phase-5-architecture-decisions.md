# Phase 5 — Architecture and Decisions

## Components

- **MCP protocol layer** — thin adapter exposing tools (`list_tables`,
  `describe_table`, `sample_rows`, `run_query`) to the AI client. A wire-format
  layer only, not business logic — calls into the modules below.
- **auth/workspace** — owns `ApiKey`, `ConsoleCredential`, `RateLimitCounter`.
  Authenticates requests, issues/revokes keys, enforces the rate limit. Does
  not decide query safety.
- **query safety + execution** — owns `DataSourceConnection`,
  `QueryExecution`, `SchemaCache`. Validates SQL, runs the cost check,
  executes against the external DB via a read-only role, writes the audit
  record. Receives an already-authenticated workspace context — does not do
  its own auth.
- **web console** — not a separate module; a client of the two above (reads
  audit records, manages keys).

## Stack

- **Backend:** Python, FastAPI, SQLAlchemy 2.x, sqlglot. Confirmed, unchanged
  from earlier drafts.
- **Console frontend:** server-rendered pages (Jinja2) from the same FastAPI
  app — no separate SPA/framework. The small amount of client-side
  interactivity (polling refresh, chart rendering) is written in
  **TypeScript**, compiled to static JS via a lightweight build step
  (esbuild/tsc) at build/deploy time, served as a static file. This build
  step is dev-time only — it does not add a running service.
- **No Redis/Celery for v1.** Nothing in v1 is actually long-running —
  rate-limit checks and `SchemaCache` reads/writes are both fine
  synchronously inside the request, with the TTL implemented as a
  `cached_at` column in Postgres checked on read.
- **Storage:** a single Postgres instance holds everything — `ApiKey`,
  `ConsoleCredential`, `RateLimitCounter`, `QueryExecution` (audit),
  `SchemaCache`, `EvalCase`/`BenchmarkResult`. No second datastore.

## Cross-cutting concerns

- **Auth:** enforced once, at the module boundary (middleware), not
  re-implemented per route.
- **Error format:** one consistent error schema across MCP tool errors and
  console API errors (code + `RejectionReason` where applicable).
- **Logging:** structured, keyed by `workspace_id` + `query_execution_id` —
  mirrors the audit record, so one query's whole path is traceable.
- **Config/secrets:** connection strings and secrets via environment
  variables, not in code. A secrets manager (Vault etc.) would be overkill at
  this scale and trust level — see ADR-005.

## ADRs

### ADR-001: Modular monolith over microservices
**Context:** Demo-scale load, solo maintainer.
**Options:** Modular monolith; microservices; serverless functions.
**Decision:** Modular monolith.
**Consequences:** All modules deploy together; simpler ops and debugging.
**Revisit if:** A module needs independent scaling, or a separate team takes
ownership — neither expected here.

### ADR-002: No Redis/Celery for v1
**Context:** Nothing in v1 is long-running; rate limiting and schema caching
are both synchronous, in-request operations.
**Options:** Include Celery/Redis as originally drafted; drop them for v1.
**Decision:** Drop them for v1.
**Consequences:** One fewer running service to deploy/monitor; no background
job infra to build or defend for operations that don't need it. Tradeoff:
doesn't demonstrate async task-queue experience in this project; the
decision itself (back-of-envelope reasoning about what's actually needed) is
the intended signal instead.
**Revisit if:** The post-v1 live/streaming monitoring phase needs pub/sub —
that's the point this becomes load-bearing.

### ADR-003: Server-rendered console, not a SPA
**Context:** ~1-week timeline, solo build; the console only needs a login, a
key-management action, and a polling-refreshed audit table with basic charts.
**Options:** Full SPA (e.g. React) with a separate build pipeline and API
client layer; server-rendered pages with a small amount of TypeScript for
interactivity.
**Decision:** Server-rendered Jinja2 pages, TypeScript compiled to static JS
for the polling/chart code, single deployable app.
**Consequences:** Much less setup (no bundler/state-management stack, no
CORS between separate frontend/backend), same user-visible result at this
scope.
**Revisit if:** The richer post-v1 visualization app needs real frontend
state management — that's the natural point to introduce a framework.

### ADR-004: Connection strings are operator-set only
**Context:** The original vision was self-serve "connect to any database via
connection string," which creates an SSRF surface (pointing the server at an
internal address or metadata endpoint) if exposed to the console or any
client.
**Decision:** For v1, connection strings are set via CLI/config by the
operator only — never exposed to the console or any client.
**Consequences:** Removes the SSRF surface entirely for v1, at the cost of
not yet delivering self-serve "any database" setup.
**Revisit if:** Self-serve connections become a real requirement — would
need connection-string validation (blocking private IP ranges, metadata
endpoints) before being exposed again.

### ADR-005: Secrets via environment variables, no secrets manager
**Context:** Solo maintainer, small scale, no real external users' credentials
in play for v1.
**Decision:** Connection strings and other secrets live in environment
variables, not a dedicated secrets manager (Vault, etc.).
**Consequences:** Simple, no extra infra. Acceptable at this trust level.
**Revisit if:** This system ever handles real external users' credentials, or
runs at a scale/trust level where env-var secrets become a real risk.

## Reversible vs. irreversible

- **Cheap to change later:** console templating choice, UI styling, exact
  benchmark eval-set size.
- **Expensive to change later:** the `Workspace`/`ApiKey`/`ConsoleCredential`
  split, audit-record-is-the-only-source-of-truth, the MCP tool contract's
  shape (names/schemas — a breaking change once an AI client integrates
  against them).

## Validated against Phase 4 flows

Every step in the happy path and failure table has a clear home: auth/rate-
limit → auth module; schema cache/list/describe → query safety module;
parse/validate/cost-check/execute/audit-write → query safety module; console
reads → console calling both modules' APIs. No step bounces awkwardly
between modules.
