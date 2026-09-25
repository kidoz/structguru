"""Tests for structguru.integrations.django."""

from __future__ import annotations

import io
import json
import logging.config
from typing import Any
from unittest.mock import MagicMock, PropertyMock

import pytest
from conftest import configure

from structguru._contextvars import (
    bind_contextvars,
    clear_contextvars,
    get_contextvars,
)
from structguru.integrations.django import StructguruMiddleware, build_logging_config


class TestBuildLoggingConfig:
    def test_returns_valid_dict(self) -> None:
        config = build_logging_config(service="myapp", level="DEBUG", json_logs=True)
        assert config["version"] == 1
        assert "json" in config["formatters"]
        assert "console" in config["handlers"]
        assert config["root"]["level"] == "DEBUG"

    def test_console_mode(self) -> None:
        config = build_logging_config(json_logs=False)
        assert config["version"] == 1

    def test_root_level(self) -> None:
        config = build_logging_config(level="WARNING")
        assert config["root"]["level"] == "WARNING"

    def test_disable_existing_loggers_defaults_to_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("STRUCTGURU_STDLIB_DISABLE_EXISTING_LOGGERS", raising=False)
        config = build_logging_config()
        assert config["disable_existing_loggers"] is False

    @pytest.mark.parametrize("value", [True, False])
    def test_disable_existing_loggers_explicit_value(self, value: bool) -> None:
        config = build_logging_config(disable_existing_loggers=value)
        assert config["disable_existing_loggers"] is value

    def test_disable_existing_loggers_reads_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STRUCTGURU_STDLIB_DISABLE_EXISTING_LOGGERS", "true")
        config = build_logging_config()
        assert config["disable_existing_loggers"] is True

    def test_disable_existing_loggers_explicit_value_overrides_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STRUCTGURU_STDLIB_DISABLE_EXISTING_LOGGERS", "true")
        config = build_logging_config(disable_existing_loggers=False)
        assert config["disable_existing_loggers"] is False

    def test_disable_existing_loggers_rejects_invalid_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STRUCTGURU_STDLIB_DISABLE_EXISTING_LOGGERS", "maybe")
        with pytest.raises(ValueError, match="STRUCTGURU_STDLIB_DISABLE_EXISTING_LOGGERS"):
            build_logging_config()

    def test_json_formatter_escapes_message(self) -> None:
        # A message with quotes/newlines must not break the JSON or forge fields.
        buf = io.StringIO()
        config = build_logging_config(service="svc", json_logs=True)
        config["handlers"]["console"]["class"] = "logging.StreamHandler"
        config["handlers"]["console"]["stream"] = buf
        logging.config.dictConfig(config)
        try:
            logging.getLogger("t").warning('x", "level": "CRITICAL\n')
            line = buf.getvalue().strip()
            parsed = json.loads(line)  # would raise if injection broke the JSON
            assert parsed["message"] == 'x", "level": "CRITICAL\n'
            assert parsed["service"] == "svc"
            assert parsed["level"] == "WARNING"
        finally:
            logging.getLogger("t").handlers.clear()
            logging.config.dictConfig({"version": 1, "disable_existing_loggers": False})

    def test_json_formatter_includes_exception_and_stack(self) -> None:
        buf = io.StringIO()
        config = build_logging_config(service="svc", json_logs=True)
        config["handlers"]["console"]["class"] = "logging.StreamHandler"
        config["handlers"]["console"]["stream"] = buf
        logging.config.dictConfig(config)
        try:
            try:
                raise RuntimeError("boom")
            except RuntimeError:
                logging.getLogger("t").exception("failed", stack_info=True)
            parsed = json.loads(buf.getvalue().strip())
            assert parsed["message"] == "failed"
            assert parsed["exception"].startswith("Traceback (most recent call last):")
            assert "RuntimeError: boom" in parsed["exception"]
            assert parsed["stack"].startswith("Stack (most recent call last):")
        finally:
            logging.getLogger("t").handlers.clear()
            logging.config.dictConfig({"version": 1, "disable_existing_loggers": False})


