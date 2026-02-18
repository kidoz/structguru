"""Sensitive data redaction processor.

Provides a processor that masks values associated with sensitive keys
(e.g. ``password``, ``token``, ``secret``) and optionally applies regex
patterns to string values.
"""

from __future__ import annotations

import re
from typing import Any

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


class RedactingProcessor:
    """Structlog processor that redacts sensitive data from event dicts.

    Parameters
    ----------
    sensitive_keys:
        Lower-cased key names whose values are fully replaced.
        Defaults to :data:`DEFAULT_SENSITIVE_KEYS`.
    patterns:
        Compiled regex patterns applied to all string values.
    replacement:
        The replacement string used for redacted values.
    """

    def __init__(
        self,
        *,
        sensitive_keys: frozenset[str] | None = None,
        patterns: list[re.Pattern[str]] | None = None,
        replacement: str = "[REDACTED]",
    ) -> None:
        self._keys = sensitive_keys if sensitive_keys is not None else DEFAULT_SENSITIVE_KEYS
        self._patterns = patterns or []
        self._replacement = replacement

    def __call__(
        self,
        _logger: Any,
        _method_name: str,
        event_dict: dict[str, Any],
    ) -> dict[str, Any]:
        self._redact_dict(event_dict, set())
        return event_dict

    def _redact_dict(self, d: dict[str, Any], seen: set[int]) -> None:
        """Recursively redact sensitive keys and apply regex patterns."""
        obj_id = id(d)
        if obj_id in seen:
            return
        seen.add(obj_id)
        for key in list(d):
            if isinstance(key, str) and key.lower() in self._keys:
                d[key] = self._replacement
            else:
                d[key] = self._redact_value(d[key], seen)

    def _redact_value(self, value: Any, seen: set[int]) -> Any:
        """Redact a single value, recursing into dicts and lists."""
        if isinstance(value, dict):
            self._redact_dict(value, seen)
            return value
        if isinstance(value, list):
            obj_id = id(value)
            if obj_id in seen:
                return value
            seen.add(obj_id)
            return [self._redact_value(item, seen) for item in value]
        if isinstance(value, str) and self._patterns:
            try:
                for pattern in self._patterns:
                    value = pattern.sub(self._replacement, value)
            except re.error:
                pass
        return value
