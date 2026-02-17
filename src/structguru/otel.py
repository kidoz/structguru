"""OpenTelemetry trace correlation processor.

Adds ``trace_id``, ``span_id``, and ``trace_flags`` from the current
OpenTelemetry span context to every log event, connecting logs to
distributed traces.

Gracefully degrades to a no-op when ``opentelemetry-api`` is not installed.
"""

from __future__ import annotations

from typing import Any


def add_otel_context(
    _logger: Any,
    _method_name: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    """Add OpenTelemetry trace context fields to the event dict."""
    try:
        from opentelemetry import trace

        span = trace.get_current_span()
        ctx = span.get_span_context()
        if ctx and ctx.is_valid:
            event_dict["trace_id"] = format(ctx.trace_id, "032x")
            event_dict["span_id"] = format(ctx.span_id, "016x")
            event_dict["trace_flags"] = int(ctx.trace_flags)
    except ImportError:
        pass
    return event_dict
