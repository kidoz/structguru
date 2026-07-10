"""Native extension access and logging runtime configuration.

Also owns the native render/enqueue path used by :class:`structguru.core.Logger`.
Use :func:`configure` to customize rendering, filtering, and sinks. Structured
fields are redacted and rendered in Rust, then handed to a background writer
thread for off-thread I/O.
"""

from __future__ import annotations

import atexit
import importlib
import io
import json
import math
import os
import queue
import sys
import threading
import traceback
import warnings
from collections.abc import Callable
from types import TracebackType
from typing import Any, Protocol, cast

# method name -> numeric level (mirrors logging levels; TRACE/SUCCESS folded)
_LEVEL_NUM: dict[str, int] = {
    "trace": 5,
    "debug": 10,
    "info": 20,
    "success": 20,
    "warning": 30,
    "warn": 30,
    "error": 40,
    "exception": 40,
    "critical": 50,
    "fatal": 50,
}


class _NativeWriter(Protocol):
    def try_enqueue(self, message: str) -> bool: ...

    def enqueue_blocking(self, message: str) -> bool: ...

    def flush(self) -> None: ...

    def close(self) -> None: ...

    def abandon(self) -> None: ...

    def messages(self) -> list[str]: ...

    def metrics(self) -> dict[str, Any]: ...


class _RustModule(Protocol):
    def normalize_level(self, level: str) -> str: ...

    def syslog_severity(self, canonical_level: str) -> int: ...

    def render_line(
        self,
        fields: dict[str, Any],
        logger: str,
        level: str,
        service: str,
        message: str,
        timestamp: str | None = None,
        sensitive_keys: list[str] | None = None,
        sensitive_patterns: list[str] | None = None,
        stack: str | None = None,
        pattern_replacement: str | None = None,
    ) -> str: ...

    def render_line_with_config(
        self,
        fields: dict[str, Any],
        logger: str,
        level: str,
        service: str,
        message: str,
        config: Any,
        timestamp: str | None = None,
        sensitive_keys: list[str] | None = None,
        stack: str | None = None,
    ) -> str: ...

    def render_line_console(
        self,
        fields: dict[str, Any],
        logger: str,
        level: str,
        service: str,
        message: str,
        colors: bool,
        timestamp: str | None = None,
        sensitive_keys: list[str] | None = None,
        sensitive_patterns: list[str] | None = None,
        stack: str | None = None,
        pattern_replacement: str | None = None,
    ) -> str: ...

    def render_console_with_config(
        self,
        fields: dict[str, Any],
        logger: str,
        level: str,
        service: str,
        message: str,
        colors: bool,
        config: Any,
        timestamp: str | None = None,
        sensitive_keys: list[str] | None = None,
        stack: str | None = None,
    ) -> str: ...

    def validate_patterns(self, patterns: list[str]) -> int: ...

    def RedactionConfig(self, patterns: list[str], replacement: str | None = None) -> Any: ...

    def NativeFilter(
        self,
        sample_rate: float = ...,
        sample_max_level: str | None = ...,
        rate_limit_max: int | None = ...,
        rate_limit_period: float = ...,
    ) -> Any: ...

    def _NativeStringWriter(
        self,
        maxsize: int,
        paused: bool = ...,
        fail_after: int | None = ...,
        target: str = ...,
        file_path: str | None = ...,
        file_max_bytes: int = ...,
        file_backup_count: int = ...,
        also_stdout: bool = ...,
    ) -> _NativeWriter: ...


def _load_rust_module() -> _RustModule | None:
    try:
        module = importlib.import_module("structguru._rust")
    except ModuleNotFoundError as exc:
        if exc.name == "structguru._rust":
            return None
        raise
    return cast(_RustModule, module)


_RUST = _load_rust_module()


def native_available() -> bool:
    """Return whether the compiled native extension is importable."""
    return _RUST is not None


def normalize_level(level: str) -> str | None:
    """Normalize *level* through the native helper when available."""
    if _RUST is None:
        return None
    return _RUST.normalize_level(level)


def syslog_severity(canonical_level: str) -> int | None:
    """Map *canonical_level* through the native helper when available."""
    if _RUST is None:
        return None
    return _RUST.syslog_severity(canonical_level)


# -- native render/enqueue fast path (experimental, opt-in) -----------------

