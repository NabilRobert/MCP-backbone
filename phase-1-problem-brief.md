# Phase 1 — Problem Brief

**Project:** a standalone, production-grade read-only database MCP
server — one of three focused portfolio pieces (this project, an eval
harness, a Beobot case study), not a piece of a larger "Jarvis" platform
shown to reviewers. See `portfolio-plan.md`: depth and finish on a small
number of things beats breadth on one sprawling platform.

## Problem

There is no artifact in Arthur's portfolio that lets a senior engineer or hiring
reviewer *verify*, rather than take on faith, that he can design and reason about
a safe, stateful, multi-tool AI system under real constraints. Existing evaluation
methods (resume keywords, algorithmic live-coding rounds, small take-homes, verbal
project walkthroughs) can't surface this kind of judgment, and tutorial-tier CRUD
projects don't exercise it in the first place. A broad multi-surface AI assistant
platform doesn't fix this either — it reads as the most common AI portfolio
project, breadth makes it harder to evaluate, not easier, and it's indistinguishable
from a weekend vibecoded demo. A small project a reviewer can actually finish
reading is the stronger signal.

## Affected parties

| Party | Wants |
|---|---|
| Arthur (builder) | A finished, standalone project with genuine backend depth (safety, validation, audit) he can defend under questioning — one of three focused pieces, not a single large platform |
| Reviewer / senior engineer | A fast, reliable signal — in ~15 minutes of reading — of whether this candidate makes real architectural tradeoffs, or just assembles tutorials |
| Future Arthur | Clean, reusable infrastructure *if* a later independent project happens to need a database MCP server — a possible side-benefit, not this project's stated purpose or a promised roadmap |

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
  audit-trail dashboard) is in scope — see Phase 2 for how this was decided.
- **Live/real-time monitoring** (streaming view of a query as it executes) is
  explicitly out of scope — the console stays polling-based.
- No mobile app, no WhatsApp/voice interface, no multi-surface platform —
  this project is the database MCP server and nothing else.
- No production on-call/uptime SLA.
- Open/unresolved: third-party plugin authoring, outbound connectors for
  firewalled databases — still roadmap-only, and not a stated commitment.

## Cost of doing nothing

A weaker portfolio and CV/website heading into job fairs and future
applications — fewer finished projects demonstrating backend depth.
