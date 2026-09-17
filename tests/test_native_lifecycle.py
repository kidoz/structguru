"""Reliability tests for native mode: shutdown flush and fork safety."""

from __future__ import annotations

import io
import json
import os
import select
import subprocess
import sys
import textwrap
import threading
import warnings
from dataclasses import replace
from pathlib import Path

import pytest

import structguru
from structguru import _runtime

pytestmark = pytest.mark.skipif(
    not _runtime.is_available(),
    reason="native extension not built",
)


@pytest.mark.parametrize("operation", ["configure", "shutdown"])
@pytest.mark.parametrize("path", ["fused", "console", "stream"])
@pytest.mark.parametrize("overflow", ["block", "drop"])
def test_retired_writer_rejections_remain_observable(
    operation: str, path: str, overflow: str
) -> None:
    entered, release = threading.Event(), threading.Event()
    stream = io.StringIO()
    _runtime.configure(
        target="memory",
        overflow=overflow,
        colors=False,
        format="console" if path == "console" else "json",
        stream_sink=stream if path == "stream" else None,
    )
    state = _runtime.current_runtime()
    assert state is not None
    before = structguru.lifecycle_metrics()["rejected"]
    structguru.logger.info("accepted")
    errors: list[BaseException] = []

    class PausedMessage:
        def __str__(self) -> str:
            entered.set()
            assert release.wait(3)
            return "late"

    def produce() -> None:
        try:
            structguru.logger.info(PausedMessage())
        except BaseException as error:
            errors.append(error)

    producer = threading.Thread(target=produce, daemon=True)
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        producer.start()
        try:
            assert entered.wait(2)
            if operation == "shutdown":
                structguru.shutdown()
            else:
                structguru.configure(target="memory")
        finally:
            release.set()
            producer.join(3)
    assert not producer.is_alive()
    assert not errors
    assert not captured
    assert state.writer.metrics()["written"] == 1
    assert state.writer.metrics()["dropped"] == 0
    assert structguru.lifecycle_metrics() == {"rejected": before + 1}
    if path == "stream":
        # This is a rejected native delivery, not necessarily a lost event.
        assert [json.loads(line)["message"] for line in stream.getvalue().splitlines()] == [
            "accepted",
            "late",
        ]
    structguru.shutdown()
    snapshot = structguru.lifecycle_metrics()
    structguru.logger.info("disabled")
    assert structguru.writer_metrics() is None
    assert structguru.lifecycle_metrics() == snapshot
    snapshot["rejected"] = -1
    structguru.configure(target="memory")
    structguru.set_level("DEBUG")
    structguru.update(level="INFO")
    log = structguru.Logger()
    token = log.add(io.StringIO())
    log.remove(token)
    structguru.logger.info("recovered")
    structguru.flush()
    assert structguru.lifecycle_metrics() == {"rejected": before + 1}
    assert [json.loads(line)["message"] for line in _runtime.drain_messages()] == ["recovered"]


