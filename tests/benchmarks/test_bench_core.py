"""Benchmarks for structguru vs structlog vs loguru."""

from __future__ import annotations

import io
import logging
from typing import Any

import pytest
import structlog
from loguru import logger as loguru_logger
from structlog.stdlib import ProcessorFormatter

from structguru import _native, logger
from structguru.config import configure_structlog


def _build_structlog_baseline(stream: Any) -> Any:
    """Minimal structlog setup for comparison."""
    structlog.reset_defaults()
    shared_processors = [
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.EventRenamer("message"),
    ]
    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
    )

    formatter = ProcessorFormatter(
        processors=[
            ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
        foreign_pre_chain=shared_processors,
    )

    root = logging.getLogger("structlog_raw")
    root.handlers.clear()
    root.setLevel(logging.DEBUG)
    handler = logging.StreamHandler(stream)
    handler.setFormatter(formatter)
    root.addHandler(handler)
    return structlog.get_logger("structlog_raw")


def _setup_loguru_raw(stream: Any) -> Any:
    """Minimal loguru setup for comparison."""
    loguru_logger.remove()
    loguru_logger.add(stream, format="{time} {level} {message}", serialize=True)
    return loguru_logger


@pytest.fixture
def benchmark_stream() -> io.StringIO:
    return io.StringIO()


def test_bench_structguru_simple(benchmark: Any, benchmark_stream: io.StringIO) -> None:
    """Benchmark structguru simple info log."""
    configure_structlog(json_logs=True, stream=benchmark_stream)

    @benchmark
    def _() -> None:
        logger.info("Hello world")
        benchmark_stream.seek(0)
        benchmark_stream.truncate()


def test_bench_structlog_raw_simple(benchmark: Any, benchmark_stream: io.StringIO) -> None:
    """Benchmark raw structlog simple info log."""
    log = _build_structlog_baseline(benchmark_stream)

    @benchmark
    def _() -> None:
        log.info("Hello world")
        benchmark_stream.seek(0)
        benchmark_stream.truncate()


def test_bench_loguru_simple(benchmark: Any, benchmark_stream: io.StringIO) -> None:
    """Benchmark loguru simple info log."""
    log = _setup_loguru_raw(benchmark_stream)

    @benchmark
    def _() -> None:
        log.info("Hello world")
        benchmark_stream.seek(0)
        benchmark_stream.truncate()


def test_bench_structguru_formatting(benchmark: Any, benchmark_stream: io.StringIO) -> None:
    """Benchmark structguru brace formatting."""
    configure_structlog(json_logs=True, stream=benchmark_stream)

    @benchmark
    def _() -> None:
        logger.info("Hello {name}, ID={id}", name="world", id=42)
        benchmark_stream.seek(0)
        benchmark_stream.truncate()


def test_bench_structlog_raw_formatting(benchmark: Any, benchmark_stream: io.StringIO) -> None:
    """Benchmark raw structlog formatting (manual kwargs)."""
    log = _build_structlog_baseline(benchmark_stream)

    @benchmark
    def _() -> None:
        log.info("Hello world, ID=42", name="world", id=42)
        benchmark_stream.seek(0)
        benchmark_stream.truncate()


def test_bench_loguru_formatting(benchmark: Any, benchmark_stream: io.StringIO) -> None:
    """Benchmark loguru formatting."""
    log = _setup_loguru_raw(benchmark_stream)

    @benchmark
    def _() -> None:
        log.info("Hello {name}, ID={id}", name="world", id=42)
        benchmark_stream.seek(0)
        benchmark_stream.truncate()


def test_bench_structguru_bind(benchmark: Any, benchmark_stream: io.StringIO) -> None:
    """Benchmark structguru context binding via `bind()`."""
    configure_structlog(json_logs=True, stream=benchmark_stream)
    bound_logger = logger.bind(request_id="12345", user_id=42)

    @benchmark
    def _() -> None:
        bound_logger.info("With context")
        benchmark_stream.seek(0)
        benchmark_stream.truncate()


def test_bench_structlog_raw_bind(benchmark: Any, benchmark_stream: io.StringIO) -> None:
    """Benchmark raw structlog context binding."""
    log = _build_structlog_baseline(benchmark_stream)
    bound_logger = log.bind(request_id="12345", user_id=42)

    @benchmark
    def _() -> None:
        bound_logger.info("With context")
        benchmark_stream.seek(0)
        benchmark_stream.truncate()


# -- native end-to-end (the numbers the full-Rust cutover is judged against) --

_needs_native = pytest.mark.skipif(
    not _native.native_available(),
    reason="native extension not built",
)


@_needs_native
def test_bench_structguru_native_simple(benchmark: Any) -> None:
    """Benchmark structguru simple info log on the native path (stdout sink)."""
    _native.configure(service="app", target="stdout")
    try:

        @benchmark
        def _() -> None:
            logger.info("Hello world")

    finally:
        _native.disable_native()


@_needs_native
def test_bench_structguru_native_formatting(benchmark: Any) -> None:
    """Benchmark structguru brace formatting on the native path."""
    _native.configure(service="app", target="stdout")
    try:

        @benchmark
        def _() -> None:
            logger.info("Hello {name}, ID={id}", name="world", id=42)

    finally:
        _native.disable_native()


@_needs_native
def test_bench_structguru_native_bind(benchmark: Any) -> None:
    """Benchmark structguru context binding on the native path."""
    _native.configure(service="app", target="stdout")
    bound_logger = logger.bind(request_id="12345", user_id=42)
    try:

        @benchmark
        def _() -> None:
            bound_logger.info("With context")

    finally:
        _native.disable_native()
