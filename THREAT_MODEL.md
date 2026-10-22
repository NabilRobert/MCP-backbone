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
- **The operator (Arthur) is trusted.** They issue invite codes and can
  seed workspaces directly from the CLI.
- **Invited users are semi-trusted.** Since ADR-007 replaced ADR-004, a
  user with a valid invite code can register their own connection URL at
  signup. That's a client-reachable surface pointing the server at a host
  of the user's choosing, so the URL is treated as untrusted input (T7).
- **Demo users are untrusted.** Anyone can sign up without an invite, but
  only for the server's default database. They can't supply a connection
  URL, so they never reach the T7 surface.

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

**A second database-level layer, with no role needed:** every app
connection to a target runs in a `READ ONLY` transaction
(`create_target_engine` in `app/db.py`). Postgres refuses every write
inside one, including a `CREATE TEMP TABLE` and a write inside a
`SECURITY DEFINER` function, which the role can't stop (T2). A query
can't switch it off mid-statement. Its strength is that it needs no
`CREATE ROLE`, so it also protects targets where no read-only role can be
made: a managed host that doesn't grant it (Sumopod), or a user's own
database supplied with a writable login. Its limit is that a separate
`SET TRANSACTION READ WRITE`, sent before any query, could undo it. That
needs a way past the validator's single-`SELECT` rule first, which is
why it complements the role rather than replacing it. Tested by sending
each write straight to the database as a superuser, bypassing the
validator (`tests/test_red_team.py`).

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
`ApiKey` presented. Within a workspace, each key is also limited to the
tools it was issued for (`app/auth/scopes.py`), so a key given to a
schema-browsing client can't read rows even if it leaks. A revoked `ApiKey` cannot authenticate at all; a
separate `ConsoleCredential` (not covered by this document in depth)
governs dashboard access independently, so key rotation doesn't lock an
operator out of their own audit history.

### T7: a user-supplied connection URL points the server at its own internals (SSRF)

The bookkeeping database (audit log, key hashes) listens only on the
server's loopback interface, which is exactly where this server connects
from. A signup with `postgresql://...@127.0.0.1:5432/...` would point the
server straight at it.

**Mitigation:** `app/query/network_guard.py` resolves the host and refuses
any loopback, private, link-local (incl. cloud metadata), carrier-grade
NAT or reserved address, including IPv4 hidden inside IPv6
(`::ffff:127.0.0.1`). It also refuses URL parameters that could add a
second host (`?host=`) or read local files (`?passfile=`). It runs at
signup and again on every use (`get_data_source`), so a DNS change after
registration is caught too. The one exemption is the server's own default
database (`APP_TARGET_DATABASE_URL`): it's set by the operator, not a
user, and may legitimately live on localhost. Workspaces on the default
store no URL at all, so a user can't impersonate it. The invite is checked before the URL is even
looked at, so an uninvited visitor can't use signup to make the server
probe hosts. Tests cover each address class and parameter.

**Residual risk:** the check resolves the name, then the driver resolves
it again to connect. A hostname whose DNS flips between those two lookups
(DNS rebinding) could slip through. Invite-only signup bounds this: only
people the operator chose can register a URL.

### T8: a leaked bookkeeping database or backup exposes users' database passwords

Connection URLs can't be hashed like API keys: the server needs the real
password to connect.

**Mitigation:** they're encrypted at rest with Fernet (`app/crypto.py`).
The key lives in the server's environment, never in the database it
protects, so a database dump or one of its S3 backups contains only
ciphertext. The server refuses to start without the key. API keys and
invite codes are stored only as hashes, as before.

**Limit:** someone with full control of the running server has both the
data and the key. This protects against leaks, not a full server
compromise.

### T9: data read through `sample_rows`, or console passwords guessed, without limit

**Mitigation:** `sample_rows` returns real rows, so it's now audited (one
`QueryExecution` per call, same as `run_query`) and rate-limited on its
own allowance. Before this, it could read the first 100 rows of every
table, as often as the caller liked, and leave no trace. Console login
and signup lock out a client IP after repeated failures
(`app/console/throttle.py`). The counts live in the database, so a
restart doesn't reset them. Only a SHA-256 of the IP is stored, pruned
after a day: pseudonymous rather than anonymous, since IPv4 hashes can be
brute-forced, so the short retention is what limits exposure. Console
sessions are signed cookies stamped with a per-login version, so "log out
everywhere" can revoke every existing session server-side. Over HTTP the
API key is accepted only in
the `Authorization` header, never as a tool argument, so it never passes
through the AI model's context or its transcripts.

