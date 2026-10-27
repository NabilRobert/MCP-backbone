"""Sends a timed trace of every tool call to an OpenTelemetry viewer, if
one is configured. The MCP SDK already opens a span per request
("tools/call run_query") and picks up the calling agent's trace context
from the request's _meta; this file installs the exporter that actually
sends those spans somewhere, and the `tracer` the tools use to add one
child span per stage (authenticate, validate, cost_check, ...).

Off unless OTEL_EXPORTER_OTLP_ENDPOINT (or the traces-only variant) is
set: then nothing is installed and every span is a no-op. And tracing
must never hurt the server: spans are exported in the background, a dead
viewer only loses spans, and a failure to set tracing up is logged, not
raised. What the spans may contain is decided where they're opened: no
SQL text or free-text reasons unless APP_OTEL_CAPTURE_CONTENT is on (see
app/config.py)."""

import logging
import os
from collections.abc import Generator, Mapping
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span, SpanKind, StatusCode
from opentelemetry.util.types import Attributes

from app.config import settings
from app.version import BUILD_VERSION

logger = logging.getLogger(__name__)

# What every stage span in app/ is opened with. Until a provider is
# installed this hands out no-op spans, so code can use it unconditionally.
tracer = trace.get_tracer("mcp-backbone")

# The exporter's default timeout (10 s, plus retries) let a dead viewer
# hold up a shutdown for 6.5 s in a test; 2 s keeps that to about 1 s.
DEFAULT_EXPORT_TIMEOUT_SECONDS = 2.0


def build_span_processor(env: Mapping[str, str] = os.environ) -> SpanProcessor | None:
    """The background exporter to install, or None when no endpoint is
    configured. The exporter reads the endpoint and headers from the
    standard OTEL_EXPORTER_OTLP_* variables itself."""
    if env.get("OTEL_SDK_DISABLED", "").strip().lower() == "true":
        return None
    if not (
        env.get("OTEL_EXPORTER_OTLP_ENDPOINT") or env.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
    ):
        return None
    timeout_set = env.get("OTEL_EXPORTER_OTLP_TIMEOUT") or env.get(
        "OTEL_EXPORTER_OTLP_TRACES_TIMEOUT"
    )
    exporter = (
        OTLPSpanExporter()
        if timeout_set
        else OTLPSpanExporter(timeout=DEFAULT_EXPORT_TIMEOUT_SECONDS)
    )
    return BatchSpanProcessor(exporter)


def configure_tracing() -> TracerProvider | None:
    """Installs the global tracer provider at startup when an endpoint is
    configured, and returns it so the caller can flush it at shutdown.
    Never raises: a server without tracing beats a server that won't
    start."""
    try:
        processor = build_span_processor()
        if processor is None:
            return None
        resource = Resource.create(
            {
                "service.name": os.environ.get("OTEL_SERVICE_NAME") or "mcp-backbone",
                "service.version": BUILD_VERSION,
            }
        )
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(processor)
        trace.set_tracer_provider(provider)
        return provider
    except Exception:  # see the docstring: never stop the server starting
        logger.warning("tracing not enabled: setting it up failed", exc_info=True)
        return None


# The attributes every database span carries (OpenTelemetry's database
# conventions).
DB_ATTRIBUTES = {"db.system.name": "postgresql"}


def mark_failed(span: Span, error_type: str, reason: str) -> None:
    """Marks the stage that ended a request: a short, stable error.type
    (safe to record), and the human-readable reason only when content
    capture is on, since a reason can quote the query."""
    span.set_attribute("error.type", error_type)
    span.set_status(StatusCode.ERROR, reason if settings.otel_capture_content else None)


@contextmanager
def stage(
    name: str, *, kind: SpanKind = SpanKind.INTERNAL, attributes: Attributes = None
) -> Generator[Span]:
    """One step of a tool call, as a child span of the request's span. An
    exception escaping the step marks the span with the exception's class
    name and goes on unchanged. The SDK's own exception recording is off:
    it would copy the message (which can hold SQL text) into the span."""
    with tracer.start_as_current_span(
        name,
        kind=kind,
        attributes=attributes,
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        try:
            yield span
        except Exception as exc:
            mark_failed(span, type(exc).__qualname__, str(exc))
            raise
