"""Loguru-like facade for the native structguru runtime.

Provides a ``Logger`` dataclass that mirrors Loguru's ergonomic API
(``bind``, ``contextualize``, ``opt``, level methods) while delegating all
actual log processing to the bundled Rust renderer and writer.

A global ``logger`` instance is exported for convenience::

    from structguru import logger

    logger.info("Hello {name}", name="world")
"""

from __future__ import annotations

import functools
import importlib
import inspect
import itertools
import logging
import os
import re
import stat
import string
import sys
import threading
import weakref
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ContextDecorator, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, TypeAlias, TypeVar, cast

from structguru import _runtime
from structguru._contextvars import _ctx, bound_contextvars
from structguru._levels import METHOD_LEVELS
from structguru._native_dispatch import callback_scope, in_callback
from structguru.exceptions import _redacted_message_repr
from structguru.otel import add_otel_context
from structguru.redaction import DEFAULT_SENSITIVE_KEYS
from structguru.settings import _level_number

HandlerId: TypeAlias = int

# Stands in for an exhausted field source once Python has merged the fields
# itself; the renderer only reads it, so one shared empty dict is safe.
_NO_FIELDS: dict[str, Any] = {}


class WritableSink(Protocol):
    """A text stream accepted by ``Logger.add``."""

    def write(self, message: str, /) -> object:
        """Write a rendered text record."""
        ...


