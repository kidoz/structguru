"""HTTPX integration for outbound structured request logging.

Provides separate event hooks for `httpx.Client` and `httpx.AsyncClient` to
automatically log outbound requests and their responses.
"""

from __future__ import annotations

import time
from typing import Any

from structguru.core import Logger
from structguru.integrations._util import sanitize_url

_logger = Logger(name="structguru.httpx")


def log_request(request: Any) -> None:
    """Event hook to log the start of an outbound HTTPX request."""
    setattr(request, "_structguru_start_time", time.perf_counter())


def log_response(response: Any) -> None:
    """Event hook to log the completion of an outbound HTTPX request."""
    request = response.request
    start_time = getattr(request, "_structguru_start_time", None)

    extra: dict[str, Any] = {
        "http_method": request.method,
        "http_url": sanitize_url(request.url),
        "status_code": response.status_code,
    }
    if start_time is not None:
        duration_ms = (time.perf_counter() - start_time) * 1000
        extra["duration_ms"] = round(duration_ms, 2)

    # Extract x-request-id or custom correlation headers if present in request headers
    # This enables end-to-end tracing
    request_id = request.headers.get("x-request-id")
    if request_id:
        extra["request_id"] = request_id

    if response.is_error:
        _logger.error("Outbound HTTP Request Failed", **extra)
    else:
        _logger.info("Outbound HTTP Request Completed", **extra)


async def async_log_request(request: Any) -> None:
    """AsyncClient-compatible request hook with the synchronous hook's behavior."""
    log_request(request)


async def async_log_response(response: Any) -> None:
    """AsyncClient-compatible response hook with the synchronous hook's behavior."""
    log_response(response)


class StructguruHTTPXLoggingHooks:
    """A convenient namespace for the HTTPX logging hooks.

    Note
    ----
    Logging happens in the ``response`` hook, which httpx invokes only once a
    response has been received. Transport-level failures (connection refused,
    timeouts, DNS errors) raise before that hook runs and are therefore not
    logged here.
    """

    @classmethod
    def get_hooks(cls) -> dict[str, list[Any]]:
        """Return fresh synchronous hooks for :class:`httpx.Client`."""
        return {
            "request": [log_request],
            "response": [log_response],
        }

    @classmethod
    def get_async_hooks(cls) -> dict[str, list[Any]]:
        """Return fresh asynchronous hooks for :class:`httpx.AsyncClient`."""
        return {
            "request": [async_log_request],
            "response": [async_log_response],
        }
