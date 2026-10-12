# Phase 1 — Problem Brief

**Project:** Jarvis (umbrella AI assistant) — v1 is the database MCP server, the backbone proof.

## Problem

There is no artifact in Arthur's portfolio that lets a senior engineer or hiring
reviewer *verify*, rather than take on faith, that he can design and reason about
a safe, stateful, multi-tool AI system under real constraints. Existing evaluation
methods (resume keywords, algorithmic live-coding rounds, small take-homes, verbal
project walkthroughs) can't surface this kind of judgment, and tutorial-tier CRUD
projects don't exercise it in the first place.

## Affected parties

| Party | Wants |
|---|---|
| Arthur (builder) | A flagship portfolio project with genuine backend depth (orchestration, tool routing, state, safety) he can defend under questioning |
| Reviewer / senior engineer | A fast, reliable signal — in ~15 minutes of reading — of whether this candidate makes real architectural tradeoffs, or just assembles tutorials |
| Future Arthur | A working backbone (Jarvis) he can actually extend with new connectors/projects, not a one-off demo |

No conflict identified between these three yet — revisit if later phases reveal
tension (e.g. "impressive to reviewers" pulling against "actually finishable
solo in the time available").

## How this is handled today, and where it breaks

Reviewers currently lean on resume keywords, short algorithmic coding rounds,
small take-home exercises, and informal "walk me through a project" conversations.
This breaks down because: keyword/tutorial-tier claims are indistinguishable from
genuine depth; algorithmic rounds test a different skill than systems judgment;
take-homes are scoped too small to force real tradeoffs; verbal walkthroughs are
easy to embellish and hard to fact-check in a short conversation. The gap: no
inspectable artifact proves judgment under constraints — safety boundaries,
failure handling, what was deliberately *not* built — with the reasoning visible,
not just the result.

## Success metrics (independently verifiable, no invented usage data)

- A senior engineer can read the repo (README + ADRs) and correctly describe the
  key architectural decisions and their tradeoffs within ~15 minutes, without
  reading implementation code.
- Every design decision with real alternatives has a written ADR citing the
  actual tradeoff, with a primary source (paper, RFC, vendor doc) where one exists.
- Text-to-SQL accuracy is measured against a documented, hand-built benchmark and
  published with its methodology.
- A red-team test suite for the safety layer (read-only enforcement, injection
  resistance) exists and its pass/fail results are published, not just claimed.
- Arthur can defend every major decision in an unscripted conversation — not just
  describe it, but explain why, including the rejected alternatives.

## Non-goals (current build pass — revise as planning continues)

- No multi-tenant / commercial SaaS ambitions (a separate existing project
  already covers that ground).
- No LLM training or fine-tuning.
- A **minimal-then-richer web console** (API-key management + polling-based
  audit-trail dashboard) is in scope for v1 — see Phase 2 for how this got
  pulled forward from the original "next phase" plan.
- **Live/real-time monitoring** (streaming view of a query as it executes) is
  explicitly deferred to after v1 — same bucket as the mobile app.
- No WhatsApp/voice interface yet — the step after web/mobile.
- No production on-call/uptime SLA.
- Open/unresolved: third-party plugin authoring, outbound connectors for
  firewalled databases — still roadmap-only.

## Long-term vision (context, not current scope)

Jarvis as an umbrella assistant. Sequencing: **v1** proves the backbone itself
(MCP connector, orchestration, tool routing, safety/audit) plus a polling-based
web console to view what it's doing. **Next**, a richer web app and mobile app
add live/real-time visualization and broader surfaces. **After that**, a
WhatsApp interface adds voice note/chat access.

## Cost of doing nothing

A weaker portfolio and CV/website heading into job fairs and future
applications — fewer flagship projects demonstrating backend depth.
