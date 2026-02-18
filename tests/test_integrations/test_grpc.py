"""Tests for structguru.integrations.grpc."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from structlog.contextvars import bind_contextvars, clear_contextvars, get_contextvars

from structguru.integrations.grpc import StructguruInterceptor


def _make_handler_details(
    method: str = "/svc/Method",
    metadata: list[tuple[str, str]] | None = None,
) -> MagicMock:
    details = MagicMock()
    details.method = method
    details.invocation_metadata = metadata or []
    return details


def _make_handler(
    unary_unary: object = None,
    unary_stream: object = None,
) -> MagicMock:
    """Return a mock gRPC handler."""
    handler = MagicMock()
    handler.unary_unary = unary_unary
    handler.unary_stream = unary_stream
    handler.stream_unary = None
    handler.stream_stream = None
    handler._replace = MagicMock(side_effect=lambda **kw: _apply_replace(handler, kw))
    return handler


def _apply_replace(handler: MagicMock, kw: dict) -> MagicMock:
    for k, v in kw.items():
        setattr(handler, k, v)
    return handler


class TestStructguruInterceptor:
    def test_binds_method_and_request_id(self) -> None:
        clear_contextvars()

        interceptor = StructguruInterceptor()
        details = _make_handler_details(
            "/myservice.MyService/GetUser",
            [("x-request-id", "grpc-req-001")],
        )

        def fake_unary(request: object, context: object) -> str:
            ctx = get_contextvars()
            assert ctx["grpc_method"] == "/myservice.MyService/GetUser"
            assert ctx["request_id"] == "grpc-req-001"
            return "ok"

        handler = _make_handler(unary_unary=fake_unary)
        continuation = MagicMock(return_value=handler)

        result = interceptor.intercept_service(continuation, details)
        # Execute the wrapped handler.
        assert result.unary_unary("req", "ctx") == "ok"

    def test_empty_metadata_generates_uuid(self) -> None:
        clear_contextvars()

        interceptor = StructguruInterceptor()
        details = _make_handler_details()

        def fake_unary(request: object, context: object) -> str:
            ctx = get_contextvars()
            assert len(ctx["request_id"]) == 36
            return "ok"

        handler = _make_handler(unary_unary=fake_unary)
        continuation = MagicMock(return_value=handler)

        result = interceptor.intercept_service(continuation, details)
        result.unary_unary("req", "ctx")

    def test_clears_context_after_handler_execution(self) -> None:
        clear_contextvars()

        interceptor = StructguruInterceptor()
        details = _make_handler_details(
            metadata=[("x-request-id", "req-999")],
        )

        def fake_unary(request: object, context: object) -> str:
            return "ok"

        handler = _make_handler(unary_unary=fake_unary)
        continuation = MagicMock(return_value=handler)

        result = interceptor.intercept_service(continuation, details)
        result.unary_unary("req", "ctx")

        # Context should be cleared after handler completes.
        ctx = get_contextvars()
        assert "grpc_method" not in ctx
        assert "request_id" not in ctx

    def test_clears_context_on_handler_exception(self) -> None:
        clear_contextvars()

        interceptor = StructguruInterceptor()
        details = _make_handler_details()

        def failing_unary(request: object, context: object) -> str:
            raise RuntimeError("boom")

        handler = _make_handler(unary_unary=failing_unary)
        continuation = MagicMock(return_value=handler)

        result = interceptor.intercept_service(continuation, details)

        with pytest.raises(RuntimeError, match="boom"):
            result.unary_unary("req", "ctx")

        ctx = get_contextvars()
        assert "grpc_method" not in ctx

    def test_stale_context_cleared_on_new_request(self) -> None:
        bind_contextvars(stale_key="leak")

        interceptor = StructguruInterceptor()
        details = _make_handler_details(
            metadata=[("x-request-id", "new-id")],
        )

        def fake_unary(request: object, context: object) -> str:
            ctx = get_contextvars()
            assert "stale_key" not in ctx
            assert ctx["request_id"] == "new-id"
            return "ok"

        handler = _make_handler(unary_unary=fake_unary)
        continuation = MagicMock(return_value=handler)

        result = interceptor.intercept_service(continuation, details)
        result.unary_unary("req", "ctx")

    def test_custom_request_id_key(self) -> None:
        clear_contextvars()

        interceptor = StructguruInterceptor(request_id_key="correlation-id")
        details = _make_handler_details(
            metadata=[("correlation-id", "corr-abc")],
        )

        def fake_unary(request: object, context: object) -> str:
            ctx = get_contextvars()
            assert ctx["request_id"] == "corr-abc"
            return "ok"

        handler = _make_handler(unary_unary=fake_unary)
        continuation = MagicMock(return_value=handler)

        result = interceptor.intercept_service(continuation, details)
        result.unary_unary("req", "ctx")

    def test_streaming_handler_has_context_during_iteration(self) -> None:
        clear_contextvars()

        interceptor = StructguruInterceptor()
        details = _make_handler_details(
            metadata=[("x-request-id", "stream-req")],
        )

        captured_contexts: list[dict] = []

        def fake_unary_stream(request: object, context: object):  # type: ignore[no-untyped-def]
            for i in range(3):
                captured_contexts.append(dict(get_contextvars()))
                yield f"item-{i}"

        handler = _make_handler(unary_stream=fake_unary_stream)
        continuation = MagicMock(return_value=handler)

        result = interceptor.intercept_service(continuation, details)
        items = list(result.unary_stream("req", "ctx"))

        assert items == ["item-0", "item-1", "item-2"]
        # Context should have been bound during each yield.
        for ctx in captured_contexts:
            assert ctx["grpc_method"] == "/svc/Method"
            assert ctx["request_id"] == "stream-req"

        # After iteration completes, context should be cleared.
        assert "grpc_method" not in get_contextvars()

    def test_streaming_partial_consumption_cleans_up_on_close(self) -> None:
        clear_contextvars()

        interceptor = StructguruInterceptor()
        details = _make_handler_details(
            metadata=[("x-request-id", "partial-req")],
        )

        def fake_unary_stream(request: object, context: object):  # type: ignore[no-untyped-def]
            for i in range(100):
                yield f"item-{i}"

        handler = _make_handler(unary_stream=fake_unary_stream)
        continuation = MagicMock(return_value=handler)

        result = interceptor.intercept_service(continuation, details)
        it = iter(result.unary_stream("req", "ctx"))
        # Consume only one item.
        assert next(it) == "item-0"
        # Context is still bound mid-iteration.
        assert get_contextvars().get("request_id") == "partial-req"
        # Explicitly close (as gRPC framework does on cancellation).
        it.close()  # type: ignore[union-attr]
        # Context should be cleared.
        assert "grpc_method" not in get_contextvars()

    def test_context_clean_after_intercept_before_handler(self) -> None:
        clear_contextvars()

        interceptor = StructguruInterceptor()
        details = _make_handler_details(
            metadata=[("x-request-id", "pre-exec-req")],
        )

        def fake_unary(request: object, context: object) -> str:
            ctx = get_contextvars()
            assert ctx["grpc_method"] == "/svc/Method"
            assert ctx["request_id"] == "pre-exec-req"
            return "ok"

        handler = _make_handler(unary_unary=fake_unary)
        continuation = MagicMock(return_value=handler)

        result = interceptor.intercept_service(continuation, details)

        # After intercept_service returns but BEFORE calling the handler,
        # context must not contain gRPC vars (prevents leakage into
        # unrelated logs on the same thread).
        ctx = get_contextvars()
        assert "grpc_method" not in ctx
        assert "request_id" not in ctx

        # Handler should still work correctly when invoked.
        assert result.unary_unary("req", "ctx") == "ok"

    def test_none_handler_clears_context(self) -> None:
        clear_contextvars()

        interceptor = StructguruInterceptor()
        details = _make_handler_details(
            metadata=[("x-request-id", "none-req")],
        )
        continuation = MagicMock(return_value=None)

        result = interceptor.intercept_service(continuation, details)
        assert result is None
        # Context should be cleared when handler is None.
        assert "grpc_method" not in get_contextvars()

    def test_continuation_exception_clears_context(self) -> None:
        clear_contextvars()

        interceptor = StructguruInterceptor()
        details = _make_handler_details()
        continuation = MagicMock(side_effect=RuntimeError("no handler"))

        with pytest.raises(RuntimeError, match="no handler"):
            interceptor.intercept_service(continuation, details)

        # Context should be cleared after continuation failure.
        assert "grpc_method" not in get_contextvars()
