"""Requests integration for outbound structured request logging.

Provides a wrapper for `requests.Session` (or monkey-patch) to automatically
log outbound requests and their responses.
"""

from __future__ import annotations

import time
from typing import Any

import requests

from structguru.core import Logger
from structguru.integrations._util import sanitize_url

_logger = Logger(name="structguru.requests")


class StructguruRequestsSession(requests.Session):
    """A wrapper around `requests.Session` that logs requests and responses."""

    def request(
        self, method: str | bytes, url: str | bytes, *args: Any, **kwargs: Any
    ) -> requests.Response:
        """Send a request and log the result."""
        method_str = method if isinstance(method, str) else method.decode()
        start_time = time.perf_counter()

        # Extract an x-request-id from the per-call headers, falling back to
        # the session-level headers, for end-to-end correlation.
        headers = kwargs.get("headers") or {}
        request_id = None
        if isinstance(headers, dict):
            request_id = {k.lower(): v for k, v in headers.items()}.get("x-request-id")

        if not request_id and self.headers:
            request_id = {k.lower(): v for k, v in self.headers.items()}.get("x-request-id")

        failed = False
        status_code = None
        try:
            response = super().request(method_str, url, *args, **kwargs)
            status_code = response.status_code
            return response
        except Exception:
            failed = True
            raise
        finally:
            duration_ms = (time.perf_counter() - start_time) * 1000

            extra: dict[str, Any] = {
                "http_method": method_str.upper(),
                "http_url": sanitize_url(url),
                "duration_ms": round(duration_ms, 2),
            }
            if status_code is not None:
                extra["status_code"] = status_code
            if request_id:
                extra["request_id"] = request_id

            if failed or (status_code and status_code >= 400):
                _logger.error("Outbound HTTP Request Failed", **extra)
            else:
                _logger.info("Outbound HTTP Request Completed", **extra)


def get_logging_session() -> StructguruRequestsSession:
    """Return a new `requests.Session` instance equipped with structured logging."""
    return StructguruRequestsSession()
