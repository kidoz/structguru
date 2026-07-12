"""Functional correctness checks specific to free-threaded CPython."""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

import structguru
from structguru import _runtime


def _gil_enabled() -> bool:
    probe = getattr(sys, "_is_gil_enabled", None)
    return True if probe is None else bool(probe())


pytestmark = pytest.mark.skipif(
    _gil_enabled(),
    reason="requires a free-threaded CPython build",
)


def test_concurrent_logging_preserves_every_record() -> None:
    workers = 4
    records_per_worker = 250
    _runtime.configure(service="free-threaded", target="memory", maxsize=32)
    try:

        def emit(worker_id: int) -> None:
            for sequence in range(records_per_worker):
                structguru.logger.info(
                    "concurrent record",
                    worker_id=worker_id,
                    sequence=sequence,
                )

        with ThreadPoolExecutor(max_workers=workers) as executor:
            list(executor.map(emit, range(workers)))
        _runtime.flush_native()

        expected = workers * records_per_worker
        metrics = _runtime.writer_metrics()
        assert metrics is not None
        assert metrics["enqueued"] == expected
        assert metrics["written"] == expected
        assert metrics["dropped"] == 0
        assert len(_runtime.drain_messages()) == expected
    finally:
        _runtime.shutdown()
