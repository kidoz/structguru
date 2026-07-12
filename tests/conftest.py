"""Shared fixtures for structguru tests."""

from __future__ import annotations

import logging
import sys
from typing import Any

import pytest

from structguru import _native


@pytest.fixture(autouse=True)
def _reset_logging() -> None:  # type: ignore[misc]
    """Reset root logger handlers and level after each test."""
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    original_factory = logging.getLogRecordFactory()
    original_excepthook = sys.excepthook

    yield  # type: ignore[misc]

    root.handlers[:] = original_handlers
    root.setLevel(original_level)
    logging.setLogRecordFactory(original_factory)
    sys.excepthook = original_excepthook
    _native.disable_native()


def configure(
    *,
    service: str = "app",
    level: str = "DEBUG",
    json: bool = True,
    stream: Any = None,
) -> None:
    """Configure native logging with a stream sink (test helper).

    Wires the native renderer to *stream* so logger output is available
    synchronously.
    """
    _native.configure(
        service=service,
        level=level,
        json=json,
        stream_sink=stream,
    )
