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
import os
import sys
import threading
import traceback
import warnings
from collections.abc import Callable
from dataclasses import dataclass, replace
from types import TracebackType
from typing import Any, Protocol, Unpack, cast

from structguru._native_dispatch import CallableSinkDispatcher
from structguru._native_env import autoconfigure_from_env
from structguru.settings import Settings, SettingsChanges, _level_number

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

    def render_enqueue_json(
        self,
        fields: dict[str, Any],
        logger: str,
        level: str,
        service: str,
        message: str,
        blocking: bool,
        config: Any = None,
        sensitive_keys: list[str] | None = None,
        stack: str | None = None,
        timestamp: str | None = None,
    ) -> bool: ...

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

    def validate_patterns(self, patterns: list[str], allow_backtracking: bool = False) -> int: ...

    def RedactionConfig(
        self,
        patterns: list[str],
        replacement: str | None = None,
        allow_backtracking: bool = False,
    ) -> Any: ...

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


def is_available() -> bool:
    """Return whether the compiled native extension is importable."""
    return _RUST is not None


# -- native configuration ---------------------------------------------------


@dataclass(frozen=True)
class _RuntimeState:
    """Coherent native configuration snapshot used for one complete log call."""

    writer: _NativeWriter
    settings: Settings
    service: str
    maxsize: int
    target: str
    overflow: str
    file_path: str | None
    file_max_bytes: int
    file_backup_count: int
    also_stdout: bool
    level_threshold: int
    otel: bool
    sensitive_keys: list[str] | None
    sensitive_patterns: list[str] | None
    redaction_config: Any
    record_filter: Any
    exception_config: dict[str, Any] | None
    exception_carets: bool
    metric_processor: Any
    sentry_processor: Any
    console: bool
    colors: bool
    stream_sink: Any
    callable_sinks: tuple[Callable[[str], None], ...]
    callable_queue_maxsize: int
    # True when a record can be rendered and enqueued in one native call: JSON
    # output with no synchronous stream sink and no Sentry line to hand back.
    fused_json: bool


_state_lock = threading.Lock()
_runtime: _RuntimeState | None = None
_lifecycle_generation = 0


_callable_dispatcher = CallableSinkDispatcher()
# Synchronous stream sink: when set (via configure(stream_sink=...)), rendered
# lines are written to this stream synchronously on the caller's thread IN
# ADDITION to the Rust writer, so logger output is available on the configured
# stream immediately after the call (no flush needed).
_hooks_registered = False
_drop_count = 0
_drop_lock = threading.Lock()


def current_runtime() -> _RuntimeState | None:
    """Return the current immutable-by-convention runtime snapshot."""
    return _runtime if _RUST is not None else None


def is_native_enabled() -> bool:
    """True when native mode is on and the extension is loaded."""
    return current_runtime() is not None


def is_below_level(method: str, runtime: _RuntimeState | None = None) -> bool:
    """True when a call at *method* is below the native threshold (drop it)."""
    state = runtime or current_runtime()
    return state is None or _LEVEL_NUM.get(method, _LEVEL_NUM["info"]) < state.level_threshold


def set_level(level: str | int) -> None:
    """Adjust the native level threshold at runtime (per-process)."""
    global _runtime, _lifecycle_generation
    threshold = _level_number(level)
    with _state_lock:
        if _runtime is not None:
            _runtime = replace(
                _runtime,
                level_threshold=threshold,
                settings=replace(_runtime.settings, level=level),
            )
            _lifecycle_generation += 1


def otel_enabled(runtime: _RuntimeState | None = None) -> bool:
    """True when OTel trace-context injection is enabled for native mode."""
    state = runtime or current_runtime()
    return state is not None and state.otel


def sensitive_keys() -> list[str] | None:
    """Custom redaction keys for native mode, or None for the defaults."""
    state = current_runtime()
    return state.sensitive_keys if state is not None else None


