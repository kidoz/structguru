"""Tests for structguru.integrations.asgi."""

from __future__ import annotations

import asyncio
import io
import json
from typing import Any

import pytest
from conftest import configure

from structguru.integrations.asgi import StructguruMiddleware


@pytest.mark.parametrize("header", ["request_id", "method", "path", "client_ip"])
async def test_extracted_headers_cannot_replace_request_metadata(header: str) -> None:
    from structguru._contextvars import get_contextvars

    contexts = []
    sent = []

    async def app(scope: dict, receive: Any, send: Any) -> None:
        contexts.append(get_contextvars())
        await _simple_app(scope, receive, send)

    async def receive() -> dict:
        return {"type": "http.request", "body": b""}

    async def send(message: dict) -> None:
        sent.append(message)

    middleware = StructguruMiddleware(app, extract_headers=[header, "x-extra"])
    await middleware(
        {
            "type": "http",
            "method": "GET",
            "path": "/real",
            "client": ("127.0.0.1", 1),
            "headers": [
                (b"x-request-id", b"real-id"),
                (header.encode(), b"spoofed"),
                (b"x-extra", b"retained"),
            ],
        },
        receive,
        send,
    )
    assert contexts == [
        {
            "request_id": "real-id",
            "method": "GET",
            "path": "/real",
            "client_ip": "127.0.0.1",
            "x-extra": "retained",
        }
    ]
    assert sent[0]["status"] == 200
    assert (b"x-request-id", b"real-id") in sent[0]["headers"]
    assert get_contextvars() == {}


async def _simple_app(scope: dict, receive: Any, send: Any) -> None:
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"OK"})


async def _error_app(scope: dict, receive: Any, send: Any) -> None:
    raise RuntimeError("boom")


class TestStructguruMiddleware:
    @pytest.mark.asyncio
    async def test_binds_context_and_logs(self) -> None:
        buf = io.StringIO()
        configure(service="test", level="DEBUG", stream=buf)

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
        configure(service="test", level="DEBUG", stream=buf)

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
        configure(service="test", level="DEBUG", stream=buf)

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
        configure(service="test", level="DEBUG", stream=buf)

        called = False

        async def lifespan_app(scope: dict, receive: Any, send: Any) -> None:
            nonlocal called
            called = True

        app = StructguruMiddleware(lifespan_app)
        await app({"type": "lifespan"}, None, None)
        assert called

    @pytest.mark.asyncio
    async def test_websocket_no_false_500(self) -> None:
        buf = io.StringIO()
        configure(service="test", level="DEBUG", stream=buf)

        async def ws_app(scope: dict, receive: Any, send: Any) -> None:
            await send({"type": "websocket.accept"})
            await send({"type": "websocket.close", "code": 1000})

        app = StructguruMiddleware(ws_app)
        scope: dict = {
            "type": "websocket",
            "path": "/ws",
            "headers": [],
            "client": ("127.0.0.1", 9000),
        }

        sent: list[dict] = []

        async def _receive() -> dict:
            return {"type": "websocket.connect"}

        async def _send(m: dict) -> None:
            sent.append(m)

        await app(scope, _receive, _send)

        output = buf.getvalue()
        assert "Request completed" in output
        # WebSocket logs should NOT contain status_code (no false 500).
        assert "status_code" not in output

    @pytest.mark.asyncio
    async def test_exception_logged(self) -> None:
        buf = io.StringIO()
        configure(service="test", level="DEBUG", stream=buf)

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

    @pytest.mark.asyncio
    async def test_invalid_utf8_request_id_header(self) -> None:
        buf = io.StringIO()
        configure(service="test", level="DEBUG", stream=buf)

        app = StructguruMiddleware(_simple_app)
        scope: dict = {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(b"x-request-id", b"\xff\xfe")],
            "client": None,
        }
        sent: list[dict] = []

        async def _receive() -> dict:
            return {"type": "http.request"}

        async def _send(m: dict) -> None:
            sent.append(m)

        await app(scope, _receive, _send)

        # Should not crash; a UUID should be generated instead.
        output = buf.getvalue()
        assert "Request completed" in output
        header_keys = [h[0] for h in sent[0]["headers"]]
        assert b"x-request-id" in header_keys


@pytest.mark.parametrize("response_started", [False, True])
async def test_cancellation_is_logged_as_cancelled_and_propagated(response_started: bool) -> None:
    from structguru._contextvars import get_contextvars

    async def cancelling_app(scope: dict, receive: Any, send: Any) -> None:
        if response_started:
            await send({"type": "http.response.start", "status": 200, "headers": []})
        raise asyncio.CancelledError

    async def send(message: dict) -> None:
        pass

    buf = io.StringIO()
    configure(service="test", level="DEBUG", stream=buf)
    app = StructguruMiddleware(cancelling_app)
    scope: dict = {"type": "http", "method": "GET", "path": "/slow", "headers": [], "client": None}

    with pytest.raises(asyncio.CancelledError):
        await app(scope, lambda: {"type": "http.request"}, send)

    record = json.loads(buf.getvalue())
    assert record["message"] == "Request cancelled"
    assert record["level"] == "INFO"
    # No status is invented for a request that never started its response.
    assert record.get("status_code") == (200 if response_started else None)
    assert get_contextvars() == {}