### T10: prompt injection through the data itself

Everything the tools return goes into the AI model's context: rows from
`run_query` and `sample_rows`, and table and column names from
`list_tables` and `describe_table`. If any of that text contains
instructions ("ignore your previous instructions and..."), the model may
follow them. Nobody can plant such text *through* this server, since
every path is read-only. So the realistic source is whoever controls the
database's contents: the operator, for the demo database, or the user,
for their own. The practical case is a user's own database holding
untrusted text, such as customer messages, form submissions or scraped
pages.

**What this server guarantees no matter what the model is told:**

- **No writes.** The validator, read-only transactions and, where
  available, a read-only role (T1, T2) stop an injected "now delete..."
  as surely as a direct one.
- **No reach beyond the workspace.** The key travels in a header the
  model never sees, so injected text can't make it reveal or swap
  credentials. Every tool is scoped to the authenticated workspace (T6).
- **No reach beyond the database.** The server's only tools are four
  reads against one database, which passes the address guard (T7). It
  has nothing that fetches URLs, sends messages or touches files, so an
  injection can't use *this server* to send data anywhere.
- **Only the tools the key was issued for.** A schema-only key gives an
  injected model nothing to read but table and column names.
- **Bounded, and on the record.** Rate limits and the 200-row cap bound
  how much an injected model can read, and every call it makes, including
  rejected ones, is in the audit log.

**What it cannot prevent:**

- **Exfiltration through the client's other tools.** If the same Claude
  session can also fetch URLs, send email or run shell commands, injected
  instructions could make it pass data it read here to those tools. This
  server never sees that happen. The control is on the client side: keep
  outbound tools on approval (Claude Code's permission prompts) in
  sessions that use this plugin, especially against a database holding
  untrusted text.
- **Wrong answers.** Injected text can make the model misreport what it
  found. The audit log shows what was actually queried and returned,
  which is how a misleading answer can be checked.

**Partial mitigation, not relied on:** the plugin's skill tells Claude to
treat everything the tools return as data, never as instructions, and to
point out text that looks like instructions rather than act on it. Models
follow such guidance most of the time, not always, which is why the
guarantees above don't depend on it.

Source: OWASP Top 10 for LLM Applications, [LLM01: Prompt Injection](https://genai.owasp.org/llmrisk/llm01-prompt-injection/)
(this is its "indirect" form: the instructions arrive in data the model
reads, not in the user's prompt).

## What this does *not* defend against (stated honestly, not hand-waved)

- **A compromised operator machine or server.** Secrets (the bookkeeping
  database password, the encryption key) live in the server's
  environment, not a secrets manager (ADR-005). That's an accepted
  tradeoff at this scale and trust level, revisited if that ever changes.
- **The public demo database's second layer depends on the deploy
  step.** Open demo signup means strangers query it, so it needs both
  layers from T1: the app-level validator and a database role that can't
  write. The original managed host couldn't create roles, so
  `deploy/setup_demo_database.sh` moves the demo database onto the VPS's
  own Postgres with a real read-only role. Run before that script, the
  demo database has the validator only. The script also closes two
  PUBLIC grants that would have let even a read-only role create objects
  (`CREATE` on the schema, from Pagila's own dump, and `TEMP` on the
  database).
- **Demo account spam beyond the caps.** Signups are capped per client IP
  and in total per day, but someone with many IP addresses can still use
  up the daily total. That locks out new demo signups for the day; it
  doesn't affect existing accounts.
- **Prompt injection carried by database contents, acted on through the
  client's other tools** (T10). The server bounds what an injected model
  can do *here*; what it does with the rest of its tools is the client's
  to control.
- **A user's own database credentials being over-privileged.** Signup
  warns when the supplied login can write or is a superuser, but accepts
  it: it's their database. The app-level validator still blocks writes.
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
the first assumption was wrong. The repeatable version of that
verification is `tests/test_red_team.py` — see `RED_TEAM.md` for the
full catalog and current pass/fail results.