def add_callable_sink(
    fn: Callable[[str], None],
    min_level: int = 0,
    *,
    level_callback: Callable[[str, int], None] | None = None,
) -> int:
    """Register a runtime callable sink and return its stable token.

    The sink is invoked with each rendered line on the dispatch thread.
    If the dispatch infrastructure is not running, it is started.
    """
    token = _callable_dispatcher.add(
        fn, min_level, enabled=is_native_enabled(), level_callback=level_callback
    )
    _sync_callable_dispatcher()
    return token


def remove_callable_sink(token: int) -> bool:
    """Drain prior records, then remove exactly one runtime sink token."""
    return _callable_dispatcher.remove(token)


def sensitive_patterns() -> list[str] | None:
    """Custom regex value-patterns for native mode, or None if unset."""
    state = current_runtime()
    return state.sensitive_patterns if state is not None else None


def should_render(
    method: str,
    message: str,
    runtime: _RuntimeState | None = None,
) -> bool:
    """True when the pre-render filter (sampling/rate-limit) keeps the record.

    Returns ``True`` immediately when no filter is configured, so the default
    native path pays no extra cost. Mirrors the shape of :func:`is_below_level`.
    """
    state = runtime or current_runtime()
    if state is None:
        return False
    if state.record_filter is None:
        return True
    return bool(state.record_filter.allow(message, method))


def notify_metrics(
    method: str,
    message: str,
    fields: dict[str, Any],
    runtime: _RuntimeState | None = None,
) -> None:
    """Invoke the configured metric processor for a kept record.

    Called on the caller's thread (never the writer thread) with a
    pre-``EventRenamer``-shaped event dict, matching what ``MetricProcessor``
    sees on the standard path. No-op when no processor is configured; hook
    errors are swallowed — metrics must never break logging.
    """
    state = runtime or current_runtime()
    if state is None or state.metric_processor is None:
        return
    try:
        state.metric_processor(None, method, {"event": message, **fields})
    except Exception:  # noqa: BLE001 - hooks must never break logging
        pass


def notify_sentry(
    method: str,
    redacted_line: str,
    field_names: tuple[str, ...],
    exc_info: Any = None,
    runtime: _RuntimeState | None = None,
) -> None:
    """Invoke the configured Sentry processor with the redacted rendered event.

    Mirrors :func:`notify_metrics` but passes the raw ``exc_info`` (in the shape
    ``SentryProcessor._resolve_exception`` expects: ``True``, a ``(type, exc, tb)``
    tuple, or a ``BaseException``) so exception capture works. Field values and
    the message are reconstructed from the JSON produced by the Rust renderer,
    guaranteeing that key and pattern redaction runs before third-party export.
    Hook errors are swallowed — Sentry must never break logging.
    """
    state = runtime or current_runtime()
    if state is None or state.sentry_processor is None:
        return
    try:
        rendered = json.loads(redacted_line)
    except (json.JSONDecodeError, TypeError):
        return
    event_dict: dict[str, Any] = {"event": rendered.get("message", "")}
    for key in (*field_names, "service", "logger", "level", "severity", "timestamp"):
        if key in rendered:
            event_dict[key] = rendered[key]
    if exc_info is not None:
        event_dict["exc_info"] = exc_info
    from structguru.redaction import REDACTED_MARKER_KEY

    event_dict[REDACTED_MARKER_KEY] = True
    try:
        state.sentry_processor(None, method, event_dict)
    except Exception:  # noqa: BLE001 - hooks must never break logging
        pass


