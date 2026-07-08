"""Import-time safe access to the optional native extension.

Also owns the experimental native render/enqueue fast path used by
:class:`structguru.core.Logger` when native mode is enabled.  The path is
opt-in via :func:`enable_native` and covers the common non-exception case:
brace-formatted message + structured fields are redacted and rendered to JSON
in Rust, then handed to a background writer thread (off-thread I/O).
"""

from __future__ import annotations

import atexit
import importlib
import os
import threading
from typing import Any, Protocol, cast


class _NativeWriter(Protocol):
    def try_enqueue(self, message: str) -> bool: ...

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
    ) -> str: ...

    def _NativeStringWriter(self, maxsize: int, target: str = ...) -> _NativeWriter: ...


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
_hooks_registered = False


def is_native_enabled() -> bool:
    """True when native mode is on and the extension is loaded."""
    return _enabled and _RUST is not None


def enable_native(*, service: str = "app", maxsize: int = 0, target: str = "stdout") -> None:
    """Route the common log path through the native renderer + writer.

    ``target`` selects the background writer's sink: ``"stdout"`` (default,
    12-factor) or ``"memory"`` (records lines for inspection/tests).
    ``maxsize=0`` is an unbounded queue; a positive size drops new records when
    full (see the drop counter in :func:`native_metrics`).

    Registers shutdown (``atexit``) and fork (``os.register_at_fork``) handlers so
    buffered records are flushed on exit and the background writer is respawned in
    forked children (gunicorn/celery prefork) instead of deadlocking.
    """
    global _enabled, _writer, _service, _maxsize, _target
    if _RUST is None:
        msg = "native extension is not available"
        raise RuntimeError(msg)
    with _state_lock:
        if _writer is not None:
            _writer.close()
        _maxsize = maxsize
        _target = target
        _service = service
        _writer = _RUST._NativeStringWriter(maxsize, target=target)
        _enabled = True
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
    global _writer
    if not _enabled or _RUST is None:
        return
    old = _writer
    if old is not None:
        old.abandon()  # never join the parent's (now absent) worker thread
    _writer = _RUST._NativeStringWriter(_maxsize, target=_target)


def disable_native() -> None:
    """Turn native mode off and stop the background writer."""
    global _enabled, _writer
    with _state_lock:
        _enabled = False
        if _writer is not None:
            _writer.close()
            _writer = None


def render_and_enqueue(
    fields: dict[str, Any],
    logger: str,
    level: str,
    message: str,
) -> bool:
    """Render one record natively and enqueue it; returns False if dropped.

    The timestamp is generated inside the Rust core (cached per second), so no
    Python-side time formatting happens on the hot path.
    """
    assert _RUST is not None and _writer is not None  # guarded by is_native_enabled
    line = _RUST.render_line(fields, logger, level, _service, message)
    return _writer.try_enqueue(line + "\n")


def flush_native() -> None:
    """Block until the writer has drained (used by shutdown/tests)."""
    if _writer is not None:
        _writer.flush()


def drain_messages() -> list[str]:
    """Return all lines the writer has flushed to its in-memory sink (tests)."""
    return _writer.messages() if _writer is not None else []


def native_metrics() -> dict[str, Any] | None:
    """Return writer metrics (enqueued/dropped/written/depth/...)."""
    return _writer.metrics() if _writer is not None else None
