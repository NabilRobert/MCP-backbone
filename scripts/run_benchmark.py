"""Runs the Slice E text-to-SQL benchmark.

What this script does, in plain terms: it loads a list of hand-written
questions (EvalCase rows, e.g. "How many customers are there?"), and for
each one, it lets an AI agent try to answer it by actually using this
project's own MCP server -- the same way a real AI client would. The
agent explores the database schema, writes a SQL query, and runs it
through our safety-checked run_query tool. We then compare what the
agent's query actually returned against the known-correct answer
(computed by running expected_sql ourselves) and record pass/fail.

"Correctness" means execution accuracy: we compare the VALUES the query
returned, not the SQL text. Many different SQL queries can correctly
answer the same question (different column aliases, different ORDER BY),
so comparing text would unfairly fail correct answers. See
app/benchmark/grading.py for exactly what this comparison does and
doesn't catch.

To actually run this for real (not just import it), you need an AI
provider set up in the environment. The main setup this project uses is
Sumopod, an OpenAI-compatible AI gateway (one key, one shared address,
access to several underlying models) -- see the SUMOPOD_* and AI_MODEL
variables in .env.example. A direct-Anthropic fallback also exists if
SUMOPOD_API_KEY isn't set. Either way, it's your own key and your own
cost to run this.
"""

import argparse
import asyncio
import datetime
import os
import time
from pathlib import Path
from typing import Any, cast

from dotenv import load_dotenv
from fastmcp import Client
from pydantic_ai import Agent, RunContext
from pydantic_ai.mcp import CallToolFunc, MCPToolset
from pydantic_ai.messages import ModelMessage, ToolCallPart, ToolReturnPart
from sqlalchemy import select, text

# app/config.py's Settings reads .env for the app's own APP_* variables,
# but that doesn't put anything into the process environment for OTHER
# code to see -- so without this, os.environ.get("SUMOPOD_API_KEY")
# below would never find a value even if it's sitting right there in
# .env. This line is what actually makes that file's values visible to
# this script.
load_dotenv()

from app.auth.service import authenticate
from app.benchmark.grading import rows_equal, rows_equal_strict
from app.benchmark.models import BenchmarkResult, EvalCase
from app.db import SessionLocal, create_target_engine
from app.mcp.server import mcp
from app.query.service import get_data_source

# The plain-English instructions given to the AI agent before it starts
# answering questions. This is the only "prompt engineering" in this
# script -- everything else is enforced by code (the MCP server's own
# safety checks), not by politely asking the model to behave.
QUESTION_TIMEOUT_SECONDS = 180
DIFFICULTY_ORDER = ["easy", "medium", "hard", "unrated"]

AGENT_INSTRUCTIONS = (
    "You answer questions about a database by using the available tools: "
    "list_tables, describe_table, sample_rows, and run_query. Always "
    "explore the schema before writing SQL. Call run_query exactly once "
    "with your final answer query."
)


def _build_model() -> Any:
    """Picks which AI provider to talk to, based on environment variables
    (see .env.example). There are two supported setups:

    1. (The main one this project uses.) Go through Sumopod, an
       OpenAI-compatible AI gateway -- one account/key gives access to
       several different underlying models, instead of signing up with
       each provider separately. Set SUMOPOD_API_KEY to your Sumopod
       key, SUMOPOD_BASE_URL to Sumopod's API address, and AI_MODEL to
       whichever model Sumopod should use (e.g. "gpt-5-mini").

    2. (Fallback.) Call Anthropic directly instead -- used only if
       SUMOPOD_API_KEY isn't set. Set AI_MODEL to something like
       "anthropic:claude-sonnet-4-5" and ANTHROPIC_API_KEY to a normal
       Anthropic key. PydanticAI reads ANTHROPIC_API_KEY automatically --
       we don't even touch it in this file.

    AI_MODEL has no default here on purpose, in either setup. A silent
    fallback to some hardcoded model would mean this could end up
    calling a real, possibly expensive model (Claude Sonnet, say)
    without anyone actually choosing that -- the whole reason this
    docstring says "it's your own key and your own cost" is undermined
    if the script can quietly pick which model that cost is against."""
    sumopod_key = os.environ.get("SUMOPOD_API_KEY")
    model_name = os.environ.get("AI_MODEL")
    if not model_name:
        raise SystemExit("AI_MODEL is not set in .env -- see .env.example for what to put there.")

    if not sumopod_key:
        # Fallback setup: hand the plain model string straight to
        # PydanticAI and let it figure out which provider to use.
        return model_name

    # Main setup: build a model that talks to Sumopod's address instead
    # of Anthropic's own servers, using the Sumopod key.
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    base_url = os.environ.get("SUMOPOD_BASE_URL")
    if not base_url:
        raise SystemExit(
            "SUMOPOD_API_KEY is set but SUMOPOD_BASE_URL is not -- "
            "add Sumopod's API address to .env (check Sumopod's own docs/dashboard)."
        )
    provider = OpenAIProvider(base_url=base_url, api_key=sumopod_key)
    return OpenAIChatModel(model_name, provider=provider)


