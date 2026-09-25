"""Flask integration for structguru.

Provides automatic request context binding via Flask's ``before_request``,
``after_request``, and ``teardown_request`` hooks.

Usage::

    from structguru.integrations.flask import setup_flask_logging

    app = Flask(__name__)
    setup_flask_logging(app)
"""

from __future__ import annotations

import time
from typing import Any

from structguru._contextvars import bind_contextvars, clear_contextvars, get_contextvars
from structguru.core import Logger
from structguru.integrations._util import ContextBoundIterator, StreamState, coerce_request_id


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
        Name for the structguru logger used by the hooks.

    Notes
    -----
    The binding hook runs before every other application ``before_request``
    hook, including ones registered earlier, so a hook that ends the request
    early (an authentication 401) still gets a request ID, a response header,
    and a summary line, and context those hooks bind is kept. The context is
    cleared after every other application ``teardown_request`` hook has run.

    A streamed response body (a generator, with or without
    :func:`flask.stream_with_context`) is produced with the request context
    bound, and its summary is logged when the body is exhausted, fails, or is
    closed, so ``duration_ms`` covers the streaming time. Bodies passed through
    to the server untouched (``send_file``) are summarized when the response is
    returned. When an exception propagates past Flask (``PROPAGATE_EXCEPTIONS``,
    testing, or debug mode) the summary is logged from the teardown hook as
    ``Request failed``.

    Every request logs exactly one summary. A non-streamed summary waits until
    Flask has finalized the response (``request_finished``), so when a later
    ``after_request`` hook fails and Flask answers with a 500 instead, the
    summary reports the 500. Without signal support (Flask before 2.3 without
    blinker) it is logged when the response is returned.
    """
    log = Logger(name=logger_name)

    def _bind_request_context() -> None:
        from flask import g, request

        clear_contextvars()

        request_id = coerce_request_id(request.headers.get(request_id_header, ""))
        g.structguru_summary = _Summary(time.perf_counter())
        g.structguru_request_id = request_id

        bind_contextvars(
            request_id=request_id,
            method=request.method,
            path=request.path,
            client_ip=request.remote_addr or "",
        )

    # Run before any before_request hook registered so far (app-level hooks run
    # before blueprint hooks). Registering through the decorator first keeps
    # Flask's check against late setup.
    app.before_request(_bind_request_context)
    hooks = app.before_request_funcs[None]
    hooks.remove(_bind_request_context)
    hooks.insert(0, _bind_request_context)

    def _finalize(summary: _Summary, status_code: int) -> None:
        """Log the summary of a finalized, non-streamed response once."""
        if summary.finished:
            return
        summary.finished = True
        if summary.stream is None and not summary.logged:
            summary.logged = True
            if log_request:
                _log_summary(log, summary.start_time, status_code, None)

    def _on_request_finished(sender: Any, response: Any, **extra: Any) -> None:
        from flask import g

        summary: _Summary | None = getattr(g, "structguru_summary", None)
        if summary is not None:
            _finalize(summary, response.status_code)

    try:
        from flask.signals import request_finished

        request_finished.connect(_on_request_finished, app)
    except RuntimeError:  # Flask < 2.3 without blinker: no signal support
        finalize_on_return = True
    else:
        finalize_on_return = False
        # The signal holds its receiver weakly; tie the receiver to the app.
        app.extensions["structguru.flask"] = _on_request_finished

    @app.after_request  # type: ignore[untyped-decorator]
    def _log_response(response: Any) -> Any:
        from flask import g
        from werkzeug.wsgi import ClosingIterator

        request_id = getattr(g, "structguru_request_id", None)
        if request_id:
            response.headers[request_id_header] = request_id
        summary: _Summary | None = getattr(g, "structguru_summary", None)
        if summary is None or summary.finished:
            return response
        # When a later after_request hook fails, Flask processes a 500 response
        # instead and runs this hook again: the new response supersedes this
        # one, including a streamed body wrapped here that is never sent.
        summary.stream = None

        # Wrap bodies produced lazily by application code. Bodies handed to the
        # server untouched (send_file) run no application code, and Werkzeug's
        # own ClosingIterator bodies (HTTPException responses such as 404/500,
        # WSGI apps returned by views) are already rendered; both are
        # summarized when the response is finalized, as are non-streamed ones.
        if (
            response.is_streamed
            and not response.direct_passthrough
            and not isinstance(response.response, ClosingIterator)
        ):
            current = summary

            def _finish(error: BaseException | None) -> None:
                if current.stream is not state or current.logged:
                    return
                current.logged = True
                if log_request:
                    _log_summary(log, current.start_time, response.status_code, error)

            state = StreamState(get_contextvars(), _finish)
            summary.stream = state
            response.response = ContextBoundIterator(response.response, state)
        elif finalize_on_return:
            _finalize(summary, response.status_code)
        return response

    def _clear_context(exc: BaseException | None = None) -> None:
        from flask import g

        try:
            summary: _Summary | None = getattr(g, "structguru_summary", None)
            if summary is not None and not summary.finished and not summary.logged:
                # No response was finalized: the exception propagated past
                # Flask (PROPAGATE_EXCEPTIONS, testing, debug) and the server
                # answers, or finalizing the error response failed too.
                summary.finished = summary.logged = True
                if log_request:
                    _log_summary(log, summary.start_time, 500 if exc is not None else None, exc)
        finally:
            clear_contextvars()

    # Teardown hooks run in reverse registration order: at the front of the
    # list, the clearing hook runs after every other application teardown hook,
    # so those still log with the request context.
    app.teardown_request(_clear_context)
    teardowns = app.teardown_request_funcs[None]
    teardowns.remove(_clear_context)
    teardowns.insert(0, _clear_context)


class _Summary:
    """Per-request summary state: exactly one summary line per request.

    Parameters
    ----------
    start_time:
        ``time.perf_counter()`` value taken when the request started.
    """

    __slots__ = ("finished", "logged", "start_time", "stream")

    def __init__(self, start_time: float) -> None:
        self.start_time = start_time
        #: The current response's streamed body, which logs its own summary.
        self.stream: StreamState | None = None
        #: Flask finalized a response (``request_finished`` or its fallback).
        self.finished = False
        #: A summary line was logged (or suppressed by ``log_request=False``).
        self.logged = False


def _log_summary(
    log: Logger,
    start_time: float,
    status_code: int | None,
    error: BaseException | None,
) -> None:
    fields: dict[str, Any] = {"duration_ms": round((time.perf_counter() - start_time) * 1000, 2)}
    if status_code is not None:
        fields["status_code"] = status_code
    if error is not None:
        log.error("Request failed", exc_info=error, **fields)
    else:
        log.info("Request completed", **fields)