Sink: TypeAlias = "str | Path | logging.Handler | Callable[[str], None] | WritableSink"
_F = TypeVar("_F", bound=Callable[..., Any])


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
    """A ``logging.Handler`` that delegates to a plain callable."""

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
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(self.baseFilename, flags, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                msg = "log path must be a regular file"
                raise ValueError(msg)
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
    """Create a ``logging.Handler`` from various *sink* types."""
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


def _emit_rendered(handler: logging.Handler, line: str, level: int = logging.INFO) -> None:
    """Forward a native rendered line through a stdlib handler."""
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


_NO_KEYS: frozenset[str] = frozenset()
# Logging controls, not template arguments: alone they never trigger formatting.
_CONTROL_KWARGS = frozenset({"exc_info", "stack_info"})
# Templates longer than this are parsed on every call instead of cached, so the
# caches never pin large (and unredacted) dynamic message text.
_MAX_CACHED_TEMPLATE = 512
_REDACTED = "[REDACTED]"
_FORMATTER = string.Formatter()
_FIELD_PART = re.compile(r"\.([^.\[]*)|\[([^\]]*)\]")

# Argument types formatted as themselves: never containers to redact.
_SCALAR_TYPES = frozenset({str, int, float, bool, type(None)})
_OBJECT_STR: object = object.__str__
_OBJECT_FORMAT: object = object.__format__

# (placeholder roots consumed from kwargs, lower-cased names on every path,
#  whether a name is a default sensitive key, whether a placeholder reaches
#  into an argument by attribute or index)
_Template: TypeAlias = tuple[frozenset[str], frozenset[str], bool, bool]


def _python_field_path(field_name: str) -> tuple[str, list[str]]:
    """Split a replacement field name into its root and attribute/index names."""
    end = len(field_name)
    for separator in ".[":
        position = field_name.find(separator)
        if 0 <= position < end:
            end = position
    parts = [attribute or index for attribute, index in _FIELD_PART.findall(field_name, end)]
    return field_name[:end], parts


_template_native = importlib.import_module("structguru._rust")
_field_path = cast(
    "Callable[[str], tuple[str, list[str]]]",
    functools.partial(_template_native._template_field_path, _python_field_path),
)
# Preserve CPython brace grammar and Unicode behavior while collecting names
# natively. The existing bounded LRU remains the only template cache.
_parse_template = cast(
    "Callable[[str], _Template | Exception]",
    functools.partial(
        _template_native._analyze_template,
        _FORMATTER.parse,
        _python_field_path,
        DEFAULT_SENSITIVE_KEYS,
    ),
)


_extract_format_keys = functools.lru_cache(maxsize=1024)(_parse_template)


def _template_key(msg: str) -> str | int:
    """Key a long template by its hash so the warning cache holds no text."""
    return msg if len(msg) <= _MAX_CACHED_TEMPLATE else hash(msg)


@functools.lru_cache(maxsize=256)
def _warn_format_failure(template: str | int, exc_type_name: str) -> None:
    """Emit a ``UserWarning`` at most once per unique (template, error type).

    Like the runtime's diagnostics, the warning never raises into the logging
    call: under ``-W error`` it goes to ``sys.unraisablehook`` instead.
    """
    _runtime._warn(
        f"structguru: failed to format log message ({exc_type_name}); "
        "using the unformatted template",
        # Do not expose the user's source line either: it may contain literals
        # covered by redaction. The normal record goes through native redaction.
        stacklevel=2,
    )


@functools.lru_cache(maxsize=256)
def _warn_unprintable_message(type_name: str, exc_type_name: str) -> None:
    """Emit a ``UserWarning`` at most once per (message type, error type).

    The warning never raises into the logging call; see
    ``_warn_format_failure``.
    """
    _runtime._warn(
        f"structguru: failed to convert log message to str ({exc_type_name}); using a placeholder",
        stacklevel=2,
    )


def _message_text(message: object) -> str:
    """Convert a non-string message, never raising into the logging call."""
    try:
        return str(message)
    except Exception as exc:  # noqa: BLE001 - logging must not raise for its argument
        _warn_unprintable_message(type(message).__qualname__, type(exc).__name__)
        return f"<unprintable {type(message).__name__} object>"


_sensitive_cache: tuple[object, frozenset[str]] = (None, DEFAULT_SENSITIVE_KEYS)


def _sensitive_names(keys: Sequence[str]) -> frozenset[str]:
    """Lower-case configured redaction keys once per configuration."""
    global _sensitive_cache
    cached_keys, names = _sensitive_cache
    if keys is not cached_keys:
        names = frozenset(key.lower() for key in keys)
        _sensitive_cache = (keys, names)
    return names


class _RedactingFormatter(string.Formatter):
    """``str.format`` rendering any placeholder that names a sensitive key redacted.

    A key, attribute, or index name matches case-insensitively, as the renderer
    matches field keys, and the value behind it is never looked up. A container
    a placeholder interpolates as its ``repr()`` (a mapping, sequence, set,
    dataclass, or namespace, under ``!r``, or without its own ``__str__`` and
    ``__format__``) renders with values under sensitive keys redacted, as
    exception locals do; with nothing to redact it formats as usual.
    """

    def __init__(self, sensitive: frozenset[str]) -> None:
        super().__init__()
        self._sensitive = sensitive

    def get_field(self, field_name: str, args: Sequence[Any], kwargs: Mapping[str, Any]) -> Any:
        root, path = _field_path(field_name)
        if any(name.lower() in self._sensitive for name in (root, *path)):
            return _REDACTED, root
        return super().get_field(field_name, args, kwargs)

    def convert_field(self, value: Any, conversion: str | None) -> Any:
        cls = type(value)
        if cls not in _SCALAR_TYPES and (
            conversion in ("r", "a")
            or (
                getattr(cls, "__str__", None) is _OBJECT_STR
                and (conversion == "s" or getattr(cls, "__format__", None) is _OBJECT_FORMAT)
            )
        ):
            rendered = _redacted_message_repr(value, self._sensitive)
            if rendered is not None:
                # A format spec then applies to the text as to any string.
                if conversion == "a":
                    return rendered.encode("ascii", "backslashreplace").decode("ascii")
                return rendered
        return super().convert_field(value, conversion)


def _may_expose_nested_secret(
    text: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    consumed: frozenset[str],
    has_path: bool,
    sensitive: frozenset[str],
) -> bool:
    """True when plain formatting may have exposed a value under a sensitive key.

    Only a non-scalar argument, or an object reached by attribute or index,
    can carry a nested key, and its text then names that key. Scalar-only
    calls cost one type lookup per argument; the text is scanned only after.
    """
    if not has_path:
        for value in args:
            if type(value) not in _SCALAR_TYPES:
                break
        else:
            for name in consumed:
                if type(kwargs.get(name)) not in _SCALAR_TYPES:
                    break
            else:
                return False
    # A case-insensitive regex alternation is several times slower than this.
    lowered = text.lower()
    return any(key in lowered for key in sensitive)


def _safe_format(
    message: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    runtime: _runtime._RuntimeState | None = None,
) -> tuple[str, frozenset[str]]:
    """Safely format *message* with ``str.format``, imitating Loguru style.

    Returns a tuple of (formatted_message, consumed_keys). *consumed_keys*
    contains the kwarg names that were used as format placeholders.

    The template is formatted whenever positional or keyword arguments other
    than ``exc_info``/``stack_info`` are present, like ``str.format`` in
    loguru. A placeholder naming a sensitive key of *runtime* (the defaults
    without one) is interpolated as ``[REDACTED]``, and so is a value under
    such a key inside a container a placeholder interpolates, or reaches by
    attribute or index. Any formatting failure falls back to the raw
    template, and a message whose ``str()`` fails to a placeholder, each with
    a one-shot ``UserWarning``; logging never raises for its arguments.
    """
    msg = message if type(message) is str else _message_text(message)
    if not (args or kwargs) or ("{" not in msg and "}" not in msg):
        return msg, _NO_KEYS
    if (
        not args
        and ("exc_info" in kwargs or "stack_info" in kwargs)
        and kwargs.keys() <= _CONTROL_KWARGS
    ):
        return msg, _NO_KEYS

    # Match the keys the renderer uses: the defaults unless configured.
    keys = runtime.settings.sensitive_keys if runtime is not None else None
    sensitive = DEFAULT_SENSITIVE_KEYS if keys is None else _sensitive_names(keys)
    try:
        formatted = _template_native._format_simple(msg, args, kwargs, sensitive)
        if formatted is not None:
            return cast("tuple[str, frozenset[str]]", formatted)
    except Exception as exc:  # noqa: BLE001 - scalar conversion must not break logging
        _warn_format_failure(_template_key(msg), type(exc).__name__)
        return msg, _NO_KEYS

    template = (
        _extract_format_keys(msg) if len(msg) <= _MAX_CACHED_TEMPLATE else _parse_template(msg)
    )
    if isinstance(template, Exception):
        # Malformed brace syntax — surface it but don't break logging.
        _warn_format_failure(_template_key(msg), type(template).__name__)
        return msg, _NO_KEYS

    consumed, names, names_default_key, has_path = template
    try:
        redact = names_default_key if keys is None else not names.isdisjoint(sensitive)
        if not redact:
            text = msg.format(*args, **kwargs)
            if not sensitive or not _may_expose_nested_secret(
                text, args, kwargs, consumed, has_path, sensitive
            ):
                return text, consumed
        # The pure-Python formatter is several times slower than str.format,
        # so it only re-renders a template whose plain text could hold a secret.
        return _RedactingFormatter(sensitive).vformat(msg, args, kwargs), consumed
    except Exception as exc:  # noqa: BLE001 - user __format__/__str__ may raise anything
        # Template/arg mismatch or a failing argument conversion from user
        # code (missing key, bad attribute, raising __format__, etc.).
        _warn_format_failure(_template_key(msg), type(exc).__name__)
        return msg, _NO_KEYS


_id_counter = itertools.count(1)
_id_counter_lock = threading.Lock()


class _RootRelay(logging.Handler):
    """One registration generation of a handler attached to the root logger."""

    def __init__(
        self,
        sink: logging.Handler,
        threshold: int,
        ownership: _HandlerOwnership,
        generation: int,
    ) -> None:
        super().__init__()
        self._sink = sink
        self._threshold = threshold
        self._ownership = ownership
        self._generation = generation
        self._condition = threading.Condition()
        self._active = 0
        self._detached = False
        self._raw_retired = False
        self._native_retired = False
        self._released = False
        self._close_on_release = True

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self._sink!r} generation={self._generation}>"

    def handle(self, record: logging.LogRecord) -> bool:
        # The relay stays at NOTSET so the root logger offers every record;
        # the ``add()`` level applies here, as ``callHandlers`` would apply it.
        # A caller's handler keeps its own level for the loggers it serves.
        if record.levelno < self._threshold or in_callback():
            return False
        with self._condition:
            if self._detached:
                return False
            self._active += 1
        try:
            with callback_scope():
                return bool(self._sink.handle(record))
        finally:
            with self._condition:
                self._active -= 1
                retired = self._detached and self._active == 0
                if self._active == 0:
                    self._condition.notify_all()
            if retired:
                try:
                    _retire_handler_generation(self, raw=True)
                except BaseException:
                    # Native dispatch contains finalizer failures too.
                    pass

    def detach(self) -> bool:
        """Reject records and report whether raw delivery has already retired."""
        with self._condition:
            self._detached = True
            if in_callback() and self._active > 0:
                return False
            self._condition.wait_for(lambda: self._active == 0)
        return True

    def _reset_after_fork(self) -> None:
        self._condition = threading.Condition()
        self._active = 0


