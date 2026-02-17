"""Shared fixtures for structguru tests."""

from __future__ import annotations

import logging

import pytest
import structlog


@pytest.fixture(autouse=True)
def _reset_logging() -> None:  # type: ignore[misc]
    """Reset root logger handlers and level after each test."""
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    original_factory = logging.getLogRecordFactory()

    yield  # type: ignore[misc]

    root.handlers[:] = original_handlers
    root.setLevel(original_level)
    logging.setLogRecordFactory(original_factory)


@pytest.fixture(autouse=True)
def _reset_structlog() -> None:  # type: ignore[misc]
    """Reset structlog configuration after each test."""
    yield  # type: ignore[misc]
    structlog.reset_defaults()
