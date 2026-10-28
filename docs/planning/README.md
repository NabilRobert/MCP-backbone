# Planning trail

Written before and during the build, in order. Each phase builds on the
previous one. They're kept as they were written, as a record of what was
planned and why, so some details have since changed (the time estimate,
for one). Where a decision was reversed, the ADR says so and points to
what replaced it, and [the build log](../journey.md) tells the story.

| Phase | What it settles |
|---|---|
| [1. Problem brief](phase-1-problem-brief.md) | The problem, who it's for, success metrics, non-goals |
| [2. Requirements and constraints](phase-2-requirements-constraints.md) | Scenarios, edge cases, must / should / could, constraints |
| [3. Domain model](phase-3-domain-model.md) | Entities and how they relate |
| [4. Flows and failures](phase-4-flows-failures.md) | The request flow and what happens when each step fails |
| [5. Architecture and decisions](phase-5-architecture-decisions.md) | The ADRs: each decision, its alternatives, consequences and sources |
| [6. Risks and skeleton](phase-6-risks-skeleton.md) | The riskiest assumptions, tested with spikes before building |
| [7. Slices and plan](phase-7-slices-plan.md) | The work cut into vertical slices, with a fallback decided in advance |

The spike write-ups (the throwaway experiments that tested the riskiest
assumptions before the build) are in
[phase 6, under "Spikes"](phase-6-risks-skeleton.md#spikes-time-boxed-throwaway-experiments).
Each spike's result is recorded there.
