"""Sensitive data redaction processor.

Provides a processor that masks values associated with sensitive keys
(e.g. ``password``, ``token``, ``secret``) and optionally applies regex
patterns to string values.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED_MARKER_KEY = "_structguru_redacted"


def strip_redaction_marker(
    _logger: Any,
    _method_name: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    """Remove :data:`REDACTED_MARKER_KEY` so it does not reach the renderer."""
    event_dict.pop(REDACTED_MARKER_KEY, None)
    return event_dict


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
        redacted_dict = self._redact_dict(event_dict, {})
        redacted_dict[REDACTED_MARKER_KEY] = True
        return redacted_dict

    def _redact_dict(self, d: dict[str, Any], seen: dict[int, Any]) -> dict[str, Any]:
        """Recursively redact sensitive keys and apply regex patterns, returning a new dict."""
        obj_id = id(d)
        if obj_id in seen:
            return seen[obj_id]  # type: ignore[no-any-return]

        redacted_dict: dict[str, Any] = {}
        seen[obj_id] = redacted_dict

        for key, value in d.items():
            if isinstance(key, str) and key.lower() in self._keys:
                redacted_dict[key] = self._replacement
            else:
                redacted_dict[key] = self._redact_value(value, seen)
        return redacted_dict

    def _redact_value(self, value: Any, seen: dict[int, Any]) -> Any:
        """Redact a single value, recursing into dicts and lists, returning new objects."""
        if isinstance(value, dict):
            return self._redact_dict(value, seen)
        if isinstance(value, list):
            obj_id = id(value)
            if obj_id in seen:
                return seen[obj_id]
            redacted_list: list[Any] = []
            seen[obj_id] = redacted_list
            for item in value:
                redacted_list.append(self._redact_value(item, seen))
            return redacted_list
        if isinstance(value, str) and self._patterns:
            try:
                for pattern in self._patterns:
                    value = pattern.sub(self._replacement, value)
            except re.error:
                pass
        return value
