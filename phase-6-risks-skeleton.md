# Phase 6 — Attack Risks and Build the Skeleton

## Risks, ranked by likelihood × impact

**Technical**

- **Read-only DB role might not be airtight.** A `SELECT` statement can call a
  function, and some functions have side effects (write data) even inside a
  read-only-looking query. If the read-only role is correctly scoped it
  shouldn't have permission to execute such functions either — but this is an
  assumption to test, not trust, since it's the last line of defense behind
  the application-level validation. **Likelihood: medium. Impact: severe** —
  this is the core safety claim of the whole project.
- **`EXPLAIN` cost estimates can be wrong.** Based on possibly-stale
  statistics; a query can pass the cost check and still be slow.
  `statement_timeout` is the real backstop, not the cost check.
  **Likelihood: medium. Impact: low** (timeout covers it).
- **`sqlglot` might not catch every disguised-dangerous query**, especially
  Postgres-specific syntax not yet tested against. **Likelihood: medium.
  Impact: high** — this is the main blocking layer.

**Integration**

- **MCP protocol behavior with a real AI client may differ from our own
  tests** (retries, error formatting, timeouts). **Likelihood: medium.
  Impact: medium.**

**Product (translated: will this land with its real audience, the reviewer)**

- **A reviewer skims the README and never reads the ADRs** — the depth built
  into the project never gets seen. **Likelihood: medium-high. Impact:
  high** — directly undermines the Phase 1 success metric.
- **The safety story gets challenged live** (e.g. "what about an injection
  pattern that still parses as one valid `SELECT`?") and the red-team suite
  — a "should," not a "must" — turns out too thin to have caught it.
  **Likelihood: medium. Impact: high** — worst possible place for a gap to
  surface.

**People**

- **Time budget vs. scope** — committed ~1 week against a ~9–14 day estimate
  plus architecture work on top. **Likelihood: high. Impact: high.** The
  single biggest risk in this plan. Already decided: should-items (red-team
  suite) get cut before any must-have.
- **New-to-you tooling** (TypeScript build step via esbuild/tsc) could eat
  unplanned hours. **Likelihood: medium. Impact: low** — small, contained.

## Pre-mortem

Imagining this project failed six months from now, the likely reasons:

1. Time ran out and the thing quietly rushed was the red-team suite or the
   benchmark write-up — code worked, but nothing rigorous backed the claims.
2. The README/ADRs weren't compelling enough, so a reviewer never actually
   saw the depth that was there.
3. Someone found a hole in the read-only enforcement during a live technical
   interview, undermining the entire safety pitch on the spot.

## Spikes (time-boxed, throwaway experiments)

1. **Can a read-only DB role be tricked into writing data via a
   SELECT-wrapped function call?** (~half a day) — tests the riskiest
   assumption above before building further on top of it.
2. **Does `sqlglot` reliably catch disguised-dangerous queries on the
   Postgres dialect specifically?** (~half a day) — validated against a set
   of deliberately nasty test queries before trusting it as the main gate.
3. **Does the `EXPLAIN`-based cost check behave sensibly on the actual public
   demo dataset?** (~a few hours) — cheap check, avoids miscalibrated
   thresholds discovered mid-build.
4. **Does the TypeScript-to-static-JS build step work cleanly served from
   FastAPI?** (~1–2 hours) — small, unfamiliar tooling, worth clearing early.

Outcome of each spike (pass/fail + notes) logged below as each one runs —
these are throwaway code, not production implementation.

### Spike 1 result: FAIL (as initially planned) — mitigation identified

Tested against a real Postgres instance: a role with only `SELECT` granted
correctly rejects a direct `INSERT` ("permission denied"). But a
`SECURITY DEFINER` function that performs a write, called as
`SELECT public.some_function()`, **succeeded** — Postgres grants `EXECUTE`
on new functions to `PUBLIC` by default, and that single statement is a
syntactically valid `SELECT`, so sqlglot's "single read-only `SELECT`" rule
would not catch it.

Tried the obvious DB-level fix — `REVOKE EXECUTE ... FROM PUBLIC` plus
`ALTER DEFAULT PRIVILEGES` — and it only protects functions created
*afterward by the same role* that ran the ALTER DEFAULT PRIVILEGES command.
A function created by whoever owns the target schema (which, for "connect
to any external database," is never us) is unaffected. So the DB-level
revoke only closes the gap for functions that exist *at setup time*, not
ones added later to a schema we don't own.

**Conclusion:** the read-only role is necessary but not sufficient on its
own for externally-owned databases. **New requirement for Slice B:**
sqlglot validation needs an explicit function allowlist (safe built-ins
only — `COUNT`, `SUM`, `AVG`, `MIN`, `MAX`, `LOWER`, `UPPER`, etc.), not just
"reject anything that isn't a single `SELECT`." This is the layer that
stays robust regardless of what the target schema's owner does later.
Revoking `EXECUTE` from `PUBLIC` at role-setup time is kept as one more
layer of defense-in-depth, not the primary defense.

## Walking skeleton

The thinnest end-to-end slice, deployed to the real VPS (not just localhost):
a test script authenticates with one hardcoded `ApiKey`, calls `list_tables`
against the real public dataset, runs one simple hardcoded read-only query,
gets results back, and a row appears in the `QueryExecution`/audit table in
Postgres. No console, no benchmark, no red-team suite, no polish yet — just
proof that auth → validation → execution → audit connects end to end, on
real infrastructure.
