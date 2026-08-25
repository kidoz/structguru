"""Shared fixtures for structguru tests."""

from __future__ import annotations

import logging
import sys
from typing import Any

import pytest

from structguru import _runtime, core
from structguru.integrations import stdlib as stdlib_integration
from structguru.integrations.stdlib import StructguruHandler, uninstall_stdlib_bridge


@pytest.fixture(autouse=True)
def _reset_logging(monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[misc]
    """Reset root logger handlers and level after each test."""
    monkeypatch.delenv("STRUCTGURU_STDLIB_DISABLE_EXISTING_LOGGERS", raising=False)
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    original_disabled = root.disabled
    original_factory = logging.getLogRecordFactory()
    original_excepthook = sys.excepthook

    yield  # type: ignore[misc]

    active_bridge = stdlib_integration._active_bridge
    if active_bridge is not None:
        uninstall_stdlib_bridge(active_bridge)
    for handler in list(root.handlers):
        if isinstance(handler, StructguruHandler):
            uninstall_stdlib_bridge(handler)
    root.handlers[:] = original_handlers
    root.setLevel(original_level)
    root.disabled = original_disabled
    logging.setLogRecordFactory(original_factory)
    sys.excepthook = original_excepthook
    # A test that installs the stdlib bridge without uninstalling would
    # otherwise leave root attachment suspended for every later test.
    core._set_stdlib_bridge_active(False)
    _runtime.shutdown()


def configure(
    *,
    service: str = "app",
    level: str = "DEBUG",
    stream: Any = None,
    format: str = "json",
) -> None:
    """Configure native logging with a stream sink (test helper).

    Wires the native renderer to *stream* so logger output is available
    synchronously. ``format`` is forwarded to ``_runtime.configure``.
    """
    _runtime.configure(
        service=service,
        level=level,
        format=format,
        stream_sink=stream,
    )
