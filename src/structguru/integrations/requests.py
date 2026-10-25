"""Requests integration for outbound structured request logging.

Provides ``StructguruRequestsSession``, a ``requests.Session`` subclass, and
``get_logging_session()`` to log outbound requests and their responses.

Requires the ``requests`` extra. Unlike the other adapters this one imports its
framework at module scope: ``StructguruRequestsSession`` subclasses
``requests.Session``, so the base class has to exist when the class is created.
Importing this module is already an explicit opt-in — nothing in ``structguru``
imports it for you — so the dependency is never pulled in implicitly.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
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
        logged_url: str | bytes = self._effective_url(url, params)

        # Mirror requests' case-insensitive merge for failures that happen
        # before a prepared response exists. Positional headers follow params
        # and data in Session.request's signature.
        headers = (
            kwargs["headers"] if "headers" in kwargs else (args[2] if len(args) > 2 else None)
        )
        merged_headers = requests.sessions.merge_setting(
            headers,
            self.headers,
            dict_class=requests.structures.CaseInsensitiveDict,
        )
        request_id = (
            merged_headers.get("x-request-id") if isinstance(merged_headers, Mapping) else None
        )

        failed = False
        status_code = None
        try:
            response = super().request(method_str, url, *args, **kwargs)
            status_code = response.status_code
            prepared_request = response.request
            if prepared_request is not None:
                logged_url = prepared_request.url or logged_url
                request_id = prepared_request.headers.get("x-request-id")
            return response
        except BaseException as exc:
            # Redirect failures carry the last prepared request on the exception.
            if isinstance(exc, requests.RequestException) and exc.request is not None:
                logged_url = exc.request.url or logged_url
                request_id = exc.request.headers.get("x-request-id")
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

    def _effective_url(self, url: str | bytes, params: Any) -> str:
        """Mark possible query parameters without consuming caller-owned data.

        Responses and transport exceptions supply the actual prepared URL.
        Before preparation succeeds, a conservative marker is sufficient.
        """
        logged = sanitize_url(url)
        if (params is not None or self.params) and not logged.endswith("?"):
            return f"{logged}?" if logged != "<unparsable-url>" else logged
        return logged


def get_logging_session() -> StructguruRequestsSession:
    """Return a new `requests.Session` instance equipped with structured logging."""
    return StructguruRequestsSession()
