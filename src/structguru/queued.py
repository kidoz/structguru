"""Non-blocking (queued) logging.

Wraps :class:`logging.handlers.QueueHandler` and
:class:`logging.handlers.QueueListener` to offload log I/O to a background
thread — similar to loguru's ``enqueue=True``.
"""

from __future__ import annotations

import atexit
import copy
import logging
from logging.handlers import QueueHandler, QueueListener
from queue import Queue
from typing import Any


class _PassthroughQueueHandler(QueueHandler):
    """A QueueHandler that does NOT pre-format records before enqueueing.

    The default ``QueueHandler.prepare()`` calls ``self.format(record)``
    which renders the record to a string and clears ``record.args``.  When
    the downstream handler uses ``structlog.stdlib.ProcessorFormatter`` it
    expects ``record.msg`` to still be the raw event dict produced by
    ``wrap_for_formatter``.  Skipping the pre-formatting step lets the
    background thread's handler render the record correctly.

    A shallow copy of the record is returned so that other handlers on the
    root logger (e.g. ``_StructlogMsgFixer``) cannot mutate the queued copy
    in place before the background thread processes it.

    .. note::
        Records are stored as objects in the queue.  ``maxsize=0`` (the
        default for :func:`configure_queued_logging`) means unbounded — under
        extreme bursts, memory usage can grow (~30MB per 100k queued
        records).  Pass a positive ``maxsize`` to apply backpressure.
    """

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        # Copy the record so sibling handlers cannot mutate our queued copy.
        # Preserve the structlog event-dict in record.msg for ProcessorFormatter.
        return copy.copy(record)


def _is_output_handler(handler: logging.Handler) -> bool:
    """True for handlers that should be moved behind the queue."""
    if isinstance(handler, QueueHandler):
        return False
    # Skip internal helpers without a formatter (e.g. _StructlogMsgFixer).
    return handler.formatter is not None


def configure_queued_logging(
    *,
    handler: logging.Handler | None = None,
    handlers: list[logging.Handler] | None = None,
    maxsize: int = 0,
) -> QueueListener:
    """Replace real output handlers on the root logger with a queue pair.

    The target handlers are moved behind a single :class:`QueueListener` so
    that formatting and I/O happen on a background thread.  The listener is
    automatically stopped via :func:`atexit.register`.

    Parameters
    ----------
    handler:
        A single handler to queue.  Mutually exclusive with *handlers*.
    handlers:
        An explicit list of handlers to queue.  If both *handler* and
        *handlers* are ``None`` (default), every handler on the root logger
        that has a formatter is queued.
    maxsize:
        Upper bound on the internal :class:`queue.Queue`.  ``0`` (default)
        means unbounded; a positive integer applies backpressure — producers
        block when the queue fills.

    Returns
    -------
    QueueListener
        The running listener (useful for manual ``stop()`` in tests).
    """
    if handler is not None and handlers is not None:
        msg = "Pass either handler= or handlers=, not both."
        raise ValueError(msg)

    from structguru._native import is_native_enabled

    if is_native_enabled():
        import warnings

        warnings.warn(
            "Native mode already offloads log I/O to a background thread; "
            "configure_queued_logging() is redundant while it is enabled.",
            stacklevel=2,
        )

    root = logging.getLogger()

    # Already configured — a _PassthroughQueueHandler is present.
    if any(isinstance(h, _PassthroughQueueHandler) for h in root.handlers):
        msg = "Queued logging is already configured. Call configure_structlog() to reset first."
        raise RuntimeError(msg)

    if handler is not None:
        targets: list[logging.Handler] = [handler]
    elif handlers is not None:
        targets = list(handlers)
    else:
        targets = [h for h in root.handlers if _is_output_handler(h)]

    if not targets:
        msg = "No suitable handler found on root logger. Call configure_structlog() first."
        raise RuntimeError(msg)

    queue: Queue[Any] = Queue(maxsize)
    queue_handler = _PassthroughQueueHandler(queue)
    # Level on the queue handler must be permissive enough to admit every
    # target's own level; the listener re-evaluates per-handler level below.
    queue_handler.setLevel(min((h.level for h in targets), default=logging.NOTSET))

    # Replace the first target in-place so the queue handler occupies the
    # same slot, then remove remaining targets from the root logger.  This
    # matters when sibling handlers (e.g. _StructlogMsgFixer) run after the
    # original handlers and mutate the LogRecord in place.
    first = targets[0]
    if first in root.handlers:
        idx = root.handlers.index(first)
        root.removeHandler(first)
        root.handlers.insert(idx, queue_handler)
    else:
        root.addHandler(queue_handler)

    for extra in targets[1:]:
        if extra in root.handlers:
            root.removeHandler(extra)

    listener = QueueListener(queue, *targets, respect_handler_level=True)
    listener.start()
    atexit.register(listener.stop)

    return listener
