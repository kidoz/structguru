"""Enhanced Sentry integration processor.

Provides a structlog processor that sends log events to Sentry as
breadcrumbs and/or captured events based on severity.

Usage::

    from structguru.integrations.sentry import SentryProcessor

    structlog.configure(
        processors=[..., SentryProcessor(tag_keys=frozenset({"service"})), ...],
    )
"""

from __future__ import annotations

import logging
from typing import Any

_METHOD_TO_LEVEL: dict[str, int] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "warn": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}


class SentryProcessor:
    """Structlog processor that forwards events to Sentry.

    .. important::
        Place :class:`~structguru.redaction.RedactingProcessor` **before** this
        processor in the chain so that sensitive data is masked before it is
        sent to Sentry.

    Parameters
    ----------
    event_level:
        Minimum :mod:`logging` level to capture as a Sentry event.
    tag_keys:
        Event-dict keys to set as Sentry tags.
    breadcrumb_level:
        Minimum level to record as a Sentry breadcrumb.
    """

    def __init__(
        self,
        *,
        event_level: int = logging.ERROR,
        tag_keys: frozenset[str] | None = None,
        breadcrumb_level: int = logging.INFO,
    ) -> None:
        self._event_level = event_level
        self._tag_keys = tag_keys or frozenset()
        self._breadcrumb_level = breadcrumb_level

    def __call__(
        self,
        _logger: Any,
        method_name: str,
        event_dict: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            import sentry_sdk
        except ImportError:
            return event_dict

        level = _METHOD_TO_LEVEL.get(method_name.lower(), logging.INFO)

        if level >= self._breadcrumb_level:
            sentry_sdk.add_breadcrumb(
                message=str(event_dict.get("event", "")),
                category="structguru",
                level=method_name,
                data={k: v for k, v in event_dict.items() if k != "event"},
            )

        if level >= self._event_level:
            with sentry_sdk.push_scope() as scope:
                for key in self._tag_keys:
                    if key in event_dict:
                        scope.set_tag(key, str(event_dict[key]))

                scope.set_extra("structlog_event", event_dict)

                exc_info = event_dict.get("exc_info")
                if exc_info:
                    sentry_sdk.capture_exception(exc_info)
                else:
                    sentry_sdk.capture_message(
                        str(event_dict.get("event", "")),
                        level=method_name,
                    )

        return event_dict
