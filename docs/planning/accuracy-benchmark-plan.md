# Accuracy benchmark: plan

Written 2026-11-04, before any work starts. Planning only: nothing here has
been built or run. Decisions marked **(Arthur)** need his OK first.

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
| Benchmark v1: 30 questions, `gpt-5-mini`, 29/30 strict | this repo: `docs/benchmark.md`, `scripts/run_benchmark.py`, `app/benchmark/` | The questions (now inside both suites below) and the history. Its runner is superseded by the harness. |
| agent-eval-harness: a 40-task Pagila suite | `agent-eval-harness`: `suites/pagila.yaml`, `agent_eval/` | Inspect AI runner, strict and lenient grading, pass^5, sourced prices and a budget cap, provenance, `publish` to RESULTS.md, and a regression gate for comparing runs |
| Project 13 (flight-analysts): 100 tasks | `flight-analysts`: `tasks/public.yaml` (70) and a private held-out file (30) | Checked answer keys: every task passes seven checks (C1 to C7), including "the server accepts the answer key" and "the likeliest wrong reading gives a different answer". Train, dev and held-out splits. Two perturbed copies of Pagila. Five "tempting" tasks that ask for a write. |
| Project 13's stack | `flight-analysts`: `compose.yaml`, `stack/` | Postgres 16 in UTC, copy A and copy B, MCP-backbone at a pinned commit, one workspace per copy with raised limits |
| Project 13's local models | Ollama on this Mac | qwen3:1.7b, qwen3.5:4b and llama3.2:3b are already pulled and approved (~7 GB), with settings that are known to work: 24,576-token context, replies capped at 4,096 tokens, sampling set explicitly |

So this plan writes **no new questions, no new grader and no new runner.**
The new work is:
- **Glue:** a harness target and prices for the Project 13 stack.
- **One new measurement:** refusals, counted per trial.
- **Reading the refusals:** each one checked by hand.
- **The write-up.**

## Decisions

### D1. Which questions: Project 13's 100 tasks

**Options considered:**

| Option | Verdict |
|---|---|
| v1's 30 | Too few. Public for weeks, and no held-out set. |
| The harness's 40 | Public, and contained in Project 13's 100. |
| **Project 13's 100** | **Chosen.** The 40 plus 60 new ones, all checked, with a private held-out set. |
| New questions | Ruled out by the reuse rule. |

How the splits are used:

| Split | Tasks | Used for | Published |
|---|---|---|---|
| dev | 10 | Diagnosing a server change before a full run | Not as a headline |
| public (train + dev) | 70 | The main figure | In full: per-task results and the agent's SQL |
| held-out | 30 | The final figure, run once per release | Totals only |

Why a held-out set matters for a server benchmark: v1's second run came after
a tool fix (`list_tables` hiding partitions) and a question rewrite, both
prompted by first-run misses. That is fine, but a figure from questions used
to tune the server overstates how it does on new ones. The held-out 30 are
the check on that.

**Pending Project 13** (asked 2026-11-04):
- that the held-out file may be used this way;
- the format the tasks reach the harness in.

### D2. Which data: copy A, graded on copy A only

**Options considered:**

| Option | Verdict |
|---|---|
| Public Pagila | The models have likely seen it. The harness's "no schema tools" run lost only 3 points (95.5% to 92.5%), which suggests some answers came from memory, not from the tools. |
| **Copy A** (perturbed by a seeded script) | **Chosen.** A remembered answer is wrong on it, so a right answer means the agent used the server. |
| Copy A and copy B, as Project 13 grades | Not needed here. Grading on B stops a policy being trained to hard-code A's answers. A fixed model can't know A's answers without querying, so it adds a second grading pass for nothing. |

### D3. Which runner: agent-eval-harness

**Options considered:**

| Option | Verdict |
|---|---|
| **agent-eval-harness** (Inspect AI) | **Chosen.** It is the portfolio's eval tool, built for exactly this: an MCP server, a suite, sourced costs, published records and a regression gate. Inspect runs Ollama models as `ollama/<model>`. |
| Project 13's runner (verifiers) | Rejected. It is an RL environment. It adds a `submit_answer` tool that real MCP clients don't have, and pins verifiers 0.4.0 and a different `mcp` version. |
| v1's `scripts/run_benchmark.py` | Rejected. A hand-written loop with no cost tracking, no pass^5 and no intervals: the harness already does all three. |

**Grading** is the harness's, unchanged. It uses execution accuracy: the rows
the agent's query returned, compared with the answer key's rows, not the SQL
text.
- **Strict** (the headline): the same columns in the same order, with equal
  values; rows may come in any order.
- **Lenient** (shown beside it): numbers may differ by up to 0.005, and
  extra columns are allowed.

Project 13's G5 test shows its copy of this grader agrees with all 212 of the
harness's committed verdicts.

### D4. Which models: local first, at $0

| Model | Role | Cost |
|---|---|---|
| **qwen3.5:4b** | The headline row: the largest model already approved | $0, already pulled |
| qwen3:1.7b, llama3.2:3b | Extra rows, if time allows | $0, already pulled |
| gpt-5-mini via Sumopod | Optional reference row, comparable with v1 and the harness's runs **(Arthur)** | About $1.35 for 100 tasks × 5 tries, from the harness's measured $0.0027 a trial. Cap: `--budget 2` |

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
- **Rate limits are not refusals.** The harness already marks those trials as
  errors, and the benchmark workspace gets raised limits so they don't happen.
