"""Concurrency regressions for callable-sink queue generations."""

from __future__ import annotations

import subprocess
import sys
import textwrap
import threading

import pytest

from structguru._native_dispatch import CallableSinkDispatcher, _DispatchChannel, _Record, _Sink


def test_close_drains_a_producer_reserved_before_retirement() -> None:
    """A producer acknowledged by the old generation cannot become orphaned."""
    received: list[str] = []
    channel = _DispatchChannel(maxsize=1)
    assert channel.reserve()

    closer = threading.Thread(target=channel.close, kwargs={"drain": True})
    closer.start()
    with channel._condition:
        assert channel._condition.wait_for(lambda: not channel._accepting, timeout=1)

    accepted = channel.put_reserved(
        _Record("accepted before close", (_Sink(1, received.append, 0),)),
        overflow="block",
    )
    closer.join(timeout=2)

    assert accepted
    assert not closer.is_alive()
    assert received == ["accepted before close"]
    assert channel.queue.unfinished_tasks == 0


def test_channel_close_is_idempotent() -> None:
    channel = _DispatchChannel(maxsize=1)
    channel.close(drain=True)
    channel.close(drain=True)
    assert not channel.thread.is_alive()
    assert channel.queue.unfinished_tasks == 0


def test_remove_waits_for_records_in_a_retired_generation() -> None:
    dispatcher = CallableSinkDispatcher()
    entered = threading.Event()
    release = threading.Event()
    removed = threading.Event()
    received: list[str] = []

    def sink(line: str) -> None:
        entered.set()
        if release.wait(3):
            received.append(line)

    token = dispatcher.add(sink, 0, enabled=True)
    dispatcher.enqueue("first", 20, overflow="block")
    assert entered.wait(1)
    dispatcher.enqueue("second", 20, overflow="block")
    old_channel = dispatcher._channel
    assert old_channel is not None
    reconfigure = threading.Thread(
        target=dispatcher.configure, args=([],), kwargs={"maxsize": 1}, daemon=True
    )
    reconfigure.start()
    with old_channel._condition:
        assert old_channel._condition.wait_for(lambda: not old_channel._accepting, timeout=1)

    def remove() -> None:
        dispatcher.remove(token)
        removed.set()

    remover = threading.Thread(target=remove, daemon=True)
    remover.start()
    try:
        assert not removed.wait(0.1), "remove returned while its retired sink was still running"
    finally:
        release.set()
        remover.join(3)
        reconfigure.join(3)
    assert not remover.is_alive()
    assert not reconfigure.is_alive()
    assert removed.is_set()
    assert received == ["first", "second"]
    dispatcher.disable()


@pytest.mark.parametrize("operation", ["flush", "disable"])
def test_external_drain_waits_after_callback_disables_dispatch(operation: str) -> None:
    dispatcher = CallableSinkDispatcher()
    disabled = threading.Event()
    release = threading.Event()
    drained = threading.Event()
    received: list[str] = []

    def sink(line: str) -> None:
        dispatcher.disable()
        disabled.set()
        if release.wait(3):
            received.append(line)

    dispatcher.configure([sink], maxsize=1)
    dispatcher.enqueue("accepted", 20, overflow="block")
    assert disabled.wait(1)

    def drain() -> None:
        getattr(dispatcher, operation)()
        drained.set()

    closer = threading.Thread(target=drain, daemon=True)
    closer.start()
    try:
        assert not drained.wait(0.1), "external drain missed the retired worker"
    finally:
        release.set()
        closer.join(3)
    assert not closer.is_alive()
    assert drained.is_set()
    assert received == ["accepted"]
    dispatcher.disable()


@pytest.mark.parametrize("operation", ["shutdown", "configure", "remove", "stop"])
def test_callback_shutdown_during_external_lifecycle_operation(operation: str) -> None:
    # A subprocess bounds a regression deadlock, including the atexit drain.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent("""
                import json
                import sys
                import threading
                import structguru
                from structguru import _runtime

                entered = threading.Event()
                release = threading.Event()
                received = []
                errors = []

                def sink(line):
                    message = json.loads(line)["message"]
                    received.append(message)
                    if message == "first":
                        entered.set()
                        assert release.wait(3)
                        structguru.shutdown()

                structguru.configure(target="null")
                token = structguru.logger.add(sink)
                structguru.logger.info("first")
                assert entered.wait(3)
                structguru.logger.info("second")
                channel = _runtime._callable_dispatcher._channel

                def transition():
                    try:
                        operation = sys.argv[1]
                        if operation == "shutdown":
                            structguru.shutdown()
                        elif operation == "configure":
                            structguru.configure(target="null")
                        elif operation == "remove":
                            structguru.logger.remove(token)
                        else:
                            _runtime._callable_dispatcher.stop(drain=True)
                    except BaseException as exc:
                        errors.append(exc)

                closer = threading.Thread(target=transition, daemon=True)
                closer.start()
                with channel._condition:
                    assert channel._condition.wait_for(
                        lambda: not channel._accepting, timeout=3
                    )
                release.set()
                closer.join(3)
                assert not closer.is_alive(), "lifecycle operation deadlocked"
                assert not errors, errors
                assert received == ["first", "second"], received
                structguru.logger.remove(token)
                structguru.shutdown()
            """),
            operation,
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
