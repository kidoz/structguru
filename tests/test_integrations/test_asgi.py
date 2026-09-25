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


def _http_scope(headers: list[tuple[bytes, bytes]] | None = None) -> dict:
    return {
        "type": "http",
        "method": "GET",
        "path": "/p",
        "headers": headers or [],
        "client": ("10.0.0.1", 1),
    }


async def _receive() -> dict:
    return {"type": "http.request", "body": b""}


async def test_outer_context_is_restored_and_request_starts_clean() -> None:
    from structguru._contextvars import bind_contextvars, clear_contextvars, get_contextvars

    configure(service="test", stream=io.StringIO())
    seen: list[dict] = []

    async def app(scope: dict, receive: Any, send: Any) -> None:
        seen.append(get_contextvars())
        await _simple_app(scope, receive, send)

    async def send(message: dict) -> None:
        pass

    middleware = StructguruMiddleware(app)
    clear_contextvars()
    bind_contextvars(tenant="acme")
    try:
        await middleware(_http_scope([(b"x-request-id", b"rid")]), _receive, send)
        assert "tenant" not in seen[0]
        assert seen[0]["request_id"] == "rid"
        assert get_contextvars() == {"tenant": "acme"}

        with pytest.raises(RuntimeError, match="boom"):
            await StructguruMiddleware(_error_app)(_http_scope(), _receive, send)
        assert get_contextvars() == {"tenant": "acme"}
    finally:
        clear_contextvars()


async def test_response_echoes_request_id_under_the_configured_header() -> None:
    configure(service="test", stream=io.StringIO())
    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    middleware = StructguruMiddleware(_simple_app, request_id_header="X-Correlation-ID")
    await middleware(_http_scope([(b"x-correlation-id", b"corr-1")]), _receive, send)
    assert sent[0]["headers"] == [(b"x-correlation-id", b"corr-1")]

    async def app_with_header(scope: dict, receive: Any, send: Any) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"X-Correlation-ID", b"from-app")],
            }
        )
        await send({"type": "http.response.body", "body": b""})

    sent.clear()
    await StructguruMiddleware(app_with_header, request_id_header="X-Correlation-ID")(
        _http_scope(), _receive, send
    )
    assert sent[0]["headers"] == [(b"X-Correlation-ID", b"from-app")]


async def test_duration_ends_when_the_response_body_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from structguru.integrations import asgi as asgi_module

    clock = [100.0]
    monkeypatch.setattr(asgi_module, "time", SimpleNamespace(perf_counter=lambda: clock[0]))
    buf = io.StringIO()
    configure(service="test", stream=buf)

    async def app(scope: dict, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 201, "headers": []})
        clock[0] += 0.5
        await send({"type": "http.response.body", "body": b"a", "more_body": True})
        clock[0] += 0.5
        await send({"type": "http.response.body", "body": b"b", "more_body": False})
        clock[0] += 30.0  # background work after the client has the response
        from structguru import logger

        logger.info("background work")

    async def send(message: dict) -> None:
        pass

    await StructguruMiddleware(app)(_http_scope(), _receive, send)
    background, summary = [json.loads(line) for line in buf.getvalue().splitlines()]
    assert background["message"] == "background work"
    assert background["path"] == "/p"
    assert summary["message"] == "Request completed"
    assert summary["status_code"] == 201
    assert summary["duration_ms"] == 1000.0


