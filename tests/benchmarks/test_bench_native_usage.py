"""Benchmarks for facade helpers and delivery paths the other modules do not time.

Each test measures one caller-visible use: a logger kept after ``bind``, a
``contextualize`` block, an ``opt`` chain, ``catch``, a full queue in drop
mode, OpenTelemetry enrichment, backtracking and integer redaction, an
``add()`` sink, a file-plus-callable fan-out, and a synchronous stream sink.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import structguru._rust as rust

from structguru import _runtime, logger

pytestmark = pytest.mark.skipif(
    not _runtime.is_available(),
    reason="native extension not built",
)


def _assert_delivered(metrics: dict[str, Any] | None) -> dict[str, Any]:
    assert metrics is not None
    assert metrics["written"] == metrics["enqueued"]
    assert metrics["dropped"] == 0
    assert metrics["enqueued"] > 0
    return metrics


def test_bench_native_bound_logger_reuse(benchmark: Any) -> None:
    """Benchmark a logger whose context was bound once and then reused."""
    _runtime.configure(service="bench", target="null", level="INFO")
    bound = logger.bind(request_id="req-123", tenant_id="tenant-7", region="eu-west-1")
    try:

        @benchmark
        def _() -> None:
            bound.info("request handled", status_code=200, duration_ms=12.5)

        _runtime.flush()
        _assert_delivered(_runtime.writer_metrics())
    finally:
        _runtime.shutdown()


def test_bench_native_contextualize_per_call(benchmark: Any) -> None:
    """Benchmark entering and leaving a context block around one log call."""
    _runtime.configure(service="bench", target="null", level="INFO")
    try:

        @benchmark
        def _() -> None:
            with logger.contextualize(request_id="req-123", tenant_id="tenant-7"):
                logger.info("request handled", status_code=200)

        _runtime.flush()
        _assert_delivered(_runtime.writer_metrics())
    finally:
        _runtime.shutdown()


def test_bench_native_opt_chain(benchmark: Any) -> None:
    """Benchmark building an ``opt`` child on every call, with no stack capture."""
    _runtime.configure(service="bench", target="null", level="INFO")
    try:

        @benchmark
        def _() -> None:
            logger.opt().info("checkpoint", order_id=987)

        _runtime.flush()
        _assert_delivered(_runtime.writer_metrics())
    finally:
        _runtime.shutdown()


def test_bench_native_catch_success_path(benchmark: Any) -> None:
    """Benchmark ``catch`` around a body that does not raise."""

    def work() -> int:
        return 987

    _runtime.configure(service="bench", target="null", level="INFO")
    try:

        @benchmark
        def _() -> None:
            with logger.catch():
                work()

        _runtime.flush()
        metrics = _runtime.writer_metrics()
        assert metrics is not None
        assert metrics["enqueued"] == 0
    finally:
        _runtime.shutdown()


def test_bench_native_catch_logs_and_suppresses(benchmark: Any) -> None:
    """Benchmark ``catch`` logging a raised exception and swallowing it."""
    _runtime.configure(service="bench", target="null", level="INFO")
    try:

        @benchmark
        def _() -> None:
            with logger.catch(message="order failed"):
                raise RuntimeError("order 987 failed")

        _runtime.flush()
        _assert_delivered(_runtime.writer_metrics())
    finally:
        _runtime.shutdown()


def test_bench_native_overflow_drop_when_queue_is_full(benchmark: Any) -> None:
    """Benchmark a record rejected by a full native queue in drop mode.

    The writer is paused with its one slot already taken, and one drop is
    emitted first so the timed calls are the steady state after the first
    queue-full warning.
    """
    _runtime.configure(service="bench", target="null", level="INFO", maxsize=1, overflow="drop")
    state = _runtime.current_runtime()
    assert state is not None
    paused = rust._NativeStringWriter(1, paused=True)
    assert paused.try_enqueue("already-queued")
    original = state.writer
    _runtime._runtime = replace(state, writer=paused)
    original.close()
    try:
        logger.info("prime the drop warning")

        @benchmark
        def _() -> None:
            logger.info("queue full", order_id=987)

        metrics = _runtime.writer_metrics()
        assert metrics is not None
        assert metrics["enqueued"] == 1
        assert metrics["written"] == 0
        assert metrics["dropped"] >= 2
    finally:
        paused.resume()
        _runtime.shutdown()


def test_bench_native_otel_without_a_span(benchmark: Any) -> None:
    """Benchmark OpenTelemetry enrichment when no span is current."""
    pytest.importorskip("opentelemetry.trace")
    _runtime.configure(service="bench", target="null", level="INFO", otel=True)
    try:

        @benchmark
        def _() -> None:
            logger.info("request handled", status_code=200)

        _runtime.flush()
        _assert_delivered(_runtime.writer_metrics())
    finally:
        _runtime.shutdown()


def test_bench_native_otel_active_span(benchmark: Any) -> None:
    """Benchmark OpenTelemetry enrichment while a valid span is current."""
    trace = pytest.importorskip("opentelemetry.trace")
    context = pytest.importorskip("opentelemetry.context")
    span_context = trace.SpanContext(
        trace_id=0x0AF7651916CD43DD8448EB211C80319C,
        span_id=0x00F067AA0BA902B7,
        is_remote=False,
        trace_flags=trace.TraceFlags(0x01),
    )
    token = context.attach(trace.set_span_in_context(trace.NonRecordingSpan(span_context)))
    _runtime.configure(service="bench", target="null", level="INFO", otel=True)
    try:

        @benchmark
        def _() -> None:
            logger.info("request handled", status_code=200)

        _runtime.flush()
        _assert_delivered(_runtime.writer_metrics())
    finally:
        context.detach(token)
        _runtime.shutdown()


def test_bench_native_backtracking_redaction(benchmark: Any) -> None:
    """Benchmark one opt-in look-behind pattern on the caller path."""
    _runtime.configure(
        service="bench",
        target="null",
        level="INFO",
        sensitive_patterns=[r"(?<=secret=)\w+"],
        allow_backtracking_patterns=True,
        pattern_replacement="[REDACTED]",
    )
    try:

        @benchmark
        def _() -> None:
            logger.info("login secret=abcdef user=ada", user="ada")

        _runtime.flush()
        _assert_delivered(_runtime.writer_metrics())
    finally:
        _runtime.shutdown()


def test_bench_native_integer_pattern_redaction(benchmark: Any) -> None:
    """Benchmark a pattern matched against an integer field's decimal text."""
    card = 4242424242424242
    _runtime.configure(
        service="bench",
        target="null",
        level="INFO",
        sensitive_patterns=[r"\d{16}"],
    )
    try:

        @benchmark
        def _() -> None:
            logger.info("card charged", card=card, amount=42.5)

        _runtime.flush()
        _assert_delivered(_runtime.writer_metrics())
    finally:
        _runtime.shutdown()


