# Accuracy benchmark: plan

Written 2026-11-04, before any work starts. Decisions marked **(Arthur)**
need his OK first.

**Status (2026-11-05):** Arthur OK'd the plan. He chose all 70 tasks,
qwen3.5:4b only, 5 tries per task, with no extra local models and no paid
row. Step 1 is built in agent-eval-harness and dry-run with a scripted mock
model at $0. See "Built so far" at the end. **Run 1 happened on 2026-11-10**:
64.0% strict, with one false refusal (`age()`). The results are in
[the benchmark report](../benchmark.md).

"Project 13" below is Arthur's RL data-analyst environment. It is being
renamed from flight-analysts to sql-analyst-env. Its answers to this plan's
questions (2026-11-04) are recorded where they apply.

## The goal

A fixed set of business questions over Pagila, answered by an AI agent
through MCP-backbone's own tools. For each model it publishes three
things:

- **Answer accuracy:** how often the agent's answer is right.
- **Refusal rate:** how often the server refused the agent's queries, and
  whether each refusal was correct.
- **Cost per question:** dollars, plus tokens, time and queries, because a
  local model costs $0 in dollars.

## What already exists (reuse first)

| What | Where | What it gives this plan |
|---|---|---|
| Benchmark v1: 30 questions, `gpt-5-mini`, 29/30 strict | this repo: `docs/benchmark.md`, `scripts/run_benchmark.py`, `app/benchmark/` | The questions (now inside both task sets below) and the history. Its runner is superseded by the harness. |
| agent-eval-harness: a 40-task Pagila suite | `agent-eval-harness`: `suites/pagila.yaml`, `agent_eval/` | Inspect AI runner, strict and lenient grading, pass^5, sourced prices and a budget cap, provenance, `publish` to RESULTS.md, and a regression gate for comparing runs |
| Project 13's 70 public tasks | Project 13's repo: `tasks/public.yaml` | The harness's 40 plus 30 new ones, as 60 train and 10 dev tasks. Each has a checked answer key: it passes seven checks (C1 to C7), such as "the server accepts it" and "the likeliest wrong reading gives a different answer". Each also has a second key written differently, the likeliest wrong reading, an `is_ordered` flag, and a canary string. Three "tempting" tasks (n44 to n46) ask for a write; two more are held out. |
| Project 13's stack | Project 13's repo: `compose.yaml`, `stack/` | Postgres 16 in UTC, two perturbed copies of Pagila (A and B), MCP-backbone at a pinned commit, and one workspace per copy with raised limits. Keys are in `/out/keys.json` inside the stack. |
| Project 13's local models | Ollama on this Mac | qwen3:1.7b, qwen3.5:4b and llama3.2:3b are already pulled and approved (~7 GB), with settings known to work: a 24,576-token context, replies capped at 4,096 tokens, sampling set explicitly |

So this plan writes **no new questions, no new grader and no new runner.**
The new work is:
- **Glue:** a harness target, a task reader and prices for the Project 13
  stack.
- **Two small grader and measurement additions:** ordered comparison, and
  refusals counted per trial.
- **Reading the refusals:** each one checked by hand.
- **The write-up.**

## Decisions

### D1. Which questions: Project 13's 70 public tasks

**Options considered:**

| Option | Verdict |
|---|---|
| v1's 30 | Too few, and all already inside the 70. |
| The harness's 40 | Also inside the 70. |
| **Project 13's 70 public tasks** | **Chosen.** All checked, with a dev split. |
| Project 13's 30 held-out tasks too | **Not used** (Project 13's advice, Arthur's call). Only Arthur runs held-out (Project 13's ADR-009), and the rule is once per model, which Project 13's own final runs use up. Every extra run makes the set less held out, and traces carry the prompts. |
| New questions | Ruled out by the reuse rule. |

How the tasks are used:
- **Published figures** cover all 70 tasks, with per-task results and the
  agent's SQL.
- **The 10 dev tasks** are also the quick check: any server change is tried
  on them first, before a full run.

**Without a held-out set, tuning is the risk.** v1's second run came after a
tool fix and a question rewrite, both prompted by first-run misses. A figure
from questions used to tune the server overstates how it does on new ones.
Two guards:
- server changes are diagnosed on the dev 10 only;
- any change made after seeing benchmark results is disclosed beside the
  figure, as v1 does.

If Arthur later wants a held-out figure, he decides that after Project 13's
final records are committed (open question below).

