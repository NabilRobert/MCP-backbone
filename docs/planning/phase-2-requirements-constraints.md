# Phase 2 — Requirements & Constraints

**Scope:** v1 = the database MCP server + a web console. No mobile app, no
WhatsApp, no live/streaming monitoring (see Phase 1 non-goals).

## Core functional scenario

An AI client connects to the MCP server using a workspace-scoped API key, lists
tables, describes schema, samples rows, and runs a read-only SQL query against
the connected database. Every execution attempt — successful, rejected, or
errored — produces an audit record.

## Resolved edge cases

- **Semantically bad queries** (valid syntax, dangerous intent): caught
  deterministically, not via AI judgment — parse with `sqlglot`, reject
  anything that isn't a single `SELECT`, auto-inject a `LIMIT` if absent, run
  `EXPLAIN` first and reject if estimated cost/rows exceed a threshold.
- **Unauthorized access:** a request without a valid, workspace-matching API
  key is rejected before touching any database. Keys are issued via the web
  console.
- **Slow/hanging queries:** hard-killed via DB `statement_timeout`, caught
  earlier where possible by the `EXPLAIN` cost check. Retry is reserved for
  *transient* infra failures (dropped DB connection, network blip) with
  backoff — never auto-retried for a query that was slow/expensive, since that
  just repeats the cost.
- **Duplicate requests:** a should, not a must — all v1 operations are
  read-only, so a duplicate is wasteful (extra audit rows) but not unsafe.

## Non-functional requirements

| Category | Decision |
|---|---|
| Performance | No hard latency number set — open, revisit if it becomes a real constraint |
| Scale | Not back-of-envelope production math — "designed to handle comfortably," demonstrated against one real-but-public dataset |
| Availability | Runs continuously on the already-provisioned VPS |
| Consistency | Audit log must be consistent — no silent drops, ever |
| Security/privacy | Demo data is public only, no real PII |
| Cost | VPS already provisioned (Singapore, 2 vCPU / 8 GB) is the budget ceiling; demo-account rate limit placeholder of 5 requests pending real cost numbers |
| Maintainability | Solo-maintained by Arthur indefinitely |

## Constraints

- **Time budget:** target is under a week, solo, focused hours. This is tight
  against the effort estimate below — flagged as a risk to revisit in Phase 6.
- **Team:** solo.
- **Stack:** Python/FastAPI/SQLAlchemy/sqlglot. RAM concern on the VPS
  resolved — no local LLM hosted there, server footprint is lightweight.

## Must / Should / Could

- **Must:** read-only enforcement (DB role + sqlglot validation + LIMIT
  injection + EXPLAIN cost check); statement timeout + transient-failure
  retry; consistent audit log; API-key auth; demo against the public dataset;
  rate-limit placeholder; published text-to-SQL accuracy benchmark with
  method; **web console** (key management + polling-based audit-trail
  dashboard with filters/basic charts).
- **Should:** red-team test suite for the safety layer.
- **Could / explicitly deferred to post-v1:** live/streaming monitoring,
  duplicate-request dedup, anything beyond a minimal CLI/test-agent client,
  mobile app, WhatsApp.

## Rough effort estimate (back-of-envelope, logged for Phase 6/7)

| Item | Est. effort |
|---|---|
| Read-only safety layer | 1–2 days |
| Statement timeout + retry | 0.5–1 day |
| Audit log | 0.5–1 day |
| API-key auth | 0.5–1 day |
| Public dataset + demo setup | 0.5 day |
| Rate-limit placeholder | a few hours |
| Text-to-SQL benchmark (build + run + write-up) | 2–3 days |
| Polling web console (keys + dashboard + filters/charts) | 2–3 days |
| Red-team suite (should) | 1–2 days |
| **Total** | **~9–14 focused days**, before ADR/write-up time |

**Open risk, carried to Phase 6:** the committed timeline (under a week) is
below this estimate. Decision on record: if time runs short, the should-item
(red-team suite) is cut first — no must-have item (safety layer, benchmark,
console) is to be silently descoped.