_state_lock = threading.Lock()
_enabled = False
_writer: _NativeWriter | None = None
_service = "app"
_maxsize = 0
_target = "stdout"
_overflow = "block"
_level_threshold = _LEVEL_NUM["info"]
_otel = False
_sensitive_keys: list[str] | None = None
_sensitive_patterns: list[str] | None = None
_redaction_config: Any = None  # compiled RedactionConfig, None when no patterns configured
_filter: Any = None  # NativeFilter, None when no sampling/rate-limit configured
_exception_config: dict[str, Any] | None = None  # structured-exception knobs, None = string
_metric_processor: Any = None  # structlog-style processor invoked per kept record
_sentry_processor: Any = None  # structlog-style processor invoked per kept record (Sentry)
# Console mode: json=False renders colored human-readable lines instead of JSON.
_console = False
_colors = False
# Callable sinks: dispatched on a dedicated daemon thread (never the Rust worker,
# which must not touch the GIL). Each entry is (callable, min_level_num).
_callable_sinks: list[tuple[Callable[[str], None], int]] | None = None
_dispatch_queue: queue.Queue[str] | None = None
_dispatch_thread: threading.Thread | None = None
# Synchronous stream sink: when set (by configure_structlog), rendered lines are
# written to this stream synchronously on the caller's thread IN ADDITION to the
# Rust writer. This preserves the pre-1.0 contract that logger output is available
# on the configured stream immediately after the call (no flush needed).
_stream_sink: Any = None
_dispatch_stop = threading.Event()
_hooks_registered = False
_drop_count = 0


def is_native_enabled() -> bool:
    """True when native mode is on and the extension is loaded."""
    return _enabled and _RUST is not None


def is_below_level(method: str) -> bool:
    """True when a call at *method* is below the native threshold (drop it)."""
    return _LEVEL_NUM.get(method, _LEVEL_NUM["info"]) < _level_threshold


def set_native_level(level: str) -> None:
    """Adjust the native level threshold at runtime (per-process)."""
    global _level_threshold
    _level_threshold = _LEVEL_NUM.get(level.lower(), _LEVEL_NUM["info"])


def otel_enabled() -> bool:
    """True when OTel trace-context injection is enabled for native mode."""
    return _otel


def sensitive_keys() -> list[str] | None:
    """Custom redaction keys for native mode, or None for the defaults."""
    return _sensitive_keys


def sensitive_patterns() -> list[str] | None:
    """Custom regex value-patterns for native mode, or None if unset."""
    return _sensitive_patterns


def should_render(method: str, message: str) -> bool:
    """True when the pre-render filter (sampling/rate-limit) keeps the record.

    Returns ``True`` immediately when no filter is configured, so the default
    native path pays no extra cost. Mirrors the shape of :func:`is_below_level`.
    """
    if _filter is None:
        return True
    return bool(_filter.allow(message, method))


def notify_metrics(method: str, message: str, fields: dict[str, Any]) -> None:
    """Invoke the configured metric processor for a kept record.

    Called on the caller's thread (never the writer thread) with a
    pre-``EventRenamer``-shaped event dict, matching what ``MetricProcessor``
    sees on the standard path. No-op when no processor is configured; hook
    errors are swallowed — metrics must never break logging.
    """
    if _metric_processor is None:
        return
    try:
        _metric_processor(None, method, {"event": message, **fields})
    except Exception:  # noqa: BLE001 - hooks must never break logging
        pass


def notify_sentry(method: str, message: str, fields: dict[str, Any], exc_info: Any = None) -> None:
    """Invoke the configured Sentry processor for a kept record.

    Mirrors :func:`notify_metrics` but passes the raw ``exc_info`` (in the shape
    ``SentryProcessor._resolve_exception`` expects: ``True``, a ``(type, exc, tb)``
    tuple, or a ``BaseException``) so exception capture works. When redaction is
    configured (``sensitive_keys`` or ``sensitive_patterns``), the
    ``REDACTED_MARKER_KEY`` is injected so the processor's ``require_redaction``
    guard recognizes that redaction already ran in Rust. No-op when no processor
    is configured; hook errors are swallowed — Sentry must never break logging.
    """
    if _sentry_processor is None:
        return
    event_dict: dict[str, Any] = {"event": message, **fields}
    if exc_info is not None:
        event_dict["exc_info"] = exc_info
    # Native Rust redaction already ran; mark the dict so SentryProcessor's
    # require_redaction guard (which checks REDACTED_MARKER_KEY) recognizes it.
    if _sensitive_keys is not None or _sensitive_patterns is not None:
        from structguru.redaction import REDACTED_MARKER_KEY

        event_dict.setdefault(REDACTED_MARKER_KEY, True)
    try:
        _sentry_processor(None, method, event_dict)
    except Exception:  # noqa: BLE001 - hooks must never break logging
        pass


