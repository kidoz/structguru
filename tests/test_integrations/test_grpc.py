"""Tests for structguru.integrations.grpc."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from structguru._contextvars import bind_contextvars, clear_contextvars, get_contextvars
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

    def test_streaming_preserves_handler_bound_context_across_yields(self) -> None:
        """Handler-bound context (e.g. user_id set during auth) must survive
        every yield in a streaming response.
        """
        clear_contextvars()

        interceptor = StructguruInterceptor()
        details = _make_handler_details(
            metadata=[("x-request-id", "stream-ctx")],
        )

        captured: list[dict] = []

        def fake_unary_stream(request: object, context: object):  # type: ignore[no-untyped-def]
            # Simulate auth code adding a user_id after the interceptor ran.
            bind_contextvars(user_id="u-42")
            for i in range(3):
                captured.append(dict(get_contextvars()))
                yield f"item-{i}"

        handler = _make_handler(unary_stream=fake_unary_stream)
        continuation = MagicMock(return_value=handler)

        result = interceptor.intercept_service(continuation, details)
        list(result.unary_stream("req", "ctx"))

        assert len(captured) == 3
        for ctx in captured:
            assert ctx["request_id"] == "stream-ctx"
            assert ctx["user_id"] == "u-42"

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

    def test_streaming_context_clean_before_iteration_starts(self) -> None:
        """A response iterator that is never consumed must not leave context bound.

        The handler returns a lazy iterator; gRPC may delay consuming it, or
        never consume it at all if the client cancels.  Until iteration starts,
        the thread must be free of gRPC context vars.
        """
        clear_contextvars()

        interceptor = StructguruInterceptor()
        details = _make_handler_details(
            metadata=[("x-request-id", "lazy-req")],
        )

        def fake_unary_stream(request: object, context: object):  # type: ignore[no-untyped-def]
            yield "item-0"

        handler = _make_handler(unary_stream=fake_unary_stream)
        continuation = MagicMock(return_value=handler)

        result = interceptor.intercept_service(continuation, details)
        stream = result.unary_stream("req", "ctx")

        # Iterator created but not consumed: nothing may be bound yet.
        ctx = get_contextvars()
        assert "grpc_method" not in ctx
        assert "request_id" not in ctx

        # Iterating still binds the context for the handler.
        it = iter(stream)
        assert next(it) == "item-0"
        assert get_contextvars()["request_id"] == "lazy-req"

        # Close explicitly so cleanup runs here rather than at an arbitrary
        # GC point in a later test.
        it.close()  # type: ignore[union-attr]
        assert "grpc_method" not in get_contextvars()

    def test_streaming_restores_context_bound_by_non_generator_handler(self) -> None:
        """A handler that returns a list (not a generator) runs eagerly.

        Context it bound during that eager call must be restored for iteration
        rather than dropped by the pre-return cleanup.
        """
        clear_contextvars()

        interceptor = StructguruInterceptor()
        details = _make_handler_details(
            metadata=[("x-request-id", "eager-req")],
        )

        def eager_stream(request: object, context: object) -> list[str]:
            bind_contextvars(user_id="u-7")
            return ["a", "b"]

        handler = _make_handler(unary_stream=eager_stream)
        continuation = MagicMock(return_value=handler)

        result = interceptor.intercept_service(continuation, details)
        stream = result.unary_stream("req", "ctx")

        assert "user_id" not in get_contextvars()

        it = iter(stream)
        assert next(it) == "a"
        ctx = get_contextvars()
        assert ctx["request_id"] == "eager-req"
        assert ctx["user_id"] == "u-7"

        it.close()  # type: ignore[union-attr]
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


@pytest.mark.parametrize("error_type", [KeyboardInterrupt, SystemExit])
def test_continuation_base_exception_clears_context(error_type: type[BaseException]) -> None:
    interceptor = StructguruInterceptor()
    continuation = MagicMock(side_effect=error_type())

    with pytest.raises(error_type):
        interceptor.intercept_service(continuation, _make_handler_details())

    assert get_contextvars() == {}


@pytest.mark.parametrize("error_type", [KeyboardInterrupt, SystemExit])
def test_handler_base_exception_clears_context(error_type: type[BaseException]) -> None:
    def interrupted(request: object, context: object) -> None:
        raise error_type()

    interceptor = StructguruInterceptor()
    handler = _make_handler(unary_unary=interrupted)
    wrapped = interceptor.intercept_service(
        MagicMock(return_value=handler), _make_handler_details()
    )

    with pytest.raises(error_type):
        wrapped.unary_unary("req", "ctx")

    assert get_contextvars() == {}


@pytest.mark.parametrize(
    "key, metadata, expected",
    [
        ("X-Request-ID", [("x-request-id", "rid-upper")], "rid-upper"),
        ("x-request-id-bin", [("x-request-id-bin", b"rid-bin")], "rid-bin"),
        ("X-Request-ID-Bin", [("x-request-id-bin", b"rid-bin-upper")], "rid-bin-upper"),
        ("x-request-id-bin", [("x-request-id-bin", b"\xff\xfe")], None),
        ("x-request-id-bin", [("x-request-id-bin", b"bad\r\nvalue")], None),
    ],
)
def test_request_id_key_is_case_insensitive_and_binary_safe(
    key: str, metadata: list[tuple[str, object]], expected: str | None
) -> None:
    clear_contextvars()
    seen: list[str] = []

    def unary(request: object, context: object) -> str:
        seen.append(get_contextvars()["request_id"])
        return "ok"

    interceptor = StructguruInterceptor(request_id_key=key)
    details = _make_handler_details(metadata=metadata)  # type: ignore[arg-type]
    result = interceptor.intercept_service(MagicMock(return_value=_make_handler(unary)), details)
    assert result.unary_unary("req", "ctx") == "ok"
    if expected is None:
        assert len(seen[0]) == 36  # a generated UUID, never a failed RPC
    else:
        assert seen == [expected]


def test_real_server_accepts_mixed_case_and_binary_request_id_keys() -> None:
    grpc = pytest.importorskip("grpc")
    from concurrent import futures

    seen: list[str] = []

    def unary(request: bytes, context: object) -> bytes:
        seen.append(get_contextvars()["request_id"])
        return b"ok"

    handler = grpc.method_handlers_generic_handler(
        "svc", {"U": grpc.unary_unary_rpc_method_handler(unary)}
    )
    for key, value in (("X-Request-ID", "rid-1"), ("x-request-id-bin", b"rid-2")):
        server = grpc.server(
            futures.ThreadPoolExecutor(max_workers=1),
            interceptors=[StructguruInterceptor(request_id_key=key)],
        )
        server.add_generic_rpc_handlers((handler,))
        port = server.add_insecure_port("127.0.0.1:0")
        server.start()
        try:
            with grpc.insecure_channel(f"127.0.0.1:{port}") as channel:
                call = channel.unary_unary("/svc/U")
                assert call(b"", metadata=[(key.lower(), value)], timeout=5) == b"ok"
        finally:
            server.stop(None)
    assert seen == ["rid-1", "rid-2"]
