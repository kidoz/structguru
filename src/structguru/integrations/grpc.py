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
        request_id = metadata.get(self.request_id_key, "")

        bind_contextvars(grpc_method=method, request_id=request_id)

        return continuation(handler_call_details)
