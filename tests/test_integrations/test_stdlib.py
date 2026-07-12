"""Tests for the stdlib -> structguru logging bridge."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator

import pytest

from structguru import _native
from structguru.integrations.stdlib import (
    StructguruHandler,
    install_stdlib_bridge,
    suppress_loggers,
)

pytestmark = pytest.mark.skipif(
    not _native.is_available(),
    reason="native extension not built",
)


@pytest.fixture
def native_memory() -> Iterator[None]:
    """Native mode writing to the in-memory sink, restored afterwards."""
    _native.configure(service="app", target="memory", level="DEBUG")
    try:
        yield
    finally:
        _native.shutdown()


@pytest.fixture
def clean_root() -> Iterator[None]:
    """Snapshot and restore the root logger's handlers and level."""
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    try:
        yield
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def _records() -> list[dict]:
    _native.flush_native()
    return [json.loads(line) for line in _native.drain_messages()]


def test_bridge_routes_stdlib_record(native_memory: None, clean_root: None) -> None:
    install_stdlib_bridge(level="DEBUG")
    logging.getLogger("sqlalchemy.engine").info("SELECT 1")
    rec = _records()[-1]
    assert rec["logger"] == "sqlalchemy.engine"
    assert rec["level"] == "INFO"
    assert rec["message"] == "SELECT 1"


def test_bridge_forwards_extra_fields(native_memory: None, clean_root: None) -> None:
    install_stdlib_bridge(level="DEBUG")
    logging.getLogger("svc").warning("slow query", extra={"duration_ms": 42})
    rec = _records()[-1]
    assert rec["duration_ms"] == 42
    assert rec["level"] == "WARN"


def test_bridge_forwards_exc_info(native_memory: None, clean_root: None) -> None:
    install_stdlib_bridge(level="DEBUG")
    try:
        raise ValueError("boom")
    except ValueError:
        logging.getLogger("svc").error("failed", exc_info=True)
    rec = _records()[-1]
    assert "exception" in rec
    assert "ValueError" in rec["exception"]


def test_bridge_forwards_stack_info(native_memory: None, clean_root: None) -> None:
    install_stdlib_bridge(level="DEBUG")
    logging.getLogger("svc").warning("stack requested", stack_info=True)
    rec = _records()[-1]
    assert "Stack (most recent call last):" in rec["stack"]
    assert "test_bridge_forwards_stack_info" in rec["stack"]


def test_bridge_preserves_literal_braces(native_memory: None, clean_root: None) -> None:
    # An already-formatted message with literal braces must pass through
    # verbatim, never re-run through structguru's brace formatting.
    install_stdlib_bridge(level="DEBUG")
    logging.getLogger("svc").info("progress {done}/{total} {")
    rec = _records()[-1]
    assert rec["message"] == "progress {done}/{total} {"


@pytest.mark.parametrize(
    "levelno,expected",
    [
        (logging.DEBUG, "DEBUG"),
        (logging.INFO, "INFO"),
        (25, "INFO"),  # between INFO and WARNING -> info
        (logging.WARNING, "WARN"),
        (logging.ERROR, "ERROR"),
        (45, "ERROR"),  # between ERROR and CRITICAL -> error
        (logging.CRITICAL, "CRITICAL"),
    ],
)
def test_level_normalization(
    native_memory: None, clean_root: None, levelno: int, expected: str
) -> None:
    install_stdlib_bridge(level="DEBUG")
    logging.getLogger("svc").log(levelno, "msg")
    assert _records()[-1]["level"] == expected


def test_bridge_level_filters_child_with_explicit_lower_level(
    native_memory: None, clean_root: None
) -> None:
    child = logging.getLogger("explicit_debug_child")
    saved_level = child.level
    saved_propagate = child.propagate
    try:
        install_stdlib_bridge(level="INFO")
        child.setLevel(logging.DEBUG)
        child.propagate = True
        child.debug("below bridge threshold")
        child.info("at bridge threshold")
        assert [record["message"] for record in _records()] == ["at bridge threshold"]
    finally:
        child.setLevel(saved_level)
        child.propagate = saved_propagate


def test_bridge_ignores_structguru_records(native_memory: None, clean_root: None) -> None:
    # Records from structguru's own loggers are skipped to avoid double-wrapping
    # and any interception loop.
    install_stdlib_bridge(level="DEBUG")
    logging.getLogger("structguru.internal").info("should not route")
    assert _records() == []


def test_install_clears_existing_handlers(clean_root: None) -> None:
    root = logging.getLogger()
    sentinel = logging.NullHandler()
    root.addHandler(sentinel)
    install_stdlib_bridge()
    assert sentinel not in root.handlers
    assert any(isinstance(h, StructguruHandler) for h in root.handlers)


def test_install_keeps_existing_handlers_when_requested(clean_root: None) -> None:
    root = logging.getLogger()
    sentinel = logging.NullHandler()
    root.addHandler(sentinel)
    install_stdlib_bridge(clear_handlers=False)
    assert sentinel in root.handlers


def test_install_returns_removable_handler(clean_root: None) -> None:
    handler = install_stdlib_bridge()
    logging.getLogger().removeHandler(handler)
    assert handler not in logging.getLogger().handlers


def test_suppress_loggers_sets_level() -> None:
    suppress_loggers("noisy_a", "noisy_b", level="ERROR")
    assert logging.getLogger("noisy_a").level == logging.ERROR
    assert logging.getLogger("noisy_b").level == logging.ERROR


def test_install_suppresses_named_loggers(clean_root: None) -> None:
    install_stdlib_bridge(suppress_loggers=("chatty",), suppress_level="WARNING")
    assert logging.getLogger("chatty").level == logging.WARNING
