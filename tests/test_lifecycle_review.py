"""Bounded regression probes for producer, flush, and generation boundaries."""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import textwrap
import threading
import time
from dataclasses import replace
from typing import Any

import pytest

import structguru
from structguru import _runtime
from structguru._native_dispatch import CallableSinkDispatcher


@pytest.mark.parametrize("plain", [True, False])
def test_flush_covers_fused_producers_before_native_enqueue(plain: bool) -> None:
    entered, release, returned = threading.Event(), threading.Event(), threading.Event()

    class WaitingDate(dt.date):
        def isoformat(self) -> str:
            entered.set()
            assert release.wait(3)
            return super().isoformat()

    _runtime.configure(target="memory")
    logger = structguru.logger if plain else structguru.logger.opt(stack_info=True)
    producer = threading.Thread(
        target=lambda: logger.info("pending", value=WaitingDate(2026, 1, 1)), daemon=True
    )
    flusher = threading.Thread(target=lambda: (_runtime.flush(), returned.set()), daemon=True)
    producer.start()
    try:
        assert entered.wait(3)
        flusher.start()
        assert not returned.wait(0.1), "flush missed an admitted fused producer"
    finally:
        release.set()
        producer.join(3)
        flusher.join(3)
    assert not producer.is_alive() and not flusher.is_alive() and returned.is_set()
    [line] = _runtime.drain_messages()
    assert json.loads(line)["value"] == "2026-01-01"


