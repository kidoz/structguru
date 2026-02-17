"""Structlog processors for structured JSON logging.

Provides processors that enrich event dicts with standardized fields:
- ``level``: normalized to ``CRITICAL``, ``ERROR``, ``WARN``, ``INFO``, ``DEBUG``.
- ``severity``: RFC 5424 syslog severity code (``2``–``7``).
- ``service``: application name.
- ``event``: guaranteed to be a string.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

_LEVEL_MAP: dict[str, str] = {
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

# RFC 5424 syslog severity codes (§6.2.1)
# https://datatracker.ietf.org/doc/html/rfc5424#section-6.2.1
_SEVERITY_MAP: dict[str, int] = {
    "DEBUG": 7,
    "INFO": 6,
    "WARN": 4,
    "ERROR": 3,
    "CRITICAL": 2,
}


def add_service(
    service_name: str,
) -> Callable[[Any, str, dict[str, Any]], dict[str, Any]]:
    """Return a processor that adds a ``service`` field to every log record."""

    def _processor(
        _logger: Any,
        _method_name: str,
        event_dict: dict[str, Any],
    ) -> dict[str, Any]:
        event_dict.setdefault("service", service_name)
        return event_dict

    return _processor


def normalize_level(
    _logger: Any,
    method_name: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    """Normalize the log level to a canonical string.

    Canonical levels: ``CRITICAL``, ``ERROR``, ``WARN``, ``INFO``, ``DEBUG``.
    """
    raw_level = event_dict.get("level", method_name)
    raw_level_str = str(raw_level).lower()
    event_dict["level"] = _LEVEL_MAP.get(raw_level_str, raw_level_str.upper())
    return event_dict


def add_syslog_severity(
    _logger: Any,
    _method_name: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    """Add RFC 5424 syslog ``severity`` code (numeric) to the event.

    Must run **after** :func:`normalize_level` so that ``event_dict["level"]``
    is already one of the canonical strings (``CRITICAL``, ``ERROR``, etc.).
    Defaults to ``6`` (Informational) for unknown levels.
    """
    level = event_dict.get("level", "INFO")
    event_dict["severity"] = _SEVERITY_MAP.get(level, 6)
    return event_dict


def ensure_event_is_str(
    _logger: Any,
    _method_name: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    """Ensure the main log message (``event``) is a string."""
    event = event_dict.get("event")
    if event is not None and not isinstance(event, str):
        event_dict["event"] = str(event)
    return event_dict