def build_exception_field(
    exc_info: Any,
    runtime: _RuntimeState | None = None,
) -> str | dict[str, Any] | None:
    """Build the ``exception`` field value for a native record.

    Returns the structured dict when ``structured_exceptions`` is enabled
    (matching :func:`structguru.exceptions.build_exception_dict` exactly, or
    ``None`` when *exc_info* does not resolve); otherwise the formatted
    traceback string (matching ``format_exc_info``). Frame walking, locals
    capture, and ``repr`` are Python-owned — the native renderer only
    serializes the result.
    """
    state = runtime or current_runtime()
    if state is None:
        return format_exception(exc_info)
    if state.exception_config is None:
        return format_exception(exc_info, carets=state.exception_carets)

    from structguru.exceptions import build_exception_dict

    return build_exception_dict(exc_info, **state.exception_config)


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


def format_exception(exc_info: Any, *, carets: bool = True) -> str:
    """Format ``exc_info`` to a traceback string matching structlog's output.

    Accepts ``True`` (use the current exception), a ``BaseException`` instance,
    or a ``(type, value, tb)`` tuple. With ``carets=False`` the PEP 657
    position markers under each frame are omitted, which is what CPython
    prints when it runs without debug ranges.
    """
    if exc_info is True:
        exc_info = sys.exc_info()
    elif isinstance(exc_info, BaseException):
        exc_info = (type(exc_info), exc_info, exc_info.__traceback__)
    if not isinstance(exc_info, tuple) or len(exc_info) != 3 or exc_info[0] is None:
        return ""
    _exc_type, exc_value, exc_tb = exc_info
    if not isinstance(exc_value, BaseException):
        return ""
    tb = cast("TracebackType | None", exc_tb)
    if carets:
        return "".join(traceback.format_exception(type(exc_value), exc_value, tb)).rstrip("\n")
    formatted = traceback.TracebackException(type(exc_value), exc_value, tb, compact=True)
    _drop_positions(formatted)
    return "".join(formatted.format()).rstrip("\n")


def _drop_positions(formatted: traceback.TracebackException) -> None:
    """Rebuild every frame summary without column data so no carets render.

    ``TracebackException.format()`` derives the ``~~~^^^`` marker lines from
    each frame's ``colno``/``end_colno``. A :class:`traceback.FrameSummary`
    built without them, and with ``end_lineno`` collapsed onto ``lineno``, is
    exactly what CPython produces under ``PYTHONNODEBUGRANGES=1``, so the
    output matches that mode line for line. Walks the ``__cause__`` and
    ``__context__`` chain and exception-group members, which ``format()``
    renders through the same frame summaries.
    """
    pending = [formatted]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        current.stack = traceback.StackSummary.from_list(
            [
                traceback.FrameSummary(
                    frame.filename,
                    frame.lineno,
                    frame.name,
                    lookup_line=False,
                    locals=frame.locals,
                    line=frame.line,
                )
                for frame in current.stack
            ]
        )
        pending.extend(
            linked for linked in (current.__cause__, current.__context__) if linked is not None
        )
        pending.extend(current.exceptions or ())


