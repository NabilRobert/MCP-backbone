"""Runs the Slice E text-to-SQL benchmark: for each EvalCase, a PydanticAI
agent connects to this server as a real MCP client, explores the schema,
and answers the question. Correctness is execution accuracy -- the
agent's query result compared to the expected_sql's result by value (see
app/benchmark/grading.py for exactly what that does and doesn't cover),
not a text match against expected_sql.

Needs ANTHROPIC_API_KEY (or whatever BENCHMARK_MODEL's provider expects)
set in the environment -- your own key, your own cost. See .env.example.
"""

import argparse
import asyncio
import os
import time
from typing import Any, cast

from fastmcp import Client
from pydantic_ai import Agent, RunContext
from pydantic_ai.mcp import CallToolFunc, MCPToolset
from pydantic_ai.messages import ModelMessage, ToolCallPart, ToolReturnPart
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.auth.service import authenticate
from app.benchmark.grading import rows_equal
from app.benchmark.models import BenchmarkResult, EvalCase
from app.db import SessionLocal
from app.mcp.server import mcp
from app.query.service import get_data_source

AGENT_INSTRUCTIONS = (
    "You answer questions about a database by using the available tools: "
    "list_tables, describe_table, sample_rows, and run_query. Always "
    "explore the schema before writing SQL. Call run_query exactly once "
    "with your final answer query."
)


def _make_api_key_injector(api_key: str) -> Any:
    """The model never needs to see or handle the real API key -- it's
    injected into every tool call here, overriding whatever (if anything)
    the model supplied. Confirmed necessary, not just tidier: a real model
    could mistype or omit it; this makes that impossible rather than
    hoping the model gets it right."""

    async def inject(
        _ctx: RunContext[Any], call_tool: CallToolFunc, name: str, args: dict[str, Any]
    ) -> Any:
        return await call_tool(name, {**args, "api_key": api_key})

    return inject


async def _ground_truth(connection_url: str, sql: str) -> list[dict[str, Any]]:
    engine = create_async_engine(connection_url)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text(sql))
            return [dict(row._mapping) for row in result]  # pyright: ignore[reportPrivateUsage]
    finally:
        await engine.dispose()


def _extract_last_run_query_call(
    messages: list[ModelMessage],
) -> tuple[str | None, list[dict[str, Any]] | None]:
    """Pulls the SQL actually sent and the structured result out of the
    last run_query tool call in the agent's message history, rather than
    trusting the agent's own prose summary of what it found."""
    call_args_by_id: dict[str, dict[str, Any]] = {}
    for message in messages:
        for part in getattr(message, "parts", []):
            if isinstance(part, ToolCallPart) and part.tool_name == "run_query":
                call_args_by_id[part.tool_call_id] = part.args_as_dict()

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
    return None, None


async def run(api_key: str) -> None:
    async with SessionLocal() as session:
        workspace = await authenticate(session, api_key)
        data_source = await get_data_source(session, workspace)
        target_connection_url = data_source.connection_url

        eval_cases = (await session.execute(select(EvalCase))).scalars().all()
    if not eval_cases:
        raise SystemExit("No eval cases found -- run scripts/seed_eval_cases.py first.")

    model = os.environ.get("BENCHMARK_MODEL", "anthropic:claude-sonnet-4-5")
    toolset = MCPToolset(Client(mcp), process_tool_call=_make_api_key_injector(api_key))
    agent = Agent(model, instructions=AGENT_INSTRUCTIONS, toolsets=[toolset])

    correct_count = 0
    async with SessionLocal() as session:
        for case in eval_cases:
            expected_rows = await _ground_truth(target_connection_url, case.expected_sql)

            start = time.monotonic()
            result = await agent.run(case.question)
            elapsed = time.monotonic() - start

            actual_sql, actual_rows = _extract_last_run_query_call(result.all_messages())
            correct = actual_rows is not None and rows_equal(actual_rows, expected_rows)
            correct_count += int(correct)

            session.add(
                BenchmarkResult(
                    eval_case_id=case.id,
                    actual_sql=actual_sql or "<no successful run_query call>",
                    correct=correct,
                )
            )
            await session.commit()

            status = "PASS" if correct else "FAIL"
            print(f"[{status}] ({elapsed:.1f}s) {case.question}")
            print(f"       actual SQL: {actual_sql!r}")

    print(f"\n{correct_count}/{len(eval_cases)} correct ({correct_count / len(eval_cases):.0%})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--api-key",
        required=True,
        help="A workspace's API key, shown once when scripts/seed_workspace.py ran.",
    )
    args = parser.parse_args()

    asyncio.run(run(args.api_key))


if __name__ == "__main__":
    main()
