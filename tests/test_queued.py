"""Tests for structguru.queued."""

from __future__ import annotations

import io
import logging
import time

import pytest

from structguru.config import configure_structlog
from structguru.queued import configure_queued_logging


class TestConfigureQueuedLogging:
    def test_replaces_handler_with_queue(self) -> None:
        buf = io.StringIO()
        configure_structlog(service="test", level="DEBUG", json_logs=True, stream=buf)

        listener = configure_queued_logging()
        try:
            root = logging.getLogger()
            from logging.handlers import QueueHandler

            assert any(isinstance(h, QueueHandler) for h in root.handlers)
        finally:
            listener.stop()

    def test_messages_still_arrive(self) -> None:
        buf = io.StringIO()
        configure_structlog(service="test", level="DEBUG", json_logs=True, stream=buf)

        listener = configure_queued_logging()
        try:
            import structlog

            log = structlog.get_logger("test")
            log.info("queued message")
            time.sleep(0.1)  # allow background thread to process
        finally:
            listener.stop()

        assert "queued message" in buf.getvalue()

    def test_raises_without_handler(self) -> None:
        root = logging.getLogger()
        root.handlers.clear()
        with pytest.raises(RuntimeError, match="No suitable handler"):
            configure_queued_logging()

    def test_explicit_handler(self) -> None:
        buf = io.StringIO()
        configure_structlog(service="test", level="DEBUG", json_logs=True, stream=buf)
        root = logging.getLogger()
        target = root.handlers[0]

        listener = configure_queued_logging(handler=target)
        try:
            from logging.handlers import QueueHandler

            assert any(isinstance(h, QueueHandler) for h in root.handlers)
        finally:
            listener.stop()
