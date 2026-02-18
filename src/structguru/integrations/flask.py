"""Flask integration for structguru.

Provides automatic request context binding via Flask's ``before_request``
and ``after_request`` hooks.

Usage::

    from structguru.integrations.flask import setup_flask_logging

    app = Flask(__name__)
    setup_flask_logging(app)
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import structlog
from structlog.contextvars import bind_contextvars, clear_contextvars


def setup_flask_logging(
    app: Any,
    *,
    request_id_header: str = "X-Request-ID",
    log_request: bool = True,
    logger_name: str = "structguru.flask",
) -> None:
    """Register Flask hooks for structured request logging.

    Parameters
    ----------
    app:
        A :class:`flask.Flask` application.
    request_id_header:
        Header name to read an existing request ID from.
    log_request:
        If ``True``, log a summary line when each request completes.
    logger_name:
        Name for the structlog logger used by the hooks.
    """
    log = structlog.get_logger(logger_name)

    @app.before_request  # type: ignore[untyped-decorator]
    def _bind_request_context() -> None:
        from flask import g, request

        clear_contextvars()

        raw_id = request.headers.get(request_id_header, "")
        if raw_id and len(raw_id) <= 128 and raw_id.isprintable():
            request_id = raw_id
        else:
            request_id = str(uuid.uuid4())
        g.structguru_start_time = time.perf_counter()
        g.structguru_request_id = request_id

        bind_contextvars(
            request_id=request_id,
            method=request.method,
            path=request.path,
            client_ip=request.remote_addr or "",
        )

    @app.after_request  # type: ignore[untyped-decorator]
    def _log_response(response: Any) -> Any:
        from flask import g

        start_time = getattr(g, "structguru_start_time", None)
        if log_request and start_time is not None:
            duration_ms = (time.perf_counter() - start_time) * 1000
            log.info(
                "Request completed",
                status_code=response.status_code,
                duration_ms=round(duration_ms, 2),
            )
        request_id = getattr(g, "structguru_request_id", None)
        if request_id:
            response.headers[request_id_header] = request_id
        return response

    @app.teardown_request  # type: ignore[untyped-decorator]
    def _clear_context(exc: BaseException | None = None) -> None:
        clear_contextvars()
