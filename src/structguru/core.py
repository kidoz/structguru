"""Loguru-like wrapper for structlog.

Provides a :class:`Logger` dataclass that mirrors Loguru's ergonomic API
(``bind``, ``contextualize``, ``opt``, level methods) while delegating all
actual log processing to :mod:`structlog`.

A global ``logger`` instance is exported for convenience::

    from structguru import logger

    logger.info("Hello {name}", name="world")
"""

from __future__ import annotations

import itertools
import logging
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, TypeAlias

import structlog
from structlog.contextvars import bound_contextvars

from structguru.config import _to_logging_level

HandlerId: TypeAlias = int
Sink: TypeAlias = "str | Path | logging.Handler | Callable[[str], None]"


def _caller_module_name() -> str:
    """Walk the call stack to find the first frame outside this module."""
    frame = sys._getframe(0)
    while frame is not None:
        name: str = frame.f_globals.get("__name__", "")
        if name != __name__:
            return name
        frame = frame.f_back  # type: ignore[assignment]
    return "unknown"


class _CallableHandler(logging.Handler):
    """A :class:`logging.Handler` that delegates to a plain callable."""

    def __init__(self, fn: Callable[[str], None]) -> None:
        super().__init__()
        self._fn = fn

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            self._fn(msg)
        except Exception:
            self.handleError(record)


def _make_handler(sink: Sink) -> logging.Handler:
    """Create a :class:`logging.Handler` from various *sink* types."""
    if isinstance(sink, logging.Handler):
        return sink
    if isinstance(sink, (str, Path)):
        return logging.FileHandler(str(sink), encoding="utf-8")
    if hasattr(sink, "write"):
        return logging.StreamHandler(sink)
    if callable(sink):
        return _CallableHandler(sink)
    msg = f"Unsupported sink type: {type(sink)!r}"
    raise TypeError(msg)


