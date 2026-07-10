"""Tests for the native-mode default (v1.0).

Native mode is the default (auto-enabled at import). ``configure()`` wires the
native renderer to a stream. ``STRUCTGURU_LEGACY=1`` opts out of auto-enable.
"""

from __future__ import annotations

import io

import pytest
from conftest import configure

import structguru
from structguru import _native

pytestmark = pytest.mark.skipif(
    not _native.native_available(),
    reason="native extension not built",
)


def test_native_auto_enabled_at_import() -> None:
    """Native mode is on by default (no configure call needed)."""
    # _maybe_configure_from_env() ran at import time; native should be on unless
    # a prior test called disable_native or configure. Re-trigger.
    _native.disable_native()
    _native._maybe_configure_from_env()
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
    _native._maybe_configure_from_env()
    try:
        assert not _native.is_native_enabled()
    finally:
        monkeypatch.delenv("STRUCTGURU_LEGACY", raising=False)
        _native.disable_native()


def test_configure_structlog_enables_native_with_stream() -> None:
    """configure_structlog wires native to the stream (v1.0 behavior)."""
    _native.disable_native()

    buf = io.StringIO()
    configure(service="test", level="DEBUG", json=True, stream=buf)
    try:
        assert _native.is_native_enabled(), "configure should enable native with stream_sink"
    finally:
        _native.disable_native()


def test_native_logs_without_configure() -> None:
    """Without configure, logger calls route through native natively."""
    _native.configure(service="svc", target="memory", level="DEBUG")
    try:
        structguru.logger.info("native default {x}", x=1)
        _native.flush_native()
        lines = _native.drain_messages()
        assert len(lines) == 1
        assert "native default 1" in lines[0]
    finally:
        _native.disable_native()
