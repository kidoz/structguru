"""Loguru-like wrapper for structlog.

Provides a :class:`Logger` dataclass that mirrors Loguru's ergonomic API
(``bind``, ``contextualize``, ``opt``, level methods) while delegating all
actual log processing to :mod:`structlog`.

A global ``logger`` instance is exported for convenience::

    from structguru import logger

    logger.info("Hello {name}", name="world")
"""

from __future__ import annotations

import functools
import itertools
import logging
import string
import sys
import threading
import warnings
from collections.abc import Callable, Iterator
from contextlib import ContextDecorator, contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, TypeAlias

import structlog
from structlog.contextvars import bound_contextvars, get_contextvars

from structguru import _native
from structguru.config import _to_logging_level
from structguru.otel import add_otel_context

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


@functools.lru_cache(maxsize=1024)
def _extract_format_keys(msg: str) -> frozenset[str] | Exception:
    """Extract format keys or return an Exception if malformed."""
    consumed: set[str] = set()
    try:
        for _, field_name, _, _ in string.Formatter().parse(msg):
            if field_name is not None:
                root = field_name.split(".")[0].split("[")[0]
                if root and not root.isdigit():
                    consumed.add(root)
        return frozenset(consumed)
    except Exception as exc:
        return exc


@functools.lru_cache(maxsize=256)
def _warn_format_failure(msg: str, exc_type_name: str, exc_msg: str) -> None:
    """Emit a ``UserWarning`` at most once per unique (template, error type)."""
    warnings.warn(
        f"structguru: failed to format log message {msg!r}: {exc_type_name}: {exc_msg}",
        stacklevel=4,
    )


def _safe_format(
    message: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> tuple[str, frozenset[str]]:
    """Safely format *message* with ``str.format``, imitating Loguru style.

    Returns a tuple of (formatted_message, consumed_keys). *consumed_keys*
    contains the kwarg names that were used as format placeholders.

    Formatting errors (``KeyError``, ``IndexError``, ``ValueError``) fall back
    to the raw message and emit a one-shot :class:`UserWarning` per template.
    """
    msg = message if type(message) is str else str(message)
    if not (args or kwargs) or "{" not in msg:
        return msg, frozenset()

    keys_or_exc = _extract_format_keys(msg)
    if isinstance(keys_or_exc, Exception):
        # Malformed brace syntax — surface it but don't break logging.
        _warn_format_failure(msg, type(keys_or_exc).__name__, str(keys_or_exc))
        return msg, frozenset()

    try:
        return msg.format(*args, **kwargs), keys_or_exc
    except (LookupError, AttributeError, TypeError, ValueError) as exc:
        # Template/arg mismatch from user code (missing key, bad attribute,
        # unknown conversion, etc.). Fall back to the raw message.
        _warn_format_failure(msg, type(exc).__name__, str(exc))
        return msg, frozenset()


_id_counter = itertools.count(1)
_id_counter_lock = threading.Lock()


class _Catcher(ContextDecorator):
    """Context manager / decorator returned by :meth:`Logger.catch`."""

    def __init__(
        self,
        logger: Logger,
        exception: type[BaseException] | tuple[type[BaseException], ...],
        level: str,
        message: str,
        reraise: bool,
    ) -> None:
        self._logger = logger
        self._exception = exception
        self._level = level
        self._message = message
        self._reraise = reraise

    def __enter__(self) -> None:
        pass

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> bool:
        if exc_type is not None and issubclass(exc_type, self._exception):
            self._logger._log(self._level, self._message, (), {"exc_info": exc_val})
            return not self._reraise
        return False


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

    def catch(
        self,
        exception: type[BaseException] | tuple[type[BaseException], ...] = Exception,
        *,
        level: str = "error",
        message: str = "An error occurred",
        reraise: bool = False,
    ) -> Any:
        """Return a context manager / decorator to catch and log exceptions.

        When an exception matching ``exception`` is raised within the context
        or decorated function, it is logged at ``level`` with ``message`` and
        the exception details.

        If ``reraise`` is False (the default), the exception is suppressed.
        """
        return _Catcher(self, exception, level, message, reraise)

    def opt(
        self,
        *,
        exception: Any = None,
        stack_info: bool = False,
    ) -> Logger:
        """Return a child logger with ``exception`` / ``stack_info`` pre-set.

        Matches loguru: the returned logger keeps the flags across every call
        it makes (not just the next one).  Chain directly
        (``log.opt(exception=True).error(...)``) for single-shot use.
        """
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
        stack_info = bool(kwargs.get("stack_info") or self._opt_stack_info)
        use_native = _native.is_native_enabled()

        # Cheap disabled path: level-filter before any formatting.
        if use_native and _native.is_below_level(method):
            return

        formatted_msg, consumed_keys = _safe_format(message, args, kwargs)

        # Strip kwargs that were consumed by brace-formatting so they don't
        # leak into the structured log fields (matches loguru behaviour).
        for key in consumed_keys:
            kwargs.pop(key, None)

        # Pre-render filter (sampling/rate-limit): decide before building fields.
        # Keys on the formatted message, matching the post-EventRenamer behaviour
        # of the standard-path RateLimitingProcessor.
        if use_native and not _native.should_render(method, formatted_msg):
            return

        if use_native:
            exc_info = kwargs.get("exc_info", self._opt_exc_info)
            name = self.name if self.name is not None else _caller_module_name()
            fields = {
                **self._bound,
                **{k: v for k, v in kwargs.items() if k not in ("exc_info", "stack_info")},
            }
            # Contextvars append after (and never override) event fields,
            # matching structlog's merge_contextvars setdefault semantics.
            for key, value in get_contextvars().items():
                fields.setdefault(key, value)
            if _native.otel_enabled():
                add_otel_context(None, method, fields)
            if exc_info:
                fields["exception"] = _native.format_exception(exc_info)
            # Stack capture is Python-owned (frame walking); rendering places
            # "stack" between "service" and "message" like StackInfoRenderer.
            stack = _native.format_stack() if stack_info else None
            _native.render_and_enqueue(fields, name, method, formatted_msg, stack=stack)
            return

        structlog_logger = self._get_structlog_logger()
        if self._opt_exc_info is not None:
            kwargs.setdefault("exc_info", self._opt_exc_info)
        if self._opt_stack_info:
            kwargs.setdefault("stack_info", True)

        getattr(structlog_logger, method)(formatted_msg, **kwargs)

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