def configure(settings: Settings | None = None, **changes: Unpack[SettingsChanges]) -> None:
    r"""Configure the native renderer and background writer.

    Replace configuration using defaults, environment, then explicit keywords.
    A supplied ``Settings`` object replaces the defaults/environment base. Keyword
    overrides still win, including explicit ``None``. Omitted settings do not carry
    over from a previous call; use :func:`update` for incremental changes.
    Invalid settings leave the active runtime untouched. Reconfiguration replaces
    writers and resets sampling/rate-limit state. Sinks added with ``logger.add()``
    survive reconfiguration and are not included in :func:`get_config`.

    ``target`` selects the background writer's sink: ``"stdout"`` (default,
    12-factor), ``"null"`` (discard), or ``"memory"`` (inspection/tests).

    The output queue is bounded to 8192 records by default. ``overflow`` governs a
    full queue: ``"block"`` (default, no loss — the caller waits with the GIL
    released) or ``"drop"`` (drop the new record, count it, and emit a rate-limited
    warning; see :func:`writer_metrics`). Pass ``maxsize=0`` only to explicitly opt
    into an unbounded queue.

    ``sensitive_patterns`` is a list of regex source strings applied (in addition
    to key-based redaction) to every string value. Rust's ``regex`` engine
    guarantees linear-time matching and rejects backreferences and look-around:
    an unsupported pattern raises ``ValueError`` here, at setup time.
    ``pattern_replacement`` is the substitution text for pattern matches and
    supports capture-group expansion (``$1``, ``${name}``; ``$$`` for a literal
    ``$``), so look-behind-style patterns can be rewritten to preserve their
    prefix — e.g. pattern ``(password=)\S+`` with replacement
    ``$1[REDACTED]``. The group must wrap the prefix you want to keep, never
    the secret: ``password=(\S+)`` with ``$1[REDACTED]`` re-emits the secret.

    ``allow_backtracking_patterns=True`` opts patterns the linear engine
    rejects into a bounded backtracking engine instead, so look-around and
    backreferences work as written. The linear-time (no-ReDoS) guarantee no
    longer applies to those patterns; evaluation is capped by a backtrack
    limit, and a string whose evaluation exceeds it is redacted *entirely*
    (fail-closed) rather than emitted unchecked. Patterns the linear engine
    accepts still use it — only the exotic ones pay the backtracking cost.
    Prefer the capture-group rewrite where possible.

    ``sample_rate`` (0.0–1.0) and ``rate_limit_max``/``rate_limit_period`` add
    pre-render filters: dropped records cost zero rendering. ``sampled`` and
    ``rate_limited`` counters are reported separately from the writer's transport
    ``dropped`` counter (see :func:`writer_metrics`). ``sample_max_level``
    restricts sampling to records at or below that level (more severe records
    always pass) — the native analog of level-gated sampling.

    ``metric_processor`` is a structlog-style processor (e.g.
    :class:`structguru.metrics.MetricProcessor`) invoked for every *kept* record
    with ``(None, method, {"event": message, **fields})`` before rendering.
    Records dropped by level filtering, sampling, or rate limiting never reach
    it. Exceptions raised by the processor are swallowed.

    ``structured_exceptions=True`` renders the ``exception`` field as the
    structured dict produced by
    :func:`structguru.exceptions.build_exception_dict` (with the
    ``exception_*`` knobs mirroring the processor's parameters and
    ``sensitive_keys`` reused for locals redaction) instead of the formatted
    traceback string.

    ``exception_carets=False`` omits the PEP 657 position markers (the
    ``~~~^^^`` lines under each frame) from formatted tracebacks. CPython
    computes them per frame while formatting, and on 3.11+ they are most of
    the cost of ``logger.exception()``; without them a traceback formats
    about five times faster. The output is what CPython itself prints under
    ``PYTHONNODEBUGRANGES=1``. The default keeps the markers so the field
    matches ``traceback.format_exception``; ``structured_exceptions=True``
    never renders them.

    ``format`` selects the renderer: ``"json"`` (default, production-friendly
    compact JSON) or ``"console"`` (colored, human-readable dev output in
    structguru's own stable format — not a structlog ``ConsoleRenderer`` clone).
    ``colors`` defaults to ``sys.stdout.isatty()`` in console mode; set it
    explicitly to override (ignored in JSON mode).

    ``file_path`` enables a native rotating-file sink (append mode). Defaults
    mirror :class:`logging.handlers.RotatingFileHandler`: ``file_max_bytes=50MB``,
    ``file_backup_count=5``. Set ``also_stdout=True`` to mirror output to both
    the file and stdout (e.g. container + persistent log).

    ``callable_sinks`` is a list of ``Callable[[str], None]`` invoked with each
    *rendered* line. They run on a dedicated daemon thread. Their queue is bounded
    by ``callable_queue_maxsize``; ``overflow="block"`` applies backpressure when
    full, while ``overflow="drop"`` drops and counts the callable delivery.
    Logs emitted inside these callbacks still reach the native writer but skip
    callable sinks, preventing feedback loops and worker self-deadlocks.

    Registers shutdown (``atexit``) and fork (``os.register_at_fork``) handlers so
    buffered records are flushed on exit and the background writer is respawned in
    forked children (gunicorn/celery prefork) instead of deadlocking.
    """
    resolved = (
        Settings.from_env(**changes)
        if settings is None
        else Settings.from_mapping({**settings.to_mapping(), **changes})
    )
    _apply_settings(resolved)