async def test_starlette_background_task_is_excluded_from_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("starlette")
    from types import SimpleNamespace

    from starlette.applications import Starlette
    from starlette.background import BackgroundTask
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    from structguru.integrations import asgi as asgi_module

    clock = [0.0]
    monkeypatch.setattr(asgi_module, "time", SimpleNamespace(perf_counter=lambda: clock[0]))
    buf = io.StringIO()
    configure(service="test", stream=buf)

    async def slow_background() -> None:
        clock[0] += 10.0

    async def endpoint(request: Any) -> PlainTextResponse:
        clock[0] += 0.25
        return PlainTextResponse("ok", background=BackgroundTask(slow_background))

    app = StructguruMiddleware(Starlette(routes=[Route("/", endpoint)]))
    scope = {
        **_http_scope(),
        "path": "/",
        "query_string": b"",
        "scheme": "http",
        "server": ("test", 80),
        "root_path": "",
        "http_version": "1.1",
        "asgi": {"version": "3.0"},
    }

    async def send(message: dict) -> None:
        pass

    await app(scope, _receive, send)
    assert clock[0] == 10.25
    record = json.loads(buf.getvalue())
    assert record["message"] == "Request completed"
    assert record["duration_ms"] == 250.0


@pytest.mark.parametrize(
    "error",
    [
        KeyboardInterrupt(),
        SystemExit(1),
        BaseExceptionGroup("group", [KeyboardInterrupt()]),
    ],
)
async def test_base_exceptions_are_logged_as_failures(error: BaseException) -> None:
    from structguru._contextvars import get_contextvars

    buf = io.StringIO()
    configure(service="test", stream=buf)

    async def app(scope: dict, receive: Any, send: Any) -> None:
        raise error

    async def send(message: dict) -> None:
        pass

    with pytest.raises(BaseException) as caught:
        await StructguruMiddleware(app)(_http_scope(), _receive, send)
    assert caught.value is error
    record = json.loads(buf.getvalue())
    assert record["message"] == "Request failed"
    assert record["level"] == "ERROR"
    assert record["status_code"] == 500
    assert get_contextvars() == {}


async def test_extracted_headers_are_bounded_printable_and_redacted() -> None:
    from structguru._contextvars import get_contextvars

    buf = io.StringIO()
    configure(service="test", stream=buf)
    contexts: list[dict] = []

    async def app(scope: dict, receive: Any, send: Any) -> None:
        contexts.append(get_contextvars())
        await _simple_app(scope, receive, send)

    async def send(message: dict) -> None:
        pass

    credential_headers = [
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "x-auth-token",
        "x-session-id",
        "X-Goog-Api-Key",
    ]
    headers = [(name.lower().encode(), b"CANARY-" + name.encode()) for name in credential_headers]
    headers += [
        (b"x-tenant-id", b"acme"),
        (b"x-big", b"A" * 20_000),
        (b"x-ctl", b"line\r\nX-Injected: 1"),
        (b"x-latin", b"\xff\xfe"),
    ]
    middleware = StructguruMiddleware(
        app, extract_headers=[*credential_headers, "x-tenant-id", "x-big", "x-ctl", "x-latin"]
    )
    await middleware(_http_scope(headers), _receive, send)

    context = contexts[0]
    for name in credential_headers:
        assert context[name.lower()] == "[REDACTED]"
    assert context["x-tenant-id"] == "acme"
    assert context["x-big"].startswith("A" * 256)
    assert len(context["x-big"]) < 300
    assert "x-ctl" not in context
    assert "x-latin" not in context
    output = buf.getvalue()
    assert "CANARY" not in output
    assert "A" * 300 not in output
    assert "X-Injected" not in output


async def test_extracted_header_redaction_follows_custom_sensitive_keys() -> None:
    from structguru import configure as configure_runtime
    from structguru._contextvars import get_contextvars

    configure_runtime(target="null", sensitive_keys=["tenant"])
    contexts: list[dict] = []

    async def app(scope: dict, receive: Any, send: Any) -> None:
        contexts.append(get_contextvars())
        await _simple_app(scope, receive, send)

    async def send(message: dict) -> None:
        pass

    middleware = StructguruMiddleware(app, extract_headers=["x-tenant-id", "x-api-key"])
    await middleware(
        _http_scope([(b"x-tenant-id", b"acme"), (b"x-api-key", b"k")]), _receive, send
    )
    # The well-known credential header stays redacted with custom keys.
    assert contexts[0]["x-tenant-id"] == "[REDACTED]"
    assert contexts[0]["x-api-key"] == "[REDACTED]"
