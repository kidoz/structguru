"""Import-time safe access to the optional native extension."""

from __future__ import annotations

import importlib
from typing import Protocol, cast


class _RustModule(Protocol):
    def normalize_level(self, level: str) -> str: ...

    def syslog_severity(self, canonical_level: str) -> int: ...


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
