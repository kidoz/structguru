"""Benchmarks for the native writer's output sinks and the callable dispatcher."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import structguru._rust as rust

from structguru import _runtime, logger

pytestmark = pytest.mark.skipif(
    not _runtime.is_available(),
    reason="native extension not built",
)

_BATCH = 256
_LINE = '{"level":"INFO","service":"bench","message":"order accepted","order_id":987}\n'
# Mirror the configure() defaults so the rotating case measures what users get.
_DEFAULT_FILE_MAX_BYTES = 50 * 1024 * 1024
_DEFAULT_FILE_BACKUP_COUNT = 5


def _drain_batch(writer: Any) -> None:
    """Enqueue one batch and wait until the writer thread has written all of it."""
    enqueue = writer.enqueue_blocking
    for _ in range(_BATCH):
        enqueue(_LINE)
    writer.flush()


def _bench_writer(benchmark: Any, writer: Any) -> None:
    try:
        benchmark(_drain_batch, writer)
        metrics = writer.metrics()
        assert metrics["written"] == metrics["enqueued"]
        assert metrics["dropped"] == 0
    finally:
        writer.close()


def test_bench_native_writer_null_sink_batch(benchmark: Any) -> None:
    """Benchmark queue hand-off plus drain for 256 records with no output cost."""
    _bench_writer(benchmark, rust._NativeStringWriter(8192, target="null"))


def test_bench_native_writer_file_sink_rotating_batch(benchmark: Any, tmp_path: Path) -> None:
    """Benchmark 256 records through the rotating file sink at its default settings."""
    writer = rust._NativeStringWriter(
        8192,
        file_path=str(tmp_path / "app.log"),
        file_max_bytes=_DEFAULT_FILE_MAX_BYTES,
        file_backup_count=_DEFAULT_FILE_BACKUP_COUNT,
    )
    _bench_writer(benchmark, writer)


def test_bench_native_writer_file_sink_no_rotation_batch(benchmark: Any, tmp_path: Path) -> None:
    """Benchmark 256 records through the file sink with rotation disabled."""
    writer = rust._NativeStringWriter(
        8192,
        file_path=str(tmp_path / "app.log"),
        file_max_bytes=0,
        file_backup_count=0,
    )
    _bench_writer(benchmark, writer)


@pytest.mark.parametrize(
    "file_max_bytes",
    [0, _DEFAULT_FILE_MAX_BYTES],
    ids=["no-rotation", "rotating"],
)
def test_bench_native_file_sink_backpressure(
    benchmark: Any, tmp_path: Path, file_max_bytes: int
) -> None:
    """Benchmark a caller throttled by a small blocking queue in front of the file sink.

    With ``maxsize=64`` the queue fills during warm-up, so each call waits for the
    writer thread to free a slot: the measured time is the writer's per-record cost.
    """
    _runtime.configure(
        service="bench",
        target="null",
        level="INFO",
        maxsize=64,
        file_path=str(tmp_path / "app.log"),
        file_max_bytes=file_max_bytes,
        file_backup_count=_DEFAULT_FILE_BACKUP_COUNT if file_max_bytes else 0,
    )
    try:

        @benchmark
        def _() -> None:
            logger.info("order accepted", order_id=987, status="paid")

        _runtime.flush()
        metrics = _runtime.writer_metrics()
        assert metrics is not None
        assert metrics["written"] == metrics["enqueued"]
        assert metrics["dropped"] == 0
    finally:
        _runtime.shutdown()


def test_bench_native_callable_sink(benchmark: Any) -> None:
    """Benchmark the caller-side cost of fanning rendered lines out to a callable sink."""
    received: list[str] = []
    _runtime.configure(
        service="bench",
        target="null",
        level="INFO",
        callable_sinks=[received.append],
    )
    try:

        @benchmark
        def _() -> None:
            logger.info("order accepted", order_id=987, status="paid")

        _runtime.flush()
        metrics = _runtime.writer_metrics()
        assert metrics is not None
        assert metrics["dropped"] == 0
        assert len(received) == metrics["enqueued"]
    finally:
        _runtime.shutdown()
