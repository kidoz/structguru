"""structguru — ergonomic structured logging with a native Rust runtime."""

from structguru._contextvars import (
    bind_contextvars,
    bound_contextvars,
    clear_contextvars,
    get_contextvars,
)
from structguru._runtime import (
    configure,
    flush,
    is_available,
    set_level,
    shutdown,
    writer_metrics,
)
from structguru.core import Logger, logger
from structguru.metrics import MetricProcessor
from structguru.otel import add_otel_context
from structguru.redaction import DEFAULT_SENSITIVE_KEYS

__version__ = "1.2.1"

__all__ = [
    "DEFAULT_SENSITIVE_KEYS",
    "Logger",
    "MetricProcessor",
    "add_otel_context",
    "bind_contextvars",
    "bound_contextvars",
    "clear_contextvars",
    "configure",
    "flush",
    "get_contextvars",
    "is_available",
    "logger",
    "set_level",
    "shutdown",
    "writer_metrics",
]
