"""Tests for structguru.integrations.django."""

from __future__ import annotations

import io
from typing import Any
from unittest.mock import MagicMock

from structguru.config import configure_structlog
from structguru.integrations.django import StructguruMiddleware, build_logging_config


class TestBuildLoggingConfig:
    def test_returns_valid_dict(self) -> None:
        config = build_logging_config(service="myapp", level="DEBUG", json_logs=True)
        assert config["version"] == 1
        assert "structlog" in config["formatters"]
        assert "console" in config["handlers"]
        assert config["root"]["level"] == "DEBUG"

    def test_console_mode(self) -> None:
        config = build_logging_config(json_logs=False)
        assert config["version"] == 1

    def test_root_level(self) -> None:
        config = build_logging_config(level="WARNING")
        assert config["root"]["level"] == "WARNING"


class TestStructguruMiddleware:
    def test_binds_context_and_logs(self) -> None:
        buf = io.StringIO()
        configure_structlog(service="test", level="DEBUG", json_logs=True, stream=buf)

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
        configure_structlog(service="test", level="DEBUG", json_logs=True, stream=buf)

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
        configure_structlog(service="test", level="DEBUG", json_logs=True, stream=buf)

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