class TestStructguruMiddleware:
    @pytest.mark.parametrize("stage", ["user", "pk", "user_id"])
    @pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt])
    def test_metadata_failure_clears_context(
        self, stage: str, error_type: type[BaseException]
    ) -> None:
        buf = io.StringIO()
        configure(service="test", stream=buf)
        error = error_type("metadata unavailable")
        request = MagicMock(method="GET", path="/failed")
        request.META = {
            "HTTP_X_REQUEST_ID": "failed-request",
            "REMOTE_ADDR": "10.0.0.1",
        }
        if stage == "user":
            type(request).user = PropertyMock(side_effect=error)
        elif stage == "pk":
            type(request.user).pk = PropertyMock(side_effect=error)
        else:
            request.user.pk.__str__.side_effect = error
        response = MagicMock(status_code=200)
        seen_contexts: list[dict[str, Any]] = []

        def get_response(req: Any) -> Any:
            seen_contexts.append(get_contextvars())
            return response

        mw = StructguruMiddleware(get_response)
        bind_contextvars(stale="previous request")
        try:
            with pytest.raises(error_type) as caught:
                mw(request)

            assert caught.value is error
            assert get_contextvars() == {}
            assert seen_contexts == []
            assert buf.getvalue() == ""

            next_request = MagicMock(method="POST", path="/next")
            next_request.META = {"HTTP_X_REQUEST_ID": "next-request"}
            next_request.user.pk = None
            assert mw(next_request) is response
            assert seen_contexts == [
                {
                    "request_id": "next-request",
                    "method": "POST",
                    "path": "/next",
                    "client_ip": "",
                }
            ]
            assert get_contextvars() == {}
            response.__setitem__.assert_called_once_with("X-Request-ID", "next-request")
        finally:
            clear_contextvars()

    @pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt])
    def test_application_failure_clears_context(self, error_type: type[BaseException]) -> None:
        buf = io.StringIO()
        configure(service="test", stream=buf)
        request = MagicMock(method="GET", path="/failed")
        request.META = {"HTTP_X_REQUEST_ID": "failed-request"}
        request.user.pk = 42
        error = error_type("application failed")

        def get_response(req: Any) -> Any:
            assert get_contextvars()["user_id"] == "42"
            raise error

        try:
            with pytest.raises(error_type) as caught:
                StructguruMiddleware(get_response)(request)

            assert caught.value is error
            assert get_contextvars() == {}
            if error_type is RuntimeError:
                record = json.loads(buf.getvalue())
                assert record["message"] == "Request failed"
                assert record["request_id"] == "failed-request"
                assert record["user_id"] == "42"
                assert "RuntimeError: application failed" in record["exception"]
            else:
                assert buf.getvalue() == ""
        finally:
            clear_contextvars()

    def test_binds_context_and_logs(self) -> None:
        buf = io.StringIO()
        configure(service="test", level="DEBUG", stream=buf)

        mock_request = MagicMock()
        mock_request.method = "GET"
        mock_request.path = "/api/test"
        mock_request.META = {"REMOTE_ADDR": "10.0.0.1"}
        mock_request.headers = {}
        mock_request.user = MagicMock(pk=None)

        mock_response = MagicMock()
        mock_response.status_code = 200

        def get_response(req: Any) -> Any:
            return mock_response

        mw = StructguruMiddleware(get_response)
        response = mw(mock_request)

        output = buf.getvalue()
        assert "Request completed" in output
        assert response is mock_response

    def test_sets_request_id_header(self) -> None:
        buf = io.StringIO()
        configure(service="test", level="DEBUG", stream=buf)

        mock_request = MagicMock()
        mock_request.method = "GET"
        mock_request.path = "/"
        mock_request.META = {"HTTP_X_REQUEST_ID": "custom-123"}
        mock_request.user = MagicMock(pk=None)

        mock_response = MagicMock()
        mock_response.status_code = 200

        mw = StructguruMiddleware(lambda r: mock_response)
        mw(mock_request)

        mock_response.__setitem__.assert_called_with("X-Request-ID", "custom-123")

    def test_binds_user_id_when_available(self) -> None:
        buf = io.StringIO()
        configure(service="test", level="DEBUG", stream=buf)

        mock_request = MagicMock()
        mock_request.method = "GET"
        mock_request.path = "/"
        mock_request.META = {}
        mock_request.headers = {}
        mock_request.user = MagicMock(pk=42)

        mock_response = MagicMock()
        mock_response.status_code = 200

        mw = StructguruMiddleware(lambda r: mock_response)
        mw(mock_request)

        output = buf.getvalue()
        assert "42" in output