**How the tasks reach the harness** (Project 13's answer):
- The harness reads `tasks/public.yaml` directly, at a pinned commit, rather
  than from a published export that could drift. The generated suite records
  that commit and the file's hash, as `pagila.yaml` already records its
  source.
- The canary string is kept in the generated suite.
- `reference_sql` is the answer key.
- `is_ordered` is honoured: those tasks need rows in the right order (see
  D3).
- `alternative_sql` and `wrong_sql` become self-tests when the suite is
  generated. On copy A, the alternative must grade as right and the wrong
  reading as wrong, or the suite isn't written.

### D2. Which data: copy A, graded on copy A only

**Options considered:**

| Option | Verdict |
|---|---|
| Public Pagila | The models have likely seen it. The harness's "no schema tools" run lost only 3 points (95.5% to 92.5%), which suggests some answers came from memory, not from the tools. |
| **Copy A** (perturbed by a seeded script) | **Chosen.** A remembered answer is wrong on it, so a right answer means the agent used the server. |
| Copy A and copy B, as Project 13 grades | Not needed here. Grading on B stops a policy being trained to hard-code A's answers. A fixed model can't know A's answers without querying, so it adds a second grading pass for nothing. |

**This figure is not Project 13's figure.** Project 13 grades on both copies,
with its own runner and a `submit_answer` tool. The two numbers aren't
comparable, and the write-up says so.

### D3. Which runner: agent-eval-harness

**Options considered:**

| Option | Verdict |
|---|---|
| **agent-eval-harness** (Inspect AI) | **Chosen.** It is the portfolio's eval tool, built for exactly this: an MCP server, a suite, sourced costs, published records and a regression gate. Inspect runs Ollama models as `ollama/<model>`. |
| Project 13's runner (verifiers) | Rejected. It is an RL environment. It adds a `submit_answer` tool that real MCP clients don't have, and pins verifiers 0.4.0 and a different `mcp` version. |
| v1's `scripts/run_benchmark.py` | Rejected. A hand-written loop with no cost tracking, no pass^5 and no intervals: the harness already does all three. |

**Grading** is the harness's. It uses execution accuracy: the rows the
agent's query returned, compared with the answer key's rows, not the SQL
text.
- **Strict** (the headline): the same columns in the same order, with equal
  values. Rows may come in any order, unless the task is `is_ordered`.
- **Lenient** (shown beside it): numbers may differ by up to 0.005, and
  extra columns are allowed.

The harness has no ordered comparison yet. Project 13 added one,
`rows_equal_strict_in_order`, for questions that ask for an order. The
harness gains the same check (step 1d below). Project 13's G5 test shows its
grader agrees with all 212 of the harness's committed verdicts.

### D4. Which models: local first, at $0

| Model | Role | Cost |
|---|---|---|
| **qwen3.5:4b** | The headline row: the largest model already approved | $0, already pulled |
| qwen3:1.7b, llama3.2:3b | Extra rows, only if Arthur wants the extra machine time (D6) | $0, already pulled |
| gpt-5-mini via Sumopod | Optional reference row, comparable with v1 and the harness's runs **(Arthur)** | About $0.95 for 70 tasks × 5 tries, from the harness's measured $0.0027 a trial. Cap: `--budget 2` |

- **No new model downloads.** Any new model needs its size reported and
  Arthur's OK first.
- **The paid row is optional.** It needs Arthur's OK on both the key and the
  cap. Without it, the write-up says the local rows are small models, so a
  low score measures the model as much as the server.

### D5. What each figure means

**Accuracy**
- Strict pass@1: each task's share of correct tries, averaged over tasks.
  Shown with a 95% bootstrap interval, the harness's method.
- Lenient pass@1 next to it.
- pass^5: the number of tasks the agent got right on all 5 tries.

**Refusal rate**
- A refusal is a `run_query` result with status `rejected` or `timed_out`.
- **Per question:** the share of tries in which the server refused at least
  one of the agent's queries.
- **Per query:** refused queries divided by all `run_query` calls.
- **Split by reason:**
  - the validator (a write, several statements, a forbidden function or a row
    lock);
  - the cost check;
  - the timeout.
- **SQL the validator couldn't parse is not a refusal.** That's the agent's
  syntax, not the server's judgement, so it's counted on its own line and
  left out of the totals.
- **Rate limits are not refusals.** The harness already marks those trials as
  errors, and the benchmark workspace has raised limits so they don't
  happen.
- **On the 3 tempting tasks, a refusal is the right answer.** They are
  reported on their own line: "write attempts refused: N of N".
- **Every refusal on the other tasks is read by hand.** Was the query really
  unsafe or too costly (a correct refusal), or a legitimate read the server
  blocked (a false refusal)? Each false refusal becomes an MCP-backbone
  issue, with a repro. The published false-refusal count is the server's own
  figure. The other numbers mix the model and the server.

**Cost per question**
- The mean and median of:
  - input and output tokens;
  - wall time;
  - `run_query` calls.
- Dollars:
  - Local rows show $0. The write-up says this means no API cost; the Mac's
    hardware and power aren't counted.
  - The paid row is priced from the harness's `prices.yaml`, whose entries
    carry a source.

### D6. Where and when it runs: Arthur's Mac, after Project 13

**Options considered:**

| Option | Verdict |
|---|---|
| **Arthur's Mac**, reusing Project 13's stack | **Chosen.** Ollama and the stack already run there, at $0. Project 13 agreed to the reuse. |
| A separate copy of the stack | Rejected. It would be a second copy of the same data, and a second heavy stack on the same Mac. |
| The production server | Rejected. A small shared server that also hosts the public demo, with no room for a model, and nothing runs on it without Arthur. |
| Rented GPUs | Rejected: not $0. |

**When:**
- Not before Project 13 has finished. It has 6 model runs left, one at a
  time, which it expects to take 8 to 12 hours, plus its rename. It tells
  the orchestrator when its stack is stopped.
- Then this benchmark starts the stack itself (`docker compose up -d` in
  Project 13's repo) and stops it when done (`docker compose down`).
- Heavy jobs one at a time, and at most 3 sessions active.

**Settings:** the same Ollama server and settings as Project 13:
`OLLAMA_CONTEXT_LENGTH=24576`, `OLLAMA_NUM_PARALLEL=1`, and the harness's
`--max-samples 1`.

**How long:**
- Project 13's qwen3:1.7b dev run took 87 minutes for 50 tries, about 1.7
  minutes a try. At that rate, 70 tasks × 5 tries (350) is about 10 hours
  for qwen3:1.7b, and qwen3.5:4b is slower.
- The harness's loop differs (no `submit_answer`, a different turn limit),
  so this is an estimate. Step 3 measures it on the dev 10 first.
- That's why the plan's minimum is **one local model**: qwen3.5:4b, run
  overnight. The other two are Arthur's call.
- If even one model is too long, the fallback is 3 tries a task instead of
  5, reporting pass^3, chosen before the run, not after.

**The server under test** is MCP-backbone at a pinned commit (the current
`main` when the run starts), recorded in the run's provenance. Project 13's
stack pins an older commit, so the benchmark moves that pin for its own run
and puts it back after.

### D7. What gets published, and where

**In this repo, `docs/benchmark.md`:**
- A new "v2" section at the top, with one row per model: strict, lenient,
  pass^5, refusal rate (with false refusals), cost per question, plus the
  provenance (harness commit, MCP-backbone commit, Project 13's task-file
  commit, data fingerprint, and the model's digest and settings).
- The hand reading of each refusal, and notes on the misses, as v1 has.
- A line saying the figure is copy A only, and not comparable with Project
  13's.
- v1 stays below it, unchanged, as history.
- The README's one-line headline is updated to match.

**In the harness repo:** the run records (`runs/<id>/`) are committed and
listed by `agent-eval publish`, as its other runs are. `docs/benchmark.md`
links to them rather than copying them.

## Work, in order (after Arthur OKs the plan)

| # | Step | Repo / owner | Heavy? |
|---|---|---|---|
| 1 | Harness changes, each in its own PR:<br>a) a target for the Project 13 stack: copy A's port, and its key read from the container, never a host file;<br>b) a reader that builds a suite from Project 13's `public.yaml` at a pinned commit, keeping the canary, with the `alternative_sql` and `wrong_sql` self-tests;<br>c) `ollama/*` model args and $0 price entries, with the source "runs locally";<br>d) ordered comparison for `is_ordered` tasks;<br>e) refusals counted per trial from `run_query`'s `status`, by reason, in the records, the report and `publish` | agent-eval-harness, its own session (SESSIONS.md) | no |
| 2 | Wait for Project 13 to say its stack is stopped. Then start it, and `generate-expected` against copy A | this session | light |
| 3 | Dev run: qwen3.5:4b, 10 dev tasks × 5 tries. It measures the time per try, and checks the refusal counts against MCP-backbone's audit log for the benchmark workspace | this session, on the Mac | light |
| 4 | Full run on all 70 tasks: qwen3.5:4b, plus the other models and the paid row only if Arthur says so. One at a time, nothing else heavy running. Stop the stack after | this session | **heavy** |
| 5 | Read every refusal and every miss by hand. File false refusals as MCP-backbone issues with repros | this session | no |
| 6 | Write the v2 section and the README line. Then a branch, the checks, a push | this session | no |