def get_config() -> Settings | None:
    """Return the active settings, or ``None`` when logging is shut down.

    Collections are immutable snapshots; streams and callbacks retain their
    identity. Settings describe configured options, not queue contents, filter
    counters, stdlib bridge state, or sinks registered with ``logger.add()``.
    """
    state = current_runtime()
    return state.settings if state is not None else None


def update(**changes: Unpack[SettingsChanges]) -> None:
    """Change active settings without rereading environment variables.

    An empty update does nothing. A level-only update preserves writers, queues,
    and rate-limit state. Other updates replace runtime resources as configure
    does, retaining omitted options. Raise ``RuntimeError`` when unconfigured.
    """
    global _runtime, _lifecycle_generation
    while True:
        state = current_runtime()
        if state is None:
            msg = "logging is not configured; call configure() before update()"
            raise RuntimeError(msg)
        settings = Settings.from_mapping({**state.settings.to_mapping(), **changes})
        if not changes:
            return
        if changes.keys() <= {"level"}:
            with _state_lock:
                if _runtime is not state:
                    continue
                _runtime = replace(
                    state, settings=settings, level_threshold=_level_number(settings.level)
                )
                _lifecycle_generation += 1
                return
        if _apply_settings(settings, expected=state):
            return


def _apply_settings(settings: Settings, *, expected: _RuntimeState | None = None) -> bool:
    """Build resources before publishing a snapshot; retry stale incremental updates."""
    service = settings.service
    maxsize = settings.maxsize
    target = settings.target
    overflow = settings.overflow
    level = settings.level
    otel = settings.otel
    sensitive_keys = settings.sensitive_keys
    sensitive_patterns = settings.sensitive_patterns
    pattern_replacement = settings.pattern_replacement
    allow_backtracking_patterns = settings.allow_backtracking_patterns
    sample_rate = settings.sample_rate
    sample_max_level = settings.sample_max_level
    rate_limit_max = settings.rate_limit_max
    rate_limit_period = settings.rate_limit_period
    metric_processor = settings.metric_processor
    sentry_processor = settings.sentry_processor
    structured_exceptions = settings.structured_exceptions
    exception_include_locals = settings.exception_include_locals
    exception_max_frames = settings.exception_max_frames
    exception_max_local_repr = settings.exception_max_local_repr
    exception_carets = settings.exception_carets
    format = settings.format
    colors = settings.colors
    file_path = settings.file_path
    file_max_bytes = settings.file_max_bytes
    file_backup_count = settings.file_backup_count
    also_stdout = settings.also_stdout
    callable_sinks = settings.callable_sinks
    callable_queue_maxsize = settings.callable_queue_maxsize
    stream_sink = settings.stream_sink

    global _runtime, _lifecycle_generation
    if _RUST is None:
        msg = "native extension is not available"
        raise RuntimeError(msg)
    # Validate regex patterns against Rust's engine before enabling. Rust's
    # `regex` guarantees linear-time matching and therefore rejects
    # backreferences and look-around; fail loudly at setup time — redaction
    # that silently differs from what was configured is worse than an error.
    # `allow_backtracking_patterns` routes rejected patterns to the bounded
    # backtracking engine instead (validated the same way).
    if sensitive_patterns:
        try:
            _RUST.validate_patterns(list(sensitive_patterns), allow_backtracking_patterns)
        except ValueError as exc:
            if allow_backtracking_patterns:
                msg = f"invalid sensitive_patterns regex ({exc})."
            else:
                msg = (
                    f"unsupported sensitive_patterns regex ({exc}). Rust's regex engine "
                    "guarantees linear-time matching and does not support backreferences "
                    "or look-around. Rewrite the pattern with a capture group around "
                    "the prefix you want to keep, e.g. lookbehind "
                    "'(?<=password=)\\S+' becomes '(password=)\\S+' with "
                    "pattern_replacement='$1[REDACTED]', or "
                    "pass allow_backtracking_patterns=True to opt this pattern into a "
                    "bounded backtracking engine (loses the linear-time guarantee)."
                )
            raise ValueError(msg) from exc
        new_config = _RUST.RedactionConfig(
            list(sensitive_patterns), pattern_replacement, allow_backtracking_patterns
        )
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

    # Resolve console/colors from the chosen format.
    new_console = format == "console"
    new_colors = colors if colors is not None else (sys.stdout.isatty() if new_console else False)

    # Construct every fallible native resource before touching the active
    # configuration. A bad target/path/value must not close a working logger.
    new_writer = _RUST._NativeStringWriter(
        maxsize,
        target=target,
        file_path=file_path,
        file_max_bytes=file_max_bytes,
        file_backup_count=file_backup_count,
        also_stdout=also_stdout,
    )
    new_runtime = _RuntimeState(
        writer=new_writer,
        settings=settings,
        service=service,
        maxsize=maxsize,
        target=target,
        overflow=overflow,
        file_path=file_path,
        file_max_bytes=file_max_bytes,
        file_backup_count=file_backup_count,
        also_stdout=also_stdout,
        level_threshold=_level_number(level),
        otel=otel,
        sensitive_keys=list(sensitive_keys) if sensitive_keys is not None else None,
        sensitive_patterns=new_patterns,
        redaction_config=new_config,
        record_filter=new_filter,
        exception_config=new_exception_config,
        exception_carets=exception_carets,
        metric_processor=metric_processor,
        sentry_processor=sentry_processor,
        console=new_console,
        colors=new_colors,
        stream_sink=stream_sink,
        callable_sinks=tuple(callable_sinks or ()),
        callable_queue_maxsize=callable_queue_maxsize,
        fused_json=not new_console and sentry_processor is None and stream_sink is None,
    )
    with _state_lock:
        stale = expected is not None and _runtime is not expected
        old_runtime = _runtime
        if not stale:
            _runtime = new_runtime
            _lifecycle_generation += 1
    if stale:
        new_writer.close()
        return False

    # Closing a retired writer is safe while an in-flight logger still owns its
    # snapshot: the Rust writer rejects a late enqueue instead of invalidating the
    # Python object. No record can observe a partially updated configuration.
    if old_runtime is not None:
        old_runtime.writer.close()
    _sync_callable_dispatcher()

    _register_lifecycle_hooks()
    return True


