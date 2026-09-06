"""Benchmarks for the Python facade and for logging-only thread contention."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from structguru import Logger, _runtime, logger

pytestmark = pytest.mark.skipif(
    not _runtime.is_available(),
    reason="native extension not built",
)

_RECORDS_PER_WORKER = 250


def _log_only_worker(worker_id: int) -> int:
    for sequence in range(_RECORDS_PER_WORKER):
        logger.info("worker record", worker_id=worker_id, sequence=sequence)
    return _RECORDS_PER_WORKER


def _threaded_logging(executor: ThreadPoolExecutor, workers: int) -> int:
    futures = [executor.submit(_log_only_worker, worker_id) for worker_id in range(workers)]
    return sum(future.result() for future in futures)


def test_bench_native_named_logger(benchmark: Any) -> None:
    """Benchmark a logger with an explicit name, which skips the caller-frame walk."""
    named = Logger(name="bench.named")
    _runtime.configure(service="bench", target="null", level="INFO")
    try:

        @benchmark
        def _() -> None:
            named.info("Hello world")

        _runtime.flush()
    finally:
        _runtime.shutdown()


def test_bench_native_bind_per_call(benchmark: Any) -> None:
    """Benchmark the per-request pattern of binding context and logging in one expression."""
    _runtime.configure(service="bench", target="null", level="INFO")
    try:

        @benchmark
        def _() -> None:
            logger.bind(request_id="12345", user_id=42).info("With context")

        _runtime.flush()
    finally:
        _runtime.shutdown()


def test_bench_native_positional_formatting(benchmark: Any) -> None:
    """Benchmark brace formatting driven by positional arguments."""
    _runtime.configure(service="bench", target="null", level="INFO")
    try:

        @benchmark
        def _() -> None:
            logger.info("Hello {}, ID={}", "world", 42)

        _runtime.flush()
    finally:
        _runtime.shutdown()


@pytest.mark.parametrize("workers", [1, 4, 8], ids=["one-thread", "four-threads", "eight-threads"])
def test_bench_native_logging_only_contention(benchmark: Any, workers: int) -> None:
    """Benchmark threads that do nothing but log, isolating contention on the logging path.

    Each worker emits 250 records, so the total work scales with ``workers``; compare
    the per-round time against the one-thread case to see how the path serialises.
    """
    _runtime.configure(service="bench", target="null", level="INFO")
    try:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            total = benchmark(_threaded_logging, executor, workers)
        assert total == workers * _RECORDS_PER_WORKER
        _runtime.flush()
        metrics = _runtime.writer_metrics()
        assert metrics is not None
        assert metrics["written"] == metrics["enqueued"]
        assert metrics["dropped"] == 0
    finally:
        _runtime.shutdown()