@pytest.mark.parametrize("during_callbacks", [False, True])
def test_flush_keeps_retiring_writer_reachable(
    during_callbacks: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    closing, flushing, release = threading.Event(), threading.Event(), threading.Event()
    callbacks, release_callbacks, returned = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    _runtime.configure(target="memory")
    state = _runtime.current_runtime()
    assert state is not None

    class RetiringWriter:
        def __getattr__(self, name: str) -> Any:
            return getattr(state.writer, name)

        def close(self, **kwargs: Any) -> int:
            closing.set()
            assert release.wait(3)
            return state.writer.close(**kwargs)

        def flush(self, **kwargs: Any) -> int:
            flushing.set()
            assert release.wait(3)
            return state.writer.flush(**kwargs)

    dispatcher = _runtime._callable_dispatcher

    class Dispatcher:
        def __getattr__(self, name: str) -> Any:
            return getattr(dispatcher, name)

        def flush(self, **kwargs: Any) -> int:
            if during_callbacks:
                callbacks.set()
                assert release_callbacks.wait(3)
            return dispatcher.flush(**kwargs)

    monkeypatch.setattr(_runtime, "_runtime", replace(state, writer=RetiringWriter()))
    monkeypatch.setattr(_runtime, "_callable_dispatcher", Dispatcher())
    configure = threading.Thread(target=lambda: _runtime.configure(target="memory"), daemon=True)
    flusher = threading.Thread(target=lambda: (_runtime.flush(), returned.set()), daemon=True)
    try:
        if during_callbacks:
            flusher.start()
            assert callbacks.wait(3)
        configure.start()
        assert closing.wait(3)
        if not during_callbacks:
            flusher.start()
        release_callbacks.set()
        assert flushing.wait(1), "flush ignored the previous writer while close was still draining"
        assert not returned.wait(0.05)
    finally:
        release_callbacks.set()
        release.set()
        configure.join(3)
        flusher.join(3)
    assert returned.is_set() and not configure.is_alive() and not flusher.is_alive()


@pytest.mark.parametrize("transition", ["configure", "sync"])
def test_replacing_callable_generation_never_advertises_idle(transition: str) -> None:
    dispatcher = CallableSinkDispatcher()
    received: list[str] = []
    dispatcher.configure([received.append], maxsize=2)
    dispatcher._pause_next_replacement_for_test()

    def replace_generation() -> None:
        getattr(dispatcher, transition)([received.append], maxsize=3)

    replacement = threading.Thread(target=replace_generation, daemon=True)
    replacement.start()
    try:
        deadline = time.monotonic() + 3
        while not dispatcher._replacement_waiting_for_test() and time.monotonic() < deadline:
            time.sleep(0.001)
        assert dispatcher._replacement_waiting_for_test()
        assert not dispatcher.idle(), "existing callable sinks disappeared during replacement"
    finally:
        dispatcher._release_replacement_for_test()
        replacement.join(3)
        dispatcher.stop(drain=True)
    assert not replacement.is_alive()


@pytest.mark.parametrize("transition", ["configure", "sync"])
@pytest.mark.skipif(
    getattr(sys, "_is_gil_enabled", lambda: True)(),
    reason="requires free-threaded producer contention",
)
def test_logging_during_callable_replacement_reaches_the_new_generation(transition: str) -> None:
    received: list[str] = []
    callback = received.append
    _runtime.configure(target="memory", callable_sinks=[callback])
    state = _runtime.current_runtime()
    assert state is not None
    dispatcher = _runtime._callable_dispatcher
    dispatcher._pause_next_replacement_for_test()
    replaced = threading.Thread(
        target=lambda: getattr(dispatcher, transition)([callback], maxsize=7), daemon=True
    )
    finished = threading.Event()
    producer = threading.Thread(
        target=lambda: (structguru.logger.info("during replacement"), finished.set()), daemon=True
    )
    replaced.start()
    try:
        deadline = time.monotonic() + 3
        while not dispatcher._replacement_waiting_for_test() and time.monotonic() < deadline:
            time.sleep(0.001)
        assert dispatcher._replacement_waiting_for_test()
        producer.start()
        deadline = time.monotonic() + 3
        while state.writer.metrics()["enqueued"] != 1 and time.monotonic() < deadline:
            time.sleep(0.001)
        assert not finished.wait(0.05), "record incorrectly bypassed the registered callable sinks"
    finally:
        dispatcher._release_replacement_for_test()
        replaced.join(3)
        if producer.ident is not None:
            producer.join(3)
    _runtime.flush()
    assert finished.is_set() and len(received) == 1
    assert json.loads(received[0])["message"] == "during replacement"


def test_callable_spawn_failure_preserves_current_channel_and_recovers() -> None:
    dispatcher = CallableSinkDispatcher()
    old_lines: list[str] = []
    new_lines: list[str] = []
    dispatcher.configure([old_lines.append], maxsize=2)
    old_channel = dispatcher._channel
    dispatcher._fail_next_spawn_for_test()
    try:
        with pytest.raises(RuntimeError, match="spawn callable"):
            dispatcher.configure([new_lines.append], maxsize=3)
        assert dispatcher._channel is old_channel and not dispatcher.idle()
        dispatcher.enqueue("old", 20, overflow="block")
        dispatcher.flush()
        assert old_lines == ["old"] and new_lines == []
        dispatcher.configure([new_lines.append], maxsize=3)
        dispatcher.enqueue("new", 20, overflow="block")
        dispatcher.flush()
        assert new_lines == ["new"]
    finally:
        dispatcher.stop(drain=True)


def test_runtime_rolls_back_after_callable_spawn_failure() -> None:
    received: list[str] = []
    _runtime.configure(target="memory", service="original", callable_sinks=[received.append])
    state = _runtime.current_runtime()
    _runtime._callable_dispatcher._fail_next_spawn_for_test()
    with pytest.raises(RuntimeError, match="spawn callable"):
        _runtime.configure(
            target="memory", service="replacement", callable_sinks=[lambda line: None]
        )
    assert _runtime.current_runtime() is state
    structguru.logger.info("still live")
    _runtime.flush()
    assert len(received) == 1 and json.loads(received[0])["service"] == "original"


def test_native_admission_lease_excludes_reentrant_own_work(
    monkeypatch: pytest.MonkeyPatch, recwarn: pytest.WarningsRecorder
) -> None:
    class FlushingDate(dt.date):
        def isoformat(self) -> str:
            _runtime.flush()
            return super().isoformat()

    monkeypatch.setattr(_runtime, "_CALLABLE_STALL_TIMEOUT", 0.05)
    _runtime.configure(target="memory")
    structguru.logger.info("own lease", value=FlushingDate(2026, 1, 1))
    _runtime.flush()
    assert not any("stopped waiting" in str(w.message) for w in recwarn)


def test_failed_configure_preserves_a_concurrent_level_only_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: list[str] = []
    _runtime.configure(target="memory", service="original", callable_sinks=[received.append])
    original = _runtime.current_runtime()
    assert original is not None
    dispatcher = _runtime._callable_dispatcher

    class Dispatcher:
        def __getattr__(self, name: str) -> Any:
            return getattr(dispatcher, name)

        def configure(self, *args: Any, **kwargs: Any) -> int:
            _runtime.set_level("WARNING")
            return dispatcher.configure(*args, **kwargs)

    monkeypatch.setattr(_runtime, "_callable_dispatcher", Dispatcher())
    dispatcher._fail_next_spawn_for_test()
    with pytest.raises(RuntimeError, match="spawn callable"):
        _runtime.configure(
            target="memory", service="replacement", callable_sinks=[received.append]
        )
    state = _runtime.current_runtime()
    assert state is not None and state.writer is original.writer
    assert state.settings.level == "WARNING" and state.service == "original"
    structguru.logger.info("filtered")
    structguru.logger.warning("still live")
    _runtime.flush()
    assert len(received) == 1 and json.loads(received[0])["message"] == "still live"


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires fork")
def test_early_child_hook_does_not_touch_parent_writer_locks() -> None:
    script = textwrap.dedent("""
        import os
        saved = []
        def early():
            saved.append(writer.enqueue_outcome("early", True))
            writer.flush()
            writer.close()
            structguru.flush()
            structguru.logger.info("before runtime child hook")
        os.register_at_fork(after_in_child=early)
        import structguru
        from structguru import _runtime
        structguru.configure(target="memory")
        writer = _runtime.current_runtime().writer
        pid = os.fork()
        if pid == 0:
            try:
                assert saved == ["closed"], saved
                structguru.logger.info("after child initialization")
                structguru.flush()
                assert len(_runtime.drain_messages()) == 1
                structguru.shutdown()
            except BaseException:
                os._exit(1)
            os._exit(0)
        _, status = os.waitpid(pid, 0)
        structguru.shutdown()
        assert os.waitstatus_to_exitcode(status) == 0
    """)
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires fork")
def test_child_publishes_runtime_before_running_deferred_sink_finalizers() -> None:
    script = textwrap.dedent("""
        import json
        import os
        import threading
        import structguru
        from structguru import _runtime

        entered, release = threading.Event(), threading.Event()
        def blocked(line):
            entered.set()
            assert release.wait(5)
        def finalize():
            structguru.configure(target="memory", service="finalized")
            structguru.logger.info("from child finalizer")

        structguru.configure(target="memory")
        dispatcher = _runtime._callable_dispatcher
        token = dispatcher.add(blocked, enabled=True)
        structguru.logger.info("parent")
        assert entered.wait(3)
        removed, abandoned = dispatcher.remove(token, finalizer=finalize, stall_timeout=0.01)
        assert removed and abandoned == 1
        pid = os.fork()
        if pid == 0:
            try:
                structguru.logger.info("after finalizer")
                structguru.flush()
                records = [json.loads(line) for line in _runtime.drain_messages()]
                assert [r["message"] for r in records] == [
                    "from child finalizer", "after finalizer"
                ], records
                assert all(r["service"] == "finalized" for r in records)
                structguru.shutdown()
            except BaseException:
                import traceback
                traceback.print_exc()
                os._exit(1)
            os._exit(0)
        release.set()
        _, status = os.waitpid(pid, 0)
        structguru.shutdown()
        assert os.waitstatus_to_exitcode(status) == 0
    """)
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, result.stderr
