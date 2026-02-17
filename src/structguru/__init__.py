"""structguru — a loguru-style ergonomic API for structlog."""

from structguru.config import configure_structlog, setup_structlog
from structguru.core import Logger, logger
from structguru.exceptions import ExceptionDictProcessor
from structguru.metrics import MetricProcessor
from structguru.otel import add_otel_context
from structguru.queued import configure_queued_logging
from structguru.redaction import RedactingProcessor
from structguru.routing import ConditionalProcessor
from structguru.sampling import RateLimitingProcessor, SamplingProcessor

__all__ = [
    "ConditionalProcessor",
    "ExceptionDictProcessor",
    "Logger",
    "MetricProcessor",
    "RateLimitingProcessor",
    "RedactingProcessor",
    "SamplingProcessor",
    "add_otel_context",
    "configure_queued_logging",
    "configure_structlog",
    "logger",
    "setup_structlog",
]