class _HandlerOwnership:
    """Shared close state for every registration generation of one handler."""

    def __init__(self, handler: logging.Handler) -> None:
        self.handler = handler
        self.generations: set[_RootRelay] = set()
        self.next_generation = 1
        self.closing = False


# Sinks registered through `Logger.add()` are attached to the stdlib root logger
# (through a `_RootRelay`) so they also receive third-party `logging` records.
# The stdlib bridge (`structguru.integrations.stdlib`) routes those same records
# through the native renderer instead, which delivers them to the very same
# sinks — already formatted and redacted. Leaving the root attachment in place
# while the bridge is installed therefore delivers every third-party record
# twice: once raw, once rendered. Track what we attached so the bridge can
# suspend it and restore it on uninstall.
# Root handler changes take logging._lock before _root_attach_condition,
# matching bridge installation and removal.
_root_relays: dict[logging.Handler, _RootRelay] = {}
_handler_owners: dict[logging.Handler, _HandlerOwnership] = {}
_root_attach_lock = threading.Lock()
_root_attach_condition = threading.Condition(_root_attach_lock)
_handler_process_pid = os.getpid()
_stdlib_bridge_active = False


def _attach_to_root(handler: logging.Handler, threshold: int) -> _RootRelay:
    """Register one handler generation, unless the bridge supersedes attachment."""
    _ensure_handler_process()
    with logging._lock, _root_attach_condition:  # type: ignore[attr-defined]
        if handler in _root_relays:
            msg = "logging handler is already registered; remove it before adding it again"
            raise ValueError(msg)
        owner = _handler_owners.get(handler)
        if owner is not None and owner.closing:
            msg = "logging handler is being closed; add it again after close completes"
            raise ValueError(msg)
        if owner is None:
            owner = _HandlerOwnership(handler)
            _handler_owners[handler] = owner
        relay = _RootRelay(handler, threshold, owner, owner.next_generation)
        owner.next_generation += 1
        owner.generations.add(relay)
        _root_relays[handler] = relay
        if not _stdlib_bridge_active:
            logging.getLogger().addHandler(relay)
        return relay


