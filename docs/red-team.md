# Red-Team Suite Results

The adversarial test suite for the safety layer, as called for in
[phase-1-problem-brief.md](planning/phase-1-problem-brief.md)'s success metrics:
"A red-team test suite for the safety layer exists and its pass/fail
results are published, not just claimed."

**Run it yourself:** `pytest tests/test_red_team.py -v`

**Current result: 16/16 passing.**

## What's covered

Organized by the threats listed in [the threat model](threat-model.md):

| Threat | Attack attempted | Defense proven |
|---|---|---|
| Write disguised as a read | A write hidden inside a CTE (`INSERT`/`UPDATE`/`DELETE`), `SELECT INTO`, and a two-level-nested CTE | The validator's recursive write-node check rejects all five variants before any database connection opens |
| Write disguised as a read (independent check) | Same CTE-hidden write, but sent **straight to a real, provisioned read-only role**, bypassing our own validator entirely | The database itself independently blocks it — Postgres checks privileges per-operation regardless of nesting |
| `SECURITY DEFINER` bypass | A function that performs a write, called as a plain `SELECT` | The validator's function allowlist rejects it (the real defense) |
| `SECURITY DEFINER` bypass (honest limit of the role) | Same function, called directly against the real read-only role, bypassing the validator | **Succeeds in writing** — demonstrated, not hidden, because the database role alone cannot stop this; it's exactly why the allowlist above is the real defense, not the role |
| Writes sent past the validator, on a connection with every privilege | CTE-hidden `INSERT`, `SELECT INTO`, `CREATE TEMP TABLE`, the `SECURITY DEFINER` write above, and `set_config` trying to switch read-only off mid-query, sent straight to the database as a superuser | All refused by Postgres: every app connection to a target runs in a `READ ONLY` transaction. Needs no `CREATE ROLE`, so it covers targets where no read-only role can be made. Limit: a *separate* `SET TRANSACTION READ WRITE` before any query could undo it, which first needs a way past the single-`SELECT` rule |
| Resource exhaustion (function) | `pg_sleep(30)` | Rejected by the function allowlist (not a recognized builtin) |
| Resource exhaustion (outside the app) | A 2-second `pg_sleep`, sent **straight to a real read-only role** provisioned with a 1-second limit, bypassing the app and its per-query timeouts | Postgres cancels it: the role carries its own `statement_timeout`, so a leaked role password doesn't mean unbounded queries |
| Resource exhaustion (query shape) | A 3-way cross join of a table with itself | Rejected by the `EXPLAIN`-based cost check |
| Privilege escalation | `SET ROLE`, `SET SESSION AUTHORIZATION`, `RESET ROLE` | Rejected by the "only `SELECT` statements" check |
| Statement stacking | A harmless `SELECT` followed by a hidden `DROP TABLE`, including a comment-obfuscated variant | Rejected by the "exactly one statement" check |
| Workspace isolation | One workspace exhausts its rate limit | A second, unrelated workspace is confirmed unaffected |
| "Read-only" role that can still create | Provisioning a role on a schema whose dump granted `CREATE` to `PUBLIC` (Pagila's does) | Provisioning refuses, rather than report a read-only role that can create tables; the Pagila loader also drops that grant |

## What this suite deliberately does NOT try to prove

Per the "shrink, don't cut" fallback decided in
[phase-7-slices-plan.md](planning/phase-7-slices-plan.md): this is a focused set
of representative attacks, not an exhaustive catalog of every Postgres
function or encoding trick. See `docs/threat-model.md`'s "what this doesn't
defend against" section for the honestly-stated boundaries — e.g. a
compromised operator machine, or a target schema's owner adding a
dangerous function disguised with a name sqlglot happens to also
recognize as a safe builtin (not possible today, since recognized
builtins are a fixed set, but stated as the boundary condition rather
than assumed away).

It also doesn't test prompt injection through database contents (data
that tells the model to do something). That depends on the model and the
client's other tools, not on this server's code, so a pass/fail test here
would prove little. docs/threat-model.md T10 states what the server guarantees
regardless and what it can't.

## Scale-up plan

If/when this expands: more encoding/obfuscation variants, more
dangerous builtins (file-access functions like `pg_read_file`,
`lo_import`), and dialect-specific patterns if the "connect to any
database" vision ever moves past Postgres. Tracked the same way as an
ADR's "revisit if" — a known, written-down next step, not a forgotten one.
