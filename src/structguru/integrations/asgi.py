"""ASGI middleware for structured request logging.

Works with any ASGI framework (FastAPI, Starlette, Litestar, etc.).
Binds ``request_id``, ``method``, ``path``, and ``client_ip`` to
structlog's context variables for every HTTP/WebSocket request.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, TypeAlias

import structlog
from structlog.contextvars import bind_contextvars, clear_contextvars

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
        Name for the structlog logger used by this middleware.
    log_request:
        If ``True``, log a summary line when each request completes.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        request_id_header: str = "x-request-id",
        logger_name: str = "structguru.asgi",
        log_request: bool = True,
    ) -> None:
        self.app = app
        self.request_id_header = request_id_header.lower().encode()
        self.logger_name = logger_name
        self.log_request = log_request

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        clear_contextvars()

        headers = dict(scope.get("headers", []))
        try:
            raw_id = headers.get(self.request_id_header, b"").decode()
        except UnicodeDecodeError:
            raw_id = ""
        # Validate: max 128 chars, no control characters.
        if raw_id and len(raw_id) <= 128 and raw_id.isprintable():
            request_id = raw_id
        else:
            request_id = str(uuid.uuid4())

        method = scope.get("method", "WS")
        path = scope.get("path", "")
        client = scope.get("client")
        client_ip = client[0] if client else ""

        bind_contextvars(
            request_id=request_id,
            method=method,
            path=path,
            client_ip=client_ip,
        )

        log = structlog.get_logger(self.logger_name)
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