def build_exception_field(exc_info: Any) -> str | dict[str, Any] | None:
    """Build the ``exception`` field value for a native record.

    Returns the structured dict when ``structured_exceptions`` is enabled
    (matching :class:`structguru.exceptions.ExceptionDictProcessor` exactly, or
    ``None`` when *exc_info* does not resolve); otherwise the formatted
    traceback string (matching ``format_exc_info``). Frame walking, locals
    capture, and ``repr`` are Python-owned — the native renderer only
    serializes the result.
    """
    if _exception_config is None:
        return format_exception(exc_info)

    from structguru.exceptions import build_exception_dict

    return build_exception_dict(exc_info, **_exception_config)


def format_stack() -> str:
    """Format the caller's stack like structlog's ``StackInfoRenderer``.

    Matches ``structlog._frames._format_stack`` (the logging-style header, the
    trailing-newline strip) but skips *structguru* frames the way structlog
    skips its own, so the rendered stack ends at the user's calling frame
    instead of at structguru internals.
    """
    frame: Any = sys._getframe()
    while frame is not None and frame.f_globals.get("__name__", "").startswith("structguru"):
        frame = frame.f_back
    sio = io.StringIO()
    sio.write("Stack (most recent call last):\n")
    traceback.print_stack(frame, file=sio)
    stack = sio.getvalue()
    return stack[:-1] if stack.endswith("\n") else stack


def format_exception(exc_info: Any) -> str:
    """Format ``exc_info`` to a traceback string matching structlog's output.

    Accepts ``True`` (use the current exception), a ``BaseException`` instance,
    or a ``(type, value, tb)`` tuple.
    """
    if exc_info is True:
        exc_info = sys.exc_info()
    elif isinstance(exc_info, BaseException):
        exc_info = (type(exc_info), exc_info, exc_info.__traceback__)
    if not exc_info or exc_info[0] is None:
        return ""
    exc_type, exc_value, exc_tb = exc_info
    assert isinstance(exc_value, BaseException)
    tb = cast("TracebackType | None", exc_tb)
    return "".join(traceback.format_exception(type(exc_value), exc_value, tb)).rstrip("\n")


