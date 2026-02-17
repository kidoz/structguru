"""Django integration for structguru.

Provides a ``LOGGING`` dict generator and a request middleware that binds
``request_id``, ``method``, ``path``, and ``client_ip`` to structlog's
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

import time
import uuid
from typing import Any

import structlog
from structlog.contextvars import bind_contextvars, clear_contextvars

from structguru.config import (
    _build_formatter_processors,
    _build_shared_processors,
    _orjson_serializer,
)


def build_logging_config(
    *,
    service: str = "app",
    level: str = "INFO",
    json_logs: bool = True,
) -> dict[str, Any]:
    """Generate a Django ``LOGGING`` dict wired to structlog's ``ProcessorFormatter``.

    Parameters
    ----------
    service:
        Application name added to every log record.
    level:
        Root log level.
    json_logs:
        ``True`` for JSON, ``False`` for colored console.
    """
    shared = _build_shared_processors(service)

    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer(serializer=_orjson_serializer)
        if json_logs
        else structlog.dev.ConsoleRenderer(event_key="message")
    )

    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "structlog": {
                "()": structlog.stdlib.ProcessorFormatter,
                "processors": _build_formatter_processors(renderer, json_mode=json_logs),
                "foreign_pre_chain": shared,
            },
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "structlog",
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
    when available) to structlog context variables for the duration of each
    request.
    """

    def __init__(self, get_response: Any) -> None:
        self.get_response = get_response
        self.log = structlog.get_logger("structguru.django")

    def __call__(self, request: Any) -> Any:
        clear_contextvars()

        request_id: str = request.META.get("HTTP_X_REQUEST_ID", str(uuid.uuid4()))

        bind_contextvars(
            request_id=request_id,
            method=request.method,
            path=request.path,
            client_ip=request.META.get("REMOTE_ADDR", ""),
        )

        if hasattr(request, "user") and hasattr(request.user, "pk") and request.user.pk:
            bind_contextvars(user_id=str(request.user.pk))

        start_time = time.perf_counter()

        response = self.get_response(request)

        duration_ms = (time.perf_counter() - start_time) * 1000
        self.log.info(
            "Request completed",
            status_code=response.status_code,
            duration_ms=round(duration_ms, 2),
        )

        response["X-Request-ID"] = request_id
        clear_contextvars()

        return response
