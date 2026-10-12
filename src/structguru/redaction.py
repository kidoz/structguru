"""Sensitive data redaction constants, sourced from the native extension.

The redaction logic itself runs in the native Rust renderer
(``configure(sensitive_keys=..., sensitive_patterns=...)``), and the Rust
core owns the key set and marker key; this module builds the Python
constants from it once at import, so message-template redaction,
exception-locals redaction, and structured-field redaction can never
disagree about which keys are sensitive.
"""

from __future__ import annotations

import importlib
from types import ModuleType


def _load_native_module() -> ModuleType:
    """Load the native extension, failing loudly when it is missing."""
    try:
        return importlib.import_module("structguru._rust")
    except ModuleNotFoundError as exc:
        if exc.name != "structguru._rust":
            raise
        msg = "structguru requires its native extension, but structguru._rust is unavailable"
        raise RuntimeError(msg) from exc


_native = _load_native_module()

# Marker key set on the event dict once redaction has run; the renderer
# strips it before output and the Sentry adapter uses it as a guard.
REDACTED_MARKER_KEY: str = _native._redacted_marker_key()

# Default key set for key-based redaction, matched case-insensitively.
DEFAULT_SENSITIVE_KEYS: frozenset[str] = frozenset(_native._default_sensitive_keys())

del _native