def _make_api_key_injector(api_key: str) -> Any:
    """This is a safety detail, not just tidiness. Every call to our MCP
    tools (list_tables, run_query, etc.) needs a workspace API key -- but
    we never want the AI model itself to see, remember, or have to
    correctly copy that key around. So instead of putting the key in the
    model's instructions and hoping it pastes it into every tool call
    correctly, this function silently stamps the real key onto every
    tool call before it leaves this script, overriding anything the
    model may have guessed. Tested and confirmed necessary: a test run
    without this showed every tool call failing with an auth error,
    because the model (reasonably) didn't know the key at all."""

    async def inject(
        _ctx: RunContext[Any], call_tool: CallToolFunc, name: str, args: dict[str, Any]
    ) -> Any:
        return await call_tool(name, {**args, "api_key": api_key})

    return inject


async def _ground_truth(connection_url: str, sql: str) -> list[dict[str, Any]]:
    """Runs the hand-written "known correct" SQL for one question
    directly against the database, bypassing the AI agent entirely. This
    is the answer key we grade the agent's own query against."""
    # The same factory the server uses: forces the asyncpg driver (a bare
    # "postgresql://" URL otherwise picks psycopg, which isn't installed)
    # and keeps the answer key's own queries read-only too.
    engine = create_target_engine(connection_url)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text(sql))
            return [dict(row._mapping) for row in result]  # pyright: ignore[reportPrivateUsage]
    finally:
        await engine.dispose()


def _extract_last_run_query_call(
    messages: list[ModelMessage],
) -> tuple[str | None, list[dict[str, Any]] | None]:
    """After the agent finishes, PydanticAI gives us its full "message
    history" -- everything it said and every tool it called, in order.
    This function digs through that history to find the SQL the agent
    actually sent to run_query, and the rows that query returned. We
    pull this from the raw tool-call data rather than trusting the
    agent's own written summary of what it found, since that summary
    could be wrong even if the query itself was right (or vice versa)."""
    # First pass: remember the arguments (including the "sql" text) of
    # every run_query call, keyed by that call's unique ID.
    call_args_by_id: dict[str, dict[str, Any]] = {}
    for message in messages:
        for part in getattr(message, "parts", []):
            if isinstance(part, ToolCallPart) and part.tool_name == "run_query":
                call_args_by_id[part.tool_call_id] = part.args_as_dict()

    # Second pass: walk backwards to find the most recent run_query call
    # that actually succeeded, and look up its matching SQL from above.
    for message in reversed(messages):
        for part in getattr(message, "parts", []):
            if isinstance(part, ToolReturnPart) and part.tool_name == "run_query":
                raw_content: object = part.content
                if isinstance(raw_content, dict):
                    content = cast(dict[str, Any], raw_content)
                    if content.get("status") == "succeeded" and isinstance(
                        content.get("rows"), list
                    ):
                        call_args = call_args_by_id.get(part.tool_call_id, {})
                        sql = call_args.get("sql")
                        return (sql if isinstance(sql, str) else None), cast(
                            list[dict[str, Any]], content["rows"]
                        )
    # The agent never made a successful run_query call at all.
    return None, None