def test_bench_native_added_sink(benchmark: Any) -> None:
    """Benchmark caller cost when ``logger.add`` delivers each rendered line."""
    received: list[str] = []
    _runtime.configure(service="bench", target="null", level="INFO")
    token = logger.add(received.append)
    try:

        @benchmark
        def _() -> None:
            logger.info("order accepted", order_id=987, status="paid")

        _runtime.flush()
        metrics = _assert_delivered(_runtime.writer_metrics())
        assert len(received) == metrics["enqueued"]
    finally:
        logger.remove(token)
        _runtime.shutdown()


def test_bench_native_file_and_callable_fanout(benchmark: Any, tmp_path: Path) -> None:
    """Benchmark one record written to a file and a callable sink together."""
    received: list[str] = []
    _runtime.configure(
        service="bench",
        target="null",
        level="INFO",
        file_path=str(tmp_path / "app.log"),
        file_max_bytes=0,
        file_backup_count=0,
        callable_sinks=[received.append],
    )
    try:

        @benchmark
        def _() -> None:
            logger.info("order accepted", order_id=987, status="paid")

        _runtime.flush()
        metrics = _assert_delivered(_runtime.writer_metrics())
        assert len(received) == metrics["enqueued"]
        assert (tmp_path / "app.log").stat().st_size > 0
    finally:
        _runtime.shutdown()


class _Discard:
    def write(self, _message: str, /) -> None:
        return None


def test_bench_native_sync_stream_sink(benchmark: Any) -> None:
    """Benchmark the synchronous stream copy taken on the caller thread."""
    _runtime.configure(service="bench", target="null", level="INFO", stream_sink=_Discard())
    try:

        @benchmark
        def _() -> None:
            logger.info("order accepted", order_id=987, status="paid")

        _runtime.flush()
        _assert_delivered(_runtime.writer_metrics())
    finally:
        _runtime.shutdown()


def test_bench_native_disabled_after_shutdown(benchmark: Any) -> None:
    """Benchmark a log call made while logging is disabled."""
    _runtime.configure(service="bench", target="null", level="INFO")
    _runtime.shutdown()

    @benchmark
    def _() -> None:
        logger.info("after shutdown", order_id=987, nested={"enabled": True})

    assert _runtime.current_runtime() is None
    assert _runtime.writer_metrics() is None
