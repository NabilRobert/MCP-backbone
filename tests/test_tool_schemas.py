"""Every tool's published input schema must pass OpenAI's strict mode,
which some clients turn on for every tool (Inspect AI does by default):
an object with additionalProperties false, every property required, and
no `default` keyword. An optional api_key argument used to break this,
rejecting every request with a 400 before the model ran."""

from typing import Any

import pytest

from app.mcp.server import mcp

pytestmark = pytest.mark.asyncio


def strict_mode_problems(schema: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    if schema.get("type") != "object":
        problems.append("not an object")
    if schema.get("additionalProperties") is not False:
        problems.append("additionalProperties isn't false")
    properties: dict[str, Any] = schema.get("properties", {})
    missing = set(properties) - set(schema.get("required", []))
    if missing:
        problems.append(f"not required: {sorted(missing)}")
    problems += [f"{name} has a default" for name, prop in properties.items() if "default" in prop]
    return problems


async def test_every_tool_schema_is_strict_mode_valid() -> None:
    tools = await mcp.list_tools()
    assert {t.name for t in tools} == {"list_tables", "describe_table", "sample_rows", "run_query"}
    problems = {t.name: strict_mode_problems(t.input_schema) for t in tools}
    assert problems == {t.name: [] for t in tools}


async def test_no_tool_takes_the_key_as_an_argument() -> None:
    """An argument travels through the model's context and transcripts.
    The key comes in the Authorization header (HTTP) or the request's
    _meta (local transports), neither of which the model sees."""
    for tool in await mcp.list_tools():
        assert "api_key" not in tool.input_schema.get("properties", {}), tool.name
