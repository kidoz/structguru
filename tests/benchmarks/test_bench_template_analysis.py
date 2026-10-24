"""Separate cached template analysis, cache misses, and full formatting costs."""

from __future__ import annotations

import itertools
from types import SimpleNamespace
from typing import Any

import pytest

from structguru import _runtime, core
from structguru.core import Logger


@pytest.mark.parametrize(
    "message",
    [
        "order {order_id} accepted for {name}",
        "request {user.name} {headers[authorization]}",
        "value {x:{width}.{precision}f}",
        "x" * 600 + " {name}",
    ],
    ids=["simple", "paths", "spec", "long"],
)
def test_bench_template_analysis_uncached(benchmark: Any, message: str) -> None:
    """Measure actual analysis without the LRU lookup hiding its cost."""
    result = benchmark(core._parse_template, message)
    assert not isinstance(result, Exception)
    assert result[0]


def test_bench_template_analysis_cache_hit(benchmark: Any) -> None:
    """Measure the existing hot-cache lookup separately."""
    message = "order {order_id} accepted for {name}"
    expected = core._extract_format_keys(message)
    before = core._extract_format_keys.cache_info().hits
    assert benchmark(core._extract_format_keys, message) == expected
    assert core._extract_format_keys.cache_info().hits > before


@pytest.mark.parametrize("case", ["simple", "paths", "spec", "redacted", "long"])
def test_bench_template_safe_format(benchmark: Any, case: str) -> None:
    """Measure parsing in the context of safe formatting with a warm cache."""
    message, fields, expected = _format_case(case)
    assert core._safe_format(message, (), fields)[0] == expected
    result = benchmark(core._safe_format, message, (), fields)
    assert result[0] == expected


def _format_case(case: str) -> tuple[str, dict[str, Any], str]:
    if case == "paths":
        return "user {user.name}", {"user": SimpleNamespace(name="Ada")}, "user Ada"
    if case == "spec":
        return (
            "value {x:{width}.{precision}f}",
            {"x": 1.25, "width": 6, "precision": 2},
            "value   1.25",
        )
    if case == "redacted":
        return (
            "login {user} {password}",
            {"user": "Ada", "password": "CANARY"},
            "login Ada [REDACTED]",
        )
    if case == "long":
        return "x" * 600 + " {name}", {"name": "Ada"}, "x" * 600 + " Ada"
    return "order {order_id} for {name}", {"order_id": 987, "name": "Ada"}, "order 987 for Ada"


@pytest.mark.parametrize("case", ["simple", "redacted", "long", "churn"])
def test_bench_template_logging(benchmark: Any, case: str) -> None:
    """Measure kept logging calls, including a working set exceeding the cache."""
    message, fields, _ = _format_case(case)
    messages = itertools.cycle([f"event-{index} {message}" for index in range(2048)])
    log = Logger(name="template.bench")
    _runtime.configure(target="null")
    try:
        if case == "churn":

            def emit() -> None:
                log.info(next(messages), **fields)

        else:

            def emit() -> None:
                log.info(message, **fields)

        benchmark(emit)
        _runtime.flush()
        metrics = _runtime.writer_metrics()
        assert metrics is not None
        assert metrics["enqueued"] == metrics["written"] > 0
        assert metrics["dropped"] == 0
    finally:
        _runtime.shutdown()
