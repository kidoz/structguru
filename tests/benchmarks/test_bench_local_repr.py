"""Benchmarks for bounded local representation and interpolated containers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from structguru import _runtime, logger
from structguru.exceptions import _format_locals, _redacted_message_repr
from structguru.redaction import DEFAULT_SENSITIVE_KEYS


@dataclass
class _Account:
    name: str
    password: str


def _payload(shape: str) -> Any:
    if shape == "flat":
        return {"user": "Ada", "password": "CANARY", "attempts": 3}
    if shape == "nested":
        return {
            "request": {"headers": {"authorization": "CANARY", "accept": "json"}},
            "items": [{"sku": f"A-{index}", "quantity": index} for index in range(20)],
        }
    if shape == "dataclass":
        return [_Account("Ada", "CANARY") for _ in range(10)]
    if shape == "cycle":
        value: dict[str, Any] = {"token": "CANARY"}
        value["self"] = value
        return value
    if shape == "wide":
        return list(range(10_000))
    return {"unicode": "Привет 🌍 e\u0301", "password": "CANARY", "surrogate": "\udcff"}


@pytest.mark.parametrize("shape", ["flat", "nested", "dataclass", "cycle", "wide", "unicode"])
def test_bench_local_repr(benchmark: Any, shape: str) -> None:
    """Measure redacted locals with realistic limits and representative shapes."""
    raw = {"payload": _payload(shape), "password": "CANARY", "status": 200}
    result = benchmark(_format_locals, raw, DEFAULT_SENSITIVE_KEYS, 200)
    assert result["password"] == "[REDACTED]"
    assert result["status"] == "200"
    assert "CANARY" not in result["payload"]


def test_bench_redacted_message_repr(benchmark: Any) -> None:
    """Measure the uncapped representation used by redacted placeholders."""
    result = benchmark(_redacted_message_repr, _payload("nested"), DEFAULT_SENSITIVE_KEYS)
    assert result is not None
    assert "CANARY" not in result
    assert "[REDACTED]" in result
    assert "A-19" in result


def test_bench_redacted_container_logging(benchmark: Any) -> None:
    """Measure the complete logging path for an interpolated nested container."""
    payload = _payload("nested")
    _runtime.configure(target="null")
    try:

        @benchmark
        def _() -> None:
            logger.info("request {payload!r}", payload=payload)

        _runtime.flush()
        metrics = _runtime.writer_metrics()
        assert metrics is not None
        assert metrics["enqueued"] == metrics["written"] > 0
        assert metrics["dropped"] == 0
    finally:
        _runtime.shutdown()
