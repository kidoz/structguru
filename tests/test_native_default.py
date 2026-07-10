"""Tests for the native-mode default flip (Phase 4b).

Native mode is now the default (auto-enabled at import). ``configure_structlog``
opts back into the standard structlog path (disabling native). ``STRUCTGURU_LEGACY=1``
opts out of auto-enable entirely.
"""

from __future__ import annotations

import io

import pytest

import structguru
from structguru import _native
from structguru.config import configure_structlog

pytestmark = pytest.mark.skipif(
    not _native.native_available(),
    reason="native extension not built",
)


def test_native_auto_enabled_at_import() -> None:
    """Native mode is on by default (no configure_structlog call needed)."""
    # _maybe_enable_from_env() ran at import time; native should be on unless
    # a prior test called disable_native or configure_structlog. Re-trigger.
    _native.disable_native()
    _native._maybe_enable_from_env()
    try:
        assert _native.is_native_enabled()
    finally:
        _native.disable_native()


def test_structguru_legacy_env_disables_native(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """STRUCTGURU_LEGACY=1 opts out of native auto-enable."""
    monkeypatch.setenv("STRUCTGURU_LEGACY", "1")
    _native.disable_native()
    _native._maybe_enable_from_env()
    try:
        assert not _native.is_native_enabled()
    finally:
        monkeypatch.delenv("STRUCTGURU_LEGACY", raising=False)
        _native.disable_native()


def test_configure_structlog_disables_native() -> None:
    """Calling configure_structlog opts into the standard path (native off)."""
    _native.enable_native(service="svc", target="memory", level="DEBUG")
    assert _native.is_native_enabled()

    buf = io.StringIO()
    configure_structlog(service="test", level="DEBUG", json_logs=True, stream=buf)
    try:
        assert not _native.is_native_enabled(), (
            "configure_structlog should disable native so output lands on the stream"
        )
    finally:
        _native.disable_native()


def test_native_logs_without_configure_structlog() -> None:
    """Without configure_structlog, logger calls route through native natively."""
    _native.enable_native(service="svc", target="memory", level="DEBUG")
    try:
        structguru.logger.info("native default {x}", x=1)
        _native.flush_native()
        lines = _native.drain_messages()
        assert len(lines) == 1
        assert "native default 1" in lines[0]
    finally:
        _native.disable_native()
