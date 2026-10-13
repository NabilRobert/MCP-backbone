# Threat Model

What this server defends against, how, and — just as important — what it
doesn't. Synthesized from the Phase 4 flow analysis, the Phase 6 spikes,
and the actual implementation, not written as a separate exercise after
the fact.

## Assets

- **The target database's data.** The thing an AI client is allowed to
  *read*, never write.
- **Audit integrity.** Every query attempt must be truthfully recorded —
  this is the server's core claim, and a gap here undermines everything
  else.
- **Workspace isolation.** One workspace's `ApiKey` must never see or
  touch another workspace's data source.
- **Server/host resources.** CPU, memory, and connection slots on the box
  running this service.

## Trust boundaries

- **The AI client is untrusted.** It writes the SQL. Nothing it sends is
  assumed safe until validated.
- **The target database's existing contents are untrusted**, for any
  database this server doesn't fully own — including functions and schema
  objects that may already exist there. See the `SECURITY DEFINER`
  finding below.
- **The console's browser session is untrusted input** in the standard
  web sense (CSRF, XSS) — out of scope for this document's focus on the
  query-safety layer, but not ignored; see Phase 4.
- **The operator (Arthur) is trusted.** Connection strings and role
  credentials are set via CLI only (ADR-004), never through any
  client-reachable surface.

## Threats considered, and how they're actually mitigated

### T1: AI client sends a query that writes data

**Mitigation:** `app/query/validation.py` parses every query with sqlglot
and recursively walks the *entire* tree for any write-node type
(`Insert`, `Update`, `Delete`, `Into`, `Create`, `Drop`, `Alter`) — not
just the top-level statement type. **This distinction is load-bearing,
not theoretical:** testing found that `WITH x AS (INSERT ... RETURNING
id) SELECT * FROM x` and `SELECT * INTO new_table FROM ...` both parse
with a top-level type of `Select`. A naive "is the statement a `Select`"
check would have let both through. (Phase 6 spike 2.)

**Defense in depth:** even if the validator had a bug, the connection
itself uses a role with no `INSERT`/`UPDATE`/`DELETE`/`CREATE` grants
(`scripts/provision_readonly_role.py`). Verified directly: a write hidden
inside a CTE, and a `SELECT INTO` (a different privilege check —
`CREATE` on the schema, not `INSERT` on a table), were both independently
blocked by the database role, confirming Postgres checks privileges
per-operation regardless of nesting.

### T2: a function call executes a write despite looking like a read

A `SELECT`-wrapped call to a `SECURITY DEFINER` function runs with the
*function owner's* privileges, not the caller's — so a read-only role
provides no protection here. **Confirmed by testing** (Phase 6 spike 1): a
`SECURITY DEFINER` function that performed an `INSERT`, called as
`SELECT public.some_function()`, succeeded even against a role with only
`SELECT` granted.

**Mitigation:** the validator rejects any function call sqlglot doesn't
recognize as a named builtin (falls back to `exp.Anonymous`) unless
explicitly allowlisted — the allowlist defaults to empty. sqlglot gives
every common analytical function (`COUNT`, `COALESCE`, `DATE_TRUNC`, ...)
its own node type, so this doesn't block legitimate queries; it blocks
both user-defined functions and un-modeled builtins like `pg_sleep`
(itself a resource-exhaustion vector, also caught by this rule).

**Residual risk, stated plainly:** `REVOKE EXECUTE ... FROM PUBLIC`
(also run by the provisioning script) only protects functions that exist
*at the time it runs*. It does not retroactively protect against a
function added later by whoever owns the target schema — which, for
"connect to an external database we don't own," is a real gap the DB
role alone cannot close. The function allowlist is the actual primary
defense for this threat; the role-level revoke is a secondary layer, not
a substitute.

### T3: a query is slow or resource-expensive enough to degrade the server

**Mitigation:** two layers. First, `EXPLAIN (FORMAT JSON)` runs under its
own short `statement_timeout` and the estimated `Total Cost` is checked
against a threshold before the real query ever runs
(`app/query/cost_check.py`). Second, the real query itself runs under a
separate, longer `statement_timeout` as a hard backstop, since planner
cost estimates can be wrong (stale statistics, complex query shapes).
**A timed-out query is never retried** — retrying something that was slow
or expensive just repeats the cost. This is verified behavior, not an
assumption: a deliberately slow query was confirmed to be killed at the
configured timeout and classified `timed_out`, not retried.

### T4: a transient infrastructure failure (dropped connection) looks like a real error

**Mitigation:** a small, explicit set of asyncpg connection-level
exceptions get one retry with backoff. Everything else — including a
genuine query timeout — does not retry. The distinction matters: retrying
a dead connection is free and correct; retrying an expensive query is
neither.

### T5: a successful query result reaches the client without being audited

**Mitigation:** the audit write happens *before* the result is returned,
and if that write fails, the exception propagates and no result is ever
handed back — by construction, not by convention. This was a deliberate
design decision (Phase 4), not an incidental property: "every answer has
an audit trail" is this project's core claim, so a logging failure is
treated as more serious than a query failure.

### T6: one workspace reads or disrupts another workspace's data

**Mitigation:** every `QueryExecution`, `DataSourceConnection`, and rate
limit check is scoped to the authenticated `Workspace`, resolved from the
`ApiKey` presented. A revoked `ApiKey` cannot authenticate at all; a
separate `ConsoleCredential` (not covered by this document in depth)
governs dashboard access independently, so key rotation doesn't lock an
operator out of their own audit history.

## What this does *not* defend against (stated honestly, not hand-waved)

- **A compromised operator machine or leaked connection string.**
  Connection strings are operator-set via CLI/environment, not a secrets
  manager (ADR-005) — an accepted tradeoff at this scale and trust level,
  revisited if that ever changes.
- **A target schema whose owner adds a dangerous `SECURITY DEFINER`
  function *after* the allowlist and role are set up**, if that function
  happens to also be given a name sqlglot recognizes as a safe builtin —
  not possible today (recognized builtins are a fixed, closed set in
  sqlglot, not extensible by a target database), but worth stating as the
  boundary condition rather than assuming it away.
- **Denial of service via sheer request volume** rather than per-query
  cost — covered only by the placeholder rate limit (Phase 2), not a real
  defense at production scale.
- **The console's own web-application attack surface** (CSRF, XSS,
  session handling) — standard mitigations are planned (Phase 4) but not
  the focus of this document.

## Verification

Every claim above with a "confirmed by testing" or "verified" note was
checked against a real Postgres instance, not reasoned about on paper —
see `JOURNEY.md` for the actual test sessions, including the cases where
the first assumption was wrong.