def _detach_from_root(relay: _RootRelay) -> None:
    """Detach one registration generation and retire its raw delivery side."""
    _ensure_handler_process()
    with logging._lock, _root_attach_condition:  # type: ignore[attr-defined]
        if _root_relays.get(relay._sink) is not relay:
            return
        del _root_relays[relay._sink]
        logging.getLogger().removeHandler(relay)
    # Wait outside the attach lock: the delivery in progress may itself be
    # adding or removing sinks.
    if relay.detach():
        _retire_handler_generation(relay, raw=True)


def _retire_handler_generation(relay: _RootRelay, *, raw: bool, close: bool | None = None) -> None:
    """Retire one delivery side and close after every handler generation retires."""
    _ensure_handler_process()
    with _root_attach_condition:
        if raw:
            relay._raw_retired = True
        else:
            relay._native_retired = True
            if close is not None:
                relay._close_on_release = close
        if relay._released or not relay._raw_retired or not relay._native_retired:
            return
        relay._released = True
        owner = relay._ownership
        owner.generations.discard(relay)
        should_close = relay._close_on_release and not owner.generations
        if should_close:
            owner.closing = True
        elif not owner.generations and _handler_owners.get(owner.handler) is owner:
            del _handler_owners[owner.handler]
        _root_attach_condition.notify_all()
    if not should_close:
        return
    try:
        owner.handler.close()
    finally:
        with _root_attach_condition:
            owner.closing = False
            if not owner.generations and _handler_owners.get(owner.handler) is owner:
                del _handler_owners[owner.handler]
            _root_attach_condition.notify_all()


def _set_stdlib_bridge_active(active: bool) -> None:
    """Suspend or restore root attachment for ``add()`` sinks.

    Called by ``structguru.integrations.stdlib`` on install/uninstall. While
    the bridge is active, third-party records reach ``add()`` sinks through the
    native path, so the direct root attachment is dropped; uninstalling restores
    it for every sink still registered.
    """
    global _stdlib_bridge_active
    _ensure_handler_process()
    root = logging.getLogger()
    with logging._lock, _root_attach_condition:  # type: ignore[attr-defined]
        _stdlib_bridge_active = active
        affected = list(_root_relays.values())
        for relay in affected:
            if active:
                root.removeHandler(relay)
            else:
                root.addHandler(relay)


