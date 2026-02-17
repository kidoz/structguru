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
        for key in list(event_dict):
            if key.lower() in self._keys:
                event_dict[key] = self._replacement
            elif isinstance(event_dict[key], str) and self._patterns:
                val = event_dict[key]
                try:
                    for pattern in self._patterns:
                        val = pattern.sub(self._replacement, val)
                except re.error:
                    pass
                event_dict[key] = val
        return event_dict
