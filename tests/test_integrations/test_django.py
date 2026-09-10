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
