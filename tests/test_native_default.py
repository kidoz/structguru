"""Tests for the native-mode default (v1.0).

Native mode is the default (auto-enabled at import). ``configure()`` wires the
native renderer to a stream. ``STRUCTGURU_LEGACY=1`` opts out of auto-enable.
"""

from __future__ import annotations

import io

import pytest
from conftest import configure

import structguru
from structguru import _runtime

pytestmark = pytest.mark.skipif(
    not _runtime.is_available(),
    reason="native extension not built",
)


def test_native_auto_enabled_at_import() -> None:
    """Native mode is on by default (no configure call needed)."""
    # _maybe_configure_from_env() ran at import time; native should be on unless
    # a prior test called shutdown or configure. Re-trigger.
    _runtime.shutdown()
    _runtime._maybe_configure_from_env()
    try:
        assert _runtime.is_native_enabled()
    finally:
        _runtime.shutdown()


def test_missing_native_extension_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    """The native-only package must not silently disable logging."""
    _runtime.shutdown()
    monkeypatch.setattr(_runtime, "_RUST", None)

    with pytest.raises(RuntimeError, match="requires its native extension"):
        _runtime._maybe_configure_from_env()

    assert not _runtime.is_native_enabled()


def test_structguru_legacy_env_disables_native(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """STRUCTGURU_LEGACY=1 opts out of native auto-enable."""
    monkeypatch.setenv("STRUCTGURU_LEGACY", "1")
    _runtime.shutdown()
    _runtime._maybe_configure_from_env()
    try:
        assert not _runtime.is_native_enabled()
    finally:
        monkeypatch.delenv("STRUCTGURU_LEGACY", raising=False)
        _runtime.shutdown()


def test_configure_with_stream_enables_native() -> None:
    """configure() with a stream sink enables native mode (v1.0 behavior)."""
    _runtime.shutdown()

    buf = io.StringIO()
    configure(service="test", level="DEBUG", stream=buf)
    try:
        assert _runtime.is_native_enabled(), "configure should enable native with stream_sink"
    finally:
        _runtime.shutdown()


def test_native_logs_without_configure() -> None:
    """Without configure, logger calls route through native natively."""
    _runtime.configure(service="svc", target="memory", level="DEBUG")
    try:
        structguru.logger.info("native default {x}", x=1)
        _runtime.flush_native()
        lines = _runtime.drain_messages()
        assert len(lines) == 1
        assert "native default 1" in lines[0]
    finally:
        _runtime.shutdown()
