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

import asyncio
import json
import logging
import os
import time
from typing import Any

from structguru._contextvars import bind_contextvars, clear_contextvars, get_contextvars
from structguru.core import Logger
from structguru.integrations._stdlib_env import optional_bool_from_env
from structguru.integrations._util import (
    ContextBoundAsyncIterator,
    ContextBoundIterator,
    StreamState,
    coerce_request_id,
)


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
        payload: dict[str, Any] = {
            "service": self.service,
            "level": record.levelname,
            "name": record.name,
            "message": record.getMessage(),
        }
        # Like logging.Formatter.format(): render exc_info once and cache it on
        # the record for other handlers, and carry stack_info, so
        # logger.exception() keeps its type, message, and traceback.
        if record.exc_info:
            if not record.exc_text:
                record.exc_text = self.formatException(record.exc_info)
            payload["exception"] = record.exc_text
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        return json.dumps(payload, ensure_ascii=False)


def build_logging_config(
    *,
    service: str = "app",
    level: str = "INFO",
    json_logs: bool = True,
    disable_existing_loggers: bool | None = None,
) -> dict[str, Any]:
    """Generate a minimal Django ``LOGGING`` dict using the stdlib logging module.

    The structguru integration binds request context via context variables
    independent of the logging backend, so this config keeps a simple,
    structlog-free stdlib setup.

    Parameters
    ----------
    service:
        Application name added to every rendered line: the ``service`` field
        in JSON mode, a prefix after the level in console mode.
    level:
        Root log level.
    json_logs:
        ``True`` for a JSON-style line format, ``False`` for a plain
        human-readable console format.
    disable_existing_loggers:
        Value for Django's ``LOGGING`` dictionary. When omitted, read
        ``STRUCTGURU_STDLIB_DISABLE_EXISTING_LOGGERS`` and fall back to ``False``.
    """
    formatter = "json" if json_logs else "console"
    if disable_existing_loggers is None:
        env_value = optional_bool_from_env(
            os.environ, "STRUCTGURU_STDLIB_DISABLE_EXISTING_LOGGERS"
        )
        disable_existing_loggers = False if env_value is None else env_value
    return {
        "version": 1,
        "disable_existing_loggers": disable_existing_loggers,
        "formatters": {
            "json": {
                # Custom factory (dictConfig "()" key) so message/name are
                # JSON-escaped rather than interpolated into a format string.
                "()": "structguru.integrations.django._JSONLogFormatter",
                "service": service,
            },
            "console": {
                # Escape '%' so the service name cannot act as a format directive.
                "format": (
                    f"%(asctime)s [%(levelname)s] {service.replace('%', '%%')} "
                    "%(name)s: %(message)s"
                ),
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

    Streaming responses (``StreamingHttpResponse`` with a synchronous or an
    asynchronous iterator, and ``FileResponse``) are summarized when the server
    closes the response, so ``duration_ms`` covers the streaming time, and
    application iterators are produced with the request context bound. A
    ``FileResponse`` over a file keeps its file for ``wsgi.file_wrapper``.
    """

    def __init__(self, get_response: Any) -> None:
        self.get_response = get_response
        self.log = Logger(name="structguru.django")

    def __call__(self, request: Any) -> Any:
        """Bind request context for the duration of one Django request.

        Context is cleared on entry and on every exit path, so a recycled
        worker thread never inherits the previous request's fields. A streamed
        body re-binds the request context only while it produces chunks.
        """
        clear_contextvars()
        try:
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
                if getattr(response, "streaming", False) is True:
                    self._summarize_on_close(response, start_time)
                else:
                    self._log_summary(start_time, response.status_code, None)
                response["X-Request-ID"] = request_id
                return response
        finally:
            clear_contextvars()

    def _log_summary(
        self, start_time: float, status_code: int, error: BaseException | None
    ) -> None:
        duration_ms = round((time.perf_counter() - start_time) * 1000, 2)
        if isinstance(error, asyncio.CancelledError):
            # The ASGI handler cancels the body when the client disconnects.
            self.log.info("Request cancelled", status_code=status_code, duration_ms=duration_ms)
        elif error is not None:
            self.log.error(
                "Request failed", exc_info=error, status_code=status_code, duration_ms=duration_ms
            )
        else:
            self.log.info("Request completed", status_code=status_code, duration_ms=duration_ms)

    def _summarize_on_close(self, response: Any, start_time: float) -> None:
        """Defer the summary of a streaming *response* until its body is done."""

        def finish(error: BaseException | None) -> None:
            self._log_summary(start_time, response.status_code, error)

        state = StreamState(get_contextvars(), finish)
        if getattr(response, "file_to_stream", None) is None:
            # Re-assigning keeps Django's own handling: the wrapper's close()
            # joins the response's closers and sync/async detection still holds.
            content = response.streaming_content
            if getattr(response, "is_async", False) is True:
                response.streaming_content = ContextBoundAsyncIterator(content, state)
            else:
                response.streaming_content = ContextBoundIterator(content, state)

        # Servers call close() once the body is sent (WSGI, including
        # wsgi.file_wrapper, and Django's ASGI handler). Closers such as a
        # generator's cleanup run with the request context bound.
        original_close = response.close

        def close() -> None:
            caller_context = get_contextvars()
            if not state.finished:
                state.enter()
            error: BaseException | None = None
            try:
                original_close()
            except BaseException as exc:
                error = exc
                raise
            finally:
                state.finish(error, caller_context)

        response.close = close
