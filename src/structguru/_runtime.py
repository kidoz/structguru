"""Native extension access and logging runtime configuration.

Also owns the native render/enqueue path used by ``structguru.core.Logger``.
Use ``configure`` to customize rendering, filtering, and sinks. Structured
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

from structguru._levels import METHOD_LEVELS
from structguru._native_dispatch import CallableDispatcherProtocol, new_dispatcher
from structguru._native_env import autoconfigure_from_env
from structguru.settings import Settings, SettingsChanges, _level_number

# Outcome codes of the writer's fused ``log`` entry points (``Outcome`` in the
# extension): accepted, dropped by a full queue, rejected by a closed writer.
_ACCEPTED, _FULL, _CLOSED = 0, 1, 2


class _NativeWriter(Protocol):
    def enqueue_outcome(self, message: str, blocking: bool) -> str: ...

    def render_enqueue_json_outcome(
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
    ) -> str: ...

    def log(
        self,
        config: Any,
        logger: str,
        level: str,
        message: str,
        bound: dict[str, Any],
        kwargs: dict[str, Any],
        contextvars: dict[str, Any],
        exception: Any,
        stack: str | None,
        created: float | None = None,
    ) -> int: ...

    def log_capture(
        self,
        config: Any,
        logger: str,
        level: str,
        message: str,
        bound: dict[str, Any],
        kwargs: dict[str, Any],
        contextvars: dict[str, Any],
        exception: Any,
        stack: str | None,
        json_copy: bool,
        created: float | None = None,
    ) -> tuple[int, str, str | None]: ...

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

    def report_unraisable(self, error: BaseException) -> None: ...

    def RedactionConfig(
        self,
        patterns: list[str],
        replacement: str | None = None,
        allow_backtracking: bool = False,
    ) -> Any: ...

    def RuntimeConfig(
        self,
        service: str,
        sensitive_keys: list[str] | None = None,
        sensitive_patterns: list[str] | None = None,
        pattern_replacement: str | None = None,
        allow_backtracking: bool = False,
        console: bool = False,
        colors: bool = False,
        blocking: bool = True,
    ) -> Any: ...

    def merge_fields(
        self, bound: dict[str, Any], kwargs: dict[str, Any], contextvars: dict[str, Any]
    ) -> dict[str, Any]: ...

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
    """Return whether the native extension is loaded.

    Always ``True`` once the package has imported: a missing extension raises
    ``RuntimeError`` at import time. Kept for callers written against releases
    that could fall back to a pure-Python path.
    """
    return _RUST is not None


# -- native configuration ---------------------------------------------------


@dataclass(frozen=True)
class _RuntimeState:
    """Coherent native configuration snapshot used for one complete log call."""

    writer: _NativeWriter
    # Rust-owned per-record policy: service, redaction keys and compiled
    # patterns, output format and colors, and the queue overflow policy.
    config: Any
    settings: Settings
    maxsize: int
    target: str
    overflow: str
    file_path: str | None
    file_max_bytes: int
    file_backup_count: int
    also_stdout: bool
    level_threshold: int
    otel: bool
    record_filter: Any
    exception_config: dict[str, Any] | None
    exception_carets: bool
    metric_processor: Any
    sentry_processor: Any
    stream_sink: Any
    callable_sinks: tuple[Callable[[str], None], ...]
    callable_queue_maxsize: int
    # True when a Python hook (OTel enrichment, metric or Sentry processor)
    # needs the merged field mapping before the record is rendered.
    enrich: bool
    # True when nothing outside the writer needs the rendered line: no
    # synchronous stream sink and no Sentry line to hand back. Callable sinks
    # are checked per call, since they register and retire at runtime.
    fused: bool

    @property
    def service(self) -> str:
        """Service name rendered into every record (owned by ``config``)."""
        return cast(str, self.config.service)

    @property
    def console(self) -> bool:
        """True when records render in the console format."""
        return bool(self.config.console)

    @property
    def colors(self) -> bool:
        """True when console output carries ANSI colors."""
        return bool(self.config.colors)


_state_lock = threading.Lock()
_runtime: _RuntimeState | None = None
_lifecycle_generation = 0


_callable_dispatcher: CallableDispatcherProtocol = new_dispatcher()
# Synchronous stream sink: when set (via configure(stream_sink=...)), rendered
# lines are written to this stream synchronously on the caller's thread IN
# ADDITION to the Rust writer, so logger output is available on the configured
# stream immediately after the call (no flush needed).
_hooks_registered = False
_drop_count = 0
_stream_error_count = 0
# A callable sink that makes no progress for this long during flush() or
# shutdown() is abandoned with a warning instead of blocking the process.
_CALLABLE_STALL_TIMEOUT = 10.0
# Largest level number handed to the callable dispatcher (a signed 64-bit int).
_MAX_LEVELNO = 2**63 - 1
_drop_lock = threading.Lock()
_lifecycle_rejected = 0


def current_runtime() -> _RuntimeState | None:
    """Return the current immutable-by-convention runtime snapshot."""
    return _runtime if _RUST is not None else None


def is_native_enabled() -> bool:
    """True when native mode is on and the extension is loaded."""
    return current_runtime() is not None


def is_below_level(method: str, runtime: _RuntimeState | None = None) -> bool:
    """True when a call at *method* is below the native threshold (drop it)."""
    state = runtime or current_runtime()
    return state is None or METHOD_LEVELS.get(method, 20) < state.level_threshold


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
    keys = state.settings.sensitive_keys if state is not None else None
    return list(keys) if keys is not None else None


def add_callable_sink(
    fn: Callable[[str], None],
    min_level: int = 0,
    *,
    level_callback: Callable[[str, int], None] | None = None,
) -> int:
    """Register a runtime callable sink and return its stable token.

    The sink is invoked with each rendered line on the dispatch thread.
    If the dispatch infrastructure is not running, it is started. Registration
    never replaces the active queue generation or waits for its backlog.
    """
    token = _callable_dispatcher.add(
        fn, min_level, enabled=is_native_enabled(), level_callback=level_callback
    )
    _sync_callable_dispatcher(reconfigured=False)
    return token


def remove_callable_sink(token: int, *, finalizer: Callable[[], None] | None = None) -> bool:
    """Drain prior records, then remove exactly one runtime sink token.

    ``finalizer`` runs once no queued delivery references the sink; inside a
    sink callback that is on the worker after the last such delivery. A sink
    that stops returning is abandoned after ``_CALLABLE_STALL_TIMEOUT``, as in
    ``flush``.
    """
    removed, abandoned = _callable_dispatcher.remove(
        token, finalizer=finalizer, stall_timeout=_CALLABLE_STALL_TIMEOUT
    )
    _note_abandoned(abandoned)
    return removed


def sensitive_patterns() -> list[str] | None:
    """Custom regex value-patterns for native mode, or None if unset."""
    state = current_runtime()
    patterns = state.settings.sensitive_patterns if state is not None else None
    return list(patterns) if patterns else None


def should_render(
    method: str,
    message: str,
    runtime: _RuntimeState | None = None,
) -> bool:
    """True when the pre-render filter (sampling/rate-limit) keeps the record.

    Returns ``True`` immediately when no filter is configured, so the default
    native path pays no extra cost. Mirrors the shape of ``is_below_level``.
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
    sees on the standard path. The message always occupies ``"event"``, as in
    ``notify_sentry``: a user field by that name renders natively but must
    not change which metrics fire. No-op when no processor is configured; hook
    errors are swallowed — metrics must never break logging.
    """
    state = runtime or current_runtime()
    if state is None or state.metric_processor is None:
        return
    event_dict = {"event": message, **fields}
    if "event" in fields:
        event_dict["event"] = message
    try:
        state.metric_processor(None, method, event_dict)
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

    Mirrors ``notify_metrics`` but passes the raw ``exc_info`` (in the shape
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
        # "event" is the hook's message slot. A user field by that name renders
        # natively as a field but must not replace the message exported here.
        if key != "event" and key in rendered:
            event_dict[key] = rendered[key]
    if exc_info is not None:
        event_dict["exc_info"] = exc_info
    from structguru.redaction import REDACTED_MARKER_KEY

    event_dict[REDACTED_MARKER_KEY] = True
    try:
        state.sentry_processor(None, method, event_dict)
    except Exception:  # noqa: BLE001 - hooks must never break logging
        pass


def merge_fields(
    bound: dict[str, Any], kwargs: dict[str, Any], contextvars: dict[str, Any]
) -> dict[str, Any]:
    """Merge a record's field sources exactly as the fused native path does.

    ``bound`` first, then ``kwargs`` (overriding in place; ``exc_info`` and
    ``stack_info`` are never fields), then contextvars for keys still absent.
    Used only when a Python hook must see the merged mapping before rendering.
    """
    assert _RUST is not None
    return _RUST.merge_fields(bound, kwargs, contextvars)


def build_exception_field(
    exc_info: Any,
    runtime: _RuntimeState | None = None,
) -> str | dict[str, Any] | None:
    """Build the ``exception`` field value for a native record.

    Returns the structured dict when ``structured_exceptions`` is enabled
    (matching ``structguru.exceptions.build_exception_dict`` exactly, or
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
    while frame is not None:
        # Only the package itself: ``structguru_ext`` and friends are user code.
        module = frame.f_globals.get("__name__", "")
        if module != "structguru" and not module.startswith("structguru."):
            break
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
    each frame's ``colno``/``end_colno``. A ``traceback.FrameSummary``
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
    over from a previous call; use ``update`` for incremental changes.
    Invalid settings leave the active runtime untouched. Reconfiguration replaces
    writers and resets sampling/rate-limit state. Sinks added with ``logger.add()``
    survive reconfiguration and are not included in ``get_config``.

    ``target`` selects the background writer's sink: ``"stdout"`` (default,
    12-factor), ``"null"`` (discard), or ``"memory"`` (inspection/tests).

    The output queue is bounded to 8192 records by default. ``overflow`` governs a
    full queue: ``"block"`` (default, no loss — the caller waits with the GIL
    released) or ``"drop"`` (drop the new record, count it, and emit a rate-limited
    warning; see ``writer_metrics``). Pass ``maxsize=0`` only to explicitly opt
    into an unbounded queue.

    ``sensitive_patterns`` is a list of regex source strings applied (in addition
    to key-based redaction) to the message, the logger, service, and stack text,
    every field key and string value at any depth, and integers by their decimal
    digits (a matching integer is emitted as the redacted string). Rust's ``regex`` engine
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
    pre-render filters: dropped records cost zero rendering. The rate limiter
    keys on the formatted message, so a message that embeds a counter or an ID
    is never limited; its key table holds at most 8192 messages and evicts the
    least recently seen when full. ``sampled`` and
    ``rate_limited`` counters are reported separately from the writer's transport
    ``dropped`` counter (see ``writer_metrics``). ``sample_max_level``
    restricts sampling to records at or below that level (more severe records
    always pass) — the native analog of level-gated sampling.

    ``metric_processor`` is a structlog-style processor (e.g.
    ``structguru.metrics.MetricProcessor``) invoked for every *kept* record
    with ``(None, method, {"event": message, **fields})`` before rendering; the
    message stays in ``"event"`` even when a field has that name.
    Records dropped by level filtering, sampling, or rate limiting never reach
    it. Exceptions raised by the processor are swallowed.

    ``structured_exceptions=True`` renders the ``exception`` field as the
    structured dict produced by
    ``structguru.exceptions.build_exception_dict`` (with the
    ``exception_*`` knobs mirroring the processor's parameters,
    ``sensitive_keys`` reused for locals redaction, and ``sensitive_patterns``
    applied to each local's whole rendering before it is truncated) instead of
    the formatted traceback string.

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
    mirror ``logging.handlers.RotatingFileHandler``: ``file_max_bytes=50MB``,
    ``file_backup_count=5``. Set ``also_stdout=True`` to mirror output to both
    the file and stdout (e.g. container + persistent log).

    ``callable_sinks`` is a list of ``Callable[[str], None]`` invoked with each
    *rendered* line. They run on a dedicated daemon thread, one record at a time
    and in log order, also across reconfiguration. Their queue is bounded
    by ``callable_queue_maxsize``; ``overflow="block"`` applies backpressure when
    full, while ``overflow="drop"`` drops and counts the callable delivery.
    Exceptions they raise are counted in ``callable_errors`` (see
    ``writer_metrics``) and reported by a rate-limited warning.
    Logs emitted inside these callbacks still reach the native writer but skip
    callable sinks, preventing feedback loops and worker self-deadlocks.

    Registers fork (``os.register_at_fork``) handlers so the background writer is
    respawned in forked children (gunicorn/celery prefork) instead of deadlocking.
    Buffered records are drained at interpreter exit by an ``atexit`` handler
    registered when structguru is imported, so ``atexit`` handlers registered
    later — even before this call — still log before that drain.
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
    # Resolve console/colors from the chosen format.
    new_console = format == "console"
    new_colors = colors if colors is not None else (sys.stdout.isatty() if new_console else False)

    # Compile the per-record policy once: service, redaction keys and patterns,
    # output format, overflow. Rust's `regex` guarantees linear-time matching
    # and therefore rejects backreferences and look-around; fail loudly at
    # setup time — redaction that silently differs from what was configured is
    # worse than an error. `allow_backtracking_patterns` routes rejected
    # patterns to the bounded backtracking engine instead (validated the same way).
    try:
        new_config = _RUST.RuntimeConfig(
            service,
            sensitive_keys=list(sensitive_keys) if sensitive_keys is not None else None,
            sensitive_patterns=list(sensitive_patterns) if sensitive_patterns else None,
            pattern_replacement=pattern_replacement,
            allow_backtracking=allow_backtracking_patterns,
            console=new_console,
            colors=new_colors,
            blocking=overflow == "block",
        )
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
            # Patterns must see each local's whole rendering before truncation.
            "redact": new_config.redact_patterns if new_config.pattern_count else None,
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
        config=new_config,
        settings=settings,
        maxsize=maxsize,
        target=target,
        overflow=overflow,
        file_path=file_path,
        file_max_bytes=file_max_bytes,
        file_backup_count=file_backup_count,
        also_stdout=also_stdout,
        level_threshold=_level_number(level),
        otel=otel,
        record_filter=new_filter,
        exception_config=new_exception_config,
        exception_carets=exception_carets,
        metric_processor=metric_processor,
        sentry_processor=sentry_processor,
        stream_sink=stream_sink,
        callable_sinks=tuple(callable_sinks or ()),
        callable_queue_maxsize=callable_queue_maxsize,
        enrich=otel or metric_processor is not None or sentry_processor is not None,
        fused=sentry_processor is None and stream_sink is None,
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


def _sync_callable_dispatcher(*, reconfigured: bool = True) -> None:
    """Converge callable dispatch on the newest runtime generation.

    After configure(), update(), or shutdown() (``reconfigured``), the active
    queue generation is replaced or stopped, and the records queued before are
    drained, bounded by ``_CALLABLE_STALL_TIMEOUT``. After a sink registration
    dispatch only converges: nothing is replaced or waited for when it already
    matches the runtime. Retries after a concurrent transition converge too;
    that transition applies its own change.
    """
    replacing = reconfigured
    while True:
        with _state_lock:
            generation = _lifecycle_generation
            state = _runtime
        if not replacing:
            _callable_dispatcher.sync(
                None if state is None else state.callable_sinks,
                maxsize=0 if state is None else state.callable_queue_maxsize,
            )
        elif state is None:
            _note_abandoned(_callable_dispatcher.disable(stall_timeout=_CALLABLE_STALL_TIMEOUT))
        else:
            _note_abandoned(
                _callable_dispatcher.configure(
                    state.callable_sinks,
                    maxsize=state.callable_queue_maxsize,
                    stall_timeout=_CALLABLE_STALL_TIMEOUT,
                )
            )
        with _state_lock:
            if generation == _lifecycle_generation:
                return
        replacing = False


def _register_lifecycle_hooks() -> None:
    """Register fork handlers exactly once (the atexit drain registers at import)."""
    global _hooks_registered
    with _state_lock:
        if _hooks_registered:
            return
        # register_at_fork is POSIX-only; on Windows (spawn) there is nothing to do.
        if hasattr(os, "register_at_fork"):
            os.register_at_fork(
                before=_before_fork,
                after_in_child=_after_in_child,
            )
        _hooks_registered = True


def _atexit_close() -> None:
    """Drain native and callable sinks on interpreter shutdown."""
    shutdown()


# Registered at import, before any application handler can be: atexit runs
# handlers last-in first-out, so every handler registered after importing
# structguru can still log before this final drain, even with
# STRUCTGURU_AUTOCONFIGURE=0 and a configure() call that comes later.
atexit.register(_atexit_close)


def _before_fork() -> None:
    """Flush buffered records in the parent before forking."""
    flush()


def _after_in_child() -> None:
    """Respawn the writer in the child: its worker thread did not survive fork."""
    global _runtime, _state_lock, _drop_lock, _lifecycle_rejected, _callable_dispatcher
    global _lifecycle_generation
    state = _runtime
    # No other Python thread survives fork. Replace inherited synchronization
    # objects before touching state that may have been locked by a vanished thread.
    _state_lock = threading.Lock()
    _drop_lock = threading.Lock()
    _lifecycle_rejected = 0
    if state is None or _RUST is None:
        _callable_dispatcher = _callable_dispatcher.fork_child(enabled=False)
        return
    state.writer.abandon()  # never join the parent's (now absent) worker thread
    respawned = False
    try:
        # Replay the full sink config so file paths and stdout mirroring survive fork.
        writer = _RUST._NativeStringWriter(
            state.maxsize,
            target=state.target,
            file_path=state.file_path,
            file_max_bytes=state.file_max_bytes,
            file_backup_count=state.file_backup_count,
            also_stdout=state.also_stdout,
        )
    except Exception as exc:  # noqa: BLE001 - os.fork() ignores errors raised by fork hooks
        # The abandoned writer has no worker: keeping it would accept records
        # nobody writes and hang flush(). Disable logging as shutdown() does.
        _runtime = None
        _lifecycle_generation += 1
        _report_fork_respawn_failure(exc)
    else:
        _runtime = replace(state, writer=writer)
        respawned = True
    finally:
        # The inherited dispatcher's worker thread and locks did not survive; a
        # fresh one takes over the registrations and never touches the old state.
        _callable_dispatcher = _callable_dispatcher.fork_child(enabled=respawned)


def _report_fork_respawn_failure(exc: Exception) -> None:
    """Tell the forked child's operator that its logging is disabled.

    Writes straight to fd 2: ``sys.stderr`` and the warnings machinery take
    locks that a thread which did not survive ``fork()`` may still hold.
    """
    message = (
        "structguru: logging is disabled in this forked process because the log "
        f"writer could not be restarted: {type(exc).__name__}: {exc}\n"
    )
    try:
        os.write(2, message.encode("utf-8", "replace"))
    except OSError:
        pass


def shutdown() -> None:
    """Drain accepted callbacks and native records, then disable logging.

    Calls still formatting or waiting for queue space can be rejected by the
    closing writer; ``lifecycle_metrics`` counts these native deliveries.
    Calls started after shutdown completes are disabled and are not counted.
    """
    global _runtime, _lifecycle_generation
    # Callback-generated logs need a live native writer until callbacks drain.
    _note_abandoned(_callable_dispatcher.stop(drain=True, stall_timeout=_CALLABLE_STALL_TIMEOUT))
    with _state_lock:
        old_runtime = _runtime
        _runtime = None
        _lifecycle_generation += 1
    if old_runtime is not None:
        old_runtime.writer.close()
    _sync_callable_dispatcher()


def render_and_enqueue(
    runtime: _RuntimeState,
    logger: str,
    level: str,
    message: str,
    bound: dict[str, Any],
    kwargs: dict[str, Any],
    contextvars: dict[str, Any],
    exception: Any,
    stack: str | None,
    created: float | None = None,
    levelno: int | None = None,
) -> str | None:
    """Merge, render, and enqueue one record; return redacted JSON for Sentry when enabled.

    The writer merges ``bound``, ``kwargs`` (minus ``exc_info``/``stack_info``),
    and ``contextvars`` with the facade's semantics, adds ``exception`` as a
    field, redacts, timestamps (``created`` Unix seconds when given, otherwise
    the current time, cached per second), renders in the configured format,
    and pushes the line, all in one native call using the snapshot's
    compiled ``config``. The common production shape never builds a Python
    string; the line comes back only when a stream sink, callable sinks, or the
    Sentry hook need it.

    Callable sinks select and receive the record by ``levelno`` (a bridged
    stdlib record's own number) when given, otherwise by ``level``'s number.
    """
    writer = runtime.writer
    if runtime.fused and _callable_dispatcher.idle():
        outcome = writer.log(
            runtime.config,
            logger,
            level,
            message,
            bound,
            kwargs,
            contextvars,
            exception,
            stack,
            created,
        )
        if outcome:
            _note_enqueue_outcome(outcome)
        return None
    outcome, line, sentry_line = writer.log_capture(
        runtime.config,
        logger,
        level,
        message,
        bound,
        kwargs,
        contextvars,
        exception,
        stack,
        runtime.sentry_processor is not None,
        created,
    )
    if outcome:
        _note_enqueue_outcome(outcome)
        # A writer retired by configure/update/shutdown rejected the record: a
        # lifecycle boundary, counted by lifecycle_metrics(), not queue
        # overflow. No other destination receives it either.
        if outcome == _CLOSED:
            return None
    # Every destination follows the native writer's outcome. A record the
    # writer accepted just before a reconfiguration swapped the runtime still
    # reaches the stream, the callable sinks (through the replacement queue
    # generation, which delivers after its predecessors), and Sentry.
    if runtime.stream_sink is not None:
        try:
            runtime.stream_sink.write(line)
        except Exception as exc:  # noqa: BLE001 - stream errors must never break logging
            _note_stream_error(exc)
    # A stopped dispatcher ignores the line, so callable sinks receive nothing
    # once shutdown() has begun.
    _callable_dispatcher.enqueue(
        line,
        # A stdlib level may be any int; the dispatcher takes a 64-bit one.
        METHOD_LEVELS.get(level, 20) if levelno is None else min(levelno, _MAX_LEVELNO),
        overflow=runtime.overflow,
    )
    # Nor does Sentry, once shutdown() has disabled logging.
    return sentry_line if current_runtime() is not None else None


def _note_enqueue_outcome(outcome: int) -> None:
    """Account for the reason observed under the native queue lock."""
    global _lifecycle_rejected
    if outcome == _CLOSED:
        with _drop_lock:
            _lifecycle_rejected += 1
    elif outcome == _FULL:
        _note_drop()


def lifecycle_metrics() -> dict[str, int]:
    """Return cumulative native delivery rejections for this process.

    ``rejected`` counts records rejected because their native writer was closed,
    including producers interrupted by shutdown or reconfiguration. It excludes
    queue-full drops, sink errors, filtered records, and calls begun while logging
    is disabled. A rejected record reaches no destination: the stream sink,
    callable sinks, and Sentry follow the native writer's outcome.

    The counter survives configuration changes and shutdown; a forked child starts
    at zero. Unlike ``writer_metrics``, this always returns a snapshot.
    """
    with _drop_lock:
        return {"rejected": _lifecycle_rejected}


def _warn(message: str, *, stacklevel: int) -> None:
    """Issue a diagnostic ``UserWarning`` that never interrupts the caller.

    Diagnostics must not make logging fail. When a warnings filter turns the
    warning into an exception (``-W error``), the exception is reported through
    ``sys.unraisablehook`` instead, as CPython reports errors it cannot raise.
    ``stacklevel`` counts from the caller of this function.
    """
    try:
        warnings.warn(message, stacklevel=stacklevel + 1)
    except Exception as exc:  # noqa: BLE001 - diagnostics must never break logging
        assert _RUST is not None
        _RUST.report_unraisable(exc)


def _note_drop() -> None:
    """Emit a rate-limited warning when the queue drops a record (drop mode)."""
    global _drop_count
    with _drop_lock:
        _drop_count += 1
        dropped = _drop_count
    if dropped == 1 or dropped % 1000 == 0:
        _warn(f"structguru native logging dropped {dropped} record(s): queue full", stacklevel=3)


def _note_stream_error(exc: BaseException) -> None:
    """Count a synchronous stream sink failure and warn at a bounded rate."""
    global _stream_error_count
    with _drop_lock:
        _stream_error_count += 1
        errors = _stream_error_count
    if errors == 1 or errors % 1000 == 0:
        try:
            detail = str(exc)
        except Exception:  # noqa: BLE001 - diagnostics must never break logging
            detail = "<unprintable>"
        _warn(
            f"structguru stream sink write failed {errors} time(s), "
            f"latest {type(exc).__name__}: {detail}",
            stacklevel=3,
        )


def _note_abandoned(count: int) -> None:
    """Warn when a stalled callable sink forced deliveries to be abandoned."""
    if count:
        # A replacement generation (configure/update) keeps delivering; a
        # stopped or halted dispatcher waits for the next configure().
        halted = (
            "; callable sinks are disabled until the next configure()"
            if _callable_dispatcher.idle()
            else ""
        )
        _warn(
            f"structguru abandoned {count} callable sink delivery record(s): a sink "
            f"did not return within {_CALLABLE_STALL_TIMEOUT:g}s{halted}",
            stacklevel=3,
        )


def _reset_drop_count() -> None:
    """Reset the drop and stream error counters (used by tests)."""
    global _drop_count, _stream_error_count
    with _drop_lock:
        _drop_count = 0
        _stream_error_count = 0
    _callable_dispatcher.reset_drop_count()


def flush() -> None:
    """Block until every buffered record has been written out.

    Rendering hands each record to a background writer thread (and, for callable
    sinks, a bounded dispatch queue), so a returning ``logger.info()`` does not
    mean the line has reached its destination yet. Call this when you need that
    guarantee — before asserting on output in a test, or at a checkpoint where
    losing buffered records would matter.

    Not needed for normal shutdown: ``shutdown``, reconfiguration, fork, and
    interpreter exit all drain automatically.
    """
    # Callbacks may log to the native writer or replace it by reconfiguring.
    # Drain them first, then flush the resulting runtime's writer.
    _note_abandoned(_callable_dispatcher.flush(stall_timeout=_CALLABLE_STALL_TIMEOUT))
    state = current_runtime()
    if state is not None:
        state.writer.flush()


# Pre-1.0.5 name, kept as an alias: `flush` is the public spelling.
flush_native = flush


def drain_messages() -> list[str]:
    """Return all lines the writer has flushed to its in-memory sink (tests)."""
    state = current_runtime()
    return state.writer.messages() if state is not None else []


def writer_metrics() -> dict[str, Any] | None:
    """Return writer + filter metrics.

    Writer counters: ``enqueued``, ``dropped`` (queue-full), ``written``, ``depth``,
    and the other keys listed in the metrics reference. When a pre-render filter
    is active, ``sampled`` and ``rate_limited`` are added — these are distinct
    from the transport ``dropped`` counter.
    ``written`` counts records delivered to at least one native destination;
    ``sink_errors`` counts failed sink operations, including partial failures
    when another destination succeeds. ``stream_errors`` counts ``write()``
    calls on a synchronous ``stream_sink`` that raised; those lines reached the
    native destinations but not the stream.

    Callable sinks (``callable_sinks`` and ``logger.add()``) add
    ``callable_dropped`` (deliveries dropped by a full callable queue in drop
    mode), ``callable_errors`` (sink calls that raised; the first and every
    1000th emit a warning), ``callable_depth``, and ``callable_maxsize``. The
    callable counters restart when configure() or a non-level update() replaces
    the configuration; adding or removing a sink keeps them.

    Returns ``None`` while logging is disabled. Use ``lifecycle_metrics``
    for cumulative closed-writer rejections across configuration and shutdown.
    """
    state = current_runtime()
    if state is None:
        return None
    metrics = dict(state.writer.metrics())
    metrics.update(_callable_dispatcher.metrics())
    with _drop_lock:
        metrics["stream_errors"] = _stream_error_count
    if state.record_filter is not None:
        metrics.update(state.record_filter.stats())
    return metrics


def _maybe_configure_from_env() -> None:
    """Auto-configure native mode at import time (the default since v1.0).

    Set ``STRUCTGURU_AUTOCONFIGURE=0`` to skip import-time configuration. Logging
    then remains disabled until ``configure`` is called. The legacy inverse
    switch ``STRUCTGURU_LEGACY=1`` remains supported when the new switch is absent.

    ``Settings.from_env`` resolves native options and compatibility aliases.
    A missing extension or invalid environment value raises during import so the
    application cannot start with its only logging path silently disabled.
    """
    if _RUST is None:
        msg = "structguru requires its native extension, but structguru._rust is unavailable"
        raise RuntimeError(msg)
    if autoconfigure_from_env(os.environ):
        configure()


_maybe_configure_from_env()
