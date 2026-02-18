"""Non-blocking (queued) logging.

Wraps :class:`logging.handlers.QueueHandler` and
:class:`logging.handlers.QueueListener` to offload log I/O to a background
thread — similar to loguru's ``enqueue=True``.
"""

from __future__ import annotations

import atexit
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
    """

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        # Copy the record so sibling handlers cannot mutate our queued copy.
        # Preserve the structlog event-dict in record.msg for ProcessorFormatter.
        import copy

        return copy.copy(record)


def configure_queued_logging(
    *,
    handler: logging.Handler | None = None,
) -> QueueListener:
    """Replace *handler* on the root logger with a non-blocking queue pair.

    The original *handler* is moved behind a :class:`QueueListener` so that
    formatting and I/O happen on a background thread.  If *handler* is
    ``None``, the first handler on the root logger is used.

    The listener is automatically stopped via :func:`atexit.register`.

    Returns
    -------
    QueueListener
        The running listener (useful for manual ``stop()`` in tests).
    """
    root = logging.getLogger()

    # Already configured — a _PassthroughQueueHandler is present.
    if any(isinstance(h, _PassthroughQueueHandler) for h in root.handlers):
        msg = "Queued logging is already configured. Call configure_structlog() to reset first."
        raise RuntimeError(msg)

    if handler is None:
        for h in root.handlers:
            # Skip QueueHandlers and internal helpers without a formatter
            # (e.g. _StructlogMsgFixer) — they are not real output handlers.
            if not isinstance(h, QueueHandler) and h.formatter is not None:
                handler = h
                break
        if handler is None:
            msg = "No suitable handler found on root logger. Call configure_structlog() first."
            raise RuntimeError(msg)

    queue: Queue[Any] = Queue(-1)
    queue_handler = _PassthroughQueueHandler(queue)
    queue_handler.setLevel(handler.level)

    # Replace the target handler in-place so the queue handler occupies the
    # same position in the handler list.  This matters when sibling handlers
    # (e.g. _StructlogMsgFixer) run after the original handler and mutate the
    # LogRecord in place — by inserting at the same index we ensure the queue
    # handler runs before those siblings and enqueues a clean copy.
    if handler in root.handlers:
        idx = root.handlers.index(handler)
        root.removeHandler(handler)
        root.handlers.insert(idx, queue_handler)
    else:
        root.addHandler(queue_handler)

    listener = QueueListener(queue, handler, respect_handler_level=True)
    listener.start()
    atexit.register(listener.stop)

    return listener
