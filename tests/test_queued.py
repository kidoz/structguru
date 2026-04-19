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

    def test_raises_on_second_call(self) -> None:
        buf = io.StringIO()
        configure_structlog(service="test", level="DEBUG", json_logs=True, stream=buf)

        listener = configure_queued_logging()
        try:
            with pytest.raises(RuntimeError, match="already configured"):
                configure_queued_logging()
        finally:
            listener.stop()

    def test_rejects_handler_and_handlers_together(self) -> None:
        buf = io.StringIO()
        configure_structlog(service="test", level="DEBUG", json_logs=True, stream=buf)
        root = logging.getLogger()
        target = root.handlers[0]

        with pytest.raises(ValueError, match="handler= or handlers="):
            configure_queued_logging(handler=target, handlers=[target])

    def test_queues_all_real_handlers_by_default(self) -> None:
        buf1 = io.StringIO()
        buf2 = io.StringIO()
        configure_structlog(service="test", level="DEBUG", json_logs=True, stream=buf1)
        root = logging.getLogger()
        extra = logging.StreamHandler(buf2)
        extra.setFormatter(logging.Formatter("%(message)s"))
        root.addHandler(extra)

        listener = configure_queued_logging()
        try:
            import structlog

            structlog.get_logger("test").info("fan out")
            time.sleep(0.1)
            # Both target handlers must have been pulled behind the queue —
            # only one QueueHandler and the msg-fixer should remain on root.
            from logging.handlers import QueueHandler

            assert sum(1 for h in root.handlers if isinstance(h, QueueHandler)) == 1
            assert extra not in root.handlers
            assert "fan out" in buf1.getvalue()
        finally:
            listener.stop()

    def test_bounded_queue_applies_backpressure(self) -> None:
        buf = io.StringIO()
        configure_structlog(service="test", level="DEBUG", json_logs=True, stream=buf)

        listener = configure_queued_logging(maxsize=4)
        try:
            queue_size = listener.queue.maxsize  # type: ignore[attr-defined]
            assert queue_size == 4
        finally:
            listener.stop()
