"""Benchmarks for structguru vs structlog vs loguru."""

from __future__ import annotations

import io
import logging
from typing import Any

import pytest
import structlog
from loguru import logger as loguru_logger
from structlog.stdlib import ProcessorFormatter

from structguru import configure_structlog, logger


def _setup_structlog_raw(stream: Any) -> Any:
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
    log = _setup_structlog_raw(benchmark_stream)

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


def test_bench_loguru_formatting(benchmark: Any, benchmark_stream: io.StringIO) -> None:
    """Benchmark loguru formatting."""
    log = _setup_loguru_raw(benchmark_stream)

    @benchmark
    def _() -> None:
        log.info("Hello {name}, ID={id}", name="world", id=42)
        benchmark_stream.seek(0)
        benchmark_stream.truncate()