def _sync_callable_dispatcher() -> None:
    """Converge callable dispatch on the newest runtime generation."""
    while True:
        with _state_lock:
            generation = _lifecycle_generation
            state = _runtime
        if state is None:
            _callable_dispatcher.disable()
        else:
            _callable_dispatcher.configure(
                state.callable_sinks,
                maxsize=state.callable_queue_maxsize,
            )
        with _state_lock:
            if generation == _lifecycle_generation:
                return


def _register_lifecycle_hooks() -> None:
    """Register atexit + fork handlers exactly once."""
    global _hooks_registered
    with _state_lock:
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
    """Drain native and callable sinks on interpreter shutdown."""
    _callable_dispatcher.stop(drain=True)
    state = current_runtime()
    if state is not None:
        state.writer.close()


def _before_fork() -> None:
    """Flush buffered records in the parent before forking."""
    state = current_runtime()
    if state is not None:
        state.writer.flush()
    _callable_dispatcher.flush()


def _after_in_child() -> None:
    """Respawn the writer in the child: its worker thread did not survive fork."""
    global _runtime, _state_lock, _drop_lock
    state = _runtime
    # No other Python thread survives fork. Replace inherited synchronization
    # objects before touching state that may have been locked by a vanished thread.
    _state_lock = threading.Lock()
    _drop_lock = threading.Lock()
    if state is None or _RUST is None:
        _callable_dispatcher.after_fork(enabled=False)
        return
    state.writer.abandon()  # never join the parent's (now absent) worker thread
    # Replay the full sink config so file paths and stdout mirroring survive fork.
    writer = _RUST._NativeStringWriter(
        state.maxsize,
        target=state.target,
        file_path=state.file_path,
        file_max_bytes=state.file_max_bytes,
        file_backup_count=state.file_backup_count,
        also_stdout=state.also_stdout,
    )
    _runtime = replace(state, writer=writer)
    _callable_dispatcher.after_fork(enabled=True)


