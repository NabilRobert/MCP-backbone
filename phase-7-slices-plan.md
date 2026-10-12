# Phase 7 — Slice and Plan the Work

## Deployment note (reaffirms ADR-003)

Backend and console frontend are **one app, one deployment, on the one VPS**
— MCP server routes and console pages (Jinja2-rendered) are served from the
same FastAPI process. No separate frontend host (Vercel or otherwise):
splitting deployment would add CORS handling, cross-origin auth, and two
independently-breakable deploys, for no benefit at this scale and timeline.
Revisit only if a richer, post-v1 frontend needs independent scaling.

## Slices (vertical, not horizontal — each is a complete, demoable, working
path through every layer, not "all the backend" then "all the frontend")

| Slice | What it proves, end to end | Risk/value | Est. |
|---|---|---|---|
| **A. Walking skeleton** | Hardcoded key → list tables → run one query → audit row appears → on the real VPS | Do first — de-risks everything else | 0.5–1 day |
| **B. Real safety + execution + audit** | Arbitrary queries correctly validated (sqlglot: single-statement check + **recursive write-node check** + **function-call allowlist** — see Phase 6 spikes 1 and 2), cost-checked, timed out, executed via the read-only role, always producing a correct audit record | Highest risk, highest value — run the Phase 6 spikes as part of starting this slice | 1.5–2.5 days |
| **C. Real auth** | Multiple workspaces, real key issuance/revocation (CLI), rate limiting enforced | Dependency for everything else, lower risk | 0.5–1 day |
| **D. Full MCP tool surface + schema cache** | `describe_table`/`sample_rows`/cached `list_tables`, tested against a real AI client, not just a script | Medium risk (MCP integration surprises) | 1–1.5 days |
| **E. Text-to-SQL benchmark** | Eval set built, run, accuracy measured, method published | Must-have | 1.5–2.5 days |
| **F. Red-team suite** | Phase 6 spike findings formalized into an automated adversarial test suite with a pass/fail report | Should-have — **first to shrink under time pressure, see fallback below** | 1–2 days |
| **G. Web console** | Key management + polling audit dashboard, in-browser | Lowest technical risk, depends on B and C existing already, naturally last | 1.5–2.5 days |

**Total estimate: ~8–14 days.** Consistent with the Phase 2 estimate. Against
the committed "under a week," the gap is real and already has an agreed
fallback (below) rather than being left to be discovered mid-build.

## Fallback decision (made in advance, not under pressure)

**If time runs short, Slice F (red-team suite) is shrunk, not cut.** A
version with 3–4 well-chosen adversarial cases — prioritizing the ones tied
directly to the Phase 6 spikes (the read-only-role function-call-write
attempt, the sqlglot bypass attempts) — still demonstrates the safety-testing
discipline. Zero tests demonstrates nothing. No must-have slice (B, C, D, E,
G) is to be silently descoped under pressure; F is the only one that flexes.

This is decided now, calm, rather than mid-crunch, for three reasons: pressure
degrades judgment; deciding in advance makes this a deliberate, owned choice
rather than an accidental gap; and it changes *how* F gets built from the
start — highest-value test cases first, so a shrunk version is still
coherent rather than an arbitrary partial set.

**Scale-back-up plan:** once v1 ships (or time pressure eases), the red-team
suite expands to additional adversarial categories not in the shrunk version
— more SQL injection variants, encoding tricks, multi-statement disguises,
edge cases around the `EXPLAIN` cost check, and (if the "connect to any
database" vision ever expands past Postgres) dialect-specific attack
patterns. Tracked the same way as an ADR's "revisit if": this is a known,
written-down gap, not a forgotten one.
