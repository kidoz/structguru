"""Benchmarks for production native logging pipeline paths."""

from __future__ import annotations

import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
import structguru._rust as rust

from structguru import _native, logger
from structguru.core import _safe_format

pytestmark = pytest.mark.skipif(
    not _native.native_available(),
    reason="native extension not built",
)


def _gil_enabled() -> bool:
    probe = getattr(sys, "_is_gil_enabled", None)
    return True if probe is None else bool(probe())


def _cpu_heavy_work(iterations: int, seed: int = 1) -> int:
    """Run deterministic pure-Python CPU work that contends on the GIL."""
    state = seed
    for index in range(iterations):
        state = ((state * 1_664_525) + 1_013_904_223 + index) & 0xFFFFFFFF
        state ^= state >> 13
    return state


def _cpu_and_logging_worker(worker_id: int) -> int:
    state = worker_id + 1
    for batch in range(8):
        state = _cpu_heavy_work(400, state)
        logger.info("worker batch", worker_id=worker_id, batch=batch, checksum=state)
    return state


def _threaded_workload(executor: ThreadPoolExecutor, workers: int) -> int:
    futures = [executor.submit(_cpu_and_logging_worker, worker_id) for worker_id in range(workers)]
    return sum(future.result() for future in futures)


def _request_fields() -> dict[str, Any]:
    return {
        "request": {
            "id": "req-123",
            "method": "POST",
            "path": "/orders",
            "headers": {"traceparent": "00-abc-def-01"},
        },
        "order": {
            "id": 987,
            "total": 42.5,
            "currency": "USD",
            "items": [
                {"sku": "A-1", "qty": 2},
                {"sku": "B-2", "qty": 1},
            ],
        },
        "retry": False,
    }


def test_bench_native_structured_record(benchmark: Any) -> None:
    """Benchmark formatting, conversion, rendering, and enqueue of nested fields."""
    fields = _request_fields()
    _native.configure(service="checkout", target="null", level="INFO")
    try:

        @benchmark
        def _() -> None:
            logger.info("order accepted", **fields)

        _native.flush_native()
        metrics = _native.native_metrics()
        assert metrics is not None
        assert metrics["written"] > 0
    finally:
        _native.disable_native()


def test_bench_native_contextvars_merge(benchmark: Any) -> None:
    """Benchmark a native record with request-scoped context already bound."""
    _native.configure(service="api", target="null", level="INFO")
    try:
        with logger.contextualize(request_id="req-123", tenant_id="tenant-7"):

            @benchmark
            def _() -> None:
                logger.info("request handled", status_code=200, duration_ms=12.5)

        _native.flush_native()
    finally:
        _native.disable_native()


def test_bench_native_redaction(benchmark: Any) -> None:
    """Benchmark key and compiled-pattern redaction on nested structured fields."""
    _native.configure(
        service="api",
        target="null",
        sensitive_keys=["authorization"],
        sensitive_patterns=[r"token-[A-Za-z0-9]+"],
    )
    fields = {
        "authorization": "Bearer token-secret123",
        "request": {"query": "access=token-secret123", "user_id": 42},
    }
    try:

        @benchmark
        def _() -> None:
            logger.info("authenticated token-secret123", **fields)

        _native.flush_native()
    finally:
        _native.disable_native()


def test_bench_native_disabled_level_fast_path(benchmark: Any) -> None:
    """Benchmark a record rejected before formatting and native rendering."""
    _native.configure(service="api", target="null", level="WARNING")
    try:

        @benchmark
        def _() -> None:
            logger.debug("debug payload {value}", value=42, nested={"enabled": True})

        metrics = _native.native_metrics()
        assert metrics is not None
        assert metrics["enqueued"] == 0
    finally:
        _native.disable_native()