- **On the 5 tempting tasks, a refusal is the right answer.** They are
  reported on their own line: "write attempts refused: N of N".
- **Every refusal on the other public tasks is read by hand.** Was the query
  really unsafe or too costly (a correct refusal), or a legitimate read the
  server blocked (a false refusal)? Each false refusal becomes an MCP-backbone
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

### D6. Where it runs: Arthur's Mac, one job at a time

**Options considered:**

| Option | Verdict |
|---|---|
| **Arthur's Mac** | **Chosen.** Ollama and Project 13's Docker stack already run there, at $0. |
| The production server | Rejected. A small shared server that also hosts the public demo, with no room for a model, and nothing runs on it without Arthur. |
| Rented GPUs | Rejected: not $0. |

Constraints on running:
- **Heavy jobs one at a time.** No benchmark run while Project 13's model runs
  are going; at most 3 sessions active.
- **Shared Ollama settings:** the benchmark uses the same Ollama server, with
  the same settings, as Project 13: `OLLAMA_CONTEXT_LENGTH=24576`,
  `OLLAMA_NUM_PARALLEL=1`, and `--max-samples 1`.
- **Time is not yet measured.** If a local try takes 30 to 60 seconds, a full
  run (100 tasks × 5 tries = 500) takes about 4 to 8 hours per model. The dev
  smoke run (step 4 below) measures this before anything bigger is
  scheduled.
- **The server under test** is MCP-backbone at a pinned commit, the current
  `main` when the run starts, recorded in the run's provenance.

### D7. What gets published, and where

**In this repo, `docs/benchmark.md`:**
- A new "v2" section at the top, with one row per model: strict, lenient,
  pass^5, refusal rate (with false refusals), cost per question, plus the
  provenance (harness commit, MCP-backbone commit, data fingerprint, and the
  model's digest and settings).
- The hand reading of each refusal, and notes on the misses, as v1 has.
- v1 stays below it, unchanged, as history.
- The README's one-line headline is updated to match.

**In the harness repo:** the public runs' records (`runs/<id>/`) are committed
and listed by `agent-eval publish`, as its other runs are. `docs/benchmark.md`
links to them rather than copying them.

**The held-out run:**
- Its records hold the prompts, so they are written outside every repo, with
  `--out` pointed beside the private held-out file.
- Only its totals are copied into `docs/benchmark.md`, marked "not
  reproducible by others: the tasks are private".

## Work, in order (after Arthur OKs the plan)

| # | Step | Repo / owner | Heavy? |
|---|---|---|---|
| 1 | Settle the open questions with Project 13 (below) | this session, by message | no |
| 2 | Harness changes, each in its own PR:<br>a) a target for the Project 13 stack (copy A's port and key, read from the container, never a host file);<br>b) `ollama/*` model args and $0 price entries, with the source "runs locally";<br>c) refusals counted per trial from `run_query`'s `status`, by reason, in the records, the report and `publish`;<br>d) a test that `--out` outside the repo leaves nothing behind in it | agent-eval-harness, its own session (SESSIONS.md) | no |
| 3 | Suite: convert the 70 public tasks (and, separately, the 30 held-out) to the harness's source format, then `generate-expected` against copy A | whoever D1's answer names | no |
| 4 | Dev smoke run: qwen3.5:4b, 10 dev tasks × 5 tries. Measures the time per try and checks the refusal counts against MCP-backbone's audit log for the benchmark workspace | this session, on the Mac | light |
| 5 | Full public run, model by model, each only when nothing else heavy is running | this session | **heavy** |
| 6 | Read every refusal and every miss by hand. File false refusals as MCP-backbone issues with repros | this session | no |
| 7 | Held-out run, once per model | this session | **heavy** |
| 8 | Write the v2 section and the README line. Then a branch, the checks, a push | this session | no |

MCP-backbone itself needs no code change for this. `run_query` already
returns `status` and `error`, which is everything the refusal count needs.
`/healthz` already reports the commit for provenance.

**Done means:** `docs/benchmark.md` shows, for at least qwen3.5:4b, the
following on the 70 public tasks and on the 30 held-out ones:
- the strict and lenient accuracy;
- pass^5;
- the refusal rate, by reason, with every public refusal classified as
  correct or false;
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
- **Settings drift:** Inspect's Ollama route might not pass the same
  sampling, context and reply-cap settings as Project 13's runner. Step 4
  checks the settings Ollama reports, and the record stores them.
- **Held-out leaks:** a record or trace with prompts committed by mistake.
  Step 2d's test, plus a grep of the held-out ids before every push.
- **Collisions:** the benchmark and Project 13 both use the same Ollama and
  stack. They run one at a time and never overlap (D6).

## Open questions

- **Project 13** (asked 2026-11-04, answers pending):
  1. May the held-out 30 be used as D1 and D7 describe?
  2. Should `public.yaml` be converted as it is, exported by Project 13, or
     read by the harness directly?
  3. Should the benchmark reuse their stack, or run its own copy?
  4. When does their current model run finish?
- **Arthur:**
  - OK the plan.
  - Decide on the paid reference row and its cap (D4).
- **Follow-up, separate and Arthur's call:** retire v1's runner
  (`scripts/run_benchmark.py`, `scripts/seed_eval_cases.py`, `app/benchmark/`
  and its tables) once v2 is published. The harness carries the same
  questions. `docs/journey.md` keeps the history.
