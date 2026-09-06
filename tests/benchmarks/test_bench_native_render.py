"""Benchmarks for renderer formats and field payload shapes on the native pipeline."""

from __future__ import annotations

import datetime as dt
import enum
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from structguru import _runtime, logger

pytestmark = pytest.mark.skipif(
    not _runtime.is_available(),
    reason="native extension not built",
)


class _Status(enum.Enum):
    PAID = "paid"


@dataclass(frozen=True)
class _Order:
    id: uuid.UUID
    created_at: dt.datetime
    status: _Status
    total: float


def _wide_fields(count: int) -> dict[str, Any]:
    return {f"field_{index}": index for index in range(count)}


def _nested_fields(depth: int) -> dict[str, Any]:
    fields: dict[str, Any] = {"leaf": True}
    for level in range(depth):
        fields = {f"level_{level}": fields}
    return fields


def _bench_record(benchmark: Any, message: str, fields: dict[str, Any], **config: Any) -> None:
    _runtime.configure(service="bench", target="null", level="INFO", **config)
    try:

        @benchmark
        def _() -> None:
            logger.info(message, **fields)

        _runtime.flush()
        metrics = _runtime.writer_metrics()
        assert metrics is not None
        assert metrics["written"] == metrics["enqueued"]
        assert metrics["dropped"] == 0
    finally:
        _runtime.shutdown()


@pytest.mark.parametrize("colors", [False, True], ids=["plain", "colors"])
def test_bench_native_console_format(benchmark: Any, colors: bool) -> None:
    """Benchmark the console renderer against the JSON default for the same record."""
    _bench_record(
        benchmark,
        "order accepted",
        {"order_id": 987, "status": "paid", "total": 42.5},
        format="console",
        colors=colors,
    )


def test_bench_native_escaping_heavy_strings(benchmark: Any) -> None:
    """Benchmark JSON escaping of quotes, backslashes, control characters, and non-ASCII."""
    payload = (
        'path="C:\\Users\\tmp"\n\ttab: "quoted" \u00fc\u00f1\u00ee \u4e2d\u6587 \U0001f600' * 4
    )
    _bench_record(benchmark, "escaped {value}", {"value": payload, "raw": payload})


def test_bench_native_wide_record(benchmark: Any) -> None:
    """Benchmark a flat record with 32 scalar fields."""
    _bench_record(benchmark, "wide record", _wide_fields(32))


def test_bench_native_deeply_nested_record(benchmark: Any) -> None:
    """Benchmark a record nested eight mappings deep."""
    _bench_record(benchmark, "nested record", _nested_fields(8))


def test_bench_native_long_message(benchmark: Any) -> None:
    """Benchmark a two-kilobyte message with no fields."""
    _bench_record(benchmark, "x" * 2048, {})


def test_bench_native_list_of_mappings(benchmark: Any) -> None:
    """Benchmark a field holding 100 small mappings."""
    items = [{"sku": f"SKU-{index}", "qty": index % 5, "price": 9.99} for index in range(100)]
    _bench_record(benchmark, "cart snapshot", {"items": items})


def test_bench_native_exotic_types_end_to_end(benchmark: Any) -> None:
    """Benchmark datetime, UUID, Enum, and dataclass fields through the full pipeline."""
    order = _Order(
        id=uuid.UUID("12345678-1234-5678-1234-567812345678"),
        created_at=dt.datetime(2026, 7, 11, 12, 0, tzinfo=dt.UTC),
        status=_Status.PAID,
        total=42.5,
    )
    _bench_record(
        benchmark,
        "order accepted",
        {"order": order, "when": order.created_at, "status": _Status.PAID},
    )


def test_bench_native_unsupported_value_markers(benchmark: Any) -> None:
    """Benchmark the fallback markers for values the renderer cannot represent."""
    _bench_record(
        benchmark,
        "unsupported values",
        {"path": Path("/tmp/app.log"), "nested": {"handle": object(), "items": [object()]}},
    )
