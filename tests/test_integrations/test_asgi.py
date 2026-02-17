"""Tests for structguru.integrations.asgi."""

from __future__ import annotations

import io
from typing import Any

import pytest

from structguru.config import configure_structlog
from structguru.integrations.asgi import StructguruMiddleware


async def _simple_app(scope: dict, receive: Any, send: Any) -> None:
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"OK"})


async def _error_app(scope: dict, receive: Any, send: Any) -> None:
    raise RuntimeError("boom")


class TestStructguruMiddleware:
    @pytest.mark.asyncio
    async def test_binds_context_and_logs(self) -> None:
        buf = io.StringIO()
        configure_structlog(service="test", level="DEBUG", json_logs=True, stream=buf)

        app = StructguruMiddleware(_simple_app)
        scope: dict = {
            "type": "http",
            "method": "GET",
            "path": "/health",
            "headers": [],
            "client": ("127.0.0.1", 8000),
        }

        sent_messages: list[dict] = []

        async def receive() -> dict:
            return {"type": "http.request", "body": b""}

        async def send(msg: dict) -> None:
            sent_messages.append(msg)

        await app(scope, receive, send)

        output = buf.getvalue()
        assert "Request completed" in output
        assert "/health" in output

    @pytest.mark.asyncio
    async def test_injects_request_id_header(self) -> None:
        buf = io.StringIO()
        configure_structlog(service="test", level="DEBUG", json_logs=True, stream=buf)

        app = StructguruMiddleware(_simple_app)
        scope: dict = {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [],
            "client": None,
        }
        sent: list[dict] = []

        async def _receive() -> dict:
            return {"type": "http.request"}

        async def _send(m: dict) -> None:
            sent.append(m)

        await app(scope, _receive, _send)

        start_msg = sent[0]
        header_keys = [h[0] for h in start_msg["headers"]]
        assert b"x-request-id" in header_keys

    @pytest.mark.asyncio
    async def test_reads_existing_request_id(self) -> None:
        buf = io.StringIO()
        configure_structlog(service="test", level="DEBUG", json_logs=True, stream=buf)

        app = StructguruMiddleware(_simple_app)
        scope: dict = {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(b"x-request-id", b"custom-id-123")],
            "client": None,
        }
        sent: list[dict] = []

        async def _receive() -> dict:
            return {"type": "http.request"}

        async def _send(m: dict) -> None:
            sent.append(m)

        await app(scope, _receive, _send)

        output = buf.getvalue()
        assert "custom-id-123" in output

    @pytest.mark.asyncio
    async def test_non_http_passthrough(self) -> None:
        buf = io.StringIO()
        configure_structlog(service="test", level="DEBUG", json_logs=True, stream=buf)

        called = False

        async def lifespan_app(scope: dict, receive: Any, send: Any) -> None:
            nonlocal called
            called = True

        app = StructguruMiddleware(lifespan_app)
        await app({"type": "lifespan"}, None, None)
        assert called

    @pytest.mark.asyncio
    async def test_exception_logged(self) -> None:
        buf = io.StringIO()
        configure_structlog(service="test", level="DEBUG", json_logs=True, stream=buf)

        app = StructguruMiddleware(_error_app)
        scope: dict = {
            "type": "http",
            "method": "POST",
            "path": "/fail",
            "headers": [],
            "client": None,
        }

        with pytest.raises(RuntimeError, match="boom"):
            await app(scope, lambda: {"type": "http.request"}, lambda m: None)

        output = buf.getvalue()
        assert "Request failed" in output
