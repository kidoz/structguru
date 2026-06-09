"""Tests for stdlib InterceptHandler."""

import io
import json
import logging

import pytest

from structguru.config import configure_structlog
from structguru.integrations.stdlib import InterceptHandler


def _records(buf: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]


@pytest.fixture
def isolated_logger() -> logging.Logger:
    """A non-propagating logger with a unique name, cleaned up after the test."""
    log = logging.getLogger("structguru_test.intercept")
    log.handlers = []
    log.propagate = False
    log.setLevel(logging.DEBUG)
    yield log
    log.handlers = []


def test_intercept_forwards_to_structguru_handler(isolated_logger: logging.Logger) -> None:
    buf = io.StringIO()
    configure_structlog(service="test", level="DEBUG", json_logs=True, stream=buf)

    isolated_logger.handlers = [InterceptHandler()]
    isolated_logger.info("intercepted message")

    records = _records(buf)
    assert len(records) == 1  # rendered exactly once, not doubled
    rec = records[0]
    assert rec["message"] == "intercepted message"
    assert rec["level"] == "INFO"
    assert rec["logger"] == "structguru_test.intercept"
    assert rec["service"] == "test"


def test_intercept_preserves_level_and_exc_info(isolated_logger: logging.Logger) -> None:
    buf = io.StringIO()
    configure_structlog(service="test", level="DEBUG", json_logs=True, stream=buf)

    isolated_logger.handlers = [InterceptHandler()]
    try:
        raise ValueError("boom")
    except ValueError:
        isolated_logger.error("failed", exc_info=True)

    records = _records(buf)
    assert len(records) == 1
    rec = records[0]
    assert rec["level"] == "ERROR"
    assert "ValueError: boom" in rec.get("exception", "")


def test_intercept_no_loop_when_attached_to_root() -> None:
    """Attaching to root must not loop; structguru's own handler renders it."""
    buf = io.StringIO()
    configure_structlog(service="test", level="DEBUG", json_logs=True, stream=buf)

    root = logging.getLogger()
    intercept = InterceptHandler()
    root.addHandler(intercept)
    try:
        # propagate=False so only the root handlers see it once.
        log = logging.getLogger("structguru_test.root_case")
        log.handlers = []
        log.propagate = True
        log.info("root routed")
    finally:
        root.removeHandler(intercept)

    # The record is rendered (at least once) and the process does not hang/recurse.
    assert any(r["message"] == "root routed" for r in _records(buf))
