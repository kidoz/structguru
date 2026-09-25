"""Requests integration for outbound structured request logging.

Provides a wrapper for `requests.Session` (or monkey-patch) to automatically
log outbound requests and their responses.

Requires the ``requests`` extra. Unlike the other adapters this one imports its
framework at module scope: :class:`StructguruRequestsSession` subclasses
``requests.Session``, so the base class has to exist when the class is created.
Importing this module is already an explicit opt-in — nothing in ``structguru``
imports it for you — so the dependency is never pulled in implicitly.
"""

from __future__ import annotations

import time
from typing import Any

import requests

from structguru.core import Logger
from structguru.integrations._util import sanitize_url

_logger = Logger(name="structguru.requests")


class StructguruRequestsSession(requests.Session):
    """A wrapper around `requests.Session` that logs requests and responses.

    The logged ``http_url`` is the URL requests actually sends — including
    ``params=`` and session-level ``params`` — with credentials and query
    values removed (a bare ``?`` marks that parameters were present).
    """

    def request(
        self, method: str | bytes, url: str | bytes, *args: Any, **kwargs: Any
    ) -> requests.Response:
        """Send a request and log the result."""
        method_str = method if isinstance(method, str) else method.decode()
        start_time = time.perf_counter()
        # params is the first positional argument after url in Session.request.
        params = kwargs["params"] if "params" in kwargs else (args[0] if args else None)
        logged_url = self._effective_url(url, params)

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
        except BaseException:
            # KeyboardInterrupt/SystemExit interrupt the request too: a failure.
            failed = True
            raise
        finally:
            duration_ms = (time.perf_counter() - start_time) * 1000

            extra: dict[str, Any] = {
                "http_method": method_str.upper(),
                "http_url": sanitize_url(logged_url),
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

    def _effective_url(self, url: str | bytes, params: Any) -> str | bytes:
        """Return *url* with the merged request and session params applied.

        Parameters are merged later, while requests prepares the request, so
        without this the logged URL would lack the ``?`` marker. On any
        preparation failure the original URL is returned; requests reports
        the error itself.
        """
        try:
            merged = requests.sessions.merge_setting(params, self.params)
            if not merged:
                return url
            prepared = requests.PreparedRequest()
            prepared.prepare_url(url, merged)
        except Exception:  # noqa: BLE001 - diagnostics must not mask request failures
            return url
        return prepared.url or url


def get_logging_session() -> StructguruRequestsSession:
    """Return a new `requests.Session` instance equipped with structured logging."""
    return StructguruRequestsSession()
