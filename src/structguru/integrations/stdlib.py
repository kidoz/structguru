"""Route standard-library logging into structguru's native pipeline.

Third-party libraries log through the stdlib :mod:`logging` module. Installing
the bridge attaches a handler to the root logger that re-emits every such record
through structguru's native renderer, so foreign logs share the same JSON /
console formatting, redaction, level filtering, and output stream as
``structguru.logger``::

    from structguru.integrations.stdlib import install_stdlib_bridge

    install_stdlib_bridge(level="INFO", suppress_loggers=("urllib3", "botocore"))

    import logging
    logging.getLogger("sqlalchemy.engine").info("SELECT 1")
    # -> {"logger":"sqlalchemy.engine","level":"INFO",...,"message":"SELECT 1"}

For a logger that sets ``propagate=False`` (so its records never reach the root
logger), attach :class:`StructguruHandler` to it directly.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Iterable
from typing import Any

from structguru.config import _to_logging_level
from structguru.core import Logger, _set_stdlib_bridge_active

# Attributes present on a vanilla LogRecord (plus the two the Formatter adds).
# Anything else in ``record.__dict__`` came from a user ``extra=`` mapping and is
# forwarded as a structured field.
_STANDARD_RECORD_ATTRS = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "taskName",
        "message",
        "asctime",
    }
)


def _method_for_level(levelno: int) -> str:
    """Map a stdlib numeric level to the structguru method that emits it."""
    if levelno >= logging.CRITICAL:
        return "critical"
    if levelno >= logging.ERROR:
        return "error"
    if levelno >= logging.WARNING:
        return "warning"
    if levelno >= logging.INFO:
        return "info"
    return "debug"


@functools.lru_cache(maxsize=1024)
def _logger_for(name: str) -> Logger:
    """Return a cached structguru :class:`Logger` bound to *name*.

    The base logger is immutable in practice — ``bind()``/``opt()`` return fresh
    copies — so a shared per-name instance is safe to reuse across threads.
    """
    return Logger(name=name)


def _record_extras(record: logging.LogRecord) -> dict[str, Any]:
    """Extract user-supplied ``extra=`` fields from a record."""
    return {
        key: value for key, value in record.__dict__.items() if key not in _STANDARD_RECORD_ATTRS
    }


class StructguruHandler(logging.Handler):
    """A :class:`logging.Handler` that re-emits stdlib records via structguru.

    The record's logger name becomes the ``logger`` field, its level selects the
    structguru method, ``extra=`` fields are forwarded as structured fields, and
    ``exc_info`` and ``stack_info`` are carried through. The already-formatted
    message is passed verbatim (no brace re-formatting), so literal ``{...}`` in
    a message is never misinterpreted.
    """

    def emit(self, record: logging.LogRecord) -> None:
        # Never intercept structguru's own records: it keeps the bridge from
        # looping and stops native output being wrapped a second time.
        if record.name == "structguru" or record.name.startswith("structguru."):
            return
        try:
            target = _logger_for(record.name)
            extras = _record_extras(record)
            if extras:
                target = target.bind(**extras)
            if record.exc_info:
                target = target.opt(exception=record.exc_info)
            emit_method = getattr(target, _method_for_level(record.levelno))
            if record.stack_info:
                emit_method(record.getMessage(), stack_info=record.stack_info)
            else:
                emit_method(record.getMessage())
        except Exception:  # noqa: BLE001 - a logging handler must never raise
            self.handleError(record)


def _apply_suppression(names: Iterable[str], level: str) -> None:
    threshold = _to_logging_level(level)
    for name in names:
        logging.getLogger(name).setLevel(threshold)


def suppress_loggers(*names: str, level: str = "WARNING") -> None:
    """Raise the level threshold of the named loggers.

    Use this to quiet noisy third-party loggers (``"urllib3"``,
    ``"botocore"``, ``"sqlalchemy.engine"``, …) regardless of whether they reach
    structguru through the bridge or their own handlers.
    """
    _apply_suppression(names, level)


def install_stdlib_bridge(
    *,
    level: str = "INFO",
    suppress_loggers: Iterable[str] = (),
    suppress_level: str = "WARNING",
    clear_handlers: bool = True,
) -> StructguruHandler:
    """Route root stdlib logging through structguru and quiet noisy loggers.

    Installs a :class:`StructguruHandler` on the root logger so any library
    logging through :mod:`logging` renders through structguru's native path.
    Pass the returned handler to :func:`uninstall_stdlib_bridge` to undo it.

    Sinks registered with ``logger.add()`` already receive third-party records
    directly from the root logger, unrendered. The bridge delivers those same
    records to those same sinks through the native renderer, so installing it
    suspends the raw root-logger delivery; uninstalling restores it. Without
    that, every third-party record would reach each sink twice — once raw, once
    rendered.

    Parameters
    ----------
    level:
        Level set on the root logger — the floor for which stdlib records
        propagate to the bridge. structguru's own native level (set via
        :func:`structguru.configure`) still applies on top.
    suppress_loggers:
        Logger names to raise to ``suppress_level`` (see :func:`suppress_loggers`).
    suppress_level:
        Level applied to ``suppress_loggers`` (default ``"WARNING"``).
    clear_handlers:
        Remove existing root handlers first (default ``True``), so records are
        not also emitted by a previously configured stdlib handler.
    """
    root = logging.getLogger()
    if clear_handlers:
        for handler in list(root.handlers):
            root.removeHandler(handler)
    # Suspend raw root delivery for `logger.add()` sinks before the bridge goes
    # live, so no record is ever delivered through both paths.
    _set_stdlib_bridge_active(True)
    threshold = _to_logging_level(level)
    bridge = StructguruHandler()
    bridge.setLevel(threshold)
    root.addHandler(bridge)
    root.setLevel(threshold)
    _apply_suppression(suppress_loggers, suppress_level)
    return bridge


def uninstall_stdlib_bridge(bridge: StructguruHandler) -> None:
    """Detach *bridge* from the root logger and restore direct sink delivery.

    Reverses :func:`install_stdlib_bridge`: third-party records stop flowing
    through the native renderer, and ``logger.add()`` sinks are re-attached to
    the root logger so they keep receiving them (raw, as before the install).
    Root handlers removed by ``clear_handlers=True`` are not restored.
    """
    logging.getLogger().removeHandler(bridge)
    bridge.close()
    _set_stdlib_bridge_active(False)