def configure(
    *,
    service: str = "app",
    maxsize: int = 0,
    target: str = "stdout",
    overflow: str = "block",
    level: str = "INFO",
    otel: bool = False,
    sensitive_keys: list[str] | None = None,
    sensitive_patterns: list[str] | None = None,
    pattern_replacement: str = "[REDACTED]",
    sample_rate: float = 1.0,
    sample_max_level: str | None = None,
    rate_limit_max: int | None = None,
    rate_limit_period: float = 60.0,
    metric_processor: Any = None,
    sentry_processor: Any = None,
    structured_exceptions: bool = False,
    exception_include_locals: bool = False,
    exception_max_frames: int = 20,
    exception_max_local_repr: int = 200,
    json: bool = True,
    colors: bool | None = None,
    file_path: str | None = None,
    file_max_bytes: int = 50 * 1024 * 1024,
    file_backup_count: int = 5,
    also_stdout: bool = False,
    callable_sinks: list[Callable[[str], None]] | None = None,
    stream_sink: Any = None,
) -> None:
    """Configure the native renderer and background writer.

    ``target`` selects the background writer's sink: ``"stdout"`` (default,
    12-factor) or ``"memory"`` (records lines for inspection/tests).

    ``maxsize=0`` is an unbounded queue. With a positive ``maxsize``, ``overflow``
    governs a full queue: ``"block"`` (default, no loss — the caller waits with the
    GIL released) or ``"drop"`` (drop the new record, count it, and emit a
    rate-limited warning; see :func:`native_metrics`).

    ``sensitive_patterns`` is a list of regex source strings applied (in addition
    to key-based redaction) to every string value. Rust's ``regex`` engine
    guarantees linear-time matching and rejects backreferences and look-around:
    an unsupported pattern raises ``ValueError`` here, at setup time.
    ``pattern_replacement`` is the substitution text for pattern matches and
    supports capture-group expansion (``$1``, ``${name}``; ``$$`` for a literal
    ``$``), so look-behind-style patterns can be rewritten to preserve their
    prefix — e.g. pattern ``password=(\\S+)`` with replacement
    ``password=[REDACTED]``.

    ``sample_rate`` (0.0–1.0) and ``rate_limit_max``/``rate_limit_period`` add
    pre-render filters: dropped records cost zero rendering. ``sampled`` and
    ``rate_limited`` counters are reported separately from the writer's transport
    ``dropped`` counter (see :func:`native_metrics`). ``sample_max_level``
    restricts sampling to records at or below that level (more severe records
    always pass) — the native analog of wrapping ``SamplingProcessor`` in
    ``ConditionalProcessor(max_level=...)``.

    ``metric_processor`` is a structlog-style processor (e.g.
    :class:`structguru.metrics.MetricProcessor`) invoked for every *kept* record
    with ``(None, method, {"event": message, **fields})`` before rendering.
    Records dropped by level filtering, sampling, or rate limiting never reach
    it. Exceptions raised by the processor are swallowed.

    ``structured_exceptions=True`` renders the ``exception`` field as the
    structured dict produced by
    :class:`structguru.exceptions.ExceptionDictProcessor` (with the
    ``exception_*`` knobs mirroring the processor's parameters and
    ``sensitive_keys`` reused for locals redaction) instead of the formatted
    traceback string.

    ``json=False`` selects the native console renderer (colored, human-readable
    dev output) instead of JSON. ``colors`` defaults to ``sys.stdout.isatty()``
    in console mode; set it explicitly to override. The console format is
    structguru's own stable dev format, not a structlog ``ConsoleRenderer`` clone.

    ``file_path`` enables a native rotating-file sink (append mode). Defaults
    mirror :class:`logging.handlers.RotatingFileHandler`: ``file_max_bytes=50MB``,
    ``file_backup_count=5``. Set ``also_stdout=True`` to mirror output to both
    the file and stdout (e.g. container + persistent log).

    ``callable_sinks`` is a list of ``Callable[[str], None]`` invoked with each
    *rendered* line. They run on a dedicated daemon thread (never the Rust
    writer thread, which must not touch the GIL), so a blocking callable cannot
    deadlock the logging path. Callable errors are swallowed.

    Registers shutdown (``atexit``) and fork (``os.register_at_fork``) handlers so
    buffered records are flushed on exit and the background writer is respawned in
    forked children (gunicorn/celery prefork) instead of deadlocking.
    """
    global _enabled, _writer, _service, _maxsize, _target, _overflow
    global _level_threshold, _otel, _sensitive_keys, _sensitive_patterns
    global _redaction_config, _filter, _exception_config, _metric_processor, _sentry_processor
    global _console, _colors, _callable_sinks, _dispatch_queue, _dispatch_thread, _stream_sink
    if _RUST is None:
        msg = "native extension is not available"
        raise RuntimeError(msg)
    if overflow not in ("block", "drop"):
        msg = f"overflow must be 'block' or 'drop', not {overflow!r}"
        raise ValueError(msg)
    if not math.isfinite(sample_rate) or not 0.0 <= sample_rate <= 1.0:
        msg = f"sample_rate must be between 0.0 and 1.0, got {sample_rate}"
        raise ValueError(msg)
    if rate_limit_max is not None and rate_limit_max < 1:
        msg = f"rate_limit_max must be >= 1, got {rate_limit_max}"
        raise ValueError(msg)
    if not math.isfinite(rate_limit_period) or rate_limit_period <= 0:
        msg = f"rate_limit_period must be > 0, got {rate_limit_period}"
        raise ValueError(msg)
    if sample_max_level is not None and sample_max_level.lower() not in _LEVEL_NUM:
        msg = f"sample_max_level must be a known level name, got {sample_max_level!r}"
        raise ValueError(msg)
    if metric_processor is not None and not callable(metric_processor):
        msg = f"metric_processor must be callable, got {type(metric_processor)!r}"
        raise TypeError(msg)
    if sentry_processor is not None and not callable(sentry_processor):
        msg = f"sentry_processor must be callable, got {type(sentry_processor)!r}"
        raise TypeError(msg)

    # Validate regex patterns against Rust's engine before enabling. Rust's
    # `regex` guarantees linear-time matching and therefore rejects
    # backreferences and look-around; fail loudly at setup time — redaction
    # that silently differs from what was configured is worse than an error.
    if sensitive_patterns:
        try:
            _RUST.validate_patterns(list(sensitive_patterns))
        except ValueError as exc:
            msg = (
                f"unsupported sensitive_patterns regex ({exc}). Rust's regex engine "
                "guarantees linear-time matching and does not support backreferences "
                "or look-around. Rewrite the pattern with a capture group instead, "
                "e.g. lookbehind '(?<=password=)\\S+' becomes 'password=(\\S+)'."
            )
            raise ValueError(msg) from exc
        new_config = _RUST.RedactionConfig(list(sensitive_patterns), pattern_replacement)
        new_patterns: list[str] | None = list(sensitive_patterns)
    else:
        new_config = None
        new_patterns = None

    new_exception_config: dict[str, Any] | None = None
    if structured_exceptions:
        new_exception_config = {
            "include_locals": exception_include_locals,
            "max_frames": exception_max_frames,
            "max_local_repr": exception_max_local_repr,
            # Reuse the record-redaction keys for locals redaction; the
            # processor compares lower-cased names against a frozenset.
            "sensitive_keys": (
                frozenset(k.lower() for k in sensitive_keys)
                if sensitive_keys is not None
                else None
            ),
        }

    new_filter: Any = None
    if sample_rate < 1.0 or rate_limit_max is not None:
        new_filter = _RUST.NativeFilter(
            sample_rate=sample_rate,
            sample_max_level=sample_max_level.lower() if sample_max_level else None,
            rate_limit_max=rate_limit_max,
            rate_limit_period=rate_limit_period,
        )
        if new_filter.is_empty():
            new_filter = None

    # Validate callable sinks: each must be callable.
    if callable_sinks is not None:
        for i, fn in enumerate(callable_sinks):
            if not callable(fn):
                msg = f"callable_sinks[{i}] must be callable, got {type(fn)!r}"
                raise TypeError(msg)

    # Resolve console/colors.
    new_console = not json
    new_colors = colors if colors is not None else (sys.stdout.isatty() if new_console else False)

    # Stop any existing dispatch thread before re-enabling.
    _stop_dispatch_thread()

    with _state_lock:
        if _writer is not None:
            _writer.close()
        _maxsize = maxsize
        _target = target
        _service = service
        _overflow = overflow
        _level_threshold = _LEVEL_NUM.get(level.lower(), _LEVEL_NUM["info"])
        _otel = otel
        _sensitive_keys = list(sensitive_keys) if sensitive_keys is not None else None
        _sensitive_patterns = new_patterns
        _redaction_config = new_config
        _filter = new_filter
        _exception_config = new_exception_config
        _metric_processor = metric_processor
        _sentry_processor = sentry_processor
        _console = new_console
        _colors = new_colors
        _writer = _RUST._NativeStringWriter(
            maxsize,
            target=target,
            file_path=file_path,
            file_max_bytes=file_max_bytes,
            file_backup_count=file_backup_count,
            also_stdout=also_stdout,
        )
        _callable_sinks = (
            [(fn, _LEVEL_NUM["debug"]) for fn in callable_sinks] if callable_sinks else None
        )
        _dispatch_queue = queue.Queue() if _callable_sinks else None
        _stream_sink = stream_sink
        _enabled = True

    # Start the dispatch thread outside the state lock.
    if _callable_sinks:
        _start_dispatch_thread()
    _register_lifecycle_hooks()


