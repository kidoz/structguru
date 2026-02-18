"""Metric extraction processor.

Provides a processor that derives counters and histograms from structured
log events by calling user-registered callbacks.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


class MetricProcessor:
    """Extract metrics from log events via registered callbacks.

    Example::

        metrics = MetricProcessor()
        metrics.counter("user.login", lambda ed: login_counter.inc())
        metrics.histogram("db.query", "duration_ms", lambda v, ed: query_hist.observe(v))
    """

    def __init__(self) -> None:
        self._counters: dict[str, Callable[[dict[str, Any]], None]] = {}
        self._histograms: dict[str, tuple[Callable[[float, dict[str, Any]], None], str]] = {}

    def counter(
        self,
        event_pattern: str,
        callback: Callable[[dict[str, Any]], None],
    ) -> MetricProcessor:
        """Register a counter callback for events matching *event_pattern*."""
        self._counters[event_pattern] = callback
        return self

    def histogram(
        self,
        event_pattern: str,
        value_key: str,
        callback: Callable[[float, dict[str, Any]], None],
    ) -> MetricProcessor:
        """Register a histogram callback for events matching *event_pattern*."""
        self._histograms[event_pattern] = (callback, value_key)
        return self

    def __call__(
        self,
        _logger: Any,
        _method_name: str,
        event_dict: dict[str, Any],
    ) -> dict[str, Any]:
        event = str(event_dict.get("event", ""))

        for pattern, callback in self._counters.items():
            if pattern in event:
                try:
                    callback(event_dict)
                except Exception:
                    pass

        for pattern, (hist_callback, value_key) in self._histograms.items():
            if pattern in event and value_key in event_dict:
                try:
                    hist_callback(float(event_dict[value_key]), event_dict)
                except Exception:
                    pass

        return event_dict
