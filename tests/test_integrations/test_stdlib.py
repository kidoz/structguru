"""Tests for the stdlib -> structguru logging bridge."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator

import pytest

from structguru import _runtime
from structguru.core import Logger
from structguru.integrations.stdlib import (
    StructguruHandler,
    install_stdlib_bridge,
    install_stdlib_bridge_from_env,
    suppress_loggers,
    uninstall_stdlib_bridge,
)

pytestmark = pytest.mark.skipif(
    not _runtime.is_available(),
    reason="native extension not built",
)


@pytest.fixture
def native_memory() -> Iterator[None]:
    """Native mode writing to the in-memory sink, restored afterwards."""
    _runtime.configure(service="app", target="memory", level="DEBUG")
    try:
        yield
    finally:
        _runtime.shutdown()


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
    _runtime.flush_native()
    return [json.loads(line) for line in _runtime.drain_messages()]


def test_bridge_routes_stdlib_record(native_memory: None, clean_root: None) -> None:
    # Use a synthetic logger name, not a real library's (e.g. "sqlalchemy.engine"):
    # a library imported elsewhere in the session may set propagate=False on its
    # own logger, which would keep the record from ever reaching the root bridge.
    install_stdlib_bridge(level="DEBUG")
    logging.getLogger("thirdparty.db.engine").info("SELECT 1")
    rec = _records()[-1]
    assert rec["logger"] == "thirdparty.db.engine"
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
    uninstall_stdlib_bridge(handler)


def test_suppress_loggers_sets_level() -> None:
    suppress_loggers("noisy_a", "noisy_b", level="ERROR")
    assert logging.getLogger("noisy_a").level == logging.ERROR
    assert logging.getLogger("noisy_b").level == logging.ERROR


def test_install_suppresses_named_loggers(clean_root: None) -> None:
    install_stdlib_bridge(suppress_loggers=("chatty",), suppress_level="WARNING")
    assert logging.getLogger("chatty").level == logging.WARNING


def test_existing_logger_policy_none_preserves_states(clean_root: None) -> None:
    enabled = logging.getLogger("structguru_test_policy_none_enabled")
    disabled = logging.getLogger("structguru_test_policy_none_disabled")
    enabled.disabled = False
    disabled.disabled = True

    bridge = install_stdlib_bridge(disable_existing_loggers=None)
    try:
        assert not enabled.disabled
        assert disabled.disabled
    finally:
        uninstall_stdlib_bridge(bridge)


def test_install_reads_existing_logger_policy_from_environment(
    clean_root: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    existing = logging.getLogger("structguru_test_policy_default_env")
    existing.disabled = False
    monkeypatch.setenv("STRUCTGURU_STDLIB_DISABLE_EXISTING_LOGGERS", "true")

    bridge = install_stdlib_bridge()
    try:
        assert existing.disabled
    finally:
        uninstall_stdlib_bridge(bridge)


def test_explicit_existing_logger_policy_overrides_environment(
    clean_root: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    existing = logging.getLogger("structguru_test_policy_explicit_override")
    existing.disabled = False
    monkeypatch.setenv("STRUCTGURU_STDLIB_DISABLE_EXISTING_LOGGERS", "true")

    bridge = install_stdlib_bridge(disable_existing_loggers=False)
    try:
        assert not existing.disabled
    finally:
        uninstall_stdlib_bridge(bridge)


def test_regular_install_rejects_invalid_existing_logger_environment(
    clean_root: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = logging.getLogger()
    sentinel = logging.NullHandler()
    root.addHandler(sentinel)
    monkeypatch.setenv("STRUCTGURU_STDLIB_DISABLE_EXISTING_LOGGERS", "maybe")

    with pytest.raises(ValueError, match="STRUCTGURU_STDLIB_DISABLE_EXISTING_LOGGERS"):
        install_stdlib_bridge()

    assert root.handlers[-1] is sentinel


def test_existing_logger_policy_true_disables_and_restores(clean_root: None) -> None:
    existing = logging.getLogger("structguru_test_policy_disable")
    existing.disabled = False

    bridge = install_stdlib_bridge(disable_existing_loggers=True)
    assert existing.disabled

    uninstall_stdlib_bridge(bridge)
    assert not existing.disabled


def test_existing_logger_policy_false_enables_and_restores(clean_root: None) -> None:
    existing = logging.getLogger("structguru_test_policy_enable")
    existing.disabled = True

    bridge = install_stdlib_bridge(disable_existing_loggers=False)
    assert not existing.disabled

    uninstall_stdlib_bridge(bridge)
    assert existing.disabled


def test_existing_logger_policy_does_not_disable_root_or_later_logger(
    clean_root: None,
) -> None:
    root = logging.getLogger()
    root.disabled = False
    existing = logging.getLogger("structguru_test_policy_before")
    existing.disabled = False

    bridge = install_stdlib_bridge(disable_existing_loggers=True)
    try:
        later = logging.getLogger("structguru_test_policy_after")
        assert existing.disabled
        assert not root.disabled
        assert not later.disabled
    finally:
        uninstall_stdlib_bridge(bridge)


def test_existing_logger_policy_ignores_placeholders(clean_root: None) -> None:
    logging.getLogger("structguru_test_placeholder.child")
    placeholder = logging.root.manager.loggerDict["structguru_test_placeholder"]
    assert isinstance(placeholder, logging.PlaceHolder)

    bridge = install_stdlib_bridge(disable_existing_loggers=True)
    try:
        assert logging.root.manager.loggerDict["structguru_test_placeholder"] is placeholder
    finally:
        uninstall_stdlib_bridge(bridge)


def test_uninstall_preserves_change_when_bridge_did_not_change_logger(clean_root: None) -> None:
    existing = logging.getLogger("structguru_test_policy_later_change")
    existing.disabled = True
    bridge = install_stdlib_bridge(disable_existing_loggers=True)
    assert existing.disabled

    existing.disabled = False
    uninstall_stdlib_bridge(bridge)
    assert not existing.disabled


def test_install_rejects_duplicate_active_bridge(clean_root: None) -> None:
    bridge = install_stdlib_bridge()
    try:
        with pytest.raises(RuntimeError, match="already installed"):
            install_stdlib_bridge()
    finally:
        uninstall_stdlib_bridge(bridge)


def test_install_from_env_applies_all_options(clean_root: None) -> None:
    root = logging.getLogger()
    sentinel = logging.NullHandler()
    root.addHandler(sentinel)
    existing = logging.getLogger("structguru_test_env_existing")
    existing.disabled = False

    bridge = install_stdlib_bridge_from_env(
        {
            "LOG_LEVEL": "ERROR",
            "STRUCTGURU_STDLIB_LEVEL": "DEBUG",
            "STRUCTGURU_STDLIB_CLEAR_HANDLERS": "false",
            "STRUCTGURU_STDLIB_DISABLE_EXISTING_LOGGERS": "true",
            "STRUCTGURU_STDLIB_SUPPRESS_LOGGERS": " noisy_a, noisy_b ",
            "STRUCTGURU_STDLIB_SUPPRESS_LEVEL": "ERROR",
        }
    )
    try:
        assert sentinel in root.handlers
        assert bridge.level == logging.DEBUG
        assert root.level == logging.DEBUG
        assert existing.disabled
        assert logging.getLogger("noisy_a").level == logging.ERROR
        assert logging.getLogger("noisy_b").level == logging.ERROR
    finally:
        uninstall_stdlib_bridge(bridge)


def test_install_from_injected_env_does_not_fall_back_to_process_env(
    clean_root: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    existing = logging.getLogger("structguru_test_injected_env_isolation")
    existing.disabled = False
    monkeypatch.setenv("STRUCTGURU_STDLIB_DISABLE_EXISTING_LOGGERS", "true")

    bridge = install_stdlib_bridge_from_env({})
    try:
        assert not existing.disabled
    finally:
        uninstall_stdlib_bridge(bridge)


def test_invalid_env_does_not_change_logging_state(clean_root: None) -> None:
    root = logging.getLogger()
    sentinel = logging.NullHandler()
    root.addHandler(sentinel)
    existing = logging.getLogger("structguru_test_env_invalid")
    existing.disabled = False

    with pytest.raises(ValueError, match="STRUCTGURU_STDLIB_CLEAR_HANDLERS"):
        install_stdlib_bridge_from_env({"STRUCTGURU_STDLIB_CLEAR_HANDLERS": "maybe"})

    assert root.handlers[-1] is sentinel
    assert not existing.disabled


def test_bridge_delivers_third_party_records_to_add_sinks_exactly_once(
    native_memory: None, clean_root: None
) -> None:
    # `logger.add()` attaches its sink to the root logger so it also receives
    # third-party records raw. The bridge renders those same records through the
    # native path into the same sink, so both paths together would deliver each
    # record twice — once raw, once rendered.
    seen: list[str] = []
    log = Logger()
    install_stdlib_bridge(level="DEBUG")
    log.add(seen.append)
    try:
        logging.getLogger("thirdparty.svc").info("only once")
        _runtime.flush()
    finally:
        log.remove()

    assert len(seen) == 1
    assert json.loads(seen[0])["message"] == "only once"


def test_bridge_installed_after_add_also_delivers_exactly_once(
    native_memory: None, clean_root: None
) -> None:
    # Same invariant, opposite ordering: installing the bridge must suspend the
    # root attachment of a sink that was registered before it.
    seen: list[str] = []
    log = Logger()
    log.add(seen.append)
    install_stdlib_bridge(level="DEBUG")
    try:
        logging.getLogger("thirdparty.svc").info("still once")
        _runtime.flush()
    finally:
        log.remove()

    assert len(seen) == 1
    assert json.loads(seen[0])["message"] == "still once"


def test_uninstall_restores_raw_root_delivery(native_memory: None, clean_root: None) -> None:
    seen: list[str] = []
    log = Logger()
    log.add(seen.append)
    bridge = install_stdlib_bridge(level="DEBUG")
    uninstall_stdlib_bridge(bridge)
    try:
        logging.getLogger("thirdparty.svc").info("raw again")
        _runtime.flush()
    finally:
        log.remove()

    # Back to the pre-bridge contract: the sink sees the unrendered message.
    assert seen == ["raw again"]
