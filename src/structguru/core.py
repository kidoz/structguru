"""Loguru-like facade for the native structguru runtime.

Provides a :class:`Logger` dataclass that mirrors Loguru's ergonomic API
(``bind``, ``contextualize``, ``opt``, level methods) while delegating all
actual log processing to the bundled Rust renderer and writer.

A global ``logger`` instance is exported for convenience::

    from structguru import logger

    logger.info("Hello {name}", name="world")
"""

from __future__ import annotations

import functools
import itertools
import json
import logging
import os
import string
import sys
import threading
import warnings
import weakref
from collections.abc import Callable, Iterator
from contextlib import ContextDecorator, contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, TypeAlias

from structguru import _runtime
from structguru._contextvars import bound_contextvars, get_contextvars
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


class _SecureFileHandler(logging.FileHandler):
    """File handler that creates new Unix log files with owner-only permissions."""

    def _open(self) -> Any:
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        descriptor = os.open(self.baseFilename, flags, 0o600)
        try:
            return os.fdopen(
                descriptor,
                self.mode,
                encoding=self.encoding,
                errors=self.errors,
            )
        except Exception:
            os.close(descriptor)
            raise


def _make_handler(sink: Sink) -> logging.Handler:
    """Create a :class:`logging.Handler` from various *sink* types."""
    if isinstance(sink, logging.Handler):
        return sink
    if isinstance(sink, (str, Path)):
        return _SecureFileHandler(str(sink), encoding="utf-8")
    if hasattr(sink, "write"):
        return logging.StreamHandler(sink)
    if callable(sink):
        return _CallableHandler(sink)
    msg = f"Unsupported sink type: {type(sink)!r}"
    raise TypeError(msg)


def _emit_rendered(handler: logging.Handler, line: str) -> None:
    """Forward a native rendered line through a stdlib handler."""
    level = logging.INFO
    try:
        rendered = json.loads(line)
        level_name = str(rendered.get("level", "INFO"))
        level = _to_logging_level(level_name)
    except (json.JSONDecodeError, TypeError):
        pass
    record = logging.LogRecord(
        name="structguru",
        level=level,
        pathname="",
        lineno=0,
        msg=line.rstrip("\n"),
        args=(),
        exc_info=None,
    )
    handler.handle(record)


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


# Sinks registered through `Logger.add()` are attached to the stdlib root logger
# so they also receive third-party `logging` records. The stdlib bridge
# (`structguru.integrations.stdlib`) routes those same records through the
# native renderer instead, which delivers them to the very same sinks — already
# formatted and redacted. Leaving the root attachment in place while the bridge
# is installed therefore delivers every third-party record twice: once raw, once
# rendered. Track what we attached so the bridge can suspend it and restore it
# on uninstall.
_root_sinks: weakref.WeakSet[logging.Handler] = weakref.WeakSet()
_root_attach_lock = threading.Lock()
_stdlib_bridge_active = False


def _attach_to_root(handler: logging.Handler) -> None:
    """Register *handler* for third-party records, unless the bridge supersedes it."""
    with _root_attach_lock:
        _root_sinks.add(handler)
        if _stdlib_bridge_active:
            return
        logging.getLogger().addHandler(handler)


def _detach_from_root(handler: logging.Handler) -> None:
    """Undo :func:`_attach_to_root` (a no-op when the handler is not attached)."""
    with _root_attach_lock:
        _root_sinks.discard(handler)
    logging.getLogger().removeHandler(handler)


def _set_stdlib_bridge_active(active: bool) -> None:
    """Suspend or restore root attachment for ``add()`` sinks.

    Called by :mod:`structguru.integrations.stdlib` on install/uninstall. While
    the bridge is active, third-party records reach ``add()`` sinks through the
    native path, so the direct root attachment is dropped; uninstalling restores
    it for every sink still registered.
    """
    global _stdlib_bridge_active
    root = logging.getLogger()
    with _root_attach_lock:
        _stdlib_bridge_active = active
        affected = list(_root_sinks)
    for handler in affected:
        if active:
            root.removeHandler(handler)
        else:
            root.addHandler(handler)


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


