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
**Status:** Superseded by ADR-007 (2026-10-22).

### ADR-005: Secrets via environment variables, no secrets manager
**Context:** Solo maintainer, small scale, no real external users' credentials
in play for v1.
**Decision:** Connection strings and other secrets live in environment
variables, not a dedicated secrets manager (Vault, etc.).
**Consequences:** Simple, no extra infra. Acceptable at this trust level.
**Revisit if:** This system ever handles real external users' credentials, or
runs at a scale/trust level where env-var secrets become a real risk.

### ADR-006: PydanticAI for the Slice E benchmark's test-agent loop
**Context:** The text-to-SQL benchmark (Slice E) needs an LLM-driven client
that explores the schema and writes SQL — hand-rolling raw API calls was
the alternative, and the project already leans on standard libraries rather
than reinventing them (FastAPI, SQLAlchemy, sqlglot).
**Decision:** PydanticAI, specifically for its native MCP client support —
an agent can connect to this server as a real MCP client and use its tools
directly, rather than wrapping raw SQL execution in a separate code path.
**Verified before deciding:** `fastmcp.Client` (PydanticAI's underlying MCP
client) connects directly to the in-process `MCPServer` object, correctly
lists and calls all four tools, and an invalid API key surfaces as the
exact `ToolError` message from Slice D — confirmed through a second,
independent client implementation, not assumed from the raw SDK test alone.
**Consequences:** The benchmark doubles as another real test of the server
through the actual protocol. Adds one dependency, isolated to an optional
`benchmark` extra so the core server install stays untouched.
**Revisit if:** PydanticAI's MCP client behavior diverges from the official
SDK's in some edge case the benchmark depends on.

### ADR-007: Invite-only self-serve workspaces, with user-supplied connections
**Context:** The server is installable as a Claude Code plugin, so other
people can use it, and each person needs their own key, audit log and rate
limit. Some want to query their own database rather than the demo one.
That is exactly the requirement ADR-004 said to revisit on.
**Decision:** Anyone holding a single-use invite code
(`scripts/create_invite.py`) can create their own workspace at
`/console/signup`. The workspace queries the server's default database
unless the user supplies their own connection URL. A supplied URL must
pass the address guard (no loopback/private/metadata addresses, re-checked
on every use) and a live connection test, and is stored encrypted.
**Alternatives rejected:** Open signup: anyone on the internet could make
the server connect to hosts of their choosing, and unlimited accounts would
mean unlimited rate-limit allowance. OAuth: the "proper" way to hand out
credentials automatically, but far more machinery than a portfolio-scale
server needs.
**Consequences:** Reintroduces the SSRF surface ADR-004 removed, now
mitigated (THREAT_MODEL T7), with one stated residual risk (DNS rebinding
between check and connect). Adds a secret the server must keep:
`APP_CONNECTION_ENCRYPTION_KEY`, which must also be backed up off the server.
**Amended (same day):** the invite gate moved to where the risk is. Signup
*without* a code now gives a demo account on the default database only: no
user-chosen host, no stored password, so nothing the invite was protecting.
What's left is account spam multiplying the per-workspace rate limits,
capped per client IP and per day. Connecting your own database still needs
an invite. A fully gated demo would have protected little and made the
project impossible to try.

### ADR-008: API keys over the MCP authorization flow (OAuth), for now
**Context:** The MCP specification defines an OAuth 2.1 authorization flow
for remote servers: the client discovers the server's authorization
endpoint, the user logs in in a browser, and the client receives a token
automatically. This server instead issues API keys that the user pastes
into the Claude Code plugin once.
**Decision:** Keep API keys. They're scoped to one workspace, stored only
as hashes, revocable from the console, and sent only as an
`Authorization` header (never through the model's context). Claude Code
stores the plugin's key in the OS credential store.
**Alternatives rejected (for now):** Implementing the OAuth flow means
running an authorization server (dynamic client registration, PKCE,
token issuance, refresh and revocation, consent screens): more security-
critical code than the rest of the auth layer combined, for one gain,
not having to copy a key once.
**Revisit if:** users connect from several clients, keys start being
shared or leaked, or the server needs to act on behalf of an external
identity provider's accounts. The plugin and the `Authorization: Bearer`
header would carry over unchanged; only how the token is obtained
changes.

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
