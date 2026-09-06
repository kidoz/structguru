"""ASGI middleware for structured request logging.

Works with any ASGI framework (FastAPI, Starlette, Litestar, etc.).
Binds ``request_id``, ``method``, ``path``, and ``client_ip`` to
structguru context variables for every HTTP/WebSocket request.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, TypeAlias

from structguru._contextvars import bind_contextvars, clear_contextvars
from structguru.core import Logger
from structguru.integrations._util import coerce_request_id

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
    logger_name:
        Name for the structguru logger used by this middleware.
    log_request:
        If ``True``, log a summary line when each request completes.
    extract_headers:
        A list of additional header names to extract and bind to the context.
        Request metadata takes precedence if an extracted name collides with
        ``request_id``, ``method``, ``path``, or ``client_ip``.
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
        untouched. Context is cleared on entry and on every exit path.
        """
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        clear_contextvars()

        headers = scope.get("headers", [])
        headers_dict = dict(headers)
        try:
            raw_id = headers_dict.get(self.request_id_header, b"").decode()
        except UnicodeDecodeError:
            raw_id = ""
        request_id = coerce_request_id(raw_id)

        method = scope.get("method", "WS")
        path = scope.get("path", "")
        client = scope.get("client")
        client_ip = client[0] if client else ""

        extra_context = {}
        for header_key in self.extract_headers:
            val = headers_dict.get(header_key)
            if val is not None:
                try:
                    extra_context[header_key.decode()] = val.decode()
                except UnicodeDecodeError:
                    pass

        # Request metadata takes precedence over extracted headers with the same
        # name; headers must neither override it nor prevent request execution.
        extra_context.update(request_id=request_id, method=method, path=path, client_ip=client_ip)
        bind_contextvars(**extra_context)

        log = Logger(name=self.logger_name)
        start_time = time.perf_counter()
        is_websocket = scope["type"] == "websocket"
        status_code: int | None = None if is_websocket else 500

        async def send_wrapper(message: dict[str, Any]) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                resp_headers = list(message.get("headers", []))
                if not any(k == b"x-request-id" for k, _ in resp_headers):
                    resp_headers.append((b"x-request-id", request_id.encode()))
                message = {**message, "headers": resp_headers}
            await send(message)

        failed = False
        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            failed = True
            raise
        finally:
            if self.log_request:
                duration_ms = (time.perf_counter() - start_time) * 1000
                extra: dict[str, Any] = {"duration_ms": round(duration_ms, 2)}
                if status_code is not None:
                    extra["status_code"] = status_code
                if failed:
                    log.error("Request failed", **extra)
                else:
                    log.info("Request completed", **extra)
            clear_contextvars()
