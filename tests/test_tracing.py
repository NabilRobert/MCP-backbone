"""The trace spans a tool call produces (app/tracing.py): one child span
per stage under the MCP SDK's own span, joined to the calling agent's
trace through the request's _meta, with no SQL text in them unless content
capture is switched on -- and a dead trace viewer changes nothing.

Driven through the real MCP protocol over in-memory streams, as in
test_mcp_protocol.py, with the spans collected in memory."""

import asyncio
import contextlib
import http.server
import threading
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any

import pytest
from mcp.client.session import ClientSession
from mcp.shared.memory import create_client_server_memory_streams
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode
from sqlalchemy import text

from app.auth.models import Workspace
from app.config import settings
from app.db import engine
from app.mcp.server import mcp
from app.tracing import build_span_processor

pytestmark = pytest.mark.asyncio

# The global tracer provider can only be set once per process, so this
# module installs one for the whole run and clears its spans per test.
_exporter = InMemorySpanExporter()
_provider = TracerProvider()
_provider.add_span_processor(SimpleSpanProcessor(_exporter))
trace.set_tracer_provider(_provider)
_agent_tracer = trace.get_tracer("test-agent")

# Lowercase on purpose: Postgres folds unquoted identifiers to lowercase,
# so an error message quoting a column name still contains it verbatim.
MARKER = "zzmarker7f3a"

STAGES = ["authenticate", "rate_limit", "validate", "cost_check", "execute", "audit"]

Call = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