class TestErrorLoggingThroughBridge:
    def test_log_response_with_request_extra_is_kept(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # django.utils.log.log_response() attaches the raw request object as
        # extra["request"] on every HTTP error. The stdlib bridge must keep the
        # event — with a marker in the request's place — instead of falling
        # back to a "--- Logging error ---" diagnostic.
        pytest.importorskip("django")
        from django.conf import settings
        from django.core.handlers.wsgi import WSGIRequest
        from django.http import HttpResponse
        from django.utils.log import log_response

        from structguru import _runtime
        from structguru.integrations.stdlib import install_stdlib_bridge

        if not settings.configured:
            settings.configure(DEFAULT_CHARSET="utf-8")
        _runtime.configure(service="repro", target="memory", level="INFO")
        install_stdlib_bridge(level="INFO")
        request = WSGIRequest(
            {
                "REQUEST_METHOD": "GET",
                "PATH_INFO": "/repro",
                "wsgi.input": io.BytesIO(),
                "wsgi.url_scheme": "http",
                "SERVER_NAME": "localhost",
                "SERVER_PORT": "80",
            }
        )

        try:
            raise RuntimeError("synthetic application failure")
        except RuntimeError as exc:
            log_response(
                "%s: %s",
                "Internal Server Error",
                request.path,
                response=HttpResponse(status=500),
                request=request,
                exception=exc,
            )
        _runtime.flush_native()
        lines = _runtime.drain_messages()

        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["logger"] == "django.request"
        assert rec["level"] == "ERROR"
        assert rec["status_code"] == 500
        assert rec["message"] == "Internal Server Error: /repro"
        assert rec["request"] == "<unsupported: WSGIRequest>"
        assert "RuntimeError: synthetic application failure" in rec["exception"]
        assert "Logging error" not in capsys.readouterr().err


def _django_settings() -> None:
    pytest.importorskip("django")
    from django.conf import settings

    if not settings.configured:
        settings.configure(DEFAULT_CHARSET="utf-8")


def _stream_request(request_id: str) -> MagicMock:
    request = MagicMock(method="GET", path="/stream")
    request.META = {"HTTP_X_REQUEST_ID": request_id, "REMOTE_ADDR": "10.0.0.9"}
    request.user.pk = None
    return request


def _fake_clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    from types import SimpleNamespace

    from structguru.integrations import django as django_module

    clock = [0.0]
    monkeypatch.setattr(django_module, "time", SimpleNamespace(perf_counter=lambda: clock[0]))
    return clock


def _records(buf: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]


class TestStreamingResponses:
    def test_sync_stream_runs_with_request_context_and_summarizes_at_the_end(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _django_settings()
        from django.http import StreamingHttpResponse

        from structguru import logger

        clock = _fake_clock(monkeypatch)
        buf = io.StringIO()
        configure(service="test", stream=buf)
        clear_contextvars()

        def view(request: Any) -> StreamingHttpResponse:
            bind_contextvars(user="u1")

            def generate():  # type: ignore[no-untyped-def]
                for i in range(2):
                    clock[0] += 1.5
                    logger.info("chunk", i=i)
                    yield b"x"

            return StreamingHttpResponse(generate())

        response = StructguruMiddleware(view)(_stream_request("rid-stream"))
        assert response["X-Request-ID"] == "rid-stream"
        assert get_contextvars() == {}
        assert _records(buf) == []

        chunks = iter(response)
        assert next(chunks) == b"x"
        assert get_contextvars() == {}
        assert list(chunks) == [b"x"]
        response.close()
        assert get_contextvars() == {}

        first, second, summary = _records(buf)
        for record in (first, second):
            assert record["request_id"] == "rid-stream"
            assert record["path"] == "/stream"
            assert record["user"] == "u1"
        assert summary["message"] == "Request completed"
        assert summary["request_id"] == "rid-stream"
        assert summary["status_code"] == 200
        assert summary["duration_ms"] == 3000.0

    async def test_async_stream_runs_with_request_context(self) -> None:
        _django_settings()
        from django.http import StreamingHttpResponse

        from structguru import logger

        buf = io.StringIO()
        configure(service="test", stream=buf)
        clear_contextvars()

        def view(request: Any) -> StreamingHttpResponse:
            async def generate():  # type: ignore[no-untyped-def]
                logger.info("async chunk")
                yield b"a"
                yield b"b"

            return StreamingHttpResponse(generate())

        response = StructguruMiddleware(view)(_stream_request("rid-async"))
        assert response.is_async
        parts = [part async for part in response]
        assert parts == [b"a", b"b"]
        assert get_contextvars() == {}
        response.close()

        chunk, summary = _records(buf)
        assert chunk["message"] == "async chunk"
        assert chunk["request_id"] == "rid-async"
        assert summary["message"] == "Request completed"
        assert summary["request_id"] == "rid-async"

    async def test_cancelled_async_stream_is_logged_as_cancelled(self) -> None:
        import asyncio

        _django_settings()
        from django.http import StreamingHttpResponse

        buf = io.StringIO()
        configure(service="test", stream=buf)
        clear_contextvars()

        def view(request: Any) -> StreamingHttpResponse:
            async def generate():  # type: ignore[no-untyped-def]
                yield b"a"
                raise asyncio.CancelledError  # the client disconnected

            return StreamingHttpResponse(generate())

        response = StructguruMiddleware(view)(_stream_request("rid-gone"))
        with pytest.raises(asyncio.CancelledError):
            async for _part in response:
                pass
        response.close()
        [summary] = _records(buf)
        assert summary["message"] == "Request cancelled"
        assert summary["level"] == "INFO"
        assert summary["request_id"] == "rid-gone"
        assert get_contextvars() == {}

    def test_stream_failure_and_early_close(self) -> None:
        _django_settings()
        from django.http import StreamingHttpResponse

        buf = io.StringIO()
        configure(service="test", stream=buf)
        clear_contextvars()
        cleanup_contexts: list[dict[str, Any]] = []

        def failing(request: Any) -> StreamingHttpResponse:
            def generate():  # type: ignore[no-untyped-def]
                yield b"a"
                raise RuntimeError("stream broke")

            return StreamingHttpResponse(generate())

        response = StructguruMiddleware(failing)(_stream_request("rid-broken"))
        with pytest.raises(RuntimeError, match="stream broke"):
            list(response)
        response.close()
        [summary] = _records(buf)
        assert summary["message"] == "Request failed"
        assert summary["level"] == "ERROR"
        assert summary["request_id"] == "rid-broken"
        assert "RuntimeError: stream broke" in summary["exception"]

        buf.truncate(0)
        buf.seek(0)

        def partial(request: Any) -> StreamingHttpResponse:
            def generate():  # type: ignore[no-untyped-def]
                try:
                    yield b"a"
                    yield b"b"
                finally:
                    cleanup_contexts.append(get_contextvars())

            return StreamingHttpResponse(generate())

        response = StructguruMiddleware(partial)(_stream_request("rid-partial"))
        assert next(iter(response)) == b"a"
        response.close()  # the server gives up after one chunk
        assert cleanup_contexts[-1]["request_id"] == "rid-partial"
        [summary] = _records(buf)
        assert summary["message"] == "Request completed"
        assert summary["request_id"] == "rid-partial"
        assert get_contextvars() == {}

    def test_file_response_keeps_its_file_and_summarizes_on_close(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _django_settings()
        from django.http import FileResponse

        clock = _fake_clock(monkeypatch)
        buf = io.StringIO()
        configure(service="test", stream=buf)
        clear_contextvars()
        body = io.BytesIO(b"file-body")

        response = StructguruMiddleware(lambda request: FileResponse(body))(
            _stream_request("rid-file")
        )
        # The file stays available to wsgi.file_wrapper (sendfile) servers.
        assert response.file_to_stream is body
        assert _records(buf) == []
        clock[0] += 2.0
        assert b"".join(response) == b"file-body"
        response.close()
        assert body.closed
        [summary] = _records(buf)
        assert summary["message"] == "Request completed"
        assert summary["request_id"] == "rid-file"
        assert summary["duration_ms"] == 2000.0
        assert get_contextvars() == {}

    def test_non_streaming_response_is_summarized_immediately(self) -> None:
        _django_settings()
        from django.http import HttpResponse

        buf = io.StringIO()
        configure(service="test", stream=buf)
        response = StructguruMiddleware(lambda request: HttpResponse("ok"))(
            _stream_request("rid-plain")
        )
        [summary] = _records(buf)
        assert summary["request_id"] == "rid-plain"
        response.close()
        assert len(_records(buf)) == 1


class TestBuildLoggingConfigService:
    @pytest.mark.parametrize("json_logs", [True, False])
    def test_service_appears_in_every_mode(self, json_logs: bool) -> None:
        buf = io.StringIO()
        config = build_logging_config(service="svc-100%", json_logs=json_logs)
        config["handlers"]["console"]["stream"] = buf
        logging.config.dictConfig(config)
        try:
            logging.getLogger("t").warning("hello")
            line = buf.getvalue().strip()
            if json_logs:
                assert json.loads(line)["service"] == "svc-100%"
            else:
                assert "svc-100%" in line
                assert line.endswith("t: hello")
        finally:
            logging.config.dictConfig({"version": 1, "disable_existing_loggers": False})
