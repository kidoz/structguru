"""Enhanced Sentry event integration.

Provides a callable event hook that sends log events to Sentry as
breadcrumbs and/or captured events based on severity.

Usage::

    from structguru.integrations.sentry import SentryProcessor

    structguru.configure(
        sentry_processor=SentryProcessor(tag_keys=frozenset({"service"})),
    )
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from structguru.redaction import REDACTED_MARKER_KEY

try:
    import sentry_sdk as _sentry_sdk_mod

    _sentry_sdk: Any = _sentry_sdk_mod
except ImportError:  # pragma: no cover - exercised via _sentry_sdk = None
    _sentry_sdk = None

_METHOD_TO_LEVEL: dict[str, int] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "warn": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}


class SentryProcessor:
    """Forward events to Sentry as breadcrumbs and/or captured exceptions.

    When *require_redaction* is ``True`` (the default), this processor refuses
    to upload the event dict as Sentry extras unless the redaction marker is
    present — turning the ordering convention into a runtime guard. The native
    hook always supplies an already-redacted event and marker.

    Parameters
    ----------
    event_level:
        Minimum :mod:`logging` level to capture as a Sentry event.
    tag_keys:
        Event-dict keys to set as Sentry tags.
    breadcrumb_level:
        Minimum level to record as a Sentry breadcrumb.
    capture_messages:
        If ``True``, call :func:`sentry_sdk.capture_message` for every event
        at or above *event_level* that does not carry ``exc_info``.  Defaults
        to ``False`` to match :mod:`logging.LoggingIntegration` semantics —
        plain ``logger.error(...)`` calls only become Sentry *events* when
        they include an exception.
    require_redaction:
        If ``True`` (default), ``scope.set_extra("structlog_event", ...)`` is
        only called when :data:`~structguru.redaction.REDACTED_MARKER_KEY`
        is present on the event dict.

    Notes
    -----
    Pass this processor via ``configure(sentry_processor=SentryProcessor(...))``.
    It runs per kept record on the caller's thread with the same contract. The
    native hook supplies an already-redacted event and injects
    ``REDACTED_MARKER_KEY`` so the guard recognizes the completed redaction.
    """

    def __init__(
        self,
        *,
        event_level: int = logging.ERROR,
        tag_keys: frozenset[str] | None = None,
        breadcrumb_level: int = logging.INFO,
        capture_messages: bool = False,
        require_redaction: bool = True,
    ) -> None:
        self._event_level = event_level
        self._tag_keys = tag_keys or frozenset()
        self._breadcrumb_level = breadcrumb_level
        self._capture_messages = capture_messages
        self._require_redaction = require_redaction

    def __call__(
        self,
        _logger: Any,
        method_name: str,
        event_dict: dict[str, Any],
    ) -> dict[str, Any]:
        """Forward one event to Sentry and return *event_dict* unchanged.

        Records a breadcrumb at or above *breadcrumb_level*. At or above
        *event_level*, captures an exception when the event carries
        ``exc_info``, or a message when *capture_messages* is enabled. Acts as
        a no-op when ``sentry-sdk`` is not installed.
        """
        if _sentry_sdk is None:
            return event_dict

        level = _METHOD_TO_LEVEL.get(method_name.lower(), logging.INFO)
        # Raw exceptions are reserved for capture_exception. The SDK serializes
        # arbitrary objects in breadcrumbs/extras, which would bypass redaction.
        payload = {
            k: v for k, v in event_dict.items() if k not in ("exc_info", REDACTED_MARKER_KEY)
        }

        if level >= self._breadcrumb_level:
            _sentry_sdk.add_breadcrumb(
                message=str(event_dict.get("event", "")),
                category="structguru",
                level=method_name,
                data={k: v for k, v in payload.items() if k != "event"},
            )

        if level < self._event_level:
            return event_dict

        with _sentry_sdk.new_scope() as scope:
            for key in self._tag_keys:
                if key in payload:
                    scope.set_tag(key, str(payload[key]))

            if not self._require_redaction or event_dict.get(REDACTED_MARKER_KEY):
                scope.set_extra("structlog_event", payload)

            exc_info = event_dict.get("exc_info")
            if exc_info:
                exc = _resolve_exception(exc_info)
                if exc is not None:
                    _sentry_sdk.capture_exception(exc)
            elif self._capture_messages:
                _sentry_sdk.capture_message(
                    str(event_dict.get("event", "")),
                    level=method_name,
                )

        return event_dict


def _resolve_exception(exc_info: Any) -> BaseException | None:
    """Normalise an ``exc_info`` value into an exception instance for Sentry."""
    if exc_info is True:
        return sys.exc_info()[1]
    if isinstance(exc_info, tuple):
        return exc_info[1] if len(exc_info) >= 2 else None
    if isinstance(exc_info, BaseException):
        return exc_info
    return None
