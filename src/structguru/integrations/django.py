"""Django integration for structguru.

Provides a ``LOGGING`` dict generator and a request middleware that binds
``request_id``, ``method``, ``path``, and ``client_ip`` to structguru
context variables.

Usage in ``settings.py``::

    from structguru.integrations.django import build_logging_config

    LOGGING = build_logging_config(service="myapp", level="INFO", json_logs=True)

    MIDDLEWARE = [
        ...
        "structguru.integrations.django.StructguruMiddleware",
        ...
    ]
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from structguru._contextvars import bind_contextvars, clear_contextvars
from structguru.core import Logger
from structguru.integrations._util import coerce_request_id


class _JSONLogFormatter(logging.Formatter):
    """stdlib formatter that renders records as JSON with proper escaping.

    Unlike a ``%``-style format string (where ``%(message)s`` is substituted
    raw), this serializes each field through :func:`json.dumps`, so a log
    message or logger name containing quotes or newlines cannot break the JSON
    or forge additional fields.
    """

    def __init__(self, service: str = "app") -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "service": self.service,
            "level": record.levelname,
            "name": record.name,
            "message": record.getMessage(),
        }
        return json.dumps(payload, ensure_ascii=False)


def build_logging_config(
    *,
    service: str = "app",
    level: str = "INFO",
    json_logs: bool = True,
) -> dict[str, Any]:
    """Generate a minimal Django ``LOGGING`` dict using the stdlib logging module.

    The structguru integration binds request context via context variables
    independent of the logging backend, so this config keeps a simple,
    structlog-free stdlib setup.

    Parameters
    ----------
    service:
        Application name added to every log record (as a logging filter).
    level:
        Root log level.
    json_logs:
        ``True`` for a JSON-style line format, ``False`` for a plain
        human-readable console format.
    """
    formatter = "json" if json_logs else "console"
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "json": {
                # Custom factory (dictConfig "()" key) so message/name are
                # JSON-escaped rather than interpolated into a format string.
                "()": "structguru.integrations.django._JSONLogFormatter",
                "service": service,
            },
            "console": {
                "format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            },
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": formatter,
            },
        },
        "root": {
            "handlers": ["console"],
            "level": level.upper(),
        },
    }


class StructguruMiddleware:
    """Django middleware for structured request logging.

    Binds ``request_id``, ``method``, ``path``, ``client_ip`` (and ``user_id``
    when available) to structguru context variables for the duration of each
    request.
    """

    def __init__(self, get_response: Any) -> None:
        self.get_response = get_response
        self.log = Logger(name="structguru.django")

    def __call__(self, request: Any) -> Any:
        clear_contextvars()

        request_id = coerce_request_id(request.META.get("HTTP_X_REQUEST_ID", ""))

        bind_contextvars(
            request_id=request_id,
            method=request.method,
            path=request.path,
            client_ip=request.META.get("REMOTE_ADDR", ""),
        )

        if hasattr(request, "user") and hasattr(request.user, "pk") and request.user.pk:
            bind_contextvars(user_id=str(request.user.pk))

        start_time = time.perf_counter()

        try:
            response = self.get_response(request)
        except Exception:
            self.log.exception("Request failed")
            raise
        else:
            duration_ms = (time.perf_counter() - start_time) * 1000
            self.log.info(
                "Request completed",
                status_code=response.status_code,
                duration_ms=round(duration_ms, 2),
            )
            response["X-Request-ID"] = request_id
            return response
        finally:
            clear_contextvars()
