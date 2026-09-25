"""ASGI middleware for structured request logging.

Works with any ASGI framework (FastAPI, Starlette, Litestar, etc.).
Binds ``request_id``, ``method``, ``path``, and ``client_ip`` to
structguru context variables for every HTTP/WebSocket request.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, TypeAlias

from structguru._contextvars import bind_contextvars, get_contextvars
from structguru.core import Logger
from structguru.integrations._util import (
    REDACTED,
    active_sensitive_keys,
    coerce_header_value,
    coerce_request_id,
    is_credential_header,
    replace_contextvars,
)

Scope: TypeAlias = dict[str, Any]
Receive: TypeAlias = Callable[[], Awaitable[dict[str, Any]]]
Send: TypeAlias = Callable[[dict[str, Any]], Awaitable[None]]
ASGIApp: TypeAlias = Callable[[Scope, Receive, Send], Awaitable[None]]


class StructguruMiddleware:
    """ASGI middleware that provides structured request logging.

    Parameters
    ----------
    app:
        The ASGI application to wrap.
    request_id_header:
        Header name to read an existing request ID from (case-insensitive).
        The request ID is echoed on the response under the same name.
    logger_name:
        Name for the structguru logger used by this middleware.
    log_request:
        If ``True``, log a summary line when each request completes.
    extract_headers:
        A list of additional header names to extract and bind to the context.
        Request metadata takes precedence if an extracted name collides with
        ``request_id``, ``method``, ``path``, or ``client_ip``. Values must be
        UTF-8 and printable (otherwise they are dropped) and are truncated to
        256 characters. Credential-bearing headers (``authorization``,
        ``cookie``, ``x-api-key``, and any name containing an active sensitive
        key, such as ``x-auth-token``) are bound as ``[REDACTED]``.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        request_id_header: str = "x-request-id",
        logger_name: str = "structguru.asgi",
        log_request: bool = True,
        extract_headers: Sequence[str] | None = None,
    ) -> None:
        self.app = app
        self.request_id_header = request_id_header.lower().encode()
        self.logger_name = logger_name
        self.log_request = log_request
        self.extract_headers = (
            [h.lower().encode() for h in extract_headers] if extract_headers else []
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Bind request context for the duration of one ASGI request.

        Non-HTTP/WebSocket scopes (``lifespan``) pass straight through
        untouched. The request runs with a clean context holding only its own
        fields; the caller's context (for example, bound by an outer
        middleware) is restored on every exit path.
        """
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        outer_context = get_contextvars()
        replace_contextvars({})
        try:
            await self._handle(scope, receive, send)
        finally:
            replace_contextvars(outer_context)

    def _extract_header_context(self, headers_dict: dict[bytes, bytes]) -> dict[str, str]:
        extracted: dict[str, str] = {}
        sensitive_keys: frozenset[str] | None = None
        for header_key in self.extract_headers:
            raw = headers_dict.get(header_key)
            if raw is None:
                continue
            name = header_key.decode()
            if sensitive_keys is None:
                sensitive_keys = active_sensitive_keys()
            if is_credential_header(name, sensitive_keys):
                extracted[name] = REDACTED
                continue
            value = coerce_header_value(raw)
            if value is not None:
                extracted[name] = value
        return extracted

    async def _handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        headers = scope.get("headers", [])
        headers_dict = dict(headers)
        request_id = coerce_request_id(headers_dict.get(self.request_id_header))

        method = scope.get("method", "WS")
        path = scope.get("path", "")
        client = scope.get("client")
        client_ip = client[0] if client else ""

        extra_context = self._extract_header_context(headers_dict)
        # Request metadata takes precedence over extracted headers with the same
        # name; headers must neither override it nor prevent request execution.
        extra_context.update(request_id=request_id, method=method, path=path, client_ip=client_ip)
        bind_contextvars(**extra_context)

        log = Logger(name=self.logger_name)
        start_time = time.perf_counter()
        end_time: float | None = None
        is_websocket = scope["type"] == "websocket"
        status_code: int | None = None
        request_id_header = self.request_id_header

        async def send_wrapper(message: dict[str, Any]) -> None:
            nonlocal status_code, end_time
            message_type = message["type"]
            if message_type == "http.response.start":
                status_code = message["status"]
                resp_headers = list(message.get("headers", []))
                if not any(k.lower() == request_id_header for k, _ in resp_headers):
                    resp_headers.append((request_id_header, request_id.encode()))
                message = {**message, "headers": resp_headers}
            await send(message)
            if (
                message_type == "http.response.body"
                and not message.get("more_body", False)
                and end_time is None
            ):
                # The client has the whole response. Work the app does after
                # this point (e.g. Starlette BackgroundTasks) is not request
                # latency, although it still runs inside the request context.
                end_time = time.perf_counter()

        outcome = "completed"
        try:
            await self.app(scope, receive, send_wrapper)
        except (asyncio.CancelledError, GeneratorExit):
            # A cancelled request (client disconnect, shutdown, timeout) neither
            # completed nor failed. Record that, then let the cancellation
            # propagate so the surrounding task still observes it.
            outcome = "cancelled"
            raise
        except BaseException:
            # KeyboardInterrupt, SystemExit, and exception groups are failures
            # too; they are logged as such and re-raised untouched.
            outcome = "failed"
            raise
        finally:
            if self.log_request:
                finished = end_time if end_time is not None else time.perf_counter()
                duration_ms = (finished - start_time) * 1000
                extra: dict[str, Any] = {"duration_ms": round(duration_ms, 2)}
                if status_code is not None:
                    extra["status_code"] = status_code
                elif not is_websocket and outcome != "cancelled":
                    # No response was started: the server answers with 500. A
                    # cancelled request sends nothing, so no status is invented.
                    extra["status_code"] = 500
                if outcome == "failed":
                    log.error("Request failed", **extra)
                elif outcome == "cancelled":
                    log.info("Request cancelled", **extra)
                else:
                    log.info("Request completed", **extra)
