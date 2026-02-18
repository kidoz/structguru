"""Tests for structguru.sampling."""

from __future__ import annotations

import time

import pytest
import structlog

from structguru.sampling import RateLimitingProcessor, SamplingProcessor


class TestSamplingProcessor:
    def test_rate_1_keeps_all(self) -> None:
        proc = SamplingProcessor(rate=1.0)
        for _ in range(100):
            result = proc(None, "info", {"event": "test"})
            assert result["event"] == "test"

    def test_rate_0_drops_all(self) -> None:
        proc = SamplingProcessor(rate=0.0)
        with pytest.raises(structlog.DropEvent):
            proc(None, "info", {"event": "test"})

    def test_rate_between_0_and_1(self) -> None:
        proc = SamplingProcessor(rate=0.5)
        kept = 0
        total = 1000
        for _ in range(total):
            try:
                proc(None, "info", {"event": "test"})
                kept += 1
            except structlog.DropEvent:
                pass
        # With 1000 samples at 50%, we expect ~500 ± a wide margin
        assert 300 < kept < 700

    def test_invalid_rate_raises(self) -> None:
        with pytest.raises(ValueError, match="rate must be between"):
            SamplingProcessor(rate=1.5)
        with pytest.raises(ValueError, match="rate must be between"):
            SamplingProcessor(rate=-0.1)


class TestRateLimitingProcessor:
    def test_invalid_max_count_raises(self) -> None:
        with pytest.raises(ValueError, match="max_count must be >= 1"):
            RateLimitingProcessor(max_count=0)
        with pytest.raises(ValueError, match="max_count must be >= 1"):
            RateLimitingProcessor(max_count=-5)

    def test_invalid_period_raises(self) -> None:
        with pytest.raises(ValueError, match="period_seconds must be > 0"):
            RateLimitingProcessor(period_seconds=0)
        with pytest.raises(ValueError, match="period_seconds must be > 0"):
            RateLimitingProcessor(period_seconds=-1.0)

    def test_allows_under_limit(self) -> None:
        proc = RateLimitingProcessor(max_count=5, period_seconds=60.0)
        for _ in range(5):
            proc(None, "info", {"event": "test"})

    def test_drops_over_limit(self) -> None:
        proc = RateLimitingProcessor(max_count=3, period_seconds=60.0)
        for _ in range(3):
            proc(None, "info", {"event": "test"})
        with pytest.raises(structlog.DropEvent):
            proc(None, "info", {"event": "test"})

    def test_different_keys_independent(self) -> None:
        proc = RateLimitingProcessor(max_count=1, period_seconds=60.0)
        proc(None, "info", {"event": "alpha"})
        proc(None, "info", {"event": "beta"})
        with pytest.raises(structlog.DropEvent):
            proc(None, "info", {"event": "alpha"})

    def test_window_expiry(self) -> None:
        proc = RateLimitingProcessor(max_count=1, period_seconds=0.05)
        proc(None, "info", {"event": "test"})
        with pytest.raises(structlog.DropEvent):
            proc(None, "info", {"event": "test"})
        time.sleep(0.06)
        proc(None, "info", {"event": "test"})  # should not raise

    def test_stale_keys_cleaned_up(self) -> None:
        proc = RateLimitingProcessor(max_count=1, period_seconds=0.01)
        proc._cleanup_interval = 1  # trigger cleanup on every call

        # Generate unique keys to populate the dict
        for i in range(5):
            proc(None, "info", {"event": f"evt-{i}"})
        assert len(proc._timestamps) == 5
        time.sleep(0.02)  # let all entries expire

        # This call triggers cleanup, which evicts all expired keys
        proc(None, "info", {"event": "final"})
        assert len(proc._timestamps) == 1
        assert "final" in proc._timestamps
