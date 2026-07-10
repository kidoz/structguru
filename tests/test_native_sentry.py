"""Native-path Sentry hook tests.

Verifies the ``configure(sentry_processor=...)`` hook: a structlog-style
processor invoked per kept record on the caller's thread, mirroring
``metric_processor`` but passing raw ``exc_info`` for exception capture.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import structguru
from structguru import _native

pytestmark = pytest.mark.skipif(
    not _native.native_available(),
    reason="native extension not built",
)


def _drain_all() -> list[str]:
    _native.flush_native()
    return _native.drain_messages()


def test_sentry_hook_invoked_per_kept_record() -> None:
    calls: list[tuple[Any, str, dict[str, Any]]] = []

    def hook(_logger: Any, method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        calls.append((_logger, method, event_dict))
        return event_dict

    _native.configure(service="svc", target="memory", level="DEBUG", sentry_processor=hook)
    try:
        structguru.logger.info("first", request_id="r1")
        structguru.logger.error("second", code=500)
        _drain_all()
    finally:
        _native.disable_native()

    assert len(calls) == 2
    assert calls[0][1] == "info"
    assert calls[0][2]["event"] == "first"
    assert calls[0][2]["request_id"] == "r1"
    assert calls[1][1] == "error"
    assert calls[1][2]["code"] == 500


def test_sentry_hook_receives_raw_exc_info() -> None:
    """The hook must receive the raw exception, not a rendered string."""
    captured: list[Any] = []
    err = RuntimeError("boom")

    def hook(_logger: Any, method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        captured.append(event_dict.get("exc_info"))
        return event_dict

    _native.configure(service="svc", target="memory", level="DEBUG", sentry_processor=hook)
    try:
        structguru.logger.error("failed", exc_info=err)
        _drain_all()
    finally:
        _native.disable_native()

    assert len(captured) == 1
    assert captured[0] is err, "hook must receive the raw BaseException instance"


def test_sentry_hook_not_invoked_for_dropped_records() -> None:
    calls: list[Any] = []

    def hook(_logger: Any, method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        calls.append(event_dict)
        return event_dict

    _native.configure(
        service="svc",
        target="memory",
        level="DEBUG",
        sample_rate=0.0,  # drop everything
        sentry_processor=hook,
    )
    try:
        structguru.logger.info("dropped")
        _drain_all()
    finally:
        _native.disable_native()

    assert len(calls) == 0, "sampling must drop before the Sentry hook runs"


def test_sentry_hook_not_invoked_for_level_filtered() -> None:
    calls: list[Any] = []

    def hook(_logger: Any, method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        calls.append(event_dict)
        return event_dict

    _native.configure(
        service="svc",
        target="memory",
        level="WARNING",  # INFO is below threshold
        sentry_processor=hook,
    )
    try:
        structguru.logger.info("filtered out")
        structguru.logger.warning("kept")
        _drain_all()
    finally:
        _native.disable_native()

    assert len(calls) == 1
    assert calls[0]["event"] == "kept"


def test_sentry_hook_errors_are_swallowed() -> None:
    def bad_hook(_logger: Any, method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("sentry hook boom")

    _native.configure(service="svc", target="memory", level="DEBUG", sentry_processor=bad_hook)
    try:
        # Must not raise — the record is still rendered and enqueued.
        structguru.logger.info("survives")
        lines = _drain_all()
    finally:
        _native.disable_native()

    assert len(lines) == 1
    assert "survives" in lines[0]


def test_sentry_hook_with_real_sentry_processor() -> None:
    """End-to-end: SentryProcessor via the native hook captures exceptions."""
    from structguru.integrations import sentry as sentry_mod
    from structguru.integrations.sentry import SentryProcessor

    mock_sentry = MagicMock()
    scope = MagicMock()
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=scope)
    cm.__exit__ = MagicMock(return_value=False)
    mock_sentry.new_scope.return_value = cm

    err = RuntimeError("production failure")
    processor = SentryProcessor(event_level=40, require_redaction=False)

    _native.configure(service="svc", target="memory", level="DEBUG", sentry_processor=processor)
    try:
        with patch.object(sentry_mod, "_sentry_sdk", mock_sentry):
            structguru.logger.error("something broke", exc_info=err)
            _drain_all()
    finally:
        _native.disable_native()

    mock_sentry.capture_exception.assert_called_once_with(err)


def test_non_callable_sentry_processor_raises() -> None:
    with pytest.raises(TypeError, match="sentry_processor"):
        _native.configure(sentry_processor="not callable")  # type: ignore[arg-type]
    assert not _native.is_native_enabled()


def test_sentry_hook_injects_redaction_marker_when_redaction_configured() -> None:
    """When sensitive_keys are set, the hook injects REDACTED_MARKER_KEY so
    SentryProcessor's require_redaction guard recognizes native redaction."""
    from structguru.redaction import REDACTED_MARKER_KEY

    captured: list[dict[str, Any]] = []

    def hook(_logger: Any, method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        captured.append(event_dict)
        return event_dict

    _native.configure(
        service="svc",
        target="memory",
        level="DEBUG",
        sensitive_keys=["password"],
        sentry_processor=hook,
    )
    try:
        structguru.logger.info("login", password="hunter2")
        _drain_all()
    finally:
        _native.disable_native()

    assert len(captured) == 1
    assert captured[0].get(REDACTED_MARKER_KEY) is True