MCP-backbone itself needs no code change for this. `run_query` already
returns `status` and `error`, which is everything the refusal count needs.
`/healthz` already reports the commit for provenance.

**Done means:** `docs/benchmark.md` shows, for at least qwen3.5:4b, on the 70
public tasks:
- the strict and lenient accuracy;
- pass^5;
- the refusal rate, by reason, with every refusal classified as correct or
  false;
- the cost per question;
- the provenance of every number.

## Risks

- **Small local models may score low.**
  - Project 13's first smoke run of qwen3:1.7b scored 0 of 10. That was
    because the model never called its `submit_answer` tool, which the
    harness doesn't use.
  - A low score is still a fair published number, but then it says more
    about the model than the server. The paid reference row (D4) is the
    remedy, and it's Arthur's choice.
- **Tuning to the questions:** there's no held-out set. D1's two guards
  apply.
- **Settings drift:** Inspect's Ollama route might not pass the same
  sampling, context and reply-cap settings as Project 13's runner. Step 3
  checks the settings Ollama reports, and the record stores them.
- **Collisions:** the benchmark and Project 13 share Ollama and the stack.
  They never run at the same time (D6).
- **Task-file drift:** the task file moves with Project 13's rename. The pin
  is a commit, not a path, so a moved file fails loudly instead of changing
  the questions.

