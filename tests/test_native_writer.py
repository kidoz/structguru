from __future__ import annotations

import signal
import subprocess
import sys
import textwrap
import time
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


def test_native_file_writer_resolves_relative_path_at_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    monkeypatch.chdir(first)
    writer = rust._NativeStringWriter(
        8, file_path="app.log", file_max_bytes=12, file_backup_count=1
    )
    monkeypatch.chdir(second)
    for message in ("one\n", "two after chdir\n", "three\n"):
        assert writer.try_enqueue(message)
        writer.flush()
    writer.close()

    assert writer.metrics()["sink_errors"] == 0
    assert sorted(path.name for path in second.iterdir()) == []
    assert (first / "app.log").read_text() == "three\n"
    assert (first / "app.log.1").read_text() == "two after chdir\n"


@pytest.mark.parametrize(("max_bytes", "backup_count"), [(0, 5), (1 << 20, 0)])
def test_native_file_writer_follows_external_rotation_without_rotation_enabled(
    tmp_path: Path, max_bytes: int, backup_count: int
) -> None:
    path = tmp_path / "app.log"
    writer = rust._NativeStringWriter(
        8, file_path=str(path), file_max_bytes=max_bytes, file_backup_count=backup_count
    )
    assert writer.try_enqueue("before\n")
    writer.flush()
    path.rename(tmp_path / "app.log.rotated")  # logrotate "create" mode
    assert writer.try_enqueue("after rename\n")
    writer.flush()
    (tmp_path / "app.log.rotated").unlink()
    path.unlink()  # an operator deletes the active log
    assert writer.try_enqueue("after delete\n")
    writer.flush()
    writer.close()

    assert path.read_text() == "after delete\n"
    assert writer.metrics()["written"] == 3
    assert writer.metrics()["sink_errors"] == 0


@pytest.mark.skipif(not hasattr(signal, "pthread_sigmask"), reason="requires POSIX signals")
def test_native_file_writer_retries_lock_wait_interrupted_by_a_signal(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent("""
            import fcntl, json, os, signal, sys, time
            import structguru as sg

            path = os.path.join(sys.argv[1], "app.log")
            sg.configure(service="svc", file_path=path)
            # A Python handler installs without SA_RESTART; blocking the signal
            # here routes it to the writer thread, which waits in flock().
            signal.signal(signal.SIGUSR1, lambda *args: None)
            signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGUSR1})
            other = os.open(path + ".lock", os.O_RDWR)
            fcntl.flock(other, fcntl.LOCK_EX)  # another process holds the lock
            sg.logger.info("victim")
            time.sleep(0.2)
            os.kill(os.getpid(), signal.SIGUSR1)
            time.sleep(0.2)
            fcntl.flock(other, fcntl.LOCK_UN)
            sg.flush()
            metrics = sg.writer_metrics()
            assert (metrics["written"], metrics["sink_errors"]) == (1, 0), metrics
            assert [json.loads(line)["message"] for line in open(path)] == ["victim"]
            sg.shutdown()
        """),
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(not hasattr(signal, "SIGXFSZ"), reason="requires RLIMIT_FSIZE")
def test_native_file_writer_never_writes_failed_records_later(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent("""
            import os, resource, signal, sys
            import structguru._rust as rust

            path = os.path.join(sys.argv[1], "app.log")
            writer = rust._NativeStringWriter(8, file_path=path)
            signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
            # Writes past 25 bytes fail with EFBIG; the third record is torn.
            resource.setrlimit(resource.RLIMIT_FSIZE, (25, resource.RLIM_INFINITY))
            for i in range(5):
                assert writer.try_enqueue(f"record {i:02d}\\n")
                writer.flush()
            failing = writer.metrics()
            unlimited = resource.RLIM_INFINITY
            resource.setrlimit(resource.RLIMIT_FSIZE, (unlimited, unlimited))
            assert writer.try_enqueue("recovered\\n")
            writer.flush()
            writer.close()
            metrics = writer.metrics()
            assert (failing["written"], failing["sink_errors"]) == (2, 3), failing
            assert (metrics["written"], metrics["sink_errors"]) == (3, 3), metrics
            # The torn fragment ends its own line; failed records never reappear.
            assert open(path).read() == "record 00\\nrecord 01\\nrecor\\nrecovered\\n"
        """),
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_flush_stall_timeout_abandons_a_writer_that_makes_no_progress() -> None:
    # A paused worker never drains: flush must give up within the bound, report
    # the undrained record, and leave close() unable to hang on a stale join.
    writer = rust._NativeStringWriter(8, paused=True)
    try:
        assert writer.try_enqueue("stuck")
        began = time.monotonic()
        assert writer.flush(stall_timeout=0.2) == 1
        assert time.monotonic() - began < 3
        metrics = writer.metrics()
        assert metrics["closed"] is True
        assert metrics["worker_done"] is True
        assert not writer.try_enqueue("late")
        # The abandonment already leaked the thread handle; close cannot join.
        assert writer.close() == 0
    finally:
        writer.close()