def _register_lifecycle_hooks() -> None:
    """Register atexit + fork handlers exactly once."""
    global _hooks_registered
    if _hooks_registered:
        return
    atexit.register(_atexit_close)
    # register_at_fork is POSIX-only; on Windows (spawn) there is nothing to do.
    if hasattr(os, "register_at_fork"):
        os.register_at_fork(
            before=_before_fork,
            after_in_child=_after_in_child,
        )
    _hooks_registered = True


def _atexit_close() -> None:
    """Drain + stop the writer on interpreter shutdown (best effort)."""
    with _state_lock:
        if _writer is not None:
            _writer.close()


def _before_fork() -> None:
    """Flush buffered records in the parent before forking (best effort)."""
    if _writer is not None:
        _writer.flush()


def _after_in_child() -> None:
    """Respawn the writer in the child: its worker thread did not survive fork."""
    global _writer, _dispatch_thread, _dispatch_stop
    if not _enabled or _RUST is None:
        return
    old = _writer
    if old is not None:
        old.abandon()  # never join the parent's (now absent) worker thread
    _writer = _RUST._NativeStringWriter(_maxsize, target=_target)
    # The dispatch thread also died in the fork; respawn it if callable sinks are active.
    _dispatch_stop.clear()
    _dispatch_thread = None
    if _callable_sinks and _dispatch_queue is not None:
        _dispatch_thread = threading.Thread(target=_dispatch_loop, daemon=True)
        _dispatch_thread.start()


