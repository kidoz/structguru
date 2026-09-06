"""Benchmarks for pre-render filters, redaction scaling, and processor hooks."""

from __future__ import annotations

from typing import Any

import pytest

from structguru import DEFAULT_SENSITIVE_KEYS, MetricProcessor, _runtime, logger

pytestmark = pytest.mark.skipif(
    not _runtime.is_available(),
    reason="native extension not built",
)

_ORDER_FIELDS: dict[str, Any] = {
    "order_id": 987,
    "status": "paid",
    "total": 42.5,
    "currency": "USD",
    "customer_id": 42,
    "region": "eu-west-1",
    "channel": "web",
    "attempt": 1,
    "duration_ms": 12.5,
    "cache_hit": False,
    "authorization": "Bearer token-secret123",
    "session_id": "sess-abc",
}

_EIGHT_PATTERNS = [
    r"token-[A-Za-z0-9]+",
    r"\b\d{16}\b",
    r"\b\d{3}-\d{2}-\d{4}\b",
    r"(?i)bearer\s+\S+",
    r"AKIA[0-9A-Z]{16}",
    r"sk_(live|test)_[0-9a-zA-Z]{24}",
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
    r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+",
]


def _bench_record(
    benchmark: Any,
    message: str,
    fields: dict[str, Any],
    *,
    level: str = "info",
    prime: int = 0,
    **config: Any,
) -> dict[str, Any]:
    """Benchmark one log call; ``prime`` records are emitted first, outside the timing."""
    _runtime.configure(service="bench", target="null", level="INFO", **config)
    try:
        emit = getattr(logger, level)
        for _ in range(prime):
            emit(message, **fields)

        @benchmark
        def _() -> None:
            emit(message, **fields)

        _runtime.flush()
        metrics = _runtime.writer_metrics()
        assert metrics is not None
        assert metrics["written"] == metrics["enqueued"]
        assert metrics["dropped"] == 0
        return metrics
    finally:
        _runtime.shutdown()


def test_bench_native_rate_limit_pass_path(benchmark: Any) -> None:
    """Benchmark the rate limiter's bookkeeping when every record is admitted."""
    metrics = _bench_record(
        benchmark,
        "order accepted",
        {"order_id": 987},
        rate_limit_max=10**9,
        rate_limit_period=60.0,
    )
    assert metrics["rate_limited"] == 0
    assert metrics["enqueued"] > 0


def test_bench_native_rate_limit_drop_path(benchmark: Any) -> None:
    """Benchmark a record rejected by the rate limiter before fields are built.

    One record is emitted first to use up the window, so every timed call is
    rejected.
    """
    metrics = _bench_record(
        benchmark,
        "order accepted",
        {"order_id": 987, "nested": {"value": 42}},
        prime=1,
        rate_limit_max=1,
        rate_limit_period=3600.0,
    )
    assert metrics["rate_limited"] >= 1
    assert metrics["enqueued"] == 1


def test_bench_native_sampling_level_gate_pass_path(benchmark: Any) -> None:
    """Benchmark a record above ``sample_max_level`` that bypasses a zero sample rate."""
    metrics = _bench_record(
        benchmark,
        "order failed",
        {"order_id": 987},
        level="warning",
        sample_rate=0.0,
        sample_max_level="INFO",
    )
    assert metrics["sampled"] == 0
    assert metrics["enqueued"] > 0


def test_bench_native_redaction_default_keys(benchmark: Any) -> None:
    """Benchmark key redaction with the full default key list on a twelve-field record."""
    _bench_record(
        benchmark,
        "order accepted",
        _ORDER_FIELDS,
        sensitive_keys=sorted(DEFAULT_SENSITIVE_KEYS),
    )


def test_bench_native_redaction_eight_patterns(benchmark: Any) -> None:
    """Benchmark value redaction with eight compiled patterns on a twelve-field record."""
    _bench_record(
        benchmark,
        "order accepted for user@example.com",
        _ORDER_FIELDS,
        sensitive_keys=["authorization"],
        sensitive_patterns=_EIGHT_PATTERNS,
    )


def test_bench_native_metric_processor_hook(benchmark: Any) -> None:
    """Benchmark the per-record cost of a matching counter and histogram callback."""
    counted: list[int] = []
    observed: list[float] = []
    processor = (
        MetricProcessor()
        .counter("order accepted", lambda _event: counted.append(1))
        .histogram("order accepted", "duration_ms", lambda value, _event: observed.append(value))
    )
    metrics = _bench_record(
        benchmark,
        "order accepted",
        {"order_id": 987, "duration_ms": 12.5},
        metric_processor=processor,
    )
    assert len(counted) == metrics["enqueued"]
    assert len(observed) == metrics["enqueued"]
