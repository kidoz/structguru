"""structguru — ergonomic structured logging with a native Rust runtime."""

from structguru._native import (
    configure,
    disable_native,
    native_available,
    native_metrics,
    set_native_level,
)
from structguru.core import Logger, logger
from structguru.metrics import MetricProcessor
from structguru.otel import add_otel_context
from structguru.redaction import DEFAULT_SENSITIVE_KEYS

__version__ = "1.0.3"

__all__ = [
    "DEFAULT_SENSITIVE_KEYS",
    "Logger",
    "MetricProcessor",
    "add_otel_context",
    "configure",
    "disable_native",
    "logger",
    "native_available",
    "native_metrics",
    "set_native_level",
]
