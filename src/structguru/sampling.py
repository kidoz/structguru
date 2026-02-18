"""Log sampling and rate-limiting processors.

Provides processors that drop events probabilistically or based on per-key
rate limits, useful for suppressing noisy logs (health checks, repeated errors).
"""

from __future__ import annotations

import random
import threading
import time
from collections import defaultdict, deque
from typing import Any

import structlog


class SamplingProcessor:
    """Drop events with probability ``1 - rate``.

    Parameters
    ----------
    rate:
        Fraction of events to keep (``0.0``–``1.0``).  ``1.0`` keeps all.
    """

    def __init__(self, rate: float = 1.0) -> None:
        if not 0.0 <= rate <= 1.0:
            msg = f"rate must be between 0.0 and 1.0, got {rate}"
            raise ValueError(msg)
        self._rate = rate

    def __call__(
        self,
        _logger: Any,
        _method_name: str,
        event_dict: dict[str, Any],
    ) -> dict[str, Any]:
        if self._rate < 1.0 and random.random() > self._rate:  # noqa: S311
            raise structlog.DropEvent
        return event_dict


class RateLimitingProcessor:
    """Allow at most *max_count* messages per *key* per *period_seconds*.

    Parameters
    ----------
    max_count:
        Maximum number of events per key within the period.
    period_seconds:
        Sliding window duration in seconds.
    key:
        Event-dict key used to group messages (default ``"event"``).
    """

    def __init__(
        self,
        *,
        max_count: int = 10,
        period_seconds: float = 60.0,
        key: str = "event",
    ) -> None:
        if max_count < 1:
            msg = f"max_count must be >= 1, got {max_count}"
            raise ValueError(msg)
        if period_seconds <= 0:
            msg = f"period_seconds must be > 0, got {period_seconds}"
            raise ValueError(msg)
        self._max_count = max_count
        self._period = period_seconds
        self._key_field = key
        self._timestamps: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()
        self._cleanup_counter = 0
        self._cleanup_interval = 1000

    def __call__(
        self,
        _logger: Any,
        _method_name: str,
        event_dict: dict[str, Any],
    ) -> dict[str, Any]:
        event_key = str(event_dict.get(self._key_field, ""))
        now = time.monotonic()

        with self._lock:
            cutoff = now - self._period
            ts = self._timestamps[event_key]
            while ts and ts[0] <= cutoff:
                ts.popleft()

            if len(ts) >= self._max_count:
                raise structlog.DropEvent

            ts.append(now)

            self._cleanup_counter += 1
            if self._cleanup_counter >= self._cleanup_interval:
                self._cleanup_counter = 0
                stale = []
                for k, v in self._timestamps.items():
                    while v and v[0] <= cutoff:
                        v.popleft()
                    if not v:
                        stale.append(k)
                for k in stale:
                    del self._timestamps[k]

        return event_dict
