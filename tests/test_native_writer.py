from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import structguru._rust as rust


@pytest.mark.parametrize("fused", [False, True])
def test_enqueue_outcome_distinguishes_full_from_closed(fused: bool) -> None:
    writer = rust._NativeStringWriter(1, paused=True)

    def enqueue(message: str, blocking: bool = False) -> str:
        if fused:
            return writer.render_enqueue_json_outcome(
                {}, "logger", "info", "service", message, blocking
            )
        return writer.enqueue_outcome(message, blocking)

    try:
        assert enqueue("accepted") == "accepted"
        assert enqueue("overflow") == "full"
        writer.close()
        assert enqueue("retired") == "closed"
        assert enqueue("retired blocking", True) == "closed"
        assert writer.metrics()["dropped"] == 1
        assert writer.metrics()["written"] == 1
    finally:
        writer.close()


def test_native_string_writer_drains_messages_in_order() -> None:
    writer = rust._NativeStringWriter(0)

    assert writer.maxsize == 0
    assert writer.try_enqueue("first")
    assert writer.try_enqueue("second")
    writer.flush()

    assert writer.messages() == ["first", "second"]
    assert writer.metrics() == {
        "enqueued": 2,
        "dropped": 0,
        "dequeued": 2,
        "written": 2,
        "sink_errors": 0,
        "depth": 0,
        "maxsize": 0,
        "in_flight": 0,
        "closed": False,
        "worker_done": False,
        "paused": False,
    }
    writer.close()


def test_native_string_writer_bounded_drop_metrics() -> None:
    writer = rust._NativeStringWriter(1, paused=True)

    assert writer.try_enqueue("kept")
    assert not writer.try_enqueue("dropped")
    assert writer.metrics()["dropped"] == 1
    assert writer.metrics()["depth"] == 1

    writer.resume()
    writer.flush()

    assert writer.messages() == ["kept"]
    assert writer.metrics()["written"] == 1
    writer.close()


def test_native_string_writer_close_is_idempotent_and_rejects_new_messages() -> None:
    writer = rust._NativeStringWriter(0)

    assert writer.try_enqueue("before close")
    writer.close()
    writer.close()

    assert not writer.try_enqueue("after close")
    assert writer.messages() == ["before close"]
    assert writer.metrics()["closed"] is True
    assert writer.metrics()["worker_done"] is True


def test_native_stdout_writer_counts_closed_stdout_as_sink_errors(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent("""
            import os
            import structguru._rust as rust

            os.close(1)

            def deliver(**kwargs):
                writer = rust._NativeStringWriter(8, **kwargs)
                for i in range(3):
                    assert writer.try_enqueue(f"lost {i}\\n")
                writer.flush()
                metrics = writer.metrics()
                writer.close()
                return metrics["written"], metrics["sink_errors"]

            assert deliver(target="stdout") == (0, 3)
            # A read-only fd 1 fails writes the same way and keeps the log file
            # from being opened as fd 1. The file copy lands; the mirror fails.
            os.dup2(os.open(os.devnull, os.O_RDONLY), 1)
            assert deliver(file_path="app.log", also_stdout=True) == (3, 3)
        """),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "app.log").read_text().splitlines() == ["lost 0", "lost 1", "lost 2"]


def test_native_string_writer_counts_sink_errors_without_stopping() -> None:
    writer = rust._NativeStringWriter(0, fail_after=1)

    assert writer.try_enqueue("first")
    assert writer.try_enqueue("second")
    assert writer.try_enqueue("third")
    writer.flush()

    assert writer.messages() == ["first"]
    assert writer.metrics()["enqueued"] == 3
    assert writer.metrics()["dequeued"] == 3
    assert writer.metrics()["written"] == 1
    assert writer.metrics()["sink_errors"] == 2
    writer.close()