def _start_dispatch_thread() -> None:
    """Start the callable-sink dispatch daemon thread."""
    global _dispatch_thread, _dispatch_stop
    _dispatch_stop.clear()
    _dispatch_thread = threading.Thread(target=_dispatch_loop, daemon=True)
    _dispatch_thread.start()


def _stop_dispatch_thread() -> None:
    """Signal the dispatch thread to stop and join it (best effort)."""
    global _dispatch_thread
    _dispatch_stop.set()
    thread = _dispatch_thread
    if thread is not None and thread.is_alive():
        thread.join(timeout=2.0)
    _dispatch_thread = None


def _dispatch_loop() -> None:
    """Drain the dispatch queue and invoke each callable sink.

    Runs on a dedicated daemon thread so callable sinks (which acquire the GIL)
    never interact with the Rust writer thread. Per-sink level filtering and
    error isolation happen here.
    """
    assert _dispatch_queue is not None  # set when _callable_sinks is non-empty
    while not _dispatch_stop.is_set():
        try:
            line = _dispatch_queue.get(timeout=0.5)
        except queue.Empty:
            continue
        sinks = _callable_sinks
        if not sinks:
            continue
        # Extract the level for per-sink filtering (best effort; bad JSON → INFO).
        try:
            level_str = json.loads(line).get("level", "INFO")
        except (json.JSONDecodeError, TypeError):
            level_str = "INFO"
        level_num = _LEVEL_NUM.get(str(level_str).lower(), _LEVEL_NUM["info"])
        for fn, min_level in sinks:
            if level_num >= min_level:
                try:
                    fn(line)
                except Exception:  # noqa: BLE001 - callable errors never break logging
                    pass


def disable_native() -> None:
    """Turn native mode off and stop the background writer."""
    global _enabled, _writer, _redaction_config, _filter, _exception_config, _metric_processor
    global _sentry_processor, _callable_sinks, _dispatch_queue, _stream_sink, _console, _colors
    _stop_dispatch_thread()
    with _state_lock:
        _enabled = False
        if _writer is not None:
            _writer.close()
            _writer = None
        _redaction_config = None
        _filter = None
        _exception_config = None
        _metric_processor = None
        _sentry_processor = None
        _console = False
        _colors = False
        _callable_sinks = None
        _dispatch_queue = None
        _stream_sink = None


def render_and_enqueue(
    fields: dict[str, Any],
    logger: str,
    level: str,
    message: str,
    stack: str | None = None,
) -> bool:
    """Render one record natively and enqueue it; returns False if dropped.

    The timestamp is generated inside the Rust core (cached per second), so no
    Python-side time formatting happens on the hot path. When value-pattern
    redaction is configured, the pre-built ``RedactionConfig`` is reused so no
    per-record regex compilation occurs.
    """
    assert _RUST is not None and _writer is not None  # guarded by is_native_enabled
    if _console:
        if _redaction_config is not None:
            rendered = _RUST.render_console_with_config(
                fields,
                logger,
                level,
                _service,
                message,
                _colors,
                _redaction_config,
                None,
                _sensitive_keys,
                stack,
            )
        else:
            rendered = _RUST.render_line_console(
                fields,
                logger,
                level,
                _service,
                message,
                _colors,
                None,
                _sensitive_keys,
                None,
                stack,
            )
    elif _redaction_config is not None:
        rendered = _RUST.render_line_with_config(
            fields,
            logger,
            level,
            _service,
            message,
            _redaction_config,
            None,
            _sensitive_keys,
            stack,
        )
    else:
        rendered = _RUST.render_line(
            fields, logger, level, _service, message, None, _sensitive_keys, None, stack
        )
    line = rendered + "\n"
    # Synchronous stream sink (used by configure_structlog for backward compat).
    if _stream_sink is not None:
        try:
            _stream_sink.write(line)
        except Exception:  # noqa: BLE001 - stream errors must never break logging
            pass
    if _overflow == "block":
        enqueued = _writer.enqueue_blocking(line)
    else:
        enqueued = _writer.try_enqueue(line)
    if not enqueued:
        _note_drop()
    # Dispatch to callable sinks (non-blocking; the dispatch thread handles delivery).
    if _dispatch_queue is not None:
        try:
            _dispatch_queue.put_nowait(line)
        except queue.Full:
            _note_drop()
    return enqueued