def _safe_format(
    message: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> tuple[str, set[str]]:
    """Safely format *message* with ``str.format``, imitating Loguru style.

    Returns a tuple of (formatted_message, consumed_keys). *consumed_keys*
    contains the kwarg names that were used as format placeholders.
    """
    msg = str(message)
    if not (args or kwargs) or "{" not in msg:
        return msg, set()

    try:
        import string

        consumed: set[str] = set()
        for _, field_name, _, _ in string.Formatter().parse(msg):
            if field_name is not None:
                # field_name can be "name.attr" or "0" — take the root key
                root = field_name.split(".")[0].split("[")[0]
                if root and not root.isdigit():
                    consumed.add(root)
        return msg.format(*args, **kwargs), consumed
    except Exception:
        return msg, set()


_id_counter = itertools.count(1)
_id_counter_lock = threading.Lock()


@dataclass
class Logger:
    """A Loguru-like facade for :mod:`structlog`.

    *   ``trace``, ``debug``, ``info``, ``success``, ``warning``, ``error``,
        ``critical``, ``exception`` methods.
    *   ``bind()`` — create child loggers with persistent context.
    *   ``contextualize()`` — add request-scoped context via *contextvars*.
    *   ``add()`` / ``remove()`` — manage logging handlers (sinks).
    *   ``opt()`` — include exception info or stack traces for one call.
    """

    name: str | None = None
    _bound: dict[str, Any] = field(default_factory=dict)
    _opt_exc_info: Any = None
    _opt_stack_info: bool = False

    _handlers: dict[HandlerId, logging.Handler] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # -- structlog bridge ---------------------------------------------------

    def _get_structlog_logger(self) -> Any:
        """Return a structlog logger, applying any bound context."""
        name = self.name if self.name is not None else _caller_module_name()
        log = structlog.get_logger(name)
        if self._bound:
            log = log.bind(**self._bound)
        return log

    # -- context helpers ----------------------------------------------------

    def bind(self, **kwargs: Any) -> Logger:
        """Return a *new* logger with permanently bound context."""
        merged = {**self._bound, **kwargs}
        return replace(self, _bound=merged)

    @contextmanager
    def contextualize(self, **kwargs: Any) -> Iterator[Logger]:
        """Apply context for the duration of a ``with`` block."""
        with bound_contextvars(**kwargs):
            yield self

    def opt(
        self,
        *,
        exception: Any = None,
        stack_info: bool = False,
    ) -> Logger:
        """Configure one-time options for the next log call."""
        exc_info = exception if exception is not None else self._opt_exc_info
        return replace(self, _opt_exc_info=exc_info, _opt_stack_info=stack_info)

    # -- logging methods ----------------------------------------------------

    def trace(self, message: Any, *args: Any, **kwargs: Any) -> None:
        self._log("debug", message, args, kwargs)

    def debug(self, message: Any, *args: Any, **kwargs: Any) -> None:
        self._log("debug", message, args, kwargs)

    def info(self, message: Any, *args: Any, **kwargs: Any) -> None:
        self._log("info", message, args, kwargs)

    def success(self, message: Any, *args: Any, **kwargs: Any) -> None:
        self._log("info", message, args, kwargs)

    def warning(self, message: Any, *args: Any, **kwargs: Any) -> None:
        self._log("warning", message, args, kwargs)

    def warn(self, message: Any, *args: Any, **kwargs: Any) -> None:
        self.warning(message, *args, **kwargs)

    def error(self, message: Any, *args: Any, **kwargs: Any) -> None:
        self._log("error", message, args, kwargs)

    def critical(self, message: Any, *args: Any, **kwargs: Any) -> None:
        self._log("critical", message, args, kwargs)

    def fatal(self, message: Any, *args: Any, **kwargs: Any) -> None:
        self.critical(message, *args, **kwargs)

    def exception(self, message: Any, *args: Any, **kwargs: Any) -> None:
        """Log at ``ERROR`` level with exception information."""
        kwargs.setdefault("exc_info", True)
        self._log("error", message, args, kwargs)

    def _log(
        self,
        method: str,
        message: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> None:
        """Internal dispatch."""
        structlog_logger = self._get_structlog_logger()
        formatted_msg, consumed_keys = _safe_format(message, args, kwargs)

        # Strip kwargs that were consumed by brace-formatting so they don't
        # leak into the structured log fields (matches loguru behaviour).
        for key in consumed_keys:
            kwargs.pop(key, None)

        if self._opt_exc_info is not None:
            kwargs.setdefault("exc_info", self._opt_exc_info)
        if self._opt_stack_info:
            kwargs.setdefault("stack_info", True)

        getattr(structlog_logger, method)(formatted_msg, **kwargs)

        # Clear one-shot options after the call (loguru semantics).
        if self._opt_exc_info is not None or self._opt_stack_info:
            object.__setattr__(self, "_opt_exc_info", None)
            object.__setattr__(self, "_opt_stack_info", False)

    # -- sink (handler) management ------------------------------------------

    def add(self, sink: Sink, *, level: str | None = None) -> HandlerId:
        """Add a new logging handler (*sink*).

        Parameters
        ----------
        sink:
            A file path, a :class:`logging.Handler`, or a callable accepting
            a single string.
        level:
            Minimum level for this handler.  Inherits from the root logger
            when *None*.

        Returns
        -------
        HandlerId
            An identifier that can be passed to :meth:`remove`.
        """
        handler = _make_handler(sink)
        log_level = _to_logging_level(level) if level else logging.getLogger().level
        handler.setLevel(log_level)
        handler.setFormatter(logging.Formatter("%(message)s"))

        root = logging.getLogger()
        root.addHandler(handler)

        with _id_counter_lock:
            handler_id = next(_id_counter)
        with self._lock:
            self._handlers[handler_id] = handler
        return handler_id

    def remove(self, handler_id: HandlerId | None = None) -> None:
        """Remove a handler by its *handler_id*.

        If *handler_id* is ``None``, all handlers added via this logger
        instance are removed.
        """
        root = logging.getLogger()
        with self._lock:
            if handler_id is None:
                for h in self._handlers.values():
                    root.removeHandler(h)
                    h.close()
                self._handlers.clear()
                return

            handler_to_remove = self._handlers.pop(handler_id, None)
            if handler_to_remove:
                root.removeHandler(handler_to_remove)
                handler_to_remove.close()


logger = Logger()