@dataclass(eq=False)
class Logger:
    """A Loguru-like facade for native structured logging.

    *   ``trace``, ``debug``, ``info``, ``success``, ``warning``, ``error``,
        ``critical``, ``exception`` methods.
    *   ``bind()`` — create child loggers with persistent context.
    *   ``contextualize()`` — add request-scoped context via *contextvars*.
    *   ``add()`` / ``remove()`` — manage logging handlers (sinks).
    *   ``opt()`` — include exception info or stack traces for one call.

    Field values are rendered natively: JSON scalars, mappings, sequences,
    ``datetime``/``date``, ``UUID``, ``Enum`` (by value), and dataclasses. A
    value outside that set never fails the call; it is replaced by a marker
    naming its type (``<unsupported: PosixPath>``), a self-referencing
    container by ``<cycle: dict>``, and nesting beyond 64 levels by
    ``<max depth exceeded>``.
    """

    name: str | None = None
    _bound: dict[str, Any] = field(default_factory=dict)
    _opt_exc_info: Any = None
    _opt_stack_info: bool = False

    _handlers: dict[HandlerId, logging.Handler] = field(default_factory=dict)
    _native_sink_tokens: dict[HandlerId, int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # -- context helpers ----------------------------------------------------

    def bind(self, **kwargs: Any) -> Logger:
        """Return a *new* logger with permanently bound context.

        Only the bound fields are copied. Sinks are process-global, so the child
        deliberately shares its parent's sink registry: ``add()`` on either is
        visible to both, and ``remove()`` on either removes from both.
        """
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
        """Log at ``DEBUG`` level.

        A loguru-compatible alias; structguru has no separate ``TRACE`` level,
        so records emitted here render as ``DEBUG``.
        """
        self._log("debug", message, args, kwargs)

    def debug(self, message: Any, *args: Any, **kwargs: Any) -> None:
        """Log at ``DEBUG`` level.

        *message* is brace-formatted with *args* / *kwargs*; keys consumed by
        the format string are dropped, and the rest become structured fields.
        """
        self._log("debug", message, args, kwargs)

    def info(self, message: Any, *args: Any, **kwargs: Any) -> None:
        """Log at ``INFO`` level."""
        self._log("info", message, args, kwargs)

    def success(self, message: Any, *args: Any, **kwargs: Any) -> None:
        """Log at ``INFO`` level.

        A loguru-compatible alias; structguru has no separate ``SUCCESS``
        level, so records emitted here render as ``INFO``.
        """
        self._log("info", message, args, kwargs)

    def warning(self, message: Any, *args: Any, **kwargs: Any) -> None:
        """Log at ``WARNING`` level."""
        self._log("warning", message, args, kwargs)

    def warn(self, message: Any, *args: Any, **kwargs: Any) -> None:
        """Log at ``WARNING`` level — alias of :meth:`warning`."""
        self.warning(message, *args, **kwargs)

    def error(self, message: Any, *args: Any, **kwargs: Any) -> None:
        """Log at ``ERROR`` level.

        Pass ``exc_info=True`` (or use :meth:`exception`) to attach the active
        exception.
        """
        self._log("error", message, args, kwargs)

    def critical(self, message: Any, *args: Any, **kwargs: Any) -> None:
        """Log at ``CRITICAL`` level."""
        self._log("critical", message, args, kwargs)

    def fatal(self, message: Any, *args: Any, **kwargs: Any) -> None:
        """Log at ``CRITICAL`` level — alias of :meth:`critical`."""
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
        """Internal dispatch — native Rust path (the only path since v1.0)."""
        # One coherent snapshot follows the record through every processing stage.
        # Reconfigure/disable may retire its writer, but cannot invalidate this
        # Python object or expose a partially updated configuration.
        runtime = _runtime.current_runtime()
        if runtime is None:
            return

        stack_info = kwargs.get("stack_info") or self._opt_stack_info

        # Cheap disabled path: level-filter before any formatting.
        if _runtime.is_below_level(method, runtime):
            return

        formatted_msg, consumed_keys = _safe_format(message, args, kwargs)

        # Strip kwargs that were consumed by brace-formatting so they don't
        # leak into the structured log fields (matches loguru behaviour).
        for key in consumed_keys:
            kwargs.pop(key, None)

        # Pre-render filter (sampling/rate-limit): decide before building fields.
        # Keys on the formatted message, matching the rate-limiter's grouping.
        if not _runtime.should_render(method, formatted_msg, runtime):
            return

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
        if _runtime.otel_enabled(runtime):
            add_otel_context(None, method, fields)
        if exc_info:
            exception = _runtime.build_exception_field(exc_info, runtime)
            if exception:
                fields["exception"] = exception
        # Stack capture is Python-owned (frame walking); rendering places
        # "stack" between "service" and "message" like StackInfoRenderer.
        if isinstance(stack_info, str):
            stack = stack_info
        elif stack_info:
            stack = _runtime.format_stack()
        else:
            stack = None
        _runtime.notify_metrics(method, formatted_msg, fields, runtime)
        sentry_line = _runtime.render_and_enqueue(
            fields,
            name,
            method,
            formatted_msg,
            stack=stack,
            runtime=runtime,
        )
        if sentry_line is not None:
            _runtime.notify_sentry(
                method,
                sentry_line,
                tuple(fields),
                exc_info=exc_info,
                runtime=runtime,
            )

    # -- sink (handler) management ------------------------------------------

    def add(self, sink: Sink, *, level: str | None = None) -> HandlerId:
        """Add a sink that receives rendered log lines.

        Parameters
        ----------
        sink:
            A callable accepting a single string (the rendered log line),
            a file path (``str``/``Path``), a stream-like object with ``.write``,
            or a :class:`logging.Handler` (for third-party stdlib interop).
        level:
            Minimum level for this sink. Applies to every sink type — it gates
            native delivery at the dispatcher. Defaults to all levels.

        Returns
        -------
        HandlerId
            An identifier that can be passed to :meth:`remove`.

        Note
        ----
        Every sink receives both structguru records and third-party stdlib records.
        Native records are delivered via the bounded callable dispatch queue;
        configured backpressure and flush semantics therefore apply uniformly.

        Third-party records arrive raw (unrendered) via the stdlib root logger.
        Install the bridge (``structguru.integrations.stdlib``) to receive them
        rendered and redacted through the native path instead; while it is
        installed the raw root-logger delivery is suspended, so a sink never
        sees the same record twice.
        """
        handler = _make_handler(sink)
        # One threshold for both delivery paths. Unset means NOTSET (0) — "all
        # levels", as documented. Inheriting the root logger's level here instead
        # would silently gate the stdlib path at WARNING (the default on an
        # unconfigured root) while the native path still delivered everything.
        threshold = _to_logging_level(level) if level else logging.NOTSET
        handler.setLevel(threshold)
        handler.setFormatter(logging.Formatter("%(message)s"))

        native_callback = (
            sink
            if callable(sink) and not isinstance(sink, logging.Handler)
            else functools.partial(_emit_rendered, handler)
        )
        native_token = _runtime.add_callable_sink(native_callback, min_level=threshold)

        _attach_to_root(handler)

        with _id_counter_lock:
            handler_id = next(_id_counter)
        with self._lock:
            self._handlers[handler_id] = handler
            self._native_sink_tokens[handler_id] = native_token
        return handler_id

    def remove(self, handler_id: HandlerId | None = None) -> None:
        """Remove a sink by its *handler_id*.

        If *handler_id* is ``None``, all sinks added via this logger
        instance are removed.
        """
        with self._lock:
            if handler_id is None:
                registrations = [
                    (handler, self._native_sink_tokens.get(sink_id))
                    for sink_id, handler in self._handlers.items()
                ]
                self._handlers.clear()
                self._native_sink_tokens.clear()
            else:
                registrations = [
                    (
                        self._handlers.pop(handler_id, None),  # type: ignore[arg-type]
                        self._native_sink_tokens.pop(handler_id, None),
                    )
                ]
        for handler, token in registrations:
            if token is not None:
                _runtime.remove_callable_sink(token)
            if handler is not None:
                _detach_from_root(handler)
                handler.close()


logger = Logger()
