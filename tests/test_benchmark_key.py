"""The benchmark talks to the server in-process, with no headers, so its
key goes in each tool call's _meta. Drives the real toolset and injector
with pydantic-ai's TestModel (no AI model, no API spend)."""

import pytest
from fastmcp import Client
from pydantic_ai import Agent
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.models.test import TestModel

from app.auth.models import Workspace
from app.mcp.server import mcp
from scripts.run_benchmark import _make_api_key_injector  # pyright: ignore[reportPrivateUsage]

pytestmark = pytest.mark.asyncio


async def test_the_benchmark_injector_authenticates_tool_calls(
    seeded_workspace: tuple[Workspace, str],
) -> None:
    _, api_key = seeded_workspace
    toolset = MCPToolset(Client(mcp), process_tool_call=_make_api_key_injector(api_key))
    agent = Agent(TestModel(call_tools=["list_tables"]), toolsets=[toolset])

    result = await agent.run("what tables are there?")

    returns = [
        part.content
        for message in result.all_messages()
        for part in getattr(message, "parts", [])
        if isinstance(part, ToolReturnPart) and part.tool_name == "list_tables"
    ]
    assert returns, "list_tables was never called successfully"
    assert "demo_items" in str(returns[0])
