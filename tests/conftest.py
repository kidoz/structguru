"""Shared fixtures for structguru tests."""

from __future__ import annotations

import logging
import sys

import pytest


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
