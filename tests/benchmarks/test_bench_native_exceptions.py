"""Benchmarks for exception and stack rendering on the native pipeline."""

from __future__ import annotations

import linecache
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


def _bench_exception_info(benchmark: Any, exc_info: Any, **config: Any) -> None:
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


def _bench_exception(benchmark: Any, depth: int, **config: Any) -> None:
    _bench_exception_info(benchmark, _capture_exc_info(depth), **config)


@pytest.mark.parametrize("carets", [True, False], ids=["carets", "no-carets"])
@pytest.mark.parametrize("depth", [1, 20], ids=["shallow", "deep"])
def test_bench_native_exception_traceback_string(benchmark: Any, depth: int, carets: bool) -> None:
    """Benchmark the formatted-traceback exception field with and without PEP 657 carets."""
    _bench_exception(benchmark, depth, exception_carets=carets)


@pytest.mark.parametrize("depth", [1, 20, 200], ids=["shallow", "deep", "truncated"])
def test_bench_native_exception_structured(benchmark: Any, depth: int) -> None:
    """Benchmark the structured exception dict without locals.

    ``truncated`` raises through 200 frames while the default
    ``exception_max_frames`` keeps 20, so it measures frame selection on a
    traceback much deeper than the report, as a ``RecursionError`` produces.
    """
    _bench_exception(benchmark, depth, structured_exceptions=True)


@pytest.mark.parametrize("depth", [20, 200], ids=["deep", "truncated"])
def test_bench_native_exception_structured_with_locals(benchmark: Any, depth: int) -> None:
    """Benchmark the structured exception dict with per-frame locals captured."""
    _bench_exception(
        benchmark,
        depth,
        structured_exceptions=True,
        exception_include_locals=True,
    )


@pytest.mark.parametrize("statements", [100, 1000])
def test_bench_native_exception_structured_large_function(
    benchmark: Any, monkeypatch: pytest.MonkeyPatch, statements: int
) -> None:
    """Cover instruction lookup late in a function, independently of stack depth."""
    source = (
        "def fail():\n    value = 0\n"
        + "    value += 1\n" * statements
        + "    raise RuntimeError('synthetic failure')\n"
    )
    filename = f"<bench-large-function-{statements}>"
    monkeypatch.setitem(
        linecache.cache, filename, (len(source), None, source.splitlines(keepends=True), filename)
    )
    namespace: dict[str, Any] = {}
    exec(compile(source, filename, "exec"), namespace)
    try:
        namespace["fail"]()
    except RuntimeError:
        exc_info = sys.exc_info()
    _bench_exception_info(benchmark, exc_info, structured_exceptions=True)


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
