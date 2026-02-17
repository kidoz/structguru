"""Conditional processor chains.

Provides a processor wrapper that routes events through different processors
based on log level, enabling level-specific behaviour (e.g. full stack traces
only for ERROR+, sampling only for DEBUG).
"""

from __future__ import annotations

import logging
from typing import Any

import structlog


class ConditionalProcessor:
    """Run *processor* only when the event's log level is within range.

    Parameters
    ----------
    processor:
        The processor to conditionally apply.
    min_level:
        Minimum level name (inclusive).  Defaults to ``"DEBUG"``.
    max_level:
        Maximum level name (inclusive).  Defaults to ``"CRITICAL"``.
    """

    _LEVEL_LOOKUP: dict[str, int] = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "WARN": logging.WARNING,
        "ERROR": logging.ERROR,
        "CRITICAL": logging.CRITICAL,
    }

    def __init__(
        self,
        processor: structlog.types.Processor,
        *,
        min_level: str = "DEBUG",
        max_level: str = "CRITICAL",
    ) -> None:
        self._processor = processor
        self._min = self._LEVEL_LOOKUP.get(min_level.upper(), logging.DEBUG)
        self._max = self._LEVEL_LOOKUP.get(max_level.upper(), logging.CRITICAL)
        if self._min > self._max:
            msg = f"min_level {min_level!r} ({self._min}) > max_level {max_level!r} ({self._max})"
            raise ValueError(msg)

    def __call__(
        self,
        logger: Any,
        method_name: str,
        event_dict: dict[str, Any],
    ) -> dict[str, Any]:
        level_num = self._LEVEL_LOOKUP.get(method_name.upper(), logging.INFO)
        if self._min <= level_num <= self._max:
            result = self._processor(logger, method_name, event_dict)
            if isinstance(result, dict):
                return result
        return event_dict
