"""Phase 4 compatibility tests: public API, contextvars, and interop under native mode."""

from __future__ import annotations

import json
from typing import Any

import pytest

import structguru
from structguru import _native
from structguru._contextvars import bind_contextvars, clear_contextvars

pytestmark = pytest.mark.skipif(
    not _native.is_available(),
    reason="native extension not built",
)


def _last(record_lines: list[str]) -> dict[str, Any]:
    return json.loads(record_lines[-1])


def test_public_native_api_is_exported() -> None:
    for name in (
        "configure",
        "shutdown",
        "set_level",
        "writer_metrics",
    ):
        assert hasattr(structguru, name), name
    assert not hasattr(structguru, "enable_native")


def test_public_configure_and_runtime_level_change() -> None:
    structguru.configure(service="svc", target="memory", level="INFO")
    try:
        assert structguru.writer_metrics() is not None
        structguru.set_level("ERROR")
        structguru.logger.info("dropped")  # below ERROR now
        structguru.logger.error("kept")
        _native.flush_native()
        lines = _native.drain_messages()
        assert len(lines) == 1
        assert _last(lines)["message"] == "kept"
    finally:
        structguru.shutdown()


def test_native_honors_bind_and_contextualize() -> None:
    structguru.configure(service="svc", target="memory", level="DEBUG")
    try:
        with structguru.logger.contextualize(request_id="r1"):
            structguru.logger.bind(user="alice").info("hi")
        _native.flush_native()
        record = _last(_native.drain_messages())
        assert record["request_id"] == "r1"
        assert record["user"] == "alice"
    finally:
        structguru.shutdown()


def test_native_picks_up_integration_contextvars() -> None:
    """Integrations (asgi/flask/django/celery/grpc) bind via contextvars — the
    native path must snapshot them, so all of them work under native mode."""
    structguru.configure(service="svc", target="memory", level="DEBUG")
    clear_contextvars()
    try:
        bind_contextvars(request_id="req-9", method="GET")
        structguru.logger.info("handled")
        _native.flush_native()
        record = _last(_native.drain_messages())
        assert record["request_id"] == "req-9"
        assert record["method"] == "GET"
    finally:
        clear_contextvars()
        structguru.shutdown()


def test_env_var_auto_enable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STRUCTGURU_NATIVE_TARGET", "memory")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    _native.shutdown()
    try:
        _native._maybe_configure_from_env()
        assert _native.is_native_enabled()
    finally:
        _native.shutdown()
