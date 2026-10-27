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
import threading
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
# hold up a shutdown for 6.5 s in a test; 2 s keeps that to about 1 s
# per batch of spans.
DEFAULT_EXPORT_TIMEOUT_SECONDS = 2.0
# But a shutdown flushes every queued batch, each with its own timeout: a
# full queue (four batches) and a dead viewer took 5.3 s in the overhead
# measurement. So the shutdown as a whole is capped too.
SHUTDOWN_TIMEOUT_SECONDS = 2.0


def build_span_processor(env: Mapping[str, str] = os.environ) -> SpanProcessor | None:
    """The background exporter to install, or None when no endpoint is
    configured. Endpoint and timeout come from `env`, read the way the
    SDK reads the standard variables: the traces-only endpoint as given,
    the general one with /v1/traces added, and the timeout in seconds.
    (Headers, if any, the exporter still reads from the environment.)"""
    if env.get("OTEL_SDK_DISABLED", "").strip().lower() == "true":
        return None
    base = env.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    endpoint = env.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or (
        f"{base.rstrip('/')}/v1/traces" if base else ""
    )
    if not endpoint:
        return None
    timeout = env.get("OTEL_EXPORTER_OTLP_TRACES_TIMEOUT") or env.get("OTEL_EXPORTER_OTLP_TIMEOUT")
    return BatchSpanProcessor(
        OTLPSpanExporter(
            endpoint=endpoint,
            timeout=float(timeout) if timeout else DEFAULT_EXPORT_TIMEOUT_SECONDS,
        )
    )


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


def shutdown_tracing(
    provider: TracerProvider, timeout_seconds: float = SHUTDOWN_TIMEOUT_SECONDS
) -> bool:
    """Flushes what's still queued and stops the exporter, waiting at most
    `timeout_seconds`, so a dead viewer can't hold up a restart. The
    SDK's own shutdown has no overall limit worth the name (30 s), so it
    runs in a daemon thread; spans still queued when time runs out are
    lost, which is acceptable: traces are a view, the audit log is the
    record. Returns whether the flush finished in time."""
    flusher = threading.Thread(target=provider.shutdown, name="tracing-shutdown", daemon=True)
    flusher.start()
    flusher.join(timeout_seconds)
    if flusher.is_alive():
        logger.warning("tracing: gave up flushing spans after %.0f s", timeout_seconds)
        return False
    return True


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
