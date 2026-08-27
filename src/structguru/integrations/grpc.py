"""gRPC server interceptor for structured logging.

Binds ``grpc_method`` and ``request_id`` (from metadata) to structguru
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
from collections.abc import Iterator
from typing import Any

from structguru._contextvars import bind_contextvars, clear_contextvars, get_contextvars
from structguru.core import Logger
from structguru.integrations._util import coerce_request_id


class StructguruInterceptor:
    """gRPC server interceptor that binds request context for structured logging.

    Parameters
    ----------
    request_id_key:
        Metadata key to extract a request/correlation ID from.
    logger_name:
        Name for the structguru logger.
    """

    def __init__(
        self,
        *,
        request_id_key: str = "x-request-id",
        logger_name: str = "structguru.grpc",
    ) -> None:
        self.request_id_key = request_id_key
        self.log = Logger(name=logger_name)

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
        request_id = coerce_request_id(metadata.get(self.request_id_key, ""))

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


def _wrap_iterator(it: Iterator[Any], snapshot: dict[str, Any]) -> Iterator[Any]:
    """Wrap a response iterator so context stays bound during iteration.

    *snapshot* is the context captured when the handler returned the iterator,
    so any enrichment the handler already performed (e.g. ``user_id`` bound
    during auth) is restored here.  It is applied once at entry and cleared
    once at exit.  We deliberately do NOT rebind on every yielded item, because
    the handler may enrich the context further while streaming and we must not
    wipe those additions between yields.
    """
    clear_contextvars()
    bind_contextvars(**snapshot)
    try:
        yield from it
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
            # The response iterator is lazy: gRPC may not begin consuming it
            # for a while, or ever (the client cancels first).  Snapshot the
            # context the handler produced, wipe it here so it cannot leak into
            # unrelated logs on this thread in the meantime, and restore it
            # when iteration actually starts.
            snapshot = get_contextvars()
            clear_contextvars()
            return _wrap_iterator(result, snapshot)
        clear_contextvars()
        return result

    # gRPC handlers are namedtuple-like; replace the behavior via _replace if
    # available, otherwise set the attribute directly.
    if hasattr(handler, "_replace"):
        return handler._replace(**{attr: wrapper})
    object.__setattr__(handler, attr, wrapper)
    return handler
