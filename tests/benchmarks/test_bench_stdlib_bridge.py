"""Benchmarks for stdlib ``logging`` records routed through the native bridge."""

from __future__ import annotations

import io
import logging
import sys
from typing import Any

import pytest

from structguru import _runtime
from structguru.integrations.stdlib import install_stdlib_bridge, uninstall_stdlib_bridge

pytestmark = pytest.mark.skipif(
    not _runtime.is_available(),
    reason="native extension not built",
)


def _capture_exc_info() -> Any:
    try:
        msg = "order 987 failed"
        raise RuntimeError(msg)
    except RuntimeError:
        return sys.exc_info()
    msg = "expected RuntimeError"
    raise AssertionError(msg)


@pytest.fixture
def bridged_logger() -> Any:
    """Yield a stdlib logger whose records render through the native bridge."""
    _runtime.configure(service="bench", target="null", level="INFO")
    handler = install_stdlib_bridge(level="INFO")
    try:
        yield logging.getLogger("bench.bridge")
        _runtime.flush()
        metrics = _runtime.writer_metrics()
        assert metrics is not None
        assert metrics["written"] == metrics["enqueued"]
        assert metrics["dropped"] == 0
    finally:
        uninstall_stdlib_bridge(handler)
        _runtime.shutdown()


def test_bench_stdlib_plain_stream_handler(benchmark: Any) -> None:
    """Benchmark stdlib logging alone: a text formatter writing to an in-memory stream."""
    stream = io.StringIO()
    log = logging.getLogger("bench.plain")
    log.handlers.clear()
    log.propagate = False
    log.setLevel(logging.INFO)
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    log.addHandler(handler)
    try:

        @benchmark
        def _() -> None:
            log.info("Hello world")
            stream.seek(0)
            stream.truncate()

    finally:
        log.removeHandler(handler)


def test_bench_stdlib_bridge_simple(benchmark: Any, bridged_logger: Any) -> None:
    """Benchmark a bare stdlib record through the bridge."""

    @benchmark
    def _() -> None:
        bridged_logger.info("Hello world")


def test_bench_stdlib_bridge_percent_args(benchmark: Any, bridged_logger: Any) -> None:
    """Benchmark stdlib percent-formatting arguments through the bridge."""

    @benchmark
    def _() -> None:
        bridged_logger.info("Hello %s, ID=%d", "world", 42)


def test_bench_stdlib_bridge_extra_fields(benchmark: Any, bridged_logger: Any) -> None:
    """Benchmark ``extra=`` fields promoted to structured fields by the bridge."""
    extra = {"request_id": "12345", "user_id": 42}

    @benchmark
    def _() -> None:
        bridged_logger.info("Hello world", extra=extra)


def test_bench_stdlib_bridge_unsupported_extra(benchmark: Any, bridged_logger: Any) -> None:
    """Benchmark the Django-shaped record whose ``extra`` carries an unsupported object."""
    extra = {"status_code": 500, "request": object()}

    @benchmark
    def _() -> None:
        bridged_logger.error("Internal Server Error: %s", "/orders", extra=extra)


def test_bench_stdlib_bridge_exc_info(benchmark: Any, bridged_logger: Any) -> None:
    """Benchmark a stdlib record carrying ``exc_info`` through the bridge."""
    exc_info = _capture_exc_info()

    @benchmark
    def _() -> None:
        bridged_logger.error("order failed", exc_info=exc_info)


def test_bench_stdlib_bridge_below_level(benchmark: Any, bridged_logger: Any) -> None:
    """Benchmark a stdlib record rejected by the root level before reaching the bridge."""

    @benchmark
    def _() -> None:
        bridged_logger.debug("debug payload %s", 42)
