"""Benchmarks for exception and stack rendering on the native pipeline."""

from __future__ import annotations

import sys
from typing import Any

import pytest

from structguru import _runtime, logger

pytestmark = pytest.mark.skipif(
    not _runtime.is_available(),
    reason="native extension not built",
)


def _raise_at_depth(depth: int, order_id: int = 987) -> None:
    if depth == 0:
        msg = f"order {order_id} failed"
        raise RuntimeError(msg)
    _raise_at_depth(depth - 1, order_id)


def _capture_exc_info(depth: int) -> Any:
    try:
        _raise_at_depth(depth)
    except RuntimeError:
        return sys.exc_info()
    msg = "expected RuntimeError"
    raise AssertionError(msg)


def _bench_exception(benchmark: Any, depth: int, **config: Any) -> None:
    exc_info = _capture_exc_info(depth)
    _runtime.configure(service="bench", target="null", level="INFO", **config)
    try:
        failing = logger.opt(exception=exc_info)

        @benchmark
        def _() -> None:
            failing.error("order failed", order_id=987)

        _runtime.flush()
        metrics = _runtime.writer_metrics()
        assert metrics is not None
        assert metrics["written"] == metrics["enqueued"]
        assert metrics["dropped"] == 0
    finally:
        _runtime.shutdown()


@pytest.mark.parametrize("carets", [True, False], ids=["carets", "no-carets"])
@pytest.mark.parametrize("depth", [1, 20], ids=["shallow", "deep"])
def test_bench_native_exception_traceback_string(benchmark: Any, depth: int, carets: bool) -> None:
    """Benchmark the formatted-traceback exception field with and without PEP 657 carets."""
    _bench_exception(benchmark, depth, exception_carets=carets)


@pytest.mark.parametrize("depth", [1, 20], ids=["shallow", "deep"])
def test_bench_native_exception_structured(benchmark: Any, depth: int) -> None:
    """Benchmark the structured exception dict without locals."""
    _bench_exception(benchmark, depth, structured_exceptions=True)


def test_bench_native_exception_structured_with_locals(benchmark: Any) -> None:
    """Benchmark the structured exception dict with per-frame locals captured."""
    _bench_exception(
        benchmark,
        20,
        structured_exceptions=True,
        exception_include_locals=True,
    )


def test_bench_native_logger_exception_in_except_block(benchmark: Any) -> None:
    """Benchmark the realistic raise, catch, and ``logger.exception`` sequence."""
    _runtime.configure(service="bench", target="null", level="INFO")
    try:

        @benchmark
        def _() -> None:
            try:
                _raise_at_depth(3)
            except RuntimeError:
                logger.exception("order failed", order_id=987)

        _runtime.flush()
        metrics = _runtime.writer_metrics()
        assert metrics is not None
        assert metrics["written"] == metrics["enqueued"]
    finally:
        _runtime.shutdown()


def test_bench_native_stack_info(benchmark: Any) -> None:
    """Benchmark Python-side stack capture requested with ``opt(stack_info=True)``."""
    _runtime.configure(service="bench", target="null", level="INFO")
    try:
        traced = logger.opt(stack_info=True)

        @benchmark
        def _() -> None:
            traced.info("checkpoint", order_id=987)

        _runtime.flush()
        metrics = _runtime.writer_metrics()
        assert metrics is not None
        assert metrics["written"] == metrics["enqueued"]
    finally:
        _runtime.shutdown()
