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
    get_config,
    is_available,
    set_level,
    shutdown,
    update,
    writer_metrics,
)
from structguru.core import Logger, logger
from structguru.metrics import MetricProcessor
from structguru.otel import add_otel_context
from structguru.redaction import DEFAULT_SENSITIVE_KEYS
from structguru.settings import Settings

__version__ = "1.2.3"

__all__ = [
    "DEFAULT_SENSITIVE_KEYS",
    "Logger",
    "MetricProcessor",
    "Settings",
    "add_otel_context",
    "bind_contextvars",
    "bound_contextvars",
    "clear_contextvars",
    "configure",
    "flush",
    "get_config",
    "get_contextvars",
    "is_available",
    "logger",
    "set_level",
    "shutdown",
    "update",
    "writer_metrics",
]
