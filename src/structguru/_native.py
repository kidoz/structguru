"""Import-time safe access to the optional native extension.

Also owns the experimental native render/enqueue fast path used by
:class:`structguru.core.Logger` when native mode is enabled.  The path is
opt-in via :func:`enable_native` and covers the common non-exception case:
brace-formatted message + structured fields are redacted and rendered to JSON
in Rust, then handed to a background writer thread (off-thread I/O).
"""

from __future__ import annotations

import importlib
import threading
from datetime import datetime, timezone
from typing import Any, Protocol, cast


class _NativeWriter(Protocol):
    def try_enqueue(self, message: str) -> bool: ...

    def flush(self) -> None: ...

    def close(self) -> None: ...

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
        timestamp: str,
    ) -> str: ...

    def _NativeStringWriter(self, maxsize: int) -> _NativeWriter: ...


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


def is_native_enabled() -> bool:
    """True when native mode is on and the extension is loaded."""
    return _enabled and _RUST is not None


def enable_native(*, service: str = "app", maxsize: int = 0) -> None:
    """Route the common log path through the native renderer + writer.

    ``maxsize=0`` is an unbounded queue; a positive size drops new records when
    full (see the drop counter in :func:`native_metrics`).
    """
    global _enabled, _writer, _service
    if _RUST is None:
        msg = "native extension is not available"
        raise RuntimeError(msg)
    with _state_lock:
        if _writer is not None:
            _writer.close()
        _writer = _RUST._NativeStringWriter(maxsize)
        _service = service
        _enabled = True


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
    """Render one record natively and enqueue it; returns False if dropped."""
    assert _RUST is not None and _writer is not None  # guarded by is_native_enabled
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    line = _RUST.render_line(fields, logger, level, _service, message, timestamp)
    return _writer.try_enqueue(line)


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