## Open questions

- **Arthur:**
  - ~~OK the plan; the paid row; the two extra local models.~~ Answered
    2026-11-05: OK'd, with neither the paid row nor the extra models.
  - ~~The per-trial caps for the local run (D6).~~ Answered 2026-11-05:
    600 s and 250,000 tokens. The harness's defaults (120 s and 60,000
    tokens, a capped trial counting as a fail) were set for gpt-5-mini.
    qwen3:1.7b averaged 1.7 minutes a try in Project 13, and qwen3.5:4b is
    slower, so on the defaults many trials would fail on a cap, not on the
    answer.
    - The caps guard against runaways at $0. They aren't part of what's
      measured, and the run's metadata records them.
    - The dev run reports how many trials hit a cap. If more than a few
      do, the caps are revisited before the full run, not after it.
  - Later, only if he wants it: a held-out figure, decided after Project 13's
    final records are committed. Project 13 recommends against it.
- **Follow-up, separate and Arthur's call:** retire v1's runner
  (`scripts/run_benchmark.py`, `scripts/seed_eval_cases.py`, `app/benchmark/`
  and its tables) once v2 is published. The harness carries the same
  questions. `docs/journey.md` keeps the history.

## Built so far (2026-11-05)

Step 1 is in agent-eval-harness, in four PRs. Merge them in this order:
`feat/ordered-result-match` (1d), then `feat/sql-analyst-env-benchmark`
(1a to 1c, stacked on it), then `feat/trial-limit-options` (the
`--time-limit` and `--token-limit` options, stacked on that).
`feat/refusal-counts` (1e) can merge at any time.

The dry run cost $0, used no Docker and ran no real model. It ran a
scripted mock model through the real command-line path, against a stand-in
for copy A:
- `generate-expected` wrote all 70 tasks, keeping the canary, the commit
  and the hash.
- A run over the 10 dev tasks went through preflight, the budget, the
  records and the report, and `publish` rendered the refusals table.
- The refusals came out as scripted, by reason. The unparsable query was
  counted on its own line, and the tempting task got its own line.

What the dry run couldn't cover:
- copy A's real answers;
- a real Ollama model;
- `/healthz`;
- an ordered task through the command line. Unit tests cover that.

Bug found and fixed in the harness: Inspect refused to price any model
missing from its own database, which every Ollama model is. Without the fix,
every local run would have stopped before its first trial.

The commands for steps 2 to 4 are in `docs/benchmark.md`, under "v2".
