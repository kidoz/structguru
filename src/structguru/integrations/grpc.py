"""gRPC server interceptor for structured logging.

Binds ``grpc_method`` and ``request_id`` (from metadata) to structlog's
context variables for each incoming RPC.

Usage::

    from structguru.integrations.grpc import StructguruInterceptor

    server = grpc.server(
        ...,
        interceptors=[StructguruInterceptor()],
    )
"""

from __future__ import annotations

import functools
import uuid
from collections.abc import Iterator
from typing import Any

import structlog
from structlog.contextvars import bind_contextvars, clear_contextvars


class StructguruInterceptor:
    """gRPC server interceptor that binds request context for structured logging.

    Parameters
    ----------
    request_id_key:
        Metadata key to extract a request/correlation ID from.
    logger_name:
        Name for the structlog logger.
    """

    def __init__(
        self,
        *,
        request_id_key: str = "x-request-id",
        logger_name: str = "structguru.grpc",
    ) -> None:
        self.request_id_key = request_id_key
        self.log = structlog.get_logger(logger_name)

    def intercept_service(
        self,
        continuation: Any,
        handler_call_details: Any,
    ) -> Any:
        """Intercept an incoming RPC and bind context."""
        clear_contextvars()

        method: str = handler_call_details.method or ""
        metadata: dict[str, str] = {}
        for key, value in handler_call_details.invocation_metadata or []:
            metadata[key] = value
        raw_id = metadata.get(self.request_id_key, "")
        if raw_id and len(raw_id) <= 128 and raw_id.isprintable():
            request_id = raw_id
        else:
            request_id = str(uuid.uuid4())

        bind_contextvars(grpc_method=method, request_id=request_id)

        try:
            handler = continuation(handler_call_details)
        except Exception:
            clear_contextvars()
            raise

        if handler is None:
            clear_contextvars()
            return None

        wrapped = _wrap_rpc_handler(handler, method, request_id)
        # Clear context now; the wrapped handler re-binds it when invoked.
        # This prevents grpc_method/request_id from leaking into unrelated
        # logs between intercept_service() returning and the handler executing.
        clear_contextvars()
        return wrapped


def _wrap_rpc_handler(handler: Any, method: str, request_id: str) -> Any:
    """Wrap a gRPC handler so context is bound during execution and cleared after."""
    if handler.unary_unary:
        handler = _replace_behavior(
            handler, "unary_unary", method, request_id, streaming_response=False
        )
    if handler.unary_stream:
        handler = _replace_behavior(
            handler, "unary_stream", method, request_id, streaming_response=True
        )
    if handler.stream_unary:
        handler = _replace_behavior(
            handler, "stream_unary", method, request_id, streaming_response=False
        )
    if handler.stream_stream:
        handler = _replace_behavior(
            handler, "stream_stream", method, request_id, streaming_response=True
        )
    return handler


def _wrap_iterator(it: Iterator[Any], method: str, request_id: str) -> Iterator[Any]:
    """Wrap a response iterator so context stays bound during iteration."""
    try:
        for item in it:
            clear_contextvars()
            bind_contextvars(grpc_method=method, request_id=request_id)
            yield item
    finally:
        clear_contextvars()


def _replace_behavior(
    handler: Any, attr: str, method: str, request_id: str, *, streaming_response: bool
) -> Any:
    original_fn = getattr(handler, attr)

    @functools.wraps(original_fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        clear_contextvars()
        bind_contextvars(grpc_method=method, request_id=request_id)
        try:
            result = original_fn(*args, **kwargs)
        except Exception:
            clear_contextvars()
            raise
        if streaming_response:
            # Don't clear now — context must live through iteration.
            return _wrap_iterator(result, method, request_id)
        clear_contextvars()
        return result

    # gRPC handlers are namedtuple-like; replace the behavior via _replace if
    # available, otherwise set the attribute directly.
    if hasattr(handler, "_replace"):
        return handler._replace(**{attr: wrapper})
    object.__setattr__(handler, attr, wrapper)
    return handler
