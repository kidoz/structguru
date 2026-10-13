"""Native-path sink tests: rotating-file sink, callable sinks, and console renderer.

Covers Phase 2 of the Rust migration: native file output with rotation,
deadlock-free callable-sink dispatch, and the colored console renderer.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import warnings
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import structguru
from structguru import _runtime

pytestmark = pytest.mark.skipif(
    not _runtime.is_available(),
    reason="native extension not built",
)


# -- rotating-file sink -----------------------------------------------------


@pytest.mark.skipif(os.name != "posix", reason="uses POSIX file locks and pipe polling")
@pytest.mark.parametrize("fail_open", [False, True], ids=["success", "open-failure"])
def test_file_sink_construction_releases_gil(tmp_path: Path, fail_open: bool) -> None:
    import fcntl
    import select

    output = tmp_path / "output"
    if fail_open:
        output.mkdir()  # Lock acquisition succeeds, but opening a directory fails.
    code = textwrap.dedent("""
        import json
        import sys
        import threading
        import structguru as sg
        from structguru import _runtime

        sg.configure(sg.Settings(target='memory', service='original'))
        original = _runtime.current_runtime()
        sg.logger.info('before construction')
        entered = threading.Event()
        errors = []

        def progress():
            entered.wait()
            # Give the configuring thread time to enter the native constructor.
            # The parent keeps the actual file lock until it receives this
            # thread's acknowledgement; no timed lock release can hide a stall.
            threading.Event().wait(0.05)
            try:
                sg.logger.info('during construction')
                sg.flush()
                print('progress', flush=True)
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=progress)
        worker.start()
        print('ready', flush=True)
        entered.set()
        try:
            sg.configure(sg.Settings(file_path=sys.argv[1], service='replacement'))
        except ValueError:
            assert sys.argv[2] == 'True'
            assert _runtime.current_runtime() is original
        else:
            assert sys.argv[2] == 'False'
            assert _runtime.current_runtime() is not original
        worker.join(2)
        assert not worker.is_alive() and not errors, errors
        sg.logger.info('after construction')
        sg.flush()
        messages = [json.loads(line)['message'] for line in original.writer.messages()]
        expected = ['before construction', 'during construction']
        if sys.argv[2] == 'True':
            expected.append('after construction')
            assert sg.get_config().service == 'original'
        else:
            assert sg.get_config().service == 'replacement'
        assert messages == expected, messages
        sg.shutdown()
    """)
    with Path(f"{output}.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        # Unbuffered pipes ensure select() cannot overlook a prefetched line.
        child = subprocess.Popen(
            [sys.executable, "-u", "-c", code, str(output), str(fail_open)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        try:
            assert child.stdout is not None
            assert select.select([child.stdout], [], [], 3)[0], "child did not start"
            assert child.stdout.readline() == b"ready\n"
            progressed = bool(select.select([child.stdout], [], [], 2)[0])
            acknowledgement = child.stdout.readline() if progressed else b""
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            try:
                stdout, stderr = child.communicate(timeout=3)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.communicate()

    assert child.returncode == 0, (stdout, stderr)
    assert acknowledgement == b"progress\n", "sink construction blocked other Python threads"
    if not fail_open:
        [record] = [json.loads(line) for line in output.read_text().splitlines()]
        assert record["message"] == "after construction"
        assert record["service"] == "replacement"


def test_file_sink_writes_records_to_file() -> None:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
        path = f.name
    os.unlink(path)  # let the sink create it
    try:
        _runtime.configure(service="svc", target="memory", level="DEBUG", file_path=path)
        try:
            structguru.logger.info("file sink test", request_id="r1")
            _runtime.flush_native()
        finally:
            _runtime.shutdown()

        with open(path) as f:
            content = f.read()
        assert "file sink test" in content
        assert '"request_id":"r1"' in content
    finally:
        if os.path.exists(path):
            os.unlink(path)


def test_file_sink_reopens_after_external_rename(tmp_path: Path) -> None:
    """A logrotate-style rename by another process must not capture later records."""
    path = tmp_path / "app.log"
    rotated = tmp_path / "app.log.1"
    _runtime.configure(service="svc", target="memory", level="DEBUG", file_path=str(path))
    try:
        for i in range(50):
            structguru.logger.info("before {i}", i=i)
        _runtime.flush_native()
        os.rename(path, rotated)
        for i in range(50):
            structguru.logger.info("after {i}", i=i)
        _runtime.flush_native()
        metrics = _runtime.writer_metrics()
    finally:
        _runtime.shutdown()

    assert metrics is not None and metrics["sink_errors"] == 0
    before = rotated.read_text().splitlines()
    after = path.read_text().splitlines()
    assert len(before) == 50 and all('"message":"before ' in line for line in before)
    assert len(after) == 50 and all('"message":"after ' in line for line in after)


def test_file_sink_recreates_a_deleted_file(tmp_path: Path) -> None:
    """Deleting the log file out from under the sink must not lose later records."""
    path = tmp_path / "app.log"
    _runtime.configure(service="svc", target="memory", level="DEBUG", file_path=str(path))
    try:
        for i in range(50):
            structguru.logger.info("before {i}", i=i)
        _runtime.flush_native()
        os.remove(path)
        for i in range(50):
            structguru.logger.info("after {i}", i=i)
        _runtime.flush_native()
        metrics = _runtime.writer_metrics()
    finally:
        _runtime.shutdown()

    assert metrics is not None and metrics["sink_errors"] == 0
    after = path.read_text().splitlines()
    assert len(after) == 50 and all('"message":"after ' in line for line in after)


def test_file_sink_rotates_at_max_bytes() -> None:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
        path = f.name
    os.unlink(path)
    try:
        # Tiny max_bytes so rotation triggers quickly; backup_count=3.
        _runtime.configure(
            service="svc",
            target="memory",
            level="DEBUG",
            file_path=path,
            file_max_bytes=200,
            file_backup_count=3,
        )
        try:
            # Each record is ~150 bytes of JSON; write enough to trigger rotations.
            for i in range(10):
                structguru.logger.info("rotation test {n}", n=i)
            _runtime.flush_native()
        finally:
            _runtime.shutdown()

        # After rotation, .1 should exist (the first rotated file).
        assert os.path.exists(f"{path}.1"), "backup .1 should exist after rotation"
        # backup_count=3 means at most 3 backups.
        assert not os.path.exists(f"{path}.4"), ".4 should not exist (backup_count=3)"

        # Rotation happens before the threshold-crossing record, matching
        # logging.handlers.RotatingFileHandler.
        with open(path) as f:
            active = f.read()
        with open(f"{path}.1") as f:
            backup1 = f.read()
        assert active or backup1, "at least one file must have content"
    finally:
        for suffix in ["", ".1", ".2", ".3", ".4"]:
            p = f"{path}{suffix}"
            if os.path.exists(p):
                os.unlink(p)


def test_file_sink_and_stdout_both_receive() -> None:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
        path = f.name
    os.unlink(path)
    try:
        _runtime.configure(
            service="svc",
            target="memory",
            level="DEBUG",
            file_path=path,
            also_stdout=True,
        )
        try:
            structguru.logger.info("mirrored record")
            _runtime.flush_native()
        finally:
            _runtime.shutdown()

        with open(path) as f:
            assert "mirrored record" in f.read()
    finally:
        if os.path.exists(path):
            os.unlink(path)


# -- callable sinks ---------------------------------------------------------


def test_callable_sink_receives_rendered_lines() -> None:
    received: list[str] = []
    lock = threading.Lock()

    def collector(line: str) -> None:
        with lock:
            received.append(line)

    _runtime.configure(service="svc", target="memory", level="DEBUG", callable_sinks=[collector])
    try:
        for i in range(5):
            structguru.logger.info("callable {n}", n=i)
        _runtime.flush_native()
    finally:
        _runtime.shutdown()

    assert len(received) == 5
    assert json.loads(received[0])["message"] == "callable 0"


@pytest.mark.filterwarnings("ignore:structguru callable sink raised")
def test_callable_sink_errors_are_swallowed() -> None:
    good: list[str] = []
    lock = threading.Lock()

    def bad_sink(_line: str) -> None:
        raise RuntimeError("boom")

    def good_sink(line: str) -> None:
        with lock:
            good.append(line)

    _runtime.configure(
        service="svc",
        target="memory",
        level="DEBUG",
        callable_sinks=[bad_sink, good_sink],
    )
    try:
        structguru.logger.info("survives")
        _runtime.flush_native()
    finally:
        _runtime.shutdown()

    assert len(good) == 1, "good sink must still receive despite bad_sink raising"


def test_callable_sink_errors_are_counted_and_reported() -> None:
    # A subprocess sees the worker thread's warning on stderr on every build;
    # context-aware warning capture would miss it on free-threaded Python.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent("""
            import structguru as sg
            calls = []
            def flaky(line):
                calls.append(line)
                raise ConnectionError('collector down')
            sg.configure(target='null', callable_sinks=[flaky])
            token = sg.logger.add(flaky)
            for i in range(3):
                sg.logger.info('record {}', i)
            sg.flush()
            assert len(calls) == 6, calls
            print(sg.writer_metrics()['callable_errors'])
            sg.logger.remove(token)
            sg.shutdown()
        """),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["6"]
    # Rate-limited like stream sink failures: the first failure is reported.
    assert result.stderr.count("callable sink raised") == 1, result.stderr
    assert "latest ConnectionError" in result.stderr
    assert "collector down" not in result.stderr


def test_callable_drop_count_survives_add_and_resets_on_configure() -> None:
    started, release = threading.Event(), threading.Event()

    def blocking_sink(_line: str) -> None:
        started.set()
        release.wait(timeout=3)

    settings: dict[str, Any] = {
        "target": "null",
        "overflow": "drop",
        "callable_sinks": [blocking_sink],
        "callable_queue_maxsize": 1,
    }
    _runtime.configure(**settings)
    try:
        structguru.logger.info("first")
        assert started.wait(timeout=1)
        structguru.logger.info("second")
        with pytest.warns(UserWarning, match="callable sinks dropped 1 "):
            structguru.logger.info("third")
        token = structguru.logger.add(lambda line: None)
        metrics = _runtime.writer_metrics()
        assert metrics is not None and metrics["callable_dropped"] == 1
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always")
            structguru.logger.info("fourth")
        assert not captured, "the first-drop warning fired again after logger.add()"
        metrics = _runtime.writer_metrics()
        assert metrics is not None and metrics["callable_dropped"] == 2
    finally:
        release.set()
    structguru.logger.remove(token)
    _runtime.configure(**settings)
    metrics = _runtime.writer_metrics()
    assert metrics is not None and metrics["callable_dropped"] == 0


def test_log_call_diagnostics_never_raise_under_warnings_as_errors() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-W",
            "error",
            "-c",
            textwrap.dedent("""
            import threading
            from dataclasses import replace
            import structguru as sg
            from structguru import _runtime

            # Native queue full in drop mode.
            sg.configure(target='memory', overflow='drop')
            state = _runtime.current_runtime()
            state.writer.close()
            writer = _runtime._RUST._NativeStringWriter(1, paused=True)
            _runtime._runtime = replace(state, writer=writer)
            sg.logger.info('accepted')
            sg.logger.info('dropped by the native queue')

            # Callable queue full in drop mode.
            started, release = threading.Event(), threading.Event()
            def blocking(line):
                started.set()
                release.wait(3)
            sg.configure(target='null', overflow='drop', callable_sinks=[blocking],
                         callable_queue_maxsize=1)
            sg.logger.info('first')
            assert started.wait(3)
            sg.logger.info('queued')
            sg.logger.info('dropped by the callable queue')
            release.set()

            # A failing synchronous stream and a failing callable sink.
            class Broken:
                def write(self, line):
                    raise OSError('stream down')
            def failing(line):
                raise ConnectionError('collector down')
            sg.configure(target='null', stream_sink=Broken(), callable_sinks=[failing])
            sg.logger.info('stream and callable failure')
            sg.flush()
            sg.shutdown()
            print('completed')
        """),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["completed"]
    for diagnostic in (
        "native logging dropped 1 record(s): queue full",
        "callable sinks dropped 1 delivery record(s): queue full",
        "stream sink write failed 1 time(s), latest OSError",
        "callable sink raised 1 time(s), latest ConnectionError",
    ):
        assert diagnostic in result.stderr, result.stderr


def _collect_sentry(events: list[str]) -> Any:
    def processor(_logger: Any, _method: str, event: dict[str, Any]) -> dict[str, Any]:
        events.append(event["event"])
        return event

    return processor


class _StagedWriter:
    """Writer proxy that runs a lifecycle action around the fused capture call."""

    def __init__(self, writer: Any, before: Any = None, after: Any = None) -> None:
        self._writer, self._before, self._after = writer, before, after

    def __getattr__(self, name: str) -> Any:
        return getattr(self._writer, name)

    def log_capture(self, *args: Any, **kwargs: Any) -> Any:
        if self._before is not None:
            self._before()
        result = self._writer.log_capture(*args, **kwargs)
        if self._after is not None:
            self._after()
        return result


def test_record_accepted_before_reconfiguration_reaches_callable_sinks_and_sentry() -> None:
    received: list[str] = []
    events: list[str] = []
    _runtime.configure(target="memory", sentry_processor=_collect_sentry(events))
    token = structguru.logger.add(lambda line: received.append(json.loads(line)["message"]))
    state = _runtime.current_runtime()
    assert state is not None
    staged = _StagedWriter(state.writer, after=lambda: structguru.update(service="next"))
    _runtime._runtime = replace(state, writer=staged)  # type: ignore[arg-type]

    structguru.logger.info("accepted before update")
    structguru.flush()

    assert [json.loads(line)["message"] for line in state.writer.messages()] == [
        "accepted before update"
    ]
    assert received == ["accepted before update"]
    assert events == ["accepted before update"]
    assert structguru.get_config().service == "next"  # type: ignore[union-attr]
    structguru.logger.remove(token)


def test_writer_rejection_skips_every_secondary_destination() -> None:
    received: list[str] = []
    events: list[str] = []
    stream = io.StringIO()
    _runtime.configure(
        target="memory", stream_sink=stream, sentry_processor=_collect_sentry(events)
    )
    token = structguru.logger.add(received.append)
    state = _runtime.current_runtime()
    assert state is not None
    before = structguru.lifecycle_metrics()["rejected"]
    staged = _StagedWriter(state.writer, before=state.writer.close)
    _runtime._runtime = replace(state, writer=staged)  # type: ignore[arg-type]

    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        structguru.logger.info("rejected by a closed writer")
    structguru.flush()

    assert not captured
    assert structguru.lifecycle_metrics()["rejected"] == before + 1
    assert stream.getvalue() == ""
    assert received == []
    assert events == []
    structguru.logger.remove(token)


def test_record_accepted_while_shutting_down_is_not_delivered_afterwards() -> None:
    received: list[str] = []
    events: list[str] = []
    _runtime.configure(target="memory", sentry_processor=_collect_sentry(events))
    token = structguru.logger.add(received.append)
    state = _runtime.current_runtime()
    assert state is not None
    staged = _StagedWriter(state.writer, after=structguru.shutdown)
    _runtime._runtime = replace(state, writer=staged)  # type: ignore[arg-type]

    structguru.logger.info("accepted while shutting down")

    assert _runtime.current_runtime() is None
    assert [json.loads(line)["message"] for line in state.writer.messages()] == [
        "accepted while shutting down"
    ]
    assert received == []
    assert events == []
    structguru.logger.remove(token)


def test_stream_sink_write_errors_never_reach_the_caller() -> None:
    """A synchronous stream sink that raises loses only its own copy of the line."""

    class FlakyStream:
        def __init__(self) -> None:
            self.lines: list[str] = []
            self.failures = 0

        def write(self, message: str, /) -> None:
            if len(self.lines) == 1 and self.failures == 0:
                self.failures += 1
                raise OSError(28, "No space left on device")
            self.lines.append(message)

    stream = FlakyStream()
    _runtime._reset_drop_count()
    _runtime.configure(service="svc", target="memory", level="DEBUG", stream_sink=stream)
    try:
        with pytest.warns(UserWarning, match="stream sink write failed"):
            for i in range(3):
                structguru.logger.info("line {i}", i=i)
        _runtime.flush_native()
        rendered = _runtime.drain_messages()
        metrics = _runtime.writer_metrics()
    finally:
        _runtime.shutdown()
        _runtime._reset_drop_count()

    assert stream.failures == 1
    assert [json.loads(line)["message"] for line in stream.lines] == ["line 0", "line 2"]
    # The native destinations keep every record; only the stream copy is lost,
    # and the loss is visible instead of silent.
    assert [json.loads(line)["message"] for line in rendered] == ["line 0", "line 1", "line 2"]
    assert metrics is not None and metrics["stream_errors"] == 1


@pytest.mark.parametrize("destination", ["stream", "callable"])
def test_sink_error_diagnostics_never_stringify_exception(destination: str) -> None:
    # Capture real stderr so worker warnings are observed on free-threaded builds.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent("""
                import json, sys
                import structguru as sg
                from structguru import _runtime

                destination = sys.argv[1]
                canary = "REVIEW_SYNTHETIC_TOKEN"
                stringified = []
                class SensitiveError(ValueError):
                    def __str__(self):
                        stringified.append(True)
                        return canary
                def fail(line):
                    raise SensitiveError(canary)
                class BadStream:
                    write = staticmethod(fail)

                sg.configure(
                    target="memory",
                    sensitive_patterns=[canary],
                    stream_sink=BadStream() if destination == "stream" else None,
                    callable_sinks=[fail] if destination == "callable" else None,
                )
                sg.logger.info(canary)
                sg.flush()
                assert stringified == [], stringified
                assert json.loads(_runtime.drain_messages()[0])["message"] == "[REDACTED]"
                metrics = sg.writer_metrics()
                counter = "stream_errors" if destination == "stream" else "callable_errors"
                assert metrics[counter] == 1
                sg.shutdown()
            """),
            destination,
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert "SensitiveError" in result.stderr
    assert "1 time(s)" in result.stderr
    assert "REVIEW_SYNTHETIC_TOKEN" not in result.stderr
    assert result.stderr.count("UserWarning") == 1


def test_callable_sink_stopped_on_disable() -> None:
    received: list[str] = []
    lock = threading.Lock()

    def collector(line: str) -> None:
        with lock:
            received.append(line)

    _runtime.configure(service="svc", target="memory", level="DEBUG", callable_sinks=[collector])
    structguru.logger.info("before disable")
    _runtime.shutdown()

    count_before = len(received)
    assert count_before == 1, "shutdown must drain pending callable deliveries"
    structguru.logger.info("after disable")
    assert len(received) == count_before


def test_reconfigure_drains_old_configured_sinks_before_replacing() -> None:
    first: list[str] = []
    second: list[str] = []
    _runtime.configure(target="memory", callable_sinks=[first.append])
    structguru.logger.info("old configuration")
    _runtime.configure(target="memory", callable_sinks=[second.append])
    try:
        structguru.logger.info("new configuration")
        _runtime.flush_native()
    finally:
        _runtime.shutdown()

    assert any("old configuration" in line for line in first)
    assert not any("new configuration" in line for line in first)
    assert any("new configuration" in line for line in second)


def test_failed_reconfigure_preserves_active_writer() -> None:
    _runtime.configure(target="memory")
    structguru.logger.info("before failed reconfigure")

    with pytest.raises(ValueError, match="target"):
        _runtime.configure(target="invalid")

    assert _runtime.is_native_enabled()
    structguru.logger.info("after failed reconfigure")
    _runtime.flush_native()
    lines = _runtime.drain_messages()
    assert any("before failed reconfigure" in line for line in lines)
    assert any("after failed reconfigure" in line for line in lines)


def test_failed_writer_construction_preserves_active_writer(tmp_path: Path) -> None:
    _runtime.configure(target="memory")
    structguru.logger.info("before invalid file")

    with pytest.raises(ValueError):
        _runtime.configure(file_path=str(tmp_path))

    assert _runtime.is_native_enabled()
    structguru.logger.info("after invalid file")
    _runtime.flush_native()
    lines = _runtime.drain_messages()
    assert any("before invalid file" in line for line in lines)
    assert any("after invalid file" in line for line in lines)


def test_default_output_queue_is_bounded() -> None:
    _runtime.configure(target="memory")

    metrics = _runtime.writer_metrics()

    assert metrics is not None
    assert metrics["maxsize"] > 0


def test_atexit_drains_callable_sinks(tmp_path: Path) -> None:
    output = tmp_path / "atexit-callback.log"
    code = f"""