async def run(api_key: str, report_path: str | None) -> None:
    # Step 1: figure out which workspace this API key belongs to, and
    # which real database it's allowed to query -- same lookup the MCP
    # server itself does on every request.
    async with SessionLocal() as session:
        workspace = await authenticate(session, api_key)
        data_source = await get_data_source(session, workspace)
        target_connection_url = data_source.connection_url

        eval_cases = sorted(
            (await session.execute(select(EvalCase))).scalars().all(),
            key=lambda c: (
                (DIFFICULTY_ORDER.index(c.difficulty), c.question)
                if c.difficulty in DIFFICULTY_ORDER
                else (len(DIFFICULTY_ORDER), c.question)
            ),
        )
    if not eval_cases:
        raise SystemExit("No eval cases found -- run scripts/seed_eval_cases.py first.")

    # Step 2: the agent, connected to this project's own MCP server (the
    # same safety-checked tools a real client uses), with the API key
    # injected into every call so the model never sees it.
    model = _build_model()
    model_name = os.environ.get("AI_MODEL", "")
    toolset = MCPToolset(Client(mcp), process_tool_call=_make_api_key_injector(api_key))
    agent = Agent(model, instructions=AGENT_INSTRUCTIONS, toolsets=[toolset])
    run_started_at = datetime.datetime.now(datetime.UTC)

    # Step 3: ask every question and grade each answer twice: leniently
    # (rows_equal: rounding within 0.005 and extra columns tolerated) and
    # strictly (rows_equal_strict: exactly the asked columns and values).
    outcomes: list[dict[str, Any]] = []
    async with SessionLocal() as session:
        for case in eval_cases:
            expected_rows = await _ground_truth(target_connection_url, case.expected_sql)

            start = time.monotonic()
            try:
                # A first full run hung for hours on one question (a model
                # or network call that never returned), so each question
                # gets a deadline and a hang counts as a failure.
                result = await asyncio.wait_for(agent.run(case.question), QUESTION_TIMEOUT_SECONDS)
                actual_sql, actual_rows = _extract_last_run_query_call(result.all_messages())
            except TimeoutError:
                actual_sql, actual_rows = f"<timed out after {QUESTION_TIMEOUT_SECONDS}s>", None
            elapsed = time.monotonic() - start

            lenient = actual_rows is not None and rows_equal(actual_rows, expected_rows)
            strict = actual_rows is not None and rows_equal_strict(actual_rows, expected_rows)
            outcomes.append(
                {
                    "case": case,
                    "lenient": lenient,
                    "strict": strict,
                    "sql": actual_sql,
                    "elapsed": elapsed,
                }
            )
            session.add(
                BenchmarkResult(
                    eval_case_id=case.id,
                    actual_sql=actual_sql or "<no successful run_query call>",
                    correct=lenient,
                    strict_correct=strict,
                    model=model_name,
                    run_started_at=run_started_at,
                )
            )
            await session.commit()

            marks = f"{'PASS' if lenient else 'FAIL'}/{'PASS' if strict else 'FAIL'}"
            print(f"[{marks}] ({elapsed:.1f}s) [{case.difficulty}] {case.question}", flush=True)
            print(f"       actual SQL: {actual_sql!r}", flush=True)

    summary = _summarise(outcomes)
    print()
    for label, (lenient, strict, total) in summary.items():
        print(f"{label:8} lenient {lenient}/{total}  strict {strict}/{total}")
    if report_path:
        _write_report(report_path, outcomes, summary, model_name, run_started_at)
        print(f"\nReport written to {report_path}")


def _summarise(outcomes: list[dict[str, Any]]) -> dict[str, tuple[int, int, int]]:
    """(lenient passes, strict passes, total) per difficulty, then overall."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for outcome in outcomes:
        groups.setdefault(outcome["case"].difficulty, []).append(outcome)
    order = DIFFICULTY_ORDER
    summary = {
        name: (
            sum(o["lenient"] for o in groups[name]),
            sum(o["strict"] for o in groups[name]),
            len(groups[name]),
        )
        for name in sorted(groups, key=lambda n: order.index(n) if n in order else len(order))
    }
    summary["overall"] = (
        sum(o["lenient"] for o in outcomes),
        sum(o["strict"] for o in outcomes),
        len(outcomes),
    )
    return summary


def _write_report(
    path: str,
    outcomes: list[dict[str, Any]],
    summary: dict[str, tuple[int, int, int]],
    model_name: str,
    run_started_at: datetime.datetime,
) -> None:
    lines = [
        "# Benchmark results",
        "",
        (
            f"Model `{model_name}`, run {run_started_at:%Y-%m-%d %H:%M} UTC, "
            f"{len(outcomes)} questions. Generated by `scripts/run_benchmark.py --report`."
        ),
        "",
        (
            "Each answer is graded by execution accuracy (the rows the agent's query returned, "
            "not its SQL text) twice:"
        ),
        "",
        (
            "- **Strict:** exactly the asked columns, in order, with exactly equal values "
            "(`rows_equal_strict`)."
        ),
        "- **Lenient:** also accepts numbers within 0.005 and extra columns (`rows_equal`).",
        "",
        "| | Strict | Lenient |",
        "|---|---|---|",
    ]
    for label, (lenient, strict, total) in summary.items():
        name = f"**{label}**" if label == "overall" else label
        lines.append(f"| {name} | {strict}/{total} | {lenient}/{total} |")
    lines += [
        "",
        "| # | Difficulty | Question | Strict | Lenient | Agent's SQL |",
        "|---|---|---|---|---|---|",
    ]
    for n, o in enumerate(outcomes, 1):
        sql = (o["sql"] or "(no successful query)").replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| {n} | {o['case'].difficulty} | {o['case'].question} | "
            f"{'✓' if o['strict'] else '✗'} | {'✓' if o['lenient'] else '✗'} | `{sql}` |"
        )
    Path(path).write_text("\n".join(lines) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--api-key",
        required=True,
        help="A workspace's API key, shown once when scripts/seed_workspace.py ran.",
    )
    parser.add_argument("--report", help="Also write a Markdown report to this path.")
    args = parser.parse_args()

    asyncio.run(run(args.api_key, args.report))


if __name__ == "__main__":
    main()