@pytest.mark.parametrize("operation", ["shutdown", "_atexit_close"])
def test_shutdown_drains_callback_logs_and_disables_runtime(operation: str) -> None:
    # A subprocess bounds lifecycle deadlocks, including final interpreter cleanup.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent("""
            import json, sys, threading, warnings
            import structguru as sg
            from structguru import _runtime
            entered, release = threading.Event(), threading.Event()
            def callback(line):
                entered.set()
                assert release.wait(3)
                sg.logger.info('nested')
            sg.configure(target='memory', callable_sinks=[callback])
            state = _runtime.current_runtime()
            sg.logger.info('accepted')
            assert entered.wait(2)
            original_stop = _runtime._callable_dispatcher.stop
            def stop(*, drain, stall_timeout=None):
                release.set()
                return original_stop(drain=drain, stall_timeout=stall_timeout)
            _runtime._callable_dispatcher.stop = stop
            with warnings.catch_warnings(record=True) as captured:
                getattr(_runtime, sys.argv[1])()
                assert _runtime.current_runtime() is None
                sg.logger.info('late')
                assert not captured, captured
            assert [json.loads(line)['message'] for line in state.writer.messages()] == [
                'accepted', 'nested'
            ]
            assert sg.writer_metrics() is None
            assert sg.lifecycle_metrics() == {'rejected': 0}
        """),
            operation,
        ],
        capture_output=True,
        text=True,
        timeout=6,
    )
    assert result.returncode == 0, result.stderr


def test_earlier_atexit_handler_sees_disabled_runtime() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent("""
            import atexit, os, warnings
            def late():
                import structguru as sg
                with warnings.catch_warnings(record=True) as captured:
                    sg.logger.info('late handler')
                    if sg.get_config() is not None or captured:
                        os._exit(42)
            atexit.register(late)
            import structguru as sg
            sg.configure(target='memory')
            sg.logger.info('accepted')
        """),
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("operation", ["shutdown", "configure"])
@pytest.mark.parametrize("format", ["json", "console"])
def test_retirement_wakes_full_queue_producer_and_counts_rejection(
    operation: str, format: str
) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent("""
            import sys, threading, warnings
            from dataclasses import replace
            import structguru as sg
            from structguru import _runtime
            sg.configure(target='memory', format=sys.argv[2], maxsize=1)
            state = _runtime.current_runtime()
            state.writer.close()
            writer = _runtime._RUST._NativeStringWriter(1, paused=True)
            _runtime._runtime = replace(state, writer=writer)
            sg.logger.info('accepted')
            assert writer.metrics()['depth'] == 1
            entered, done = threading.Event(), threading.Event()
            errors = []
            def produce():
                entered.set()
                try:
                    sg.logger.info('waiting')
                except BaseException as error:
                    errors.append(error)
                finally:
                    done.set()
            producer = threading.Thread(target=produce, daemon=True)
            with warnings.catch_warnings(record=True) as captured:
                producer.start()
                assert entered.wait(2)
                assert not done.wait(0.05), 'full paused queue did not block'
                if sys.argv[1] == 'configure':
                    sg.configure(target='memory')
                else:
                    sg.shutdown()
                producer.join(3)
                assert not producer.is_alive()
                assert not errors, errors
                assert not captured, captured
            assert writer.metrics()['written'] == 1
            assert writer.metrics()['dropped'] == 0
            assert sg.lifecycle_metrics() == {'rejected': 1}
            sg.configure(target='memory')
            sg.logger.info('recovered')
            sg.flush()
            assert sg.writer_metrics()['written'] == 1
            assert sg.lifecycle_metrics() == {'rejected': 1}
            sg.shutdown()
        """),
            operation,
            format,
        ],
        capture_output=True,
        text=True,
        timeout=7,
    )
    assert result.returncode == 0, result.stderr


def test_full_queue_is_not_reclassified_when_writer_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _runtime.configure(target="memory", overflow="drop", format="console")
    state = _runtime.current_runtime()
    assert state is not None and _runtime._RUST is not None
    state.writer.close()
    writer = _runtime._RUST._NativeStringWriter(1, paused=True)
    assert writer.enqueue_outcome("accepted", False) == "accepted"
    before = structguru.lifecycle_metrics()

    class ClosingWriter:
        def __getattr__(self, name: str):
            return getattr(writer, name)

        def log(self, *args):
            outcome = writer.log(*args)
            structguru.shutdown()
            return outcome

    monkeypatch.setattr(_runtime, "_runtime", replace(state, writer=ClosingWriter()))
    _runtime._reset_drop_count()
    with pytest.warns(UserWarning, match="queue full"):
        structguru.logger.info("overflow")
    assert writer.metrics()["dropped"] == 1
    assert writer.metrics()["written"] == 1
    assert structguru.lifecycle_metrics() == before


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_lifecycle_metrics_start_fresh_in_forked_child() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent("""
            import os, signal, warnings
            import structguru as sg
            from structguru import _runtime
            sg.configure(target='memory')
            _runtime.current_runtime().writer.close()
            with warnings.catch_warnings(record=True) as captured:
                sg.logger.info('closed writer')
                assert not captured
            assert sg.lifecycle_metrics() == {'rejected': 1}
            sg.configure(target='memory')
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', DeprecationWarning)
                pid = os.fork()
            if pid == 0:
                signal.alarm(3)
                if sg.lifecycle_metrics() != {'rejected': 0}:
                    os._exit(42)
                sg.logger.info('child')
                sg.flush()
                os._exit(0 if sg.writer_metrics()['written'] == 1 else 43)
            _, status = os.waitpid(pid, 0)
            assert os.waitstatus_to_exitcode(status) == 0, status
            assert sg.lifecycle_metrics() == {'rejected': 1}
            sg.shutdown()
        """),
        ],
        capture_output=True,
        text=True,
        timeout=6,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("operation", ["flush", "before_fork"])
def test_final_native_drain_includes_callback_logs(
    operation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, release = threading.Event(), threading.Event()
    order: list[str] = []

    def callback(line: str) -> None:
        entered.set()
        assert release.wait(3)
        structguru.logger.info("callback record")
        order.append("callback")

    _runtime.configure(target="memory", callable_sinks=[callback])
    state = _runtime.current_runtime()
    assert state is not None

    class WriterProbe:
        def __getattr__(self, name: str):
            return getattr(state.writer, name)

        def flush(self) -> None:
            order.append("native flush")
            state.writer.flush()

    monkeypatch.setattr(_runtime, "_runtime", replace(state, writer=WriterProbe()))
    structguru.logger.info("outer record")
    assert entered.wait(2)
    dispatcher = _runtime._callable_dispatcher
    original_flush = dispatcher.flush

    def release_and_drain(stall_timeout: float | None = None) -> int:
        release.set()
        return original_flush(stall_timeout)

    monkeypatch.setattr(dispatcher, "flush", release_and_drain)
    try:
        operation_fn = structguru.flush if operation == "flush" else _runtime._before_fork
        operation_fn()
        assert order == ["callback", "native flush"]
        assert any("callback record" in line for line in state.writer.messages())
    finally:
        release.set()
        _runtime.shutdown()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
@pytest.mark.parametrize("held", ["root", "ids", "logger", "bridge"])
def test_child_sink_operations_replace_inherited_locks(held: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent(f"""
            import io, os, signal, threading, warnings
            from structguru import configure, flush, shutdown, core
            from structguru.integrations import stdlib
            configure(target='null')
            log = core.Logger()
            token = log.add(io.StringIO())
            clone = log.bind(child=True)
            bridge = stdlib.install_stdlib_bridge()
            locks = {{'root': core._root_attach_lock, 'ids': core._id_counter_lock,
                     'logger': log._lock, 'bridge': stdlib._bridge_lock}}
            entered, release = threading.Event(), threading.Event()
            def hold():
                with locks[{held!r}]:
                    entered.set()
                    release.wait()
            holder = threading.Thread(target=hold)
            holder.start()
            assert entered.wait(2)
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', DeprecationWarning)
                pid = os.fork()
            if pid == 0:
                signal.signal(signal.SIGALRM, lambda *_: os._exit(42))
                signal.alarm(3)
                try:
                    assert clone._lock is log._lock
                    clone.remove(token)
                    stream = io.StringIO()
                    new_token = log.add(stream)
                    log.error('child record')
                    flush()
                    assert 'child record' in stream.getvalue()
                    clone.remove(new_token)
                    stdlib.uninstall_stdlib_bridge(bridge)
                    shutdown()
                except BaseException:
                    os._exit(1)
                os._exit(0)
            _, status = os.waitpid(pid, 0)
            release.set()
            holder.join(2)
            stdlib.uninstall_stdlib_bridge(bridge)
            log.remove(token)
            shutdown()
            assert os.waitstatus_to_exitcode(status) == 0, status
        """),
        ],
        capture_output=True,
        text=True,
        timeout=8,
    )
    assert result.returncode == 0, result.stderr


def test_close_drains_buffered_records() -> None:
    """flush() must drain queued records to the sink before they are read."""
    _runtime.configure(service="svc", target="memory")
    try:
        structguru.logger.info("last message")
        _runtime.flush_native()
        assert any("last message" in line for line in _runtime.drain_messages())
    finally:
        _runtime.shutdown()


def test_metrics_track_enqueue_and_write() -> None:
    _runtime.configure(service="svc", target="memory")
    try:
        for _ in range(5):
            structguru.logger.info("m")
        _runtime.flush_native()
        metrics = _runtime.writer_metrics()
        assert metrics is not None
        assert metrics["enqueued"] == 5
        assert metrics["written"] == 5
        assert metrics["dropped"] == 0
    finally:
        _runtime.shutdown()


def test_block_overflow_never_drops_under_backpressure() -> None:
    """A small bounded queue in block mode must apply backpressure, not drop."""
    _runtime.configure(service="svc", target="memory", maxsize=4, overflow="block")
    try:
        for _ in range(200):
            structguru.logger.info("m")
        _runtime.flush_native()
        metrics = _runtime.writer_metrics()
        assert metrics is not None
        assert metrics["enqueued"] == 200
        assert metrics["written"] == 200
        assert metrics["dropped"] == 0
    finally:
        _runtime.shutdown()


def test_drop_emits_rate_limited_warning() -> None:
    _runtime._reset_drop_count()
    with pytest.warns(UserWarning, match="dropped"):
        _runtime._note_drop()


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

    _runtime.configure(target="null")

    def emit() -> None:
        try:
            structguru.logger.info(SlowMessage())
        except BaseException as exc:  # capture the exact regression, including AssertionError
            errors.append(exc)

    producer = threading.Thread(target=emit)
    producer.start()
    assert formatting_started.wait(timeout=1)
    _runtime.shutdown()
    resume_formatting.set()
    producer.join(timeout=2)

    assert not producer.is_alive()
    assert errors == []


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires os.fork (POSIX)")
@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")
def test_native_writer_survives_fork() -> None:
    """After fork, the child respawns its writer and logs without deadlocking.

    The parent's background writer thread does not exist in the child; if the
    child tried to use or join it, this test would hang (caught by the select
    timeout). The registered ``after_in_child`` hook must swap in a fresh writer.

    CPython 3.12+ warns that forking a multi-threaded process may deadlock the
    child. That is exactly the prefork-server situation this test exercises, so
    ``os.fork()`` cannot be swapped for ``subprocess`` or a ``spawn`` context
    without losing the inherited writer thread; the warning is filtered instead.
    """
    _runtime.configure(service="svc", target="memory")
    try:
        structguru.logger.info("parent log")
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:  # child
            try:
                structguru.logger.info("child log")
                _runtime.flush_native()
                logged = any("child log" in line for line in _runtime.drain_messages())
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
        _runtime.shutdown()


def test_flush_returns_while_other_threads_keep_logging(tmp_path: Path) -> None:
    """flush() waits for records enqueued before it, not for a quiet moment.

    Waiting for an empty queue never completes while several threads keep
    logging to a sink slower than they are, which stalled the pre-fork drain
    for tens of seconds.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent("""
            import os, sys, threading, time
            import structguru as sg
            from structguru import _runtime

            _runtime.configure(
                service="svc",
                target="stdout",
                file_path=os.path.join(sys.argv[1], "app.log"),
                level="INFO",
                maxsize=64,
            )
            stop = threading.Event()

            def produce():
                while not stop.is_set():
                    sg.logger.info("busy record with a payload", a=1, b="two")

            threads = [threading.Thread(target=produce, daemon=True) for _ in range(4)]
            for thread in threads:
                thread.start()
            time.sleep(0.3)
            started = time.perf_counter()
            _runtime.flush()
            elapsed = time.perf_counter() - started
            stop.set()
            for thread in threads:
                thread.join(5)
            metrics = _runtime.writer_metrics()
            assert metrics["dropped"] == 0, metrics
            assert elapsed < 5, f"flush under load took {elapsed:.1f}s"
            _runtime.shutdown()
        """),
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_fork_is_bounded_while_other_threads_keep_logging(tmp_path: Path) -> None:
    """The pre-fork drain must not block a prefork server under logging load."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent("""
            import os, sys, threading, time
            import structguru as sg
            from structguru import _runtime

            _runtime.configure(
                service="svc",
                target="stdout",
                file_path=os.path.join(sys.argv[1], "app.log"),
                level="INFO",
                maxsize=64,
            )
            stop = threading.Event()

            def produce():
                while not stop.is_set():
                    sg.logger.info("busy record with a payload", a=1, b="two")

            threads = [threading.Thread(target=produce, daemon=True) for _ in range(4)]
            for thread in threads:
                thread.start()
            time.sleep(0.3)
            started = time.perf_counter()
            pid = os.fork()
            if pid == 0:
                try:
                    sg.logger.info("child")
                    _runtime.flush()
                    os._exit(0)
                except BaseException:
                    os._exit(7)
            elapsed = time.perf_counter() - started
            stop.set()
            for thread in threads:
                thread.join(5)
            _, status = os.waitpid(pid, 0)
            assert os.waitstatus_to_exitcode(status) == 0, status
            assert elapsed < 5, f"fork under load took {elapsed:.1f}s"
            _runtime.shutdown()
        """),
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr


def test_shutdown_abandons_a_callable_sink_that_never_returns() -> None:
    """A sink stuck forever must not hold shutdown (and interpreter exit) open."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent("""
            import threading, time, warnings
            import structguru as sg
            from structguru import _runtime

            _runtime._CALLABLE_STALL_TIMEOUT = 0.3
            never = threading.Event()
            entered = threading.Event()

            def hang(line):
                entered.set()
                never.wait()

            _runtime.configure(
                service="svc", target="memory", level="INFO", callable_sinks=[hang]
            )
            sg.logger.info("stuck")
            sg.logger.info("queued behind the stuck delivery")
            assert entered.wait(5)
            started = time.perf_counter()
            with warnings.catch_warnings(record=True) as captured:
                warnings.simplefilter("always")
                _runtime.shutdown()
            elapsed = time.perf_counter() - started
            assert elapsed < 5, f"shutdown blocked for {elapsed:.1f}s"
            assert any("abandoned" in str(w.message) for w in captured), captured
            assert _runtime.current_runtime() is None
            # Release the sink while the interpreter is alive: the abandoned
            # worker must unwind without delivering anything else. Waking a
            # Python thread during interpreter finalization aborts inside
            # CPython, which is why the sink is never left parked here.
            never.set()
            time.sleep(0.2)
            # Logging still works after the stalled generation is abandoned.
            _runtime.configure(service="svc", target="memory", level="INFO")
            sg.logger.info("after recovery")
            _runtime.flush()
            assert any(
                "after recovery" in line for line in _runtime.drain_messages()
            )
            _runtime.shutdown()
        """),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
