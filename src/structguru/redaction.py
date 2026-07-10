"""Sensitive data redaction constants.

The redaction logic itself runs in the native Rust renderer
(``configure(sensitive_keys=..., sensitive_patterns=...)``).
This module retains the shared constants used across the codebase.
"""

from __future__ import annotations

REDACTED_MARKER_KEY = "_structguru_redacted"

DEFAULT_SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "authorization",
        "cookie",
        "session_id",
        "credit_card",
        "ssn",
        "private_key",
    }
)
