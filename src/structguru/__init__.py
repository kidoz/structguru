"""structguru — a loguru-style ergonomic API for structlog."""

from structguru.config import configure_structlog, setup_structlog
from structguru.core import Logger, logger
from structguru.exceptions import ExceptionDictProcessor
from structguru.metrics import MetricProcessor
from structguru.otel import add_otel_context
from structguru.processors import add_syslog_severity, normalize_level
from structguru.queued import configure_queued_logging
from structguru.redaction import DEFAULT_SENSITIVE_KEYS, RedactingProcessor
from structguru.routing import ConditionalProcessor
from structguru.sampling import RateLimitingProcessor, SamplingProcessor

__version__ = "0.1.1"

__all__ = [
    "ConditionalProcessor",
    "DEFAULT_SENSITIVE_KEYS",
    "ExceptionDictProcessor",
    "Logger",
    "MetricProcessor",
    "RateLimitingProcessor",
    "RedactingProcessor",
    "SamplingProcessor",
    "add_otel_context",
    "add_syslog_severity",
    "configure_queued_logging",
    "configure_structlog",
    "logger",
    "normalize_level",
    "setup_structlog",
]
