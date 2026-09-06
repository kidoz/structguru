"""Benchmarks for native Python-to-Rust value conversion."""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

import pytest
import structguru._rust as rust

try:
    import orjson
except ImportError:  # pragma: no cover - free-threaded builds have no orjson wheel
    orjson = None  # type: ignore[assignment]


def orjson_serializer(obj: object) -> str:
    assert orjson is not None
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


@pytest.mark.skipif(orjson is None, reason="orjson not installed")
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


def test_bench_native_flat_value_conversion(benchmark: Any) -> None:
    """Benchmark the common shallow-field conversion shape."""
    fields = {"request_id": "req-123", "status": 200, "duration_ms": 12.5, "ok": True}

    @benchmark
    def _() -> None:
        rust._conversion_stats(fields)


def test_bench_native_exotic_value_conversion(benchmark: Any) -> None:
    """Benchmark datetime, UUID, and dataclass conversion at the PyO3 boundary."""

    @dataclass(frozen=True)
    class Order:
        id: uuid.UUID
        created_at: dt.datetime
        total: float

    order = Order(
        id=uuid.UUID("12345678-1234-5678-1234-567812345678"),
        created_at=dt.datetime(2026, 7, 11, 12, 0, tzinfo=dt.UTC),
        total=42.5,
    )

    @benchmark
    def _() -> None:
        rust._convert_value_debug(order)


def test_bench_native_compiled_redaction_render(benchmark: Any) -> None:
    """Benchmark rendering with reusable compiled redaction configuration."""
    config = rust.RedactionConfig([r"token-[A-Za-z0-9]+"])
    fields = {
        "authorization": "Bearer token-secret123",
        "nested": {"query": "access=token-secret123", "user_id": 42},
    }
    redacted = rust.render_line_with_config(
        fields,
        "benchmark",
        "info",
        "api",
        "authenticated token-secret123",
        config,
        "2026-07-11T12:00:00Z",
        ["authorization"],
    )
    assert "token-secret123" not in redacted

    @benchmark
    def _() -> None:
        rust.render_line_with_config(
            fields,
            "benchmark",
            "info",
            "api",
            "authenticated token-secret123",
            config,
            "2026-07-11T12:00:00Z",
            ["authorization"],
        )