class _SinkLock:
    """Shared by logger clones, with synchronization replaced after fork."""

    _instances: weakref.WeakSet[_SinkLock] = weakref.WeakSet()

    def __init__(self) -> None:
        _ensure_handler_process()
        self.lock = threading.Lock()
        with _root_attach_condition:
            self._instances.add(self)

    def __enter__(self) -> None:
        self.lock.acquire()

    def __exit__(self, *args: object) -> None:
        self.lock.release()


@dataclass(frozen=True)
class _SinkRegistration:
    """One ``add()`` sink, resolvable by its handler id from any logger."""

    handler: logging.Handler
    token: int
    relay: _RootRelay
    # The adding logger's registry (shared with its clones), which
    # ``remove()`` without an id empties.
    owner: dict[HandlerId, logging.Handler]
    owner_lock: _SinkLock


# Handler ids name process-wide sinks, so every logger resolves every id.
# Guarded by ``_id_counter_lock``.
_sinks: dict[HandlerId, _SinkRegistration] = {}


def _after_fork() -> None:
    """Replace locks and reconcile deliveries owned by vanished threads."""
    global _root_attach_lock, _root_attach_condition, _id_counter_lock
    global _handler_process_pid
    _root_attach_lock = threading.Lock()
    _root_attach_condition = threading.Condition(_root_attach_lock)
    _id_counter_lock = threading.Lock()
    _handler_process_pid = os.getpid()
    for sink_lock in list(_SinkLock._instances):
        sink_lock.lock = threading.Lock()
    owners = list(_handler_owners.values())
    relays = [relay for owner in owners for relay in owner.generations]
    for relay in relays:
        relay._reset_after_fork()
    for owner in owners:
        owner.closing = False
        if not owner.generations and _handler_owners.get(owner.handler) is owner:
            del _handler_owners[owner.handler]
    for relay in relays:
        if relay._detached:
            try:
                _retire_handler_generation(relay, raw=True)
            except BaseException:
                # Fork hooks and native dispatch both contain close failures.
                pass


def _ensure_handler_process() -> None:
    """Reset inherited state before a child runs a deferred native finalizer."""
    if _handler_process_pid != os.getpid():
        _after_fork()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