def shutdown() -> None:
    """Turn native mode off and stop the background writer."""
    global _runtime, _lifecycle_generation
    with _state_lock:
        old_runtime = _runtime
        _runtime = None
        _lifecycle_generation += 1
    if old_runtime is not None:
        old_runtime.writer.close()
    _sync_callable_dispatcher()


def _render_json(
    runtime: _RuntimeState,
    fields: dict[str, Any],
    logger: str,
    level: str,
    message: str,
    stack: str | None,
) -> str:
    """Render a redacted JSON event, reusing compiled configuration."""
    assert _RUST is not None
    if runtime.redaction_config is not None:
        return _RUST.render_line_with_config(
            fields,
            logger,
            level,
            runtime.service,
            message,
            runtime.redaction_config,
            None,
            runtime.sensitive_keys,
            stack,
        )
    return _RUST.render_line(
        fields,
        logger,
        level,
        runtime.service,
        message,
        None,
        runtime.sensitive_keys,
        None,
        stack,
    )


def render_and_enqueue(
    fields: dict[str, Any],
    logger: str,
    level: str,
    message: str,
    stack: str | None = None,
    runtime: _RuntimeState | None = None,
) -> str | None:
    """Render and enqueue one record; return redacted JSON for Sentry when enabled.

    The timestamp is generated inside the Rust core (cached per second), so no
    Python-side time formatting happens on the hot path. When value-pattern
    redaction is configured, the pre-built ``RedactionConfig`` is reused so no
    per-record regex compilation occurs.
    """
    state = runtime or current_runtime()
    if _RUST is None or state is None:
        return None
    if state.fused_json and _callable_dispatcher.idle():
        # Common production shape: JSON to the native writer and nothing else
        # wants the rendered text. Render and enqueue in one call so the line
        # never becomes a Python string (no PyString build, concat, or re-copy).
        accepted = state.writer.render_enqueue_json(
            fields,
            logger,
            level,
            state.service,
            message,
            state.overflow == "block",
            state.redaction_config,
            state.sensitive_keys,
            stack,
        )
        active = current_runtime()
        if not accepted and active is not None and active.writer is state.writer:
            _note_drop()
        return None
    if state.console:
        if state.redaction_config is not None:
            rendered = _RUST.render_console_with_config(
                fields,
                logger,
                level,
                state.service,
                message,
                state.colors,
                state.redaction_config,
                None,
                state.sensitive_keys,
                stack,
            )
        else:
            rendered = _RUST.render_line_console(
                fields,
                logger,
                level,
                state.service,
                message,
                state.colors,
                None,
                state.sensitive_keys,
                None,
                stack,
            )
    else:
        rendered = _render_json(state, fields, logger, level, message, stack)
    sentry_line = None
    if state.sentry_processor is not None:
        sentry_line = (
            rendered
            if not state.console
            else _render_json(state, fields, logger, level, message, stack)
        )
    line = rendered + "\n"
    # Synchronous stream sink (configure(stream_sink=...)).
    if state.stream_sink is not None:
        try:
            state.stream_sink.write(line)
        except Exception:  # noqa: BLE001 - stream errors must never break logging
            pass
    if state.overflow == "block":
        enqueued = state.writer.enqueue_blocking(line)
    else:
        enqueued = state.writer.try_enqueue(line)
    # A retired writer rejects late records during configure/disable. That is a
    # lifecycle boundary, not queue overflow, and must not raise or emit a false
    # "queue full" warning from application code.
    active = current_runtime()
    # Level-only updates replace the snapshot, but keep its delivery resources.
    still_active = active is not None and active.writer is state.writer
    if not enqueued and still_active:
        _note_drop()
    if still_active:
        _callable_dispatcher.enqueue(
            line,
            _LEVEL_NUM.get(level, _LEVEL_NUM["info"]),
            overflow=state.overflow,
        )
        return sentry_line
    return None


