"""Concurrency regressions for callable-sink queue generations."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import threading
import time
import warnings
from dataclasses import replace

import pytest

import structguru
from structguru import _runtime
from structguru._native_dispatch import CallableSinkDispatcher


@pytest.mark.parametrize("registration", ["configure", "add"])
@pytest.mark.parametrize("name", [None, "application"])
def test_callable_delivery_skips_speculative_plain_submission(
    registration: str, name: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    received: list[str] = []
    _runtime.configure(
        target="memory",
        callable_sinks=[received.append] if registration == "configure" else None,
    )
    log = structguru.Logger(name=name)
    token = log.add(received.append) if registration == "add" else None
    state = _runtime.current_runtime()
    assert state is not None

    def fail_plain(*_args: object, **_kwargs: object) -> None:
        pytest.fail("active callable sinks must go directly to tracked capture")

    monkeypatch.setattr(type(state.writer), "log_plain", fail_plain)
    try:
        log.info("callable record", status_code=200)
        _runtime.flush()
        [line] = _runtime.drain_messages()
        assert received == [line]
        assert json.loads(line)["status_code"] == 200
    finally:
        if token is not None:
            log.remove(token)


@pytest.mark.parametrize("transition", ["remove", "configure", "sync", "disable"])
@pytest.mark.parametrize("cached", [False, True])
def test_callback_destructor_can_reenter_dispatcher(transition: str, cached: bool) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent("""
                import sys
                from structguru._native_dispatch import CallableSinkDispatcher

                dispatcher = CallableSinkDispatcher()
                finalized = []
                class Sink:
                    def __call__(self, line):
                        pass
                    def __del__(self):
                        dispatcher.metrics()
                        dispatcher.add(lambda line: None, enabled=False)
                        finalized.append(True)

                transition, cached = sys.argv[1:]
                if transition == 'remove':
                    token = dispatcher.add(Sink(), enabled=cached == 'True')
                else:
                    dispatcher.configure([Sink()], maxsize=4)
                if cached == 'True':
                    dispatcher.enqueue('populate eligibility cache', 20, overflow='block')
                    dispatcher.flush(stall_timeout=1)

                if transition == 'remove':
                    dispatcher.remove(token, stall_timeout=1)
                elif transition == 'configure':
                    dispatcher.configure([], maxsize=4, stall_timeout=1)
                elif transition == 'sync':
                    dispatcher.sync([], maxsize=4)
                else:
                    dispatcher.disable(stall_timeout=1)
                assert finalized == [True], finalized
                dispatcher.disable(stall_timeout=1)
            """),
            transition,
            str(cached),
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr


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
                if sys.argv[1] != "remove":  # removing an id twice raises
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


def test_reconfiguration_delivers_from_one_thread_in_enqueue_order() -> None:
    """A replacement generation accepts at once but waits for its predecessor."""
    lock = threading.Lock()
    entered, release = threading.Event(), threading.Event()
    running = 0
    overlaps: list[str] = []
    delivered: list[str] = []

    def sink(line: str) -> None:
        nonlocal running
        message = json.loads(line)["message"]
        with lock:
            running += 1
            if running > 1:
                overlaps.append(message)
        if message == "r0":
            entered.set()
            release.wait(5)
        with lock:
            delivered.append(message)
            running -= 1

    _runtime.configure(target="null")
    token = structguru.logger.add(sink)
    dispatcher = _runtime._callable_dispatcher
    updater = threading.Thread(target=structguru.update, kwargs={"service": "next"}, daemon=True)
    try:
        structguru.logger.info("r0")
        assert entered.wait(3)
        for i in range(1, 4):
            structguru.logger.info(f"r{i}")
        old = dispatcher._channel
        updater.start()
        assert _wait_for(lambda: dispatcher._channel is not old), "update() kept the generation"
        for i in range(4, 7):
            structguru.logger.info(f"r{i}")
        # The new generation must not overtake the blocked backlog.
        assert not _wait_for(lambda: len(delivered) > 0, timeout=0.2), delivered
    finally:
        release.set()
        updater.join(5)
    assert not updater.is_alive()
    structguru.flush()
    assert delivered == [f"r{i}" for i in range(7)]
    assert overlaps == []
    structguru.logger.remove(token)


def test_add_neither_waits_for_the_backlog_nor_replaces_the_generation() -> None:
    entered, release = threading.Event(), threading.Event()
    received: list[str] = []
    added: list[int] = []

    def sink(line: str) -> None:
        message = json.loads(line)["message"]
        if message == "blocked":
            entered.set()
            release.wait(5)
        received.append(message)

    _runtime.configure(target="null", callable_sinks=[sink])
    adder = threading.Thread(
        target=lambda: added.append(structguru.logger.add(lambda line: None)), daemon=True
    )
    try:
        structguru.logger.info("blocked")
        assert entered.wait(3)
        structguru.logger.info("backlog")
        channel = _runtime._callable_dispatcher._channel
        adder.start()
        adder.join(2)
        assert not adder.is_alive(), "logger.add() waited for the callable backlog"
        assert _runtime._callable_dispatcher._channel is channel
        structguru.logger.info("after add")
    finally:
        release.set()
        adder.join(3)
    structguru.flush()
    assert received == ["blocked", "backlog", "after add"]
    structguru.logger.remove(added[0])


def test_slow_sink_that_keeps_progressing_is_not_abandoned() -> None:
    """Finished deliveries count as progress while a producer waits for a slot."""
    dispatcher = CallableSinkDispatcher()
    entered, release = threading.Event(), threading.Event()
    received: list[str] = []

    def slow(line: str) -> None:
        if line == "r0":
            entered.set()
            release.wait(5)
        else:
            time.sleep(0.005)
        received.append(line)

    dispatcher.configure([slow], maxsize=64)
    channel = dispatcher._channel
    producer = threading.Thread(
        target=dispatcher.enqueue, args=("r129", 20), kwargs={"overflow": "block"}, daemon=True
    )
    try:
        dispatcher.enqueue("r0", 20, overflow="block")
        assert entered.wait(3)
        for i in range(1, 65):
            dispatcher.enqueue(f"r{i}", 20, overflow="block")
        release.set()
        # The worker now holds r1..r64 as one batch (about 0.3 s of work) and
        # the queue refills, so the producer's slot frees only after the batch.
        for i in range(65, 129):
            dispatcher.enqueue(f"r{i}", 20, overflow="block")
        producer.start()
        assert _wait_for(lambda: channel.blocked_producers == 1)
        assert dispatcher.flush(stall_timeout=0.15) == 0
    finally:
        release.set()
        producer.join(5)
    assert received == [f"r{i}" for i in range(130)]
    dispatcher.disable()


def test_producer_released_by_abandonment_is_not_a_queue_full_drop() -> None:
    dispatcher = CallableSinkDispatcher()
    entered, release = threading.Event(), threading.Event()
    results: list[bool] = []

    def stuck(line: str) -> None:
        entered.set()
        release.wait(5)

    dispatcher.configure([stuck], maxsize=1)
    channel = dispatcher._channel
    producer = threading.Thread(
        target=lambda: results.append(dispatcher.enqueue("blocked", 20, overflow="block")),
        daemon=True,
    )
    try:
        dispatcher.enqueue("first", 20, overflow="block")
        assert entered.wait(3)
        dispatcher.enqueue("queued", 20, overflow="block")
        producer.start()
        assert _wait_for(lambda: channel.blocked_producers == 1)
        # In flight, queued, and the producer the abandonment releases.
        assert dispatcher.flush(stall_timeout=0.1) == 3
        producer.join(3)
        assert results == [False]
        assert dispatcher.metrics()["callable_dropped"] == 0
    finally:
        release.set()
        producer.join(3)
        dispatcher.disable()


@pytest.mark.parametrize("operation", ["configure", "update", "remove"])
def test_lifecycle_waits_for_a_stuck_sink_are_bounded(
    operation: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_runtime, "_CALLABLE_STALL_TIMEOUT", 0.2)
    entered, release = threading.Event(), threading.Event()
    received: list[str] = []
    captured: list[str] = []

    def stuck(line: str) -> None:
        entered.set()
        release.wait(10)
        received.append(json.loads(line)["message"])

    _runtime.configure(target="null")
    stuck_token = structguru.logger.add(stuck)
    other = structguru.logger.add(lambda line: None)

    def transition() -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            if operation == "configure":
                structguru.configure(target="null")
            elif operation == "update":
                structguru.update(service="next")
            else:
                structguru.logger.remove(other)
        captured.extend(str(warning.message) for warning in caught)

    closer = threading.Thread(target=transition, daemon=True)
    try:
        structguru.logger.info("stuck")
        assert entered.wait(3)
        structguru.logger.info("queued")
        closer.start()
        closer.join(3)
        assert not closer.is_alive(), f"{operation} waited without a stall limit"
    finally:
        release.set()
        closer.join(5)
    assert any("abandoned 2 callable sink delivery" in message for message in captured), captured
    # The abandoned worker finishes its stuck call but never delivers again.
    time.sleep(0.1)
    assert received == ["stuck"]
    structguru.logger.remove(stuck_token)
    if operation != "remove":  # removing an id twice raises
        structguru.logger.remove(other)


@pytest.mark.parametrize("operation", ["configure", "disable", "remove"])
def test_dispatcher_lifecycle_drains_report_abandoned_records(operation: str) -> None:
    dispatcher = CallableSinkDispatcher()
    entered, release = threading.Event(), threading.Event()

    def stuck(line: str) -> None:
        entered.set()
        release.wait(5)

    token = dispatcher.add(stuck, 0, enabled=True)
    dispatcher.add(lambda line: None, 0, enabled=True)
    try:
        dispatcher.enqueue("first", 20, overflow="block")
        assert entered.wait(3)
        dispatcher.enqueue("queued", 20, overflow="block")
        if operation == "configure":
            abandoned = dispatcher.configure([], maxsize=4, stall_timeout=0.1)
        elif operation == "disable":
            abandoned = dispatcher.disable(stall_timeout=0.1)
        else:
            removed, abandoned = dispatcher.remove(token, stall_timeout=0.1)
            assert removed
        assert abandoned == 2
    finally:
        release.set()
        dispatcher.disable()


@pytest.mark.parametrize("stall", [float("inf"), 1e300])
def test_stall_timeout_beyond_the_clock_range_means_no_limit(stall: float) -> None:
    dispatcher = CallableSinkDispatcher()
    received: list[str] = []
    dispatcher.configure([received.append], maxsize=4)
    dispatcher.enqueue("line", 20, overflow="block")
    assert dispatcher.flush(stall_timeout=stall) == 0
    assert received == ["line"]
    assert dispatcher.stop(drain=True, stall_timeout=stall) == 0
    dispatcher.disable()


def test_sink_error_diagnostic_omits_exception_detail() -> None:
    # A sink exception can embed credentials; diagnostics never include its text.
    # The worker emits the warning on its own thread. catch_warnings in the test
    # thread stays empty when warnings are context-aware (free-threaded builds,
    # and 3.14+ with -X context_aware_warnings). The subprocess stderr is the
    # same on every build.
    command = [sys.executable]
    if sys.version_info >= (3, 14):
        command.extend(["-X", "context_aware_warnings=1"])
    command.extend(
        [
            "-c",
            textwrap.dedent("""
                from structguru._native_dispatch import CallableSinkDispatcher

                secret = "x" * 80

                def failing(line):
                    raise ValueError(secret)

                dispatcher = CallableSinkDispatcher()
                dispatcher.configure([failing], maxsize=8)
                try:
                    dispatcher.enqueue("record", 20, overflow="drop")
                    assert dispatcher.flush() == 0
                finally:
                    dispatcher.disable()
            """),
        ]
    )
    result = subprocess.run(command, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stderr.count("callable sink raised") == 1, result.stderr
    assert "structguru callable sink raised 1 time(s), latest ValueError" in result.stderr
    assert "x" * 80 not in result.stderr


def test_abandoned_dispatch_stays_disabled_until_the_next_configure() -> None:
    dispatcher = CallableSinkDispatcher()
    entered, release = threading.Event(), threading.Event()
    received: list[str] = []

    def stuck(line: str) -> None:
        entered.set()
        release.wait(5)

    dispatcher.configure([stuck], maxsize=4)
    try:
        dispatcher.enqueue("stuck", 20, overflow="block")
        assert entered.wait(3)
        assert dispatcher.flush(stall_timeout=0.1) == 1
        assert dispatcher.idle()
        dispatcher.add(received.append, 0, enabled=True)
        assert dispatcher.idle(), "registering a sink restarted abandoned dispatch"
        dispatcher.sync([stuck], maxsize=4)
        assert dispatcher.idle(), "sync() restarted abandoned dispatch"
        dispatcher.enqueue("while disabled", 20, overflow="block")
    finally:
        release.set()
    dispatcher.configure([], maxsize=4)
    assert not dispatcher.idle()
    dispatcher.enqueue("after configure", 20, overflow="block")
    assert dispatcher.flush() == 0
    assert received == ["after configure"]
    dispatcher.disable()


@pytest.mark.parametrize("halt", ["sync", "disable", "stop"])
def test_sync_with_callbacks_resumes_dispatch_halted_by_a_lifecycle_call(halt: str) -> None:
    dispatcher = CallableSinkDispatcher()
    received: list[str] = []
    dispatcher.configure([received.append], maxsize=4)
    if halt == "sync":
        dispatcher.sync(None, maxsize=0)
    elif halt == "disable":
        dispatcher.disable()
    else:
        dispatcher.stop(drain=True)
    assert dispatcher.idle()
    # The stale halt lost a race with configure(): the enabled runtime's
    # callbacks must bring dispatch back.
    dispatcher.sync([received.append], maxsize=4)
    assert not dispatcher.idle()
    dispatcher.enqueue("resumed", 20, overflow="block")
    assert dispatcher.flush() == 0
    assert received == ["resumed"]
    dispatcher.disable()


def test_shutdown_losing_a_race_with_configure_keeps_callable_dispatch() -> None:
    received: list[str] = []
    _runtime.configure(target="null", callable_sinks=[received.append])
    dispatcher = _runtime._callable_dispatcher
    real_disable = dispatcher.disable
    entered, proceed = threading.Event(), threading.Event()

    def stale_disable(**kwargs: object) -> int:
        # shutdown() already chose to disable; configure() runs meanwhile.
        entered.set()
        assert proceed.wait(5)
        return int(real_disable(**kwargs))

    dispatcher.disable = stale_disable
    closer = threading.Thread(target=_runtime.shutdown)
    try:
        closer.start()
        assert entered.wait(5)
        _runtime.configure(target="null", callable_sinks=[received.append])
    finally:
        proceed.set()
        closer.join(5)
        del dispatcher.disable
    assert _runtime.current_runtime() is not None
    assert not dispatcher.idle()
    structguru.logger.info("after race")
    _runtime.flush()
    assert [json.loads(line)["message"] for line in received] == ["after race"]


def test_add_losing_a_race_with_the_first_configure_keeps_callable_dispatch() -> None:
    _runtime.shutdown()  # logging not configured yet
    dispatcher = _runtime._callable_dispatcher
    real_sync = dispatcher.sync
    entered, proceed = threading.Event(), threading.Event()

    def stale_sync(callbacks: object, **kwargs: object) -> None:
        if callbacks is None and not entered.is_set():
            # add() saw logging disabled; the first configure() runs meanwhile.
            entered.set()
            assert proceed.wait(5)
        real_sync(callbacks, **kwargs)

    added: list[str] = []
    configured: list[str] = []
    handler_ids: list[int] = []
    dispatcher.sync = stale_sync
    adder = threading.Thread(
        target=lambda: handler_ids.append(structguru.logger.add(added.append))
    )
    try:
        adder.start()
        assert entered.wait(5)
        _runtime.configure(target="null", callable_sinks=[configured.append])
    finally:
        proceed.set()
        adder.join(5)
        del dispatcher.sync
    try:
        assert not dispatcher.idle()
        structguru.logger.info("after race")
        _runtime.flush()
        assert len(configured) == 1
        assert len(added) == 1
    finally:
        for handler_id in handler_ids:
            structguru.logger.remove(handler_id)


def test_concurrent_flushes_report_an_abandoned_generation_once() -> None:
    dispatcher = CallableSinkDispatcher()
    entered, release = threading.Event(), threading.Event()

    def stuck(line: str) -> None:
        entered.set()
        release.wait(5)

    dispatcher.configure([stuck], maxsize=16)
    results: list[int] = []
    flushers = [
        threading.Thread(target=lambda: results.append(dispatcher.flush(stall_timeout=0.3)))
        for _ in range(2)
    ]
    try:
        for index in range(5):
            dispatcher.enqueue(f"r{index}", 20, overflow="drop")
        assert entered.wait(3)
        for flusher in flushers:
            flusher.start()
        for flusher in flushers:
            flusher.join(5)
        assert sorted(results) == [0, 5]
    finally:
        release.set()
        dispatcher.disable()


def test_flush_ignores_later_lease_settlement() -> None:
    dispatcher = CallableSinkDispatcher()
    dispatcher.configure([lambda line: None], maxsize=1)
    channel = dispatcher._channel
    first = channel.reserve_for_test()
    second = channel.reserve_for_test()
    returned = threading.Event()
    flusher = threading.Thread(target=lambda: (channel.flush(), returned.set()), daemon=True)
    flusher.start()
    try:
        channel.release_lease_for_test(second)
        assert not returned.wait(0.1), "a later lease satisfied an earlier flush"
    finally:
        channel.release_lease_for_test(first)
        flusher.join(3)
        dispatcher.disable()
    assert returned.is_set() and not flusher.is_alive()


def test_remove_contains_immediate_finalizer_errors() -> None:
    dispatcher = CallableSinkDispatcher()
    token = dispatcher.add(lambda line: None, enabled=True)

    def fail() -> None:
        raise RuntimeError("close failed")

    assert dispatcher.remove(token, finalizer=fail, stall_timeout=1) == (True, 0)
    dispatcher.disable()


@pytest.mark.parametrize("destination", ["stream", "callable", "sentry"])
def test_flush_covers_native_acceptance_before_secondary_submission(
    destination: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release, returned = threading.Event(), threading.Event(), threading.Event()
    received: list[str] = []

    class Stream:
        def write(self, line: str) -> None:
            received.append(line)

    def sentry(_logger: object, _method: str, event: dict) -> dict:
        received.append(event["event"])
        return event

    _runtime.configure(
        target="memory",
        stream_sink=Stream() if destination == "stream" else None,
        sentry_processor=sentry if destination == "sentry" else None,
    )
    token = structguru.logger.add(received.append) if destination == "callable" else None
    state = _runtime.current_runtime()
    assert state is not None

    class PausedWriter:
        def __getattr__(self, name: str):
            return getattr(state.writer, name)

        def log_capture(self, *args):
            result = state.writer.log_capture(*args)
            entered.set()
            assert release.wait(3)
            return result

    monkeypatch.setattr(_runtime, "_runtime", replace(state, writer=PausedWriter()))
    producer = threading.Thread(target=lambda: structguru.logger.info("accepted"), daemon=True)
    flusher = threading.Thread(target=lambda: (_runtime.flush(), returned.set()), daemon=True)
    producer.start()
    try:
        assert entered.wait(3)
        flusher.start()
        assert not returned.wait(0.1)
        assert received == []
    finally:
        release.set()
        producer.join(3)
        flusher.join(3)
        if token is not None:
            structguru.logger.remove(token)
        _runtime.shutdown()
    assert returned.is_set() and not producer.is_alive() and not flusher.is_alive()
    assert len(received) == 1


def test_flush_excludes_later_runtime_submissions(monkeypatch: pytest.MonkeyPatch) -> None:
    received: list[str] = []
    entered = {"first": threading.Event(), "second": threading.Event()}
    release = {"first": threading.Event(), "second": threading.Event()}
    returned = threading.Event()
    _runtime.configure(target="null")
    token = structguru.logger.add(received.append)
    state = _runtime.current_runtime()
    assert state is not None

    class PausedWriter:
        def __getattr__(self, name: str):
            return getattr(state.writer, name)

        def log_capture(self, *args):
            result = state.writer.log_capture(*args)
            message = args[3]
            entered[message].set()
            assert release[message].wait(3)
            return result

    monkeypatch.setattr(_runtime, "_runtime", replace(state, writer=PausedWriter()))
    first = threading.Thread(target=lambda: structguru.logger.info("first"), daemon=True)
    second = threading.Thread(target=lambda: structguru.logger.info("second"), daemon=True)
    flusher = threading.Thread(target=lambda: (_runtime.flush(), returned.set()), daemon=True)
    first.start()
    try:
        assert entered["first"].wait(3)
        flusher.start()
        second.start()
        assert entered["second"].wait(3)
        release["first"].set()
        assert returned.wait(3)
        assert len(received) == 1 and json.loads(received[0])["message"] == "first"
    finally:
        release["first"].set()
        release["second"].set()
        first.join(3)
        second.join(3)
        flusher.join(3)
        structguru.logger.remove(token)
        _runtime.shutdown()
    assert not second.is_alive()


@pytest.mark.parametrize("destination", ["stream", "sentry"])
def test_reentrant_flush_does_not_wait_for_own_submission(destination: str) -> None:
    class Stream:
        def write(self, line: str) -> None:
            _runtime.flush()

    def sentry(_logger: object, _method: str, event: dict) -> dict:
        _runtime.flush()
        return event

    _runtime.configure(
        target="memory",
        stream_sink=Stream() if destination == "stream" else None,
        sentry_processor=sentry if destination == "sentry" else None,
    )
    producer = threading.Thread(target=lambda: structguru.logger.info("reentrant"), daemon=True)
    producer.start()
    producer.join(3)
    _runtime.shutdown()
    assert not producer.is_alive()


def test_flush_bounds_a_stalled_runtime_submission(monkeypatch: pytest.MonkeyPatch) -> None:
    entered, release = threading.Event(), threading.Event()
    returned = threading.Event()
    _runtime.configure(target="null")
    token = structguru.logger.add(lambda line: None)
    state = _runtime.current_runtime()
    assert state is not None

    class PausedWriter:
        def __getattr__(self, name: str):
            return getattr(state.writer, name)

        def log_capture(self, *args):
            result = state.writer.log_capture(*args)
            entered.set()
            assert release.wait(3)
            return result

    monkeypatch.setattr(_runtime, "_runtime", replace(state, writer=PausedWriter()))
    monkeypatch.setattr(_runtime, "_CALLABLE_STALL_TIMEOUT", 0.05)
    producer = threading.Thread(target=lambda: structguru.logger.info("stalled"), daemon=True)
    flusher = threading.Thread(target=lambda: (_runtime.flush(), returned.set()), daemon=True)
    producer.start()
    try:
        assert entered.wait(3)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            flusher.start()
            assert returned.wait(3)
        assert any("stopped waiting for 1 log submission" in str(item.message) for item in caught)
    finally:
        release.set()
        producer.join(3)
        flusher.join(3)
        structguru.logger.remove(token)
        _runtime.shutdown()
    assert not producer.is_alive()


def test_remove_contains_deferred_finalizer_errors() -> None:
    dispatcher = CallableSinkDispatcher()
    token = 0

    def fail() -> None:
        raise RuntimeError("close failed")

    def sink(line: str) -> None:
        assert dispatcher.remove(token, finalizer=fail, stall_timeout=1) == (True, 0)

    token = dispatcher.add(sink, enabled=True)
    dispatcher.enqueue("record", 20, overflow="block")
    assert dispatcher.flush(stall_timeout=1) == 0
    dispatcher.disable()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_fork_resets_inherited_runtime_submission_leases() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent("""
                import os
                import signal
                import threading
                import structguru as sg
                from structguru import _runtime

                sg.configure(target='memory')
                _runtime._CALLABLE_STALL_TIMEOUT = 0.1
                entered, release = threading.Event(), threading.Event()
                def submit():
                    lease = _runtime._begin_submission()
                    entered.set()
                    release.wait()
                    _runtime._end_submission(lease)
                producer = threading.Thread(target=submit, daemon=True)
                producer.start()
                assert entered.wait(1)
                signal.alarm(3)
                pid = os.fork()
                if pid == 0:
                    assert not _runtime._submission_active
                    sg.logger.info('child')
                    sg.flush()
                    lines = _runtime.drain_messages()
                    os._exit(0 if len(lines) == 1 and 'child' in lines[0] else 4)
                release.set()
                producer.join(1)
                _, status = os.waitpid(pid, 0)
                raise SystemExit(os.waitstatus_to_exitcode(status))
            """),
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr


def test_ending_submission_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    lease = _runtime._begin_submission()
    _runtime._end_submission(lease)
    _runtime._end_submission(lease)
    monkeypatch.setattr(_runtime, "_submission_local", threading.local())
    _runtime._end_submission(lease)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_fork_inside_stream_preserves_surviving_submission_leases() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent("""
                import os
                import signal
                import threading
                import structguru as sg
                from structguru import _runtime

                pid = None
                depth = 0
                class Stream:
                    def write(self, line):
                        global pid, depth
                        depth += 1
                        if depth == 1:
                            sg.logger.info('nested')
                        elif depth == 2:
                            pid = os.fork()
                            if pid == 0:
                                own = set(_runtime._submission_local.active)
                                assert len(own) == 2
                                assert _runtime._submission_active == own
                                lease = _runtime._begin_submission()
                                assert lease not in own
                                _runtime._end_submission(lease)
                                pending = []
                                waiter = threading.Thread(
                                    target=lambda: pending.append(
                                        _runtime._wait_for_submissions(0.01)
                                    )
                                )
                                waiter.start()
                                waiter.join(1)
                                assert pending == [2], pending
                                sg.flush()  # Does not wait on this thread's leases.

                signal.alarm(4)
                sg.configure(target='memory', stream_sink=Stream())
                sg.logger.info('outer')
                assert not _runtime._submission_active
                if pid == 0:
                    sg.logger.info('child after fork')
                    sg.flush()
                    assert len(_runtime.drain_messages()) == 1
                    os._exit(0)
                _, status = os.waitpid(pid, 0)
                raise SystemExit(os.waitstatus_to_exitcode(status))
            """),
        ],
        capture_output=True,
        text=True,
        timeout=6,
    )
    assert result.returncode == 0, result.stderr
    assert "stream sink write failed" not in result.stderr