def _note_drop() -> None:
    """Emit a rate-limited warning when the queue drops a record (drop mode)."""
    global _drop_count
    _drop_count += 1
    if _drop_count == 1 or _drop_count % 1000 == 0:
        warnings.warn(
            f"structguru native logging dropped {_drop_count} record(s): queue full",
            stacklevel=3,
        )


def _reset_drop_count() -> None:
    """Reset the drop counter (used by tests)."""
    global _drop_count
    _drop_count = 0


def flush_native() -> None:
    """Block until the writer has drained (used by shutdown/tests)."""
    if _writer is not None:
        _writer.flush()


def drain_messages() -> list[str]:
    """Return all lines the writer has flushed to its in-memory sink (tests)."""
    return _writer.messages() if _writer is not None else []


def native_metrics() -> dict[str, Any] | None:
    """Return writer + filter metrics.

    Writer counters: ``enqueued``, ``dropped`` (queue-full), ``written``, ``depth``,
    etc. When a pre-render filter is active, ``sampled`` and ``rate_limited`` are
    added — these are distinct from the transport ``dropped`` counter.
    """
    if _writer is None:
        return None
    metrics = dict(_writer.metrics())
    if _filter is not None:
        metrics.update(_filter.stats())
    return metrics


def _maybe_configure_from_env() -> None:
    """Auto-configure native mode at import time (the default since v1.0).

    Set ``STRUCTGURU_LEGACY=1`` to skip import-time configuration. Logging then
    remains disabled until :func:`configure` is called. ``STRUCTGURU_NATIVE`` is
    a no-op retained for compatibility because native logging is the only path.

    Honors ``LOG_LEVEL``, ``STRUCTGURU_SERVICE``, ``STRUCTGURU_NATIVE_TARGET``,
    ``STRUCTGURU_NATIVE_SAMPLE_RATE`` (float 0.0–1.0), and
    ``STRUCTGURU_NATIVE_RATE_LIMIT`` (``"MAX/PERIOD"`` seconds, e.g. ``"100/60"``).
    Never breaks import: a missing extension or bad config is silently ignored.
    """
    if _RUST is None:
        return
    # STRUCTGURU_LEGACY=1 opts out of native mode (old standard-path default).
    if os.environ.get("STRUCTGURU_LEGACY", "").strip().lower() in ("1", "true", "yes", "on"):
        return
    try:
        kwargs: dict[str, Any] = {
            "service": os.environ.get("STRUCTGURU_SERVICE", "app"),
            "level": os.environ.get("LOG_LEVEL", "INFO"),
            "target": os.environ.get("STRUCTGURU_NATIVE_TARGET", "stdout"),
        }
        sample_rate_str = os.environ.get("STRUCTGURU_NATIVE_SAMPLE_RATE")
        if sample_rate_str:
            kwargs["sample_rate"] = float(sample_rate_str)
        rate_limit_str = os.environ.get("STRUCTGURU_NATIVE_RATE_LIMIT")
        if rate_limit_str:
            max_str, _, period_str = rate_limit_str.partition("/")
            kwargs["rate_limit_max"] = int(max_str)
            if period_str:
                kwargs["rate_limit_period"] = float(period_str)
        configure(**kwargs)
    except Exception:  # pragma: no cover - defensive: never fail import on env config
        pass


_maybe_configure_from_env()