def _note_drop() -> None:
    """Emit a rate-limited warning when the queue drops a record (drop mode)."""
    global _drop_count
    with _drop_lock:
        _drop_count += 1
        dropped = _drop_count
    if dropped == 1 or dropped % 1000 == 0:
        warnings.warn(
            f"structguru native logging dropped {dropped} record(s): queue full",
            stacklevel=3,
        )


def _reset_drop_count() -> None:
    """Reset the drop counter (used by tests)."""
    global _drop_count
    with _drop_lock:
        _drop_count = 0
    _callable_dispatcher.reset_drop_count()


def flush() -> None:
    """Block until every buffered record has been written out.

    Rendering hands each record to a background writer thread (and, for callable
    sinks, a bounded dispatch queue), so a returning ``logger.info()`` does not
    mean the line has reached its destination yet. Call this when you need that
    guarantee — before asserting on output in a test, or at a checkpoint where
    losing buffered records would matter.

    Not needed for normal shutdown: :func:`shutdown`, reconfiguration, fork, and
    interpreter exit all drain automatically.
    """
    state = current_runtime()
    if state is not None:
        state.writer.flush()
    _callable_dispatcher.flush()


# Pre-1.0.5 name, kept as an alias: `flush` is the public spelling.
flush_native = flush


def drain_messages() -> list[str]:
    """Return all lines the writer has flushed to its in-memory sink (tests)."""
    state = current_runtime()
    return state.writer.messages() if state is not None else []


def writer_metrics() -> dict[str, Any] | None:
    """Return writer + filter metrics.

    Writer counters: ``enqueued``, ``dropped`` (queue-full), ``written``, ``depth``,
    etc. When a pre-render filter is active, ``sampled`` and ``rate_limited`` are
    added — these are distinct from the transport ``dropped`` counter.
    ``written`` counts records delivered to at least one native destination;
    ``sink_errors`` counts failed sink operations, including partial failures
    when another destination succeeds.
    """
    state = current_runtime()
    if state is None:
        return None
    metrics = dict(state.writer.metrics())
    metrics.update(_callable_dispatcher.metrics())
    if state.record_filter is not None:
        metrics.update(state.record_filter.stats())
    return metrics


def _maybe_configure_from_env() -> None:
    """Auto-configure native mode at import time (the default since v1.0).

    Set ``STRUCTGURU_AUTOCONFIGURE=0`` to skip import-time configuration. Logging
    then remains disabled until :func:`configure` is called. The legacy inverse
    switch ``STRUCTGURU_LEGACY=1`` remains supported when the new switch is absent.

    :meth:`Settings.from_env` resolves native options and compatibility aliases.
    A missing extension or invalid environment value raises during import so the
    application cannot start with its only logging path silently disabled.
    """
    if _RUST is None:
        msg = "structguru requires its native extension, but structguru._rust is unavailable"
        raise RuntimeError(msg)
    if autoconfigure_from_env(os.environ):
        configure()


_maybe_configure_from_env()
