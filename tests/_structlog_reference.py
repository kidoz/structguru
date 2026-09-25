"""Independent structlog/orjson reference for the byte-parity suites.

Kept out of ``conftest.py`` so test runs that install only pytest (the
free-threaded CI gates) do not need structlog or orjson.
"""

from __future__ import annotations

import re
import string
from collections.abc import Mapping, Sequence
from typing import Any

import orjson
import structlog

# -- Independent structlog/orjson reference -----------------------------------
#
# Before 1.0, records went through structlog: the facade brace-formatted the
# message, and a shared processor chain (``build_shared_processors`` plus the
# ``ProcessorFormatter`` stage) rendered it with ``JSONRenderer(orjson)``. The
# native renderer reproduces that output. The parity suites compare against
# this reconstruction of the chain, built from structlog's own processors, the
# pre-1.0 structguru processors copied verbatim in behaviour, and orjson. It
# never calls into structguru, so a change in native serialization, key
# order, level mapping, or redaction fails the comparison instead of being
# compared with itself.

_REFERENCE_LEVELS = {
    "trace": "DEBUG",
    "debug": "DEBUG",
    "info": "INFO",
    "success": "INFO",
    "warning": "WARN",
    "warn": "WARN",
    "error": "ERROR",
    "critical": "CRITICAL",
    "fatal": "CRITICAL",
    "exception": "ERROR",
}
_REFERENCE_SEVERITIES = {"DEBUG": 7, "INFO": 6, "WARN": 4, "ERROR": 3, "CRITICAL": 2}
_REFERENCE_SENSITIVE_KEYS = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "authorization",
        "cookie",
        "session_id",
        "credit_card",
        "ssn",
        "private_key",
    }
)
_REFERENCE_MARKER_KEY = "_structguru_redacted"


def _reference_format(
    message: Any, args: Sequence[Any], kwargs: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    """Brace-format like the facade: consumed keyword arguments leave the fields."""
    text = message if type(message) is str else str(message)
    if not (args or kwargs) or "{" not in text:
        return text, kwargs
    try:
        consumed = {
            name.split(".")[0].split("[")[0]
            for _, name, _, _ in string.Formatter().parse(text)
            if name and not name.split(".")[0].split("[")[0].isdigit()
        }
        formatted = text.format(*args, **kwargs)
    except (LookupError, AttributeError, TypeError, ValueError):
        return text, kwargs
    return formatted, {key: value for key, value in kwargs.items() if key not in consumed}


def _reference_merge_contextvars(contextvars: Mapping[str, Any]) -> Any:
    # structlog's merge_contextvars has the same setdefault semantics but walks
    # a contextvars.Context, whose iteration order is arbitrary; the native
    # contract appends context fields in insertion order.
    def processor(_logger: Any, _method: str, event: dict[str, Any]) -> Any:
        for key, value in contextvars.items():
            event.setdefault(key, value)
        return event

    return processor


def _reference_normalize_level(_logger: Any, method: str, event: dict[str, Any]) -> Any:
    raw = str(event.get("level", method)).lower()
    event["level"] = _REFERENCE_LEVELS.get(raw, raw.upper())
    return event


def _reference_add_severity(_logger: Any, _method: str, event: dict[str, Any]) -> Any:
    event["severity"] = _REFERENCE_SEVERITIES.get(event.get("level", "INFO"), 6)
    return event


def _reference_add_service(service: str) -> Any:
    def processor(_logger: Any, _method: str, event: dict[str, Any]) -> Any:
        event.setdefault("service", service)
        return event

    return processor


def _reference_add_stack(stack: str | None) -> Any:
    # Stands in for StackInfoRenderer at the same chain position. The captured
    # frames differ by design (native skips structguru frames), so the caller
    # supplies the text and only its placement is compared.
    def processor(_logger: Any, _method: str, event: dict[str, Any]) -> Any:
        if stack is not None:
            event["stack"] = stack
        return event

    return processor


def _reference_ensure_event_is_str(_logger: Any, _method: str, event: dict[str, Any]) -> Any:
    if event.get("event") is not None and not isinstance(event["event"], str):
        event["event"] = str(event["event"])
    return event


def _reference_redactor(
    keys: frozenset[str], patterns: Sequence[re.Pattern[str]], replacement: str
) -> Any:
    """Pre-1.0 ``RedactingProcessor``: key redaction plus patterns on string values."""

    def redact(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: replacement if isinstance(key, str) and key.lower() in keys else redact(child)
                for key, child in value.items()
            }
        if isinstance(value, list):
            return [redact(item) for item in value]
        if isinstance(value, str):
            for pattern in patterns:
                value = pattern.sub(replacement, value)
        return value

    def processor(_logger: Any, _method: str, event: dict[str, Any]) -> Any:
        redacted = redact(event)
        redacted[_REFERENCE_MARKER_KEY] = True
        return redacted

    return processor


def _reference_strip_marker(_logger: Any, _method: str, event: dict[str, Any]) -> Any:
    event.pop(_REFERENCE_MARKER_KEY, None)
    return event


class _ReferenceSink:
    """Wrapped structlog logger: every method returns the rendered line."""

    def __init__(self, name: str) -> None:
        self.name = name

    def __getattr__(self, method: str) -> Any:
        return lambda line: line


def structlog_reference_line(
    method: str,
    message: Any,
    args: Sequence[Any] = (),
    *,
    logger_name: str,
    bound: Mapping[str, Any] | None = None,
    kwargs: Mapping[str, Any] | None = None,
    contextvars: Mapping[str, Any] | None = None,
    service: str = "svc",
    stack: str | None = None,
    sensitive_keys: frozenset[str] = _REFERENCE_SENSITIVE_KEYS,
    sensitive_patterns: Sequence[str] = (),
    pattern_replacement: str = "[REDACTED]",
) -> str:
    """Render one logging call through the pre-1.0 structlog/orjson chain.

    Pass ``exc_info`` in *kwargs*, as the facade did; ``format_exc_info``
    renders it in the formatter stage, after ``message``.
    """
    formatted, fields = _reference_format(message, args, dict(kwargs or {}))
    processors: list[Any] = [
        _reference_merge_contextvars(contextvars or {}),
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        _reference_normalize_level,
        _reference_add_severity,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
        _reference_add_service(service),
        _reference_add_stack(stack),
        structlog.processors.UnicodeDecoder(),
        _reference_ensure_event_is_str,
        _reference_redactor(
            sensitive_keys,
            [re.compile(pattern) for pattern in sensitive_patterns],
            pattern_replacement,
        ),
        _reference_strip_marker,
        structlog.processors.EventRenamer("message"),
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer(serializer=lambda obj, **_: orjson.dumps(obj).decode()),
    ]
    log = structlog.wrap_logger(
        _ReferenceSink(logger_name),
        processors=processors,
        wrapper_class=structlog.BoundLogger,
        context_class=dict,
    ).bind(**(bound or {}))
    line = log._proxy_to_logger(method, formatted, **fields)
    assert isinstance(line, str)
    return line