def test_bench_native_sampling_drop_fast_path(benchmark: Any) -> None:
    """Benchmark deterministic sampling rejection before field construction."""
    _native.configure(service="api", target="null", sample_rate=0.0)
    try:

        @benchmark
        def _() -> None:
            logger.info("sampled event", nested={"value": 42})

        metrics = _native.native_metrics()
        assert metrics is not None
        assert metrics["sampled"] > 0
        assert metrics["enqueued"] == 0
    finally:
        _native.disable_native()


def test_bench_native_writer_blocking_enqueue(benchmark: Any) -> None:
    """Benchmark the PyO3 boundary and bounded Rust queue enqueue operation."""
    writer = rust._NativeStringWriter(8192, target="null")
    try:
        result = benchmark(writer.enqueue_blocking, '{"message":"queued"}\n')
        assert result
        writer.flush()
        assert writer.metrics()["written"] > 0
    finally:
        writer.close()


def test_bench_safe_format_cached_template(benchmark: Any) -> None:
    """Benchmark brace formatting after the template-key cache is warm."""
    message = "order {order_id} accepted for {customer.name}"
    kwargs = {"order_id": 987, "customer": _Customer("Ada")}
    expected = ("order 987 accepted for Ada", frozenset({"order_id", "customer"}))
    assert _safe_format(message, (), kwargs) == expected

    result = benchmark(_safe_format, message, (), kwargs)
    assert result == expected


class _Customer:
    def __init__(self, name: str) -> None:
        self.name = name


@pytest.mark.skipif(not _gil_enabled(), reason="requires a GIL-enabled CPython build")
@pytest.mark.parametrize("workers", [1, 4], ids=["one-thread", "four-threads"])
def test_bench_cpu_heavy_logging_gil(benchmark: Any, workers: int) -> None:
    """Benchmark CPU-heavy threaded logging on standard GIL-enabled CPython."""
    _native.configure(service="workers", target="null", level="INFO")
    try:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            checksum = benchmark(_threaded_workload, executor, workers)
        assert checksum > 0
        _native.flush_native()
        metrics = _native.native_metrics()
        assert metrics is not None
        assert metrics["enqueued"] == metrics["written"]
        assert metrics["dropped"] == 0
    finally:
        _native.disable_native()


@pytest.mark.skipif(_gil_enabled(), reason="requires a free-threaded CPython build")
@pytest.mark.parametrize("workers", [1, 4], ids=["one-thread", "four-threads"])
def test_bench_cpu_heavy_logging_free_threaded(benchmark: Any, workers: int) -> None:
    """Benchmark the same workload on a free-threaded CPython build."""
    _native.configure(service="workers", target="null", level="INFO")
    try:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            checksum = benchmark(_threaded_workload, executor, workers)
        assert checksum > 0
        _native.flush_native()
        metrics = _native.native_metrics()
        assert metrics is not None
        assert metrics["enqueued"] == metrics["written"]
        assert metrics["dropped"] == 0
    finally:
        _native.disable_native()


@pytest.mark.skipif(not _gil_enabled(), reason="GIL-release behavior is specific to GIL builds")
def test_bench_python_work_while_native_enqueue_is_blocked(benchmark: Any) -> None:
    """Verify blocking Rust backpressure releases the GIL for unrelated Python work."""
    writer = rust._NativeStringWriter(1, paused=True)
    started = threading.Event()
    finished = threading.Event()
    enqueue_results: list[bool] = []

    assert writer.try_enqueue("fills the bounded queue")

    def enqueue_blocked_record() -> None:
        started.set()
        enqueue_results.append(writer.enqueue_blocking("waits for queue space"))
        finished.set()

    producer = threading.Thread(target=enqueue_blocked_record)
    producer.start()
    try:
        assert started.wait(timeout=1)
        assert not finished.wait(timeout=0.05)

        checksum = benchmark(_cpu_heavy_work, 4_000)

        assert checksum > 0
        assert not finished.is_set()
    finally:
        writer.resume()
        producer.join(timeout=2)
        writer.flush()
        writer.close()

    assert not producer.is_alive()
    assert enqueue_results == [True]
