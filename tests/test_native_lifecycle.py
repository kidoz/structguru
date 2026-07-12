"""Reliability tests for native mode: shutdown flush and fork safety."""

from __future__ import annotations

import os
import select
import threading

import pytest

import structguru
from structguru import _native

pytestmark = pytest.mark.skipif(
    not _native.is_available(),
    reason="native extension not built",
)


def test_close_drains_buffered_records() -> None:
    """flush() must drain queued records to the sink before they are read."""
    _native.configure(service="svc", target="memory")
    try:
        structguru.logger.info("last message")
        _native.flush_native()
        assert any("last message" in line for line in _native.drain_messages())
    finally:
        _native.shutdown()


def test_metrics_track_enqueue_and_write() -> None:
    _native.configure(service="svc", target="memory")
    try:
        for _ in range(5):
            structguru.logger.info("m")
        _native.flush_native()
        metrics = _native.writer_metrics()
        assert metrics is not None
        assert metrics["enqueued"] == 5
        assert metrics["written"] == 5
        assert metrics["dropped"] == 0
    finally:
        _native.shutdown()


def test_block_overflow_never_drops_under_backpressure() -> None:
    """A small bounded queue in block mode must apply backpressure, not drop."""
    _native.configure(service="svc", target="memory", maxsize=4, overflow="block")
    try:
        for _ in range(200):
            structguru.logger.info("m")
        _native.flush_native()
        metrics = _native.writer_metrics()
        assert metrics is not None
        assert metrics["enqueued"] == 200
        assert metrics["written"] == 200
        assert metrics["dropped"] == 0
    finally:
        _native.shutdown()


def test_drop_emits_rate_limited_warning() -> None:
    _native._reset_drop_count()
    with pytest.warns(UserWarning, match="dropped"):
        _native._note_drop()


def test_disable_during_in_flight_formatting_never_raises() -> None:
    """A record that started before shutdown may be retired, but cannot crash."""
    formatting_started = threading.Event()
    resume_formatting = threading.Event()
    errors: list[BaseException] = []

    class SlowMessage:
        def __str__(self) -> str:
            formatting_started.set()
            assert resume_formatting.wait(timeout=2)
            return "in flight"

    _native.configure(target="null")

    def emit() -> None:
        try:
            structguru.logger.info(SlowMessage())
        except BaseException as exc:  # capture the exact regression, including AssertionError
            errors.append(exc)

    producer = threading.Thread(target=emit)
    producer.start()
    assert formatting_started.wait(timeout=1)
    _native.shutdown()
    resume_formatting.set()
    producer.join(timeout=2)

    assert not producer.is_alive()
    assert errors == []


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires os.fork (POSIX)")
def test_native_writer_survives_fork() -> None:
    """After fork, the child respawns its writer and logs without deadlocking.

    The parent's background writer thread does not exist in the child; if the
    child tried to use or join it, this test would hang (caught by the select
    timeout). The registered ``after_in_child`` hook must swap in a fresh writer.
    """
    _native.configure(service="svc", target="memory")
    try:
        structguru.logger.info("parent log")
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:  # child
            try:
                structguru.logger.info("child log")
                _native.flush_native()
                logged = any("child log" in line for line in _native.drain_messages())
                os.write(write_fd, b"1" if logged else b"0")
            except BaseException:
                os.write(write_fd, b"E")
            finally:
                os._exit(0)

        # parent
        os.close(write_fd)
        ready, _, _ = select.select([read_fd], [], [], 5.0)
        assert ready, "child deadlocked after fork (no writer respawn)"
        result = os.read(read_fd, 1)
        os.close(read_fd)
        os.waitpid(pid, 0)
        assert result == b"1", f"child failed to log natively after fork: {result!r}"
    finally:
        _native.shutdown()