@contextlib.asynccontextmanager
async def mcp_client(api_key: str) -> AsyncGenerator[Call]:
    """Calls tools the way an instrumented agent would: each call inside
    the agent's own span, so the MCP client carries that span's context
    in _meta. A context manager, not a fixture, so the streams open and
    close in the test's own task (anyio requires it). Spans from the
    session's setup are cleared, so a test sees only its own calls."""
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        lowlevel = mcp._lowlevel_server  # pyright: ignore[reportPrivateUsage]
        server_task = asyncio.ensure_future(
            lowlevel.run(
                *server_streams, lowlevel.create_initialization_options(), raise_exceptions=False
            )
        )
        try:
            async with ClientSession(*client_streams) as session:
                await session.initialize()
                _exporter.clear()

                async def call(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
                    with _agent_tracer.start_as_current_span("agent turn"):
                        result = await session.call_tool(tool, arguments, meta={"api_key": api_key})
                    return {"is_error": result.is_error, **(result.structured_content or {})}

                yield call
        finally:
            server_task.cancel()


def _spans() -> list[ReadableSpan]:
    return list(_exporter.get_finished_spans())


def _named(spans: list[ReadableSpan], name: str) -> list[ReadableSpan]:
    return [s for s in spans if s.name == name]


def _ids(span: ReadableSpan) -> tuple[int, int]:
    """(trace ID, span ID). A finished SDK span always has a context."""
    assert span.context is not None
    return span.context.trace_id, span.context.span_id


def _children(spans: list[ReadableSpan], parent: ReadableSpan) -> list[ReadableSpan]:
    kids = [s for s in spans if s.parent is not None and s.parent.span_id == _ids(parent)[1]]
    return sorted(kids, key=lambda s: s.start_time or 0)


def _attr(span: ReadableSpan, key: str) -> Any:
    return (span.attributes or {}).get(key)


def _stage_names(spans: list[ReadableSpan]) -> list[list[str]]:
    """The stage spans under each tools/call run_query span, in order."""
    return [[c.name for c in _children(spans, s)] for s in _named(spans, "tools/call run_query")]


def _everything_recorded(span: ReadableSpan) -> str:
    """Every string a span would send to the viewer."""
    parts = [span.name, span.status.description or ""]
    parts += [f"{k}={v}" for k, v in (span.attributes or {}).items()]
    for event in span.events:
        parts.append(event.name)
        parts += [f"{k}={v}" for k, v in (event.attributes or {}).items()]
    return "\n".join(parts)


async def test_a_query_is_one_trace_with_a_span_per_stage(
    seeded_workspace: tuple[Workspace, str],
) -> None:
    async with mcp_client(seeded_workspace[1]) as call:
        result = await call("run_query", {"sql": "select label from demo_items"})
    assert result["status"] == "succeeded"

    spans = _spans()
    (agent,) = _named(spans, "agent turn")
    (client,) = _named(spans, "MCP send tools/call run_query")
    (server,) = _named(spans, "tools/call run_query")

    # The server span's parent is the agent's client span, received
    # through _meta: one trace from the agent down to the audit write.
    server_parent, client_parent = server.parent, client.parent
    assert server.kind is SpanKind.SERVER
    assert server_parent is not None and server_parent.is_remote
    assert server_parent.span_id == _ids(client)[1]
    assert client_parent is not None and client_parent.span_id == _ids(agent)[1]
    stages = _children(spans, server)
    assert {_ids(s)[0] for s in [client, server, *stages]} == {_ids(agent)[0]}

    assert [s.name for s in stages] == STAGES
    assert all(s.status.status_code is not StatusCode.ERROR for s in stages)
    by_name = {s.name: s for s in stages}
    assert _attr(server, "mcp_backbone.query.status") == "succeeded"
    assert by_name["execute"].kind is SpanKind.CLIENT
    assert dict(by_name["execute"].attributes or {}) == {
        "db.system.name": "postgresql",
        "db.operation.name": "SELECT",
        "mcp_backbone.execute.attempts": 1,
        "db.response.returned_rows": 2,
        "mcp_backbone.result.truncated": False,
    }
    estimate = _attr(by_name["cost_check"], "mcp_backbone.cost.estimate")
    assert isinstance(estimate, float) and estimate > 0
    assert _attr(by_name["cost_check"], "mcp_backbone.cost.threshold") == settings.cost_threshold
    assert _attr(by_name["audit"], "db.collection.name") == "query_executions"


async def test_the_stage_that_refuses_a_query_is_marked_and_later_stages_never_run(
    seeded_workspace: tuple[Workspace, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    async with mcp_client(seeded_workspace[1]) as call:
        assert (await call("run_query", {"sql": "delete from demo_items"}))["status"] == "rejected"

        monkeypatch.setattr(settings, "cost_threshold", 0)
        too_costly = await call("run_query", {"sql": "select label from demo_items"})
        assert too_costly["status"] == "rejected"

        monkeypatch.setattr(settings, "demo_rate_limit", 0)
        limited = await call("run_query", {"sql": "select label from demo_items"})
        assert limited["status"] == "rejected"

    spans = _spans()
    assert _stage_names(spans) == [
        ["authenticate", "rate_limit", "validate", "audit"],
        ["authenticate", "rate_limit", "validate", "cost_check", "audit"],
        ["authenticate", "rate_limit", "audit"],
    ]
    refusing = [s for s in spans if s.status.status_code is StatusCode.ERROR]
    assert [(s.name, _attr(s, "error.type")) for s in refusing] == [
        ("validate", "rejected_by_validation"),
        ("cost_check", "rejected_by_cost"),
        ("rate_limit", "rate_limited"),
    ]
    requests = _named(spans, "tools/call run_query")
    assert [_attr(s, "mcp_backbone.query.status") for s in requests] == ["rejected"] * 3


@pytest.mark.usefixtures("seeded_workspace")
async def test_a_bad_key_shows_on_the_authenticate_span() -> None:
    async with mcp_client("jarvis_totally_bogus") as call:
        assert (await call("list_tables", {}))["is_error"] is True

    spans = _spans()
    (server,) = _named(spans, "tools/call list_tables")
    (auth,) = _children(spans, server)
    assert auth.name == "authenticate"
    assert auth.status.status_code is StatusCode.ERROR
    assert _attr(auth, "error.type") == "AuthenticationError"


async def _calls_that_quote_the_marker(api_key: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every way a query's text can come back: run, rejected by the
    validator, errored in EXPLAIN (the error quotes the column), and
    rejected for cost (the stage that holds the normalised SQL)."""
    async with mcp_client(api_key) as call:
        ok = await call("run_query", {"sql": f"select '{MARKER}' as m from demo_items"})
        assert ok["status"] == "succeeded"
        refused = await call(
            "run_query", {"sql": f"delete from demo_items where label = '{MARKER}'"}
        )
        assert refused["status"] == "rejected"
        errored = await call("run_query", {"sql": f"select {MARKER} from demo_items"})
        assert errored["status"] == "errored" and MARKER in errored["error"]
        monkeypatch.setattr(settings, "cost_threshold", 0)
        costly = await call("run_query", {"sql": f"select '{MARKER}' as m from demo_items"})
        assert costly["status"] == "rejected"


async def test_no_query_text_reaches_a_span_by_default(
    seeded_workspace: tuple[Workspace, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert settings.otel_capture_content is False
    await _calls_that_quote_the_marker(seeded_workspace[1], monkeypatch)

    spans = _spans()
    assert len(_named(spans, "tools/call run_query")) == 4
    assert [s.name for s in spans if MARKER in _everything_recorded(s)] == []


async def test_content_capture_puts_the_query_text_in_the_spans(
    seeded_workspace: tuple[Workspace, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The opt-in, and proof that the test above would see a leak."""
    monkeypatch.setattr(settings, "otel_capture_content", True)
    await _calls_that_quote_the_marker(seeded_workspace[1], monkeypatch)

    spans = _spans()
    (execute,) = _named(spans, "execute")
    assert MARKER in str(_attr(execute, "db.query.text"))
    (refused,) = [s for s in _named(spans, "validate") if s.status.status_code is StatusCode.ERROR]
    assert refused.status.description
    explain_errors = [
        s for s in _named(spans, "cost_check") if MARKER in (s.status.description or "")
    ]
    assert len(explain_errors) == 1


async def test_a_failed_audit_write_leaks_no_query_text(
    seeded_workspace: tuple[Workspace, str],
) -> None:
    """The database's error for a failed INSERT quotes its parameters,
    the query text among them. The call fails, as it must (no unaudited
    query), and the audit span says only which exception it was."""
    async with engine.begin() as conn:
        await conn.execute(text("drop table query_executions cascade"))
    async with mcp_client(seeded_workspace[1]) as call:
        result = await call("run_query", {"sql": f"select '{MARKER}' as m from demo_items"})
    assert result["is_error"] is True

    spans = _spans()
    (audit,) = _named(spans, "audit")
    assert audit.status.status_code is StatusCode.ERROR
    assert _attr(audit, "error.type") == "ProgrammingError"
    assert [s.name for s in spans if MARKER in _everything_recorded(s)] == []


async def test_a_dead_trace_viewer_changes_nothing(
    seeded_workspace: tuple[Workspace, str],
) -> None:
    """An exporter pointed at a closed port: every call still succeeds,
    and flushing at shutdown gives up within the 2 s export timeout
    instead of the exporter's default (6.5 s in the planning spike)."""
    processor = build_span_processor({"OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:9"})
    assert isinstance(processor, BatchSpanProcessor)
    _provider.add_span_processor(processor)
    try:
        async with mcp_client(seeded_workspace[1]) as call:
            for _ in range(3):
                result = await call("run_query", {"sql": "select label from demo_items"})
                assert result["status"] == "succeeded"
    finally:
        started = time.monotonic()
        processor.shutdown()
        assert time.monotonic() - started < 4


async def test_spans_go_to_the_configured_endpoint() -> None:
    """The endpoint is read from the env given, as the SDK reads it: the
    traces-only variable as is, the general one with /v1/traces added."""
    paths: list[str] = []

    class Collector(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # the stdlib handler's name for a POST
            self.rfile.read(int(self.headers["Content-Length"]))
            paths.append(self.path)
            self.send_response(200)
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Collector)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        for env in (
            {"OTEL_EXPORTER_OTLP_ENDPOINT": base},
            {"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": f"{base}/custom/path"},
        ):
            processor = build_span_processor(env)
            assert processor is not None
            provider = TracerProvider()
            provider.add_span_processor(processor)
            provider.get_tracer("test").start_span("one").end()
            provider.shutdown()
    finally:
        server.shutdown()
    assert paths == ["/v1/traces", "/custom/path"]


async def test_tracing_is_off_unless_an_endpoint_is_set() -> None:
    assert build_span_processor({}) is None
    disabled = {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318", "OTEL_SDK_DISABLED": "true"}
    assert build_span_processor(disabled) is None
    for key in ("OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"):
        processor = build_span_processor({key: "http://127.0.0.1:4318/v1/traces"})
        assert isinstance(processor, BatchSpanProcessor)
        processor.shutdown()