class _Catcher(ContextDecorator):
    """Context manager / decorator returned by ``Logger.catch``."""

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
        if not isinstance(level, str) or level.lower() not in METHOD_LEVELS:
            raise ValueError(f"unknown catch level: {level!r}")
        self._level = level.lower()
        # Like Logger.trace(), catch uses DEBUG as TRACE's effective severity.
        if self._level == "trace":
            self._level = "debug"
        self._message = message
        self._reraise = reraise

    def __enter__(self) -> None:
        pass

    def __call__(self, func: _F) -> _F:
        """Wrap synchronous calls or the awaited execution of a coroutine."""
        catcher = self._for_function(func)
        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def wrapped(*args: Any, **kwargs: Any) -> Any:
                with catcher:
                    return await func(*args, **kwargs)
                return None

            return cast(_F, wrapped)

        @functools.wraps(func)
        def wrapped_sync(*args: Any, **kwargs: Any) -> Any:
            with catcher:
                return func(*args, **kwargs)
            return None

        return cast(_F, wrapped_sync)

    def _for_function(self, func: Callable[..., Any]) -> _Catcher:
        """Log under the decorated function's module when the logger has no name.

        Frame walking would find the event loop (coroutines) or a wrapper's
        module instead of the code that owns the failure.
        """
        module = getattr(func, "__module__", None)
        if (
            self._logger.name is not None
            or not isinstance(module, str)
            or not (inspect.isroutine(func) or inspect.isclass(func))
        ):
            return self
        logger = self._logger._clone(name=module)
        return _Catcher(logger, self._exception, self._level, self._message, self._reraise)

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

    *   ``trace``, ``debug``, ``info``, ``success``, ``warning``, ``warn``,
        ``error``, ``critical``, ``fatal``, ``exception`` methods.
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

    # Sinks added through this logger or its clones, for ``remove()``.
    _handlers: dict[HandlerId, logging.Handler] = field(default_factory=dict)
    _lock: _SinkLock = field(default_factory=_SinkLock, repr=False)

    def __repr__(self) -> str:
        """Name the logger without its bound values, ``opt()`` state, or sinks.

        A logger held in a local reaches exception locals and debugging output
        through its ``repr()``.
        """
        return f"<{type(self).__module__}.{type(self).__qualname__} name={self.name!r}>"

    # -- context helpers ----------------------------------------------------

    def _clone(self, **overrides: Any) -> Logger:
        """Return a shallow copy with *overrides* applied.

        Equivalent to ``dataclasses.replace``, which re-runs ``__init__``
        for every field, but a plain ``__dict__`` copy runs in a third of the
        time. ``bind()`` and ``opt()`` sit on the per-request hot path.
        """
        clone = object.__new__(type(self))
        clone.__dict__.update(self.__dict__)
        clone.__dict__.update(overrides)
        return clone

    def bind(self, /, **kwargs: Any) -> Logger:
        """Return a *new* logger with permanently bound context.

        Only the bound fields are copied. Sinks are process-global, so the child
        deliberately shares its parent's sink registry: ``add()`` on either is
        visible to both, and ``remove()`` on either removes from both.
        """
        merged = {**self._bound, **kwargs}
        return self._clone(_bound=merged)

    @contextmanager
    def contextualize(self, /, **kwargs: Any) -> Iterator[Logger]:
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
        Decorators support synchronous and coroutine functions; coroutine
        exceptions are handled while awaiting the decorated call.
        Level names are case-insensitive; unknown names raise ``ValueError``
        when the catcher is constructed. ``trace`` logs at ``DEBUG``, matching
        ``trace``.
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
        return self._clone(_opt_exc_info=exc_info, _opt_stack_info=stack_info)

    # -- logging methods ----------------------------------------------------

    def trace(self, message: Any, /, *args: Any, **kwargs: Any) -> None:
        """Log at ``DEBUG`` level.

        A loguru-compatible alias; structguru has no separate ``TRACE`` level,
        so records emitted here render as ``DEBUG``.
        """
        self._log("debug", message, args, kwargs)

    def debug(self, message: Any, /, *args: Any, **kwargs: Any) -> None:
        """Log at ``DEBUG`` level.

        *message* is brace-formatted with *args* / *kwargs* when any are given
        (``exc_info`` and ``stack_info`` do not count); keys consumed by the
        format string are dropped, and the rest become structured fields. A
        placeholder naming a sensitive key is interpolated as ``[REDACTED]``.
        *message* is positional-only, so ``message=`` and ``self=`` are fields.
        Formatting failures log the raw template with a one-shot warning.
        """
        self._log("debug", message, args, kwargs)

    def info(self, message: Any, /, *args: Any, **kwargs: Any) -> None:
        """Log at ``INFO`` level."""
        self._log("info", message, args, kwargs)

    def success(self, message: Any, /, *args: Any, **kwargs: Any) -> None:
        """Log at ``INFO`` level.

        A loguru-compatible alias; structguru has no separate ``SUCCESS``
        level, so records emitted here render as ``INFO``.
        """
        self._log("info", message, args, kwargs)

    def warning(self, message: Any, /, *args: Any, **kwargs: Any) -> None:
        """Log at ``WARNING`` level."""
        self._log("warning", message, args, kwargs)

    def warn(self, message: Any, /, *args: Any, **kwargs: Any) -> None:
        """Log at ``WARNING`` level — alias of ``warning``."""
        self.warning(message, *args, **kwargs)

    def error(self, message: Any, /, *args: Any, **kwargs: Any) -> None:
        """Log at ``ERROR`` level.

        Pass ``exc_info=True`` (or use ``exception``) to attach the active
        exception.
        """
        self._log("error", message, args, kwargs)

    def critical(self, message: Any, /, *args: Any, **kwargs: Any) -> None:
        """Log at ``CRITICAL`` level."""
        self._log("critical", message, args, kwargs)

    def fatal(self, message: Any, /, *args: Any, **kwargs: Any) -> None:
        """Log at ``CRITICAL`` level — alias of ``critical``."""
        self.critical(message, *args, **kwargs)

    def exception(self, message: Any, /, *args: Any, **kwargs: Any) -> None:
        """Log at ``ERROR`` level with exception information."""
        kwargs.setdefault("exc_info", True)
        self._log("error", message, args, kwargs)

    def _log(
        self,
        method: str,
        message: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        levelno: int | None = None,
        created: float | None = None,
        *,
        _bound_fields: dict[str, Any] | None = None,
    ) -> None:
        """Submit one record natively, falling back to Python capture and hooks."""
        # One coherent snapshot follows the record through every processing stage.
        runtime = _runtime.current_runtime()
        if runtime is None:
            return
        outcome = None
        if (
            type(runtime.writer) is _runtime._NATIVE_WRITER_TYPE
            and type(_runtime._callable_dispatcher) is _runtime._NATIVE_DISPATCHER_TYPE
        ):
            outcome = _runtime.submit_plain(
                runtime,
                runtime.writer,
                runtime.config,
                runtime.record_filter,
                runtime.level_threshold,
                runtime.enrich,
                runtime.fused,
                _safe_format,
                self.name,
                self._bound if _bound_fields is None else _bound_fields,
                self._opt_exc_info,
                self._opt_stack_info,
                method,
                message,
                args,
                kwargs,
                _ctx.get,
                sys._getframe,
                _runtime._callable_dispatcher,
                levelno,
                created,
            )
        if outcome is not None:
            if outcome:
                _runtime._note_enqueue_outcome(outcome)
            return
        self._log_slow(
            runtime, method, message, args, kwargs, levelno, created, _bound_fields=_bound_fields
        )

    def _log_slow(
        self,
        runtime: _runtime._RuntimeState,
        method: str,
        message: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        levelno: int | None = None,
        created: float | None = None,
        *,
        _bound_fields: dict[str, Any] | None = None,
    ) -> None:
        """Handle exception, stack, enrichment, and secondary-destination paths.

        The stdlib bridge passes a record's own ``levelno``, which the level
        threshold, sampling, and callable-sink routing use instead of
        *method*'s number, and its ``created`` time (Unix seconds) for the
        ``timestamp`` field. ``_bound_fields`` supplies an already assembled
        per-record mapping without cloning its cached facade.
        """
        level_number = METHOD_LEVELS.get(method, 20) if levelno is None else levelno
        if level_number < runtime.level_threshold:
            return

        stack_info = kwargs.get("stack_info") or self._opt_stack_info
        # Read before placeholders consume kwargs: "{exc_info}" must not drop it.
        exc_info = kwargs.get("exc_info", self._opt_exc_info)
        formatted_msg, consumed_keys = _safe_format(message, args, kwargs, runtime)
        for key in consumed_keys:
            kwargs.pop(key, None)

        record_filter = runtime.record_filter
        if record_filter is not None and not record_filter.allow(formatted_msg, level_number):
            return

        name = self.name if self.name is not None else _caller_module_name()
        # Exception and stack capture are Python-owned (frame walking). The
        # renderer adds "exception" as the last field and places "stack"
        # between "service" and "message" like StackInfoRenderer.
        exception = (
            (_runtime.build_exception_field(exc_info, runtime) or None) if exc_info else None
        )
        if isinstance(stack_info, str):
            stack = stack_info
        elif stack_info:
            stack = _runtime.format_stack()
        else:
            stack = None

        # Contextvars append after (and never override) event fields, matching
        # structlog's merge_contextvars setdefault semantics; the writer merges.
        bound = self._bound if _bound_fields is None else _bound_fields
        contextvars = _ctx.get()
        field_names: tuple[str, ...] = ()
        if runtime.enrich:
            fields = _runtime.merge_fields(bound, kwargs, contextvars)
            if runtime.otel:
                add_otel_context(None, method, fields)
            if exception is not None:
                fields["exception"] = exception
            _runtime.notify_metrics(method, formatted_msg, fields, runtime)
            field_names = tuple(fields)
            bound, kwargs, contextvars, exception = fields, _NO_FIELDS, _NO_FIELDS, None
        _runtime.render_and_enqueue(
            runtime,
            name,
            method,
            formatted_msg,
            bound,
            kwargs,
            contextvars,
            exception,
            stack,
            created,
            level_number,
            field_names,
            exc_info,
        )

    # -- sink (handler) management ------------------------------------------

    def add(self, sink: Sink, *, level: str | int | None = None) -> HandlerId:
        """Add a sink that receives rendered log lines.

        Parameters
        ----------
        sink:
            A callable accepting a single string (the rendered log line),
            a file path (``str``/``Path``), a stream-like object with ``.write``,
            or a ``logging.Handler`` (for third-party stdlib interop).
        level:
            Minimum level for this sink: a case-insensitive name or a
            non-negative integer, validated like ``configure(level=...)``
            (``ValueError`` otherwise). Applies to every sink type — it gates
            native delivery at the dispatcher. Defaults to all levels.

        Returns
        -------
        HandlerId
            A process-wide identifier that any logger can pass to ``remove``.

        Note
        ----
        Every sink receives both structguru records and third-party stdlib records.
        Native records are delivered via the bounded callable dispatch queue;
        configured backpressure and flush semantics therefore apply uniformly.
        Logs emitted inside a sink callback, on either delivery path, reach the
        native writer but bypass all callable/``add()`` sinks to prevent
        recursive delivery and deadlocks. Callback failures, including
        ``BaseException`` subclasses, are isolated on the worker so later
        records and lifecycle operations can complete.

        A ``logging.Handler`` is used as given: its own formatter and
        filters apply to each record, whose message is the rendered line, and
        its level and formatter are left unchanged for the loggers it already
        serves. Only ``level`` gates what this sink receives.
        ``remove`` closes it.
        Registering the same handler again before removing it raises
        ``ValueError``; handler ownership is process-wide.

        Third-party records arrive raw (unrendered) via the stdlib root logger.
        Install the bridge (``structguru.integrations.stdlib``) to receive them
        rendered and redacted through the native path instead; while it is
        installed the raw root-logger delivery is suspended, so a sink never
        sees the same record twice. ``remove`` waits for raw deliveries in
        progress as it does for native ones.
        """
        # Validate before opening a file or registering anything. Unset means
        # NOTSET (0) — "all levels", as documented. Inheriting the root logger's
        # level instead would silently gate the stdlib path at WARNING (the
        # default on an unconfigured root) while the native path still
        # delivered everything.
        threshold = logging.NOTSET if level is None else _level_number(level)
        handler = _make_handler(sink)
        if handler is not sink:
            # Handlers built here pass the rendered line through verbatim.
            handler.setLevel(threshold)
            handler.setFormatter(logging.Formatter("%(message)s"))

        native_callback = (
            sink
            if callable(sink) and not isinstance(sink, logging.Handler)
            else functools.partial(_emit_rendered, handler)
        )
        # Reserve handler ownership before native registration, so duplicate
        # registrations cannot partially succeed or change the original relay.
        relay = _attach_to_root(handler, threshold)
        try:
            native_token = _runtime.add_callable_sink(
                native_callback,
                min_level=threshold,
                level_callback=(
                    None
                    if callable(sink) and not isinstance(sink, logging.Handler)
                    else functools.partial(_emit_rendered, handler)
                ),
            )
        except BaseException:
            _detach_from_root(relay)
            _retire_handler_generation(relay, raw=False, close=handler is not sink)
            raise

        with _id_counter_lock:
            handler_id = next(_id_counter)
            _sinks[handler_id] = _SinkRegistration(
                handler, native_token, relay, self._handlers, self._lock
            )
        with self._lock:
            self._handlers[handler_id] = handler
        return handler_id

    def remove(self, handler_id: HandlerId | None = None) -> None:
        """Remove a sink by its *handler_id*.

        Handler ids are process-wide, so any logger removes a sink added
        through another. An id that names no active sink raises
        ``ValueError`` and a non-integer id ``TypeError``, as in loguru.

        If *handler_id* is ``None``, all sinks added via this logger instance
        (including loggers derived from it with ``bind()`` or ``opt()``) are
        removed.
        """
        if handler_id is None:
            with self._lock:
                handler_ids = list(self._handlers)
                self._handlers.clear()
            with _id_counter_lock:
                registrations = [
                    registration
                    for sink_id in handler_ids
                    if (registration := _sinks.pop(sink_id, None)) is not None
                ]
        else:
            if not isinstance(handler_id, int):
                msg = (
                    "Invalid handler id, it should be an integer as returned by the "
                    f"'add()' method (or None), not: '{type(handler_id).__name__}'"
                )
                raise TypeError(msg)
            with _id_counter_lock:
                registration = _sinks.pop(handler_id, None)
            if registration is None:
                msg = f"There is no existing handler with id {handler_id:d}"
                raise ValueError(msg)
            with registration.owner_lock:
                registration.owner.pop(handler_id, None)
            registrations = [registration]
        for registration in registrations:
            _detach_from_root(registration.relay)
            # Close only after this generation's native deliveries and every
            # other generation of the same handler have retired.
            _runtime.remove_callable_sink(
                registration.token,
                finalizer=functools.partial(
                    _retire_handler_generation, registration.relay, raw=False
                ),
            )


logger = Logger()