from pathlib import Path
import structguru

output = Path({str(output)!r})
def sink(line):
    output.write_text(line, encoding="utf-8")

structguru.configure(target="null", callable_sinks=[sink])
structguru.logger.info("delivered at exit")
"""
    subprocess.run([sys.executable, "-c", code], check=True)
    assert "delivered at exit" in output.read_text(encoding="utf-8")


def test_callable_sink_queue_is_bounded_and_counts_drops() -> None:
    started = threading.Event()
    release = threading.Event()

    def blocking_sink(_line: str) -> None:
        started.set()
        release.wait(timeout=2)

    _runtime.configure(
        target="memory",
        overflow="drop",
        callable_sinks=[blocking_sink],
        callable_queue_maxsize=1,
    )
    try:
        structguru.logger.info("first")
        assert started.wait(timeout=1)
        structguru.logger.info("second")
        with pytest.warns(UserWarning, match="callable sinks dropped"):
            structguru.logger.info("third")
        metrics = _runtime.writer_metrics()
        assert metrics is not None
        assert metrics["callable_maxsize"] == 1
        assert metrics["callable_dropped"] == 1
    finally:
        release.set()
        _runtime.shutdown()


def test_invalid_callable_queue_maxsize_raises() -> None:
    with pytest.raises(ValueError, match="callable_queue_maxsize"):
        _runtime.configure(callable_queue_maxsize=0)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"exception_max_frames": -1},
        {"exception_max_local_repr": -1},
    ],
)
def test_invalid_structured_exception_limits_raise(kwargs: dict[str, int]) -> None:
    with pytest.raises(ValueError, match="exception_max"):
        _runtime.configure(structured_exceptions=True, **kwargs)


def test_non_callable_sink_raises() -> None:
    with pytest.raises(TypeError, match="callable_sinks"):
        _runtime.configure(callable_sinks=["not callable"])  # type: ignore[list-item]
    assert not _runtime.is_native_enabled()


# -- console renderer -------------------------------------------------------


def _drain_last_line() -> str:
    _runtime.flush_native()
    return _runtime.drain_messages()[-1].rstrip("\n")


def test_console_renderer_human_readable_no_colors() -> None:
    _runtime.configure(
        service="svc", target="memory", level="DEBUG", format="console", colors=False
    )
    try:
        structguru.logger.info("hello {name}", name="world", count=3)
        line = _drain_last_line()
    finally:
        _runtime.shutdown()

    # Format: <timestamp> [<LEVEL>] <message>  k=v ...
    assert "[INFO    ]" in line
    assert "hello world" in line
    assert "count=3" in line
    assert "\x1b" not in line  # no ANSI codes


def test_console_renderer_applies_colors() -> None:
    _runtime.configure(
        service="svc", target="memory", level="DEBUG", format="console", colors=True
    )
    try:
        structguru.logger.error("boom")
        line = _drain_last_line()
    finally:
        _runtime.shutdown()

    assert "\x1b[31m" in line  # ANSI red
    assert "\x1b[0m" in line  # ANSI reset
    assert "[ERROR   ]" in line


def test_console_renderer_redacts_sensitive_keys() -> None:
    _runtime.configure(
        service="svc", target="memory", level="DEBUG", format="console", colors=False
    )
    try:
        structguru.logger.info("login", password="hunter2", user="alice")
        line = _drain_last_line()
    finally:
        _runtime.shutdown()

    assert 'password="[REDACTED]"' in line
    assert "hunter2" not in line
    assert 'user="alice"' in line


def test_console_renderer_warn_level_uses_yellow() -> None:
    _runtime.configure(
        service="svc", target="memory", level="DEBUG", format="console", colors=True
    )
    try:
        structguru.logger.warning("careful")
        line = _drain_last_line()
    finally:
        _runtime.shutdown()

    assert "\x1b[33m" in line  # ANSI yellow
    assert "[WARN    ]" in line


def test_partial_file_failure_is_visible_while_stdout_keeps_working(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "partial.log"
    _runtime.configure(
        file_path=str(path), file_max_bytes=10, file_backup_count=1, also_stdout=True
    )
    structguru.logger.info("first")
    structguru.flush()
    # A directory at the oldest backup path makes rotation fail portably.
    (tmp_path / "partial.log.1").mkdir()
    structguru.logger.info("second")
    structguru.flush()
    metrics = structguru.writer_metrics()
    assert metrics is not None
    assert metrics["written"] == 2
    assert metrics["sink_errors"] == 1
    assert len(path.read_text().splitlines()) == 1
    assert len(capfd.readouterr().out.splitlines()) == 2
