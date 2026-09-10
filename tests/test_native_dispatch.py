"""Concurrency regressions for callable-sink queue generations."""

from __future__ import annotations

import subprocess
import sys
import textwrap
import threading
import time

import pytest

from structguru._native_dispatch import CallableSinkDispatcher


@pytest.mark.parametrize("registration", ["configure", "add"])
def test_stdlib_backpressure_allows_callback_logging(registration: str) -> None:
    # Run in a subprocess so a lock inversion cannot hang pytest or its teardown.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent("""
                import json
                import logging
                import sys
                import threading
                import time
                import structguru as sg
                from structguru import _runtime
                from structguru.integrations.stdlib import install_stdlib_bridge

                entered = threading.Event()
                received = []
                errors = []

                def sink(line):
                    message = json.loads(line)['message']
                    received.append(message)
                    if message == 'first':
                        entered.set()
                        # The stdlib producer is inside Handler.handle(), and
                        # 'queued' occupies the only slot until this sink returns.
                        channel = _runtime._callable_dispatcher._channel
                        deadline = time.monotonic() + 3
                        while channel.blocked_producers == 0 and time.monotonic() < deadline:
                            time.sleep(0.005)
                        if channel.blocked_producers == 0:
                            errors.append('producer did not reach the full queue')
                            return
                        assert channel.qsize == 1
                        logging.getLogger('callback').warning('nested')

                sg.configure(target='memory', callable_queue_maxsize=1,
                             callable_sinks=[sink] if sys.argv[1] == 'configure' else [])
                token = sg.logger.add(sink) if sys.argv[1] == 'add' else None
                install_stdlib_bridge()
                sg.logger.info('first')
                assert entered.wait(3)
                sg.logger.info('queued')
                logging.getLogger('producer').warning('blocked')
                sg.flush()
                assert not errors, errors
                assert received == ['first', 'queued', 'blocked'], received
                records = [json.loads(line) for line in _runtime.drain_messages()]
                assert sorted(record['message'] for record in records) == [
                    'blocked', 'first', 'nested', 'queued'
                ], records
                assert next(r for r in records if r['message'] == 'nested')['logger'] == 'callback'
                if token is not None:
                    sg.logger.remove(token)
                sg.shutdown()
            """),
            registration,
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("error", ["SystemExit", "KeyboardInterrupt", "BaseException"])
def test_callback_base_exception_does_not_strand_dispatch(error: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent(f"""
            from structguru import configure, logger, flush, shutdown
            received = []
            def failing(line):
                raise {error}('callback failure')
            configure(target='null', callable_sinks=[failing, received.append],
                      callable_queue_maxsize=1)
            token = logger.add(received.append)
            logger.info('first')
            logger.info('second')
            flush()
            assert len(received) == 4, received
            logger.remove(token)
            shutdown()
        """),
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr


def _wait_for(predicate, timeout: float = 3.0) -> bool:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.005)
    return True


def test_close_drains_a_producer_reserved_before_retirement() -> None:
    """A producer acknowledged by the old generation cannot become orphaned."""
    dispatcher = CallableSinkDispatcher()
    entered, release = threading.Event(), threading.Event()
    received: list[str] = []

    def sink(line: str) -> None:
        if line == "first":
            entered.set()
            assert release.wait(3)
        received.append(line)

    dispatcher.configure([sink], maxsize=1)
    channel = dispatcher._channel
    dispatcher.enqueue("first", 20, overflow="block")
    assert entered.wait(1)
    dispatcher.enqueue("queued", 20, overflow="block")
    producer = threading.Thread(
        target=dispatcher.enqueue, args=("blocked", 20), kwargs={"overflow": "block"}, daemon=True
    )
    producer.start()
    assert _wait_for(lambda: channel.blocked_producers == 1), (
        "producer did not block on the full queue"
    )

    closer = threading.Thread(target=dispatcher.configure, args=([],), kwargs={"maxsize": 1})
    closer.start()
    assert channel.wait_retired(1)
    assert not closer.join(0.1) and closer.is_alive(), (
        "retirement must wait for the leased producer"
    )
    release.set()
    producer.join(2)
    closer.join(2)

    assert not producer.is_alive() and not closer.is_alive()
    assert received == ["first", "queued", "blocked"]
    assert channel.unfinished_tasks == 0
    assert not channel.is_alive()
    dispatcher.disable()


def test_channel_close_is_idempotent() -> None:
    dispatcher = CallableSinkDispatcher()
    dispatcher.configure([lambda line: None], maxsize=1)
    channel = dispatcher._channel
    channel.close(drain=True)
    channel.close(drain=True)
    assert not channel.is_alive()
    assert channel.unfinished_tasks == 0
    dispatcher.disable()


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
    assert old_channel.wait_retired(1)

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
                assert channel.wait_retired(3)
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


@pytest.mark.parametrize("capacity", [1, 8])
def test_callback_logging_bypasses_callable_delivery(capacity: int) -> None:
    # Bound both a worker self-deadlock (capacity=1) and a feedback loop when
    # there is space. Reentrant records must still reach the native writer.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent("""
            import json
            import sys
            import threading
            import structguru as sg
            from structguru import _runtime

            entered = threading.Event()
            proceed = threading.Event()
            delivered = []
            def sink(line):
                message = json.loads(line)['message']
                delivered.append(message)
                if message == 'trigger':
                    entered.set()
                    assert proceed.wait(3)
                    sg.logger.info('nested')
            sg.configure(target='memory', callable_sinks=[sink],
                         callable_queue_maxsize=int(sys.argv[1]))
            sg.logger.info('trigger')
            assert entered.wait(3)
            sg.logger.info('queued')
            proceed.set()
            sg.flush()
            messages = [json.loads(line)['message'] for line in _runtime.drain_messages()]
            assert messages == ['trigger', 'queued', 'nested'], messages
            assert delivered == ['trigger', 'queued'], delivered
            sg.shutdown()
        """),
            str(capacity),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_raw_handler_can_remove_itself_while_native_delivery_waits_for_its_lock() -> None:
    # A stdlib handler added with logger.add() runs emit() under its own lock on
    # the raw root-logger path. If it calls logger.remove() there while a native
    # record for the same handler is queued, the dispatch worker is blocked on
    # that lock; removal must not wait for the worker or both hang forever.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent("""
            import logging
            import threading
            import time
            import structguru as sg
            from structguru import _runtime

            entered = threading.Event()
            queued = threading.Event()

            class Handler(logging.Handler):
                def __init__(self):
                    super().__init__()
                    self.messages = []

                def emit(self, record):
                    self.messages.append(record.getMessage())
                    if record.getMessage() == 'raw':
                        entered.set()
                        assert queued.wait(3)
                        sg.logger.remove(token)

            handler = Handler()
            sg.configure(target='memory')
            token = sg.logger.add(handler)
            raw = threading.Thread(
                target=logging.getLogger('third_party').warning, args=('raw',), daemon=True
            )
            raw.start()
            assert entered.wait(3)
            sg.logger.info('native')
            deadline = time.monotonic() + 3
            while _runtime.writer_metrics()['callable_depth'] and time.monotonic() < deadline:
                time.sleep(0.01)
            queued.set()
            raw.join(3)
            assert not raw.is_alive(), 'remove() inside a raw handler waited for the worker'
            sg.flush()
            assert handler.messages[0] == 'raw', handler.messages
            sg.shutdown()
        """),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_remove_finalizer_runs_after_the_last_queued_delivery() -> None:
    dispatcher = CallableSinkDispatcher()
    received: list[str] = []
    closed: list[int] = []
    entered, proceed = threading.Event(), threading.Event()
    sink = dispatcher.add(received.append, 0, enabled=True)

    def trigger(line: str) -> None:
        if line == "trigger":
            entered.set()
            assert proceed.wait(3)
            dispatcher.remove(sink, finalizer=lambda: closed.append(len(received)))

    dispatcher.add(trigger, 0, enabled=True)
    dispatcher.enqueue("trigger", 20, overflow="block")
    assert entered.wait(3)
    dispatcher.enqueue("queued", 20, overflow="block")
    proceed.set()
    dispatcher.flush()
    assert received == ["trigger", "queued"]
    assert closed == [2], "finalizer ran before the queued delivery finished"
    dispatcher.disable()


def test_remove_finalizer_runs_at_once_when_nothing_is_queued() -> None:
    dispatcher = CallableSinkDispatcher()
    closed: list[str] = []
    sink = dispatcher.add(lambda line: None, 0, enabled=True)
    dispatcher.enqueue("delivered", 20, overflow="block")
    dispatcher.remove(sink, finalizer=lambda: closed.append("closed"))
    assert closed == ["closed"]
    dispatcher.disable()
