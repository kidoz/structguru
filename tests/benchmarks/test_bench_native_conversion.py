"""Benchmarks for native Python-to-Rust value conversion."""

from __future__ import annotations

from typing import Any

import orjson
import structguru._rust as rust


def orjson_serializer(obj: object) -> str:
    return orjson.dumps(obj).decode()


def _realistic_record() -> dict[str, Any]:
    return {
        "timestamp": "2026-07-06T00:00:00Z",
        "level": "INFO",
        "severity": 6,
        "message": "order accepted",
        "service": "checkout",
        "logger": "structguru.benchmark",
        "request": {
            "id": "req-123",
            "path": "/orders",
            "method": "POST",
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
        "flags": ["paid", "new_customer"],
        "retry": False,
        "error": None,
    }


def test_bench_native_conversion_stats(benchmark: Any) -> None:
    """Benchmark conversion of a realistic structured log record."""
    record = _realistic_record()

    @benchmark
    def _() -> None:
        rust._conversion_stats(record)


def test_bench_python_orjson_serializer(benchmark: Any) -> None:
    """Benchmark current Python orjson serialization."""
    record = _realistic_record()

    @benchmark
    def _() -> None:
        orjson_serializer(record)


def test_bench_native_json_render(benchmark: Any) -> None:
    """Benchmark native conversion plus Rust JSON rendering."""
    record = _realistic_record()

    @benchmark
    def _() -> None:
        rust._render_json_debug(record)
