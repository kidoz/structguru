"""Tests for structguru.integrations.grpc."""

from __future__ import annotations

from unittest.mock import MagicMock

from structlog.contextvars import clear_contextvars, get_contextvars

from structguru.integrations.grpc import StructguruInterceptor


class TestStructguruInterceptor:
    def test_binds_method_and_request_id(self) -> None:
        clear_contextvars()

        interceptor = StructguruInterceptor()

        handler_details = MagicMock()
        handler_details.method = "/myservice.MyService/GetUser"
        handler_details.invocation_metadata = [("x-request-id", "grpc-req-001")]

        continuation = MagicMock(return_value="handler")
        result = interceptor.intercept_service(continuation, handler_details)

        ctx = get_contextvars()
        assert ctx["grpc_method"] == "/myservice.MyService/GetUser"
        assert ctx["request_id"] == "grpc-req-001"
        continuation.assert_called_once_with(handler_details)
        assert result == "handler"

    def test_empty_metadata(self) -> None:
        clear_contextvars()

        interceptor = StructguruInterceptor()

        handler_details = MagicMock()
        handler_details.method = "/svc/Method"
        handler_details.invocation_metadata = []

        continuation = MagicMock()
        interceptor.intercept_service(continuation, handler_details)

        ctx = get_contextvars()
        assert ctx["grpc_method"] == "/svc/Method"
        assert ctx["request_id"] == ""

    def test_custom_request_id_key(self) -> None:
        clear_contextvars()

        interceptor = StructguruInterceptor(request_id_key="correlation-id")

        handler_details = MagicMock()
        handler_details.method = "/svc/Method"
        handler_details.invocation_metadata = [("correlation-id", "corr-abc")]

        continuation = MagicMock()
        interceptor.intercept_service(continuation, handler_details)

        ctx = get_contextvars()
        assert ctx["request_id"] == "corr-abc"
