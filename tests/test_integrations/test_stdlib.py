"""Tests for the stdlib -> structguru logging bridge."""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from structguru import _runtime, core
from structguru.core import Logger
from structguru.integrations.stdlib import (
    StructguruHandler,
    install_stdlib_bridge,
    install_stdlib_bridge_from_env,
    suppress_loggers,
    uninstall_stdlib_bridge,
)

pytestmark = pytest.mark.skipif(
    not _runtime.is_available(),
    reason="native extension not built",
)


@pytest.fixture
def native_memory() -> Iterator[None]:
    """Native mode writing to the in-memory sink, restored afterwards."""
    _runtime.configure(service="app", target="memory", level="DEBUG")
    try:
        yield
    finally:
        _runtime.shutdown()


@pytest.fixture
def clean_root() -> Iterator[None]:
    """Snapshot and restore the root logger's handlers and level."""
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    try:
        yield
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def _records() -> list[dict]:
    _runtime.flush_native()
    return [json.loads(line) for line in _runtime.drain_messages()]


@pytest.mark.parametrize("extra", [False, True])
@pytest.mark.parametrize("exception", [False, True])
@pytest.mark.parametrize("stack", [False, True])
def test_bridge_forwards_without_logger_clones(
    native_memory: None,
    monkeypatch: pytest.MonkeyPatch,
    extra: bool,
    exception: bool,
    stack: bool,
) -> None:
    try:
        raise ValueError("synthetic failure")
    except ValueError:
        exc_info = sys.exc_info()
    record = logging.LogRecord(
        "clone.free",
        25,
        "",
        1,
        "literal {value}: %s",
        ("Ada",),
        exc_info if exception else None,
        sinfo="Stack snapshot" if stack else None,
    )
    if extra:
        record.value = "metadata"
        record._bound_fields = "field"
    before = record.__dict__.copy()

    def fail_clone(*_args: object, **_kwargs: object) -> None:
        pytest.fail("forwarding a record must not clone a Logger")

    monkeypatch.setattr(Logger, "_clone", fail_clone)
    StructguruHandler().handle(record)
    [rendered] = _records()
    assert rendered["message"] == "literal {value}: Ada"
    assert ("exception" in rendered) == exception
    assert ("stack" in rendered) == stack
    if extra:
        assert rendered["value"] == "metadata"
        assert rendered["_bound_fields"] == "field"
    assert record.__dict__ == before


def test_plain_bridge_record_bypasses_logger_facade(
    native_memory: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_log(*_args: object, **_kwargs: object) -> None:
        pytest.fail("a plain stdlib record must submit directly through the native pipeline")

    monkeypatch.setattr(Logger, "_log", fail_log)
    record = logging.LogRecord("native.direct", 25, "", 1, "literal {value}", (), None)
    record.value = "metadata"
    StructguruHandler().handle(record)
    [rendered] = _records()
    assert rendered["logger"] == "native.direct"
    assert rendered["level"] == "INFO"
    assert rendered["message"] == "literal {value}"
    assert rendered["value"] == "metadata"


def test_bridge_retains_record_read_order_around_get_message(native_memory: None) -> None:
    _runtime.configure(target="memory", level=30)

    class ChangingRecord(logging.LogRecord):
        def getMessage(self) -> str:
            self.name = "changed"
            self.levelno = 35
            self.created = 1e9
            self.request_id = "after"
            self.stack_info = "after stack"
            self.exc_info = (ValueError, ValueError("after exception"), None)
            return "literal {request_id}"

    record = ChangingRecord("before", 25, "", 1, "ignored", (), None, sinfo="before stack")
    record.request_id = "before"
    StructguruHandler().handle(record)
    [rendered] = _records()
    assert rendered["logger"] == "before"
    assert rendered["level"] == "INFO"
    assert rendered["timestamp"] == "2001-09-09T01:46:40.000000Z"
    assert rendered["request_id"] == "before"
    assert rendered["message"] == "literal {request_id}"
    assert rendered["stack"] == "after stack"
    assert "exception" not in rendered


def test_bridge_rejects_invalid_extra_names_before_get_message(
    native_memory: None, capfd: pytest.CaptureFixture[str]
) -> None:
    class UnreadableRecord(logging.LogRecord):
        def getMessage(self) -> str:
            pytest.fail("invalid extra names must fail before message conversion")

    record = UnreadableRecord("invalid", logging.INFO, "", 1, "ignored", (), None)
    record.__dict__[1] = "invalid name"
    handler = StructguruHandler()
    handler.handle(record)
    assert "stdlib emit failed" in capfd.readouterr().err
    assert _records() == []


def test_bridge_record_fields_override_context_without_leaking_to_cached_logger(
    native_memory: None,
) -> None:
    first = logging.makeLogRecord(
        {
            "name": "shared",
            "levelno": logging.INFO,
            "msg": "first {request_id}",
            "request_id": "record",
            "self": "field",
        }
    )
    second = logging.makeLogRecord({"name": "shared", "levelno": logging.INFO, "msg": "second"})
    handler = StructguruHandler()
    with Logger().contextualize(request_id="context", context_only=True):
        handler.handle(first)
        handler.handle(second)
    one, two = _records()
    assert one["message"] == "first {request_id}"
    assert one["request_id"] == "record"
    assert one["self"] == "field"
    assert two["request_id"] == "context"
    assert "self" not in two
    assert one["context_only"] is two["context_only"] is True


def test_bridge_handle_respects_filters(native_memory: None) -> None:
    handler = StructguruHandler()

    def filter_record(record: logging.LogRecord) -> bool:
        if record.msg == "drop":
            return False
        record.msg = "filtered"
        return True

    handler.addFilter(filter_record)
    assert not handler.handle(logging.LogRecord("foreign", logging.INFO, "", 0, "drop", (), None))
    assert handler.handle(logging.LogRecord("foreign", logging.INFO, "", 0, "keep", (), None))
    assert [record["message"] for record in _records()] == ["filtered"]


@pytest.mark.skipif(sys.version_info < (3, 12), reason="replacement filters require Python 3.12")
def test_bridge_handle_respects_replacement_record_filter(native_memory: None) -> None:
    handler = StructguruHandler()
    original = logging.LogRecord("foreign", logging.INFO, "", 0, "original", (), None)
    replacement = logging.LogRecord("filtered", logging.ERROR, "", 0, "replacement", (), None)
    handler.addFilter(lambda record: replacement)
    assert handler.handle(original)
    [record] = _records()
    assert record["message"] == "replacement"
    assert record["logger"] == "filtered"
    assert record["level"] == "ERROR"
    assert original.msg == "original"


@pytest.mark.parametrize("message", ["literal {stack_info}", "literal {missing}", "{{json}}"])
def test_bridge_preserves_braces_with_stack_info(native_memory: None, message: str) -> None:
    record = logging.LogRecord(
        "foreign", logging.ERROR, "", 1, message, (), None, sinfo="STACK CONTENT"
    )
    StructguruHandler().handle(record)
    rendered = _records()[0]
    assert rendered["message"] == message
    assert rendered["stack"] == "STACK CONTENT"


def test_bridge_escapes_console_stack_info() -> None:
    _runtime.configure(target="memory", format="console", colors=False)
    record = logging.LogRecord(
        "foreign",
        logging.ERROR,
        "",
        1,
        "failed",
        (),
        None,
        sinfo="Stack:\n\x1b[2J2030-01-01 [INFO] forged\r\t\x00",
    )
    StructguruHandler().handle(record)
    _runtime.flush_native()
    [line] = _runtime.drain_messages()
    assert line.split("\n")[1:] == [
        "  Stack:",
        r"  \x1b[2J2030-01-01 [INFO] forged\r\t\x00",
        "",
    ]
    assert all(ord(ch) >= 32 or ch == "\n" for ch in line)


def test_bridge_uninstall_cannot_reattach_removed_sink(
    native_memory: None,
    clean_root: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: list[str] = []
    log = Logger()
    token = log.add(received.append)
    handler = log._handlers[token]
    relay = core._root_relays[handler]
    bridge = install_stdlib_bridge()
    root = logging.getLogger()
    original_add = root.addHandler
    entered, release, removing = threading.Event(), threading.Event(), threading.Event()

    def delayed_add(candidate: logging.Handler) -> None:
        if candidate is relay:
            entered.set()
            assert release.wait(3)
        original_add(candidate)

    monkeypatch.setattr(root, "addHandler", delayed_add)
    uninstall = threading.Thread(target=uninstall_stdlib_bridge, args=(bridge,))

    def remove() -> None:
        removing.set()
        log.remove(token)

    remover = threading.Thread(target=remove)
    uninstall.start()
    try:
        assert entered.wait(2)
        remover.start()
        assert removing.wait(2)
        # On the old implementation removal completes during the paused attach.
        remover.join(0.1)
    finally:
        release.set()
        uninstall.join(3)
        if remover.ident is not None:
            remover.join(3)
    assert not uninstall.is_alive()
    assert not remover.is_alive()
    assert relay not in root.handlers
    logging.getLogger("foreign").warning("after removal")
    assert received == []


def test_bridge_routes_stdlib_record(native_memory: None, clean_root: None) -> None:
    # Use a synthetic logger name, not a real library's (e.g. "sqlalchemy.engine"):
    # a library imported elsewhere in the session may set propagate=False on its
    # own logger, which would keep the record from ever reaching the root bridge.
    install_stdlib_bridge(level="DEBUG")
    logging.getLogger("thirdparty.db.engine").info("SELECT 1")
    rec = _records()[-1]
    assert rec["logger"] == "thirdparty.db.engine"
    assert rec["level"] == "INFO"
    assert rec["message"] == "SELECT 1"


def test_bridge_forwards_extra_fields(native_memory: None, clean_root: None) -> None:
    install_stdlib_bridge(level="DEBUG")
    logging.getLogger("svc").warning("slow query", extra={"duration_ms": 42})
    rec = _records()[-1]
    assert rec["duration_ms"] == 42
    assert rec["level"] == "WARN"


@pytest.mark.parametrize("filter_kind", ["none", "sampling", "rate_limit"])
def test_bridge_keeps_records_whose_message_carries_surrogates(
    native_memory: None, clean_root: None, filter_kind: str
) -> None:
    # A filename from os.fsdecode() is not valid UTF-8; the record must still
    # ship (with U+FFFD) rather than fail inside the handler and be dropped.
    filename = os.fsdecode(b"/tmp/\xff")
    _runtime.update(
        sample_rate=0.0 if filter_kind == "sampling" else 1.0,
        sample_max_level="INFO",
        rate_limit_max=1 if filter_kind == "rate_limit" else None,
    )
    bridge = install_stdlib_bridge(level="INFO")
    raise_exceptions = logging.raiseExceptions
    logging.raiseExceptions = False
    try:
        logging.getLogger("third_party").warning(
            "cannot open %s", filename, extra={filename: "retained"}
        )
        _runtime.flush()
        lines = [line for line in _runtime.drain_messages() if "cannot open" in line]
    finally:
        logging.raiseExceptions = raise_exceptions
        uninstall_stdlib_bridge(bridge)
    [line] = lines
    assert "\udcff" not in line
    record = json.loads(line)
    message = record["message"]
    prefix, _, rest = message.partition("cannot open /tmp/")
    assert not prefix and rest and set(rest) == {"\ufffd"}, message
    assert record[f"/tmp/{rest}"] == "retained"


def test_bridge_forwards_exc_info(native_memory: None, clean_root: None) -> None:
    install_stdlib_bridge(level="DEBUG")
    try:
        raise ValueError("boom")
    except ValueError:
        logging.getLogger("svc").error("failed", exc_info=True)
    rec = _records()[-1]
    assert "exception" in rec
    assert "ValueError" in rec["exception"]


def test_bridge_forwards_stack_info(native_memory: None, clean_root: None) -> None:
    install_stdlib_bridge(level="DEBUG")
    logging.getLogger("svc").warning("stack requested", stack_info=True)
    rec = _records()[-1]
    assert "Stack (most recent call last):" in rec["stack"]
    assert "test_bridge_forwards_stack_info" in rec["stack"]


def test_bridge_preserves_literal_braces(native_memory: None, clean_root: None) -> None:
    # An already-formatted message with literal braces must pass through
    # verbatim, never re-run through structguru's brace formatting.
    install_stdlib_bridge(level="DEBUG")
    logging.getLogger("svc").info("progress {done}/{total} {")
    rec = _records()[-1]
    assert rec["message"] == "progress {done}/{total} {"


@pytest.mark.parametrize(
    "levelno,expected",
    [
        (logging.DEBUG, "DEBUG"),
        (logging.INFO, "INFO"),
        (25, "INFO"),  # between INFO and WARNING -> info
        (logging.WARNING, "WARN"),
        (logging.ERROR, "ERROR"),
        (45, "ERROR"),  # between ERROR and CRITICAL -> error
        (logging.CRITICAL, "CRITICAL"),
    ],
)
def test_level_normalization(
    native_memory: None, clean_root: None, levelno: int, expected: str
) -> None:
    install_stdlib_bridge(level="DEBUG")
    logging.getLogger("svc").log(levelno, "msg")
    assert _records()[-1]["level"] == expected


def test_bridge_thresholds_custom_levels_by_their_number(clean_root: None) -> None:
    # A record between two named levels is judged by its own number, not by
    # the named level below it; only the rendered level name is banded.
    _runtime.configure(service="app", target="memory", level=25)
    install_stdlib_bridge(level="DEBUG")
    log = logging.getLogger("svc.custom")
    for levelno in (20, 24, 25, 26, 35):
        log.log(levelno, f"at {levelno}")
    assert [(record["message"], record["level"]) for record in _records()] == [
        ("at 25", "INFO"),
        ("at 26", "INFO"),
        ("at 35", "WARN"),
    ]


def test_bridge_thresholds_trace_band_records_by_their_number() -> None:
    # Numbers 5-9 render as DEBUG, but stay below a DEBUG threshold.
    handler = StructguruHandler()
    record = logging.makeLogRecord({"name": "svc", "msg": "fine", "levelno": 7})
    _runtime.configure(service="app", target="memory", level="DEBUG")
    handler.handle(record)
    assert _records() == []
    _runtime.configure(service="app", target="memory", level="TRACE")
    handler.handle(record)
    assert [(rec["message"], rec["level"]) for rec in _records()] == [("fine", "DEBUG")]


def test_bridge_samples_custom_levels_by_their_number(clean_root: None) -> None:
    # Records more severe than sample_max_level always pass, including a
    # custom level just above it.
    _runtime.configure(
        service="app", target="memory", level="DEBUG", sample_rate=0.0, sample_max_level="INFO"
    )
    install_stdlib_bridge(level="DEBUG")
    log = logging.getLogger("svc.sampled")
    log.log(logging.INFO, "info")
    log.log(25, "notice")
    log.log(logging.WARNING, "warning")
    assert [record["message"] for record in _records()] == ["notice", "warning"]
    metrics = _runtime.writer_metrics()
    assert metrics is not None
    assert metrics["sampled"] == 1


def test_bridge_routes_custom_levels_to_sinks_by_their_number(clean_root: None) -> None:
    # A sink added at a custom level receives a bridged record at that level,
    # and a handler sink sees the record's own number.
    _runtime.configure(service="app", target="memory", level="DEBUG")
    lines: list[str] = []
    seen: list[int] = []

    class _Levels(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            seen.append(record.levelno)

    handler = _Levels()
    tokens = [Logger().add(lines.append, level=25), Logger().add(handler, level=25)]
    try:
        install_stdlib_bridge(level="DEBUG")
        log = logging.getLogger("svc.routed")
        for levelno in (24, 25, 30):
            log.log(levelno, f"at {levelno}")
        _runtime.flush()
        assert [json.loads(line)["message"] for line in lines] == ["at 25", "at 30"]
        assert seen == [25, 30]
    finally:
        for token in tokens:
            Logger().remove(token)


@pytest.mark.parametrize(
    "created", ["1700000000", 10**400, None, object()], ids=["str", "huge", "none", "object"]
)
def test_bridge_falls_back_to_the_current_time_for_unusable_creation_times(
    native_memory: None, created: object
) -> None:
    # A filter or record factory may set any object; the record must survive.
    import datetime

    record = logging.makeLogRecord(
        {"name": "odd", "msg": "odd created", "levelno": logging.INFO, "created": created}
    )
    before = datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=1)
    StructguruHandler().handle(record)
    [rendered] = _records()
    assert rendered["message"] == "odd created"
    assert rendered["timestamp"] >= before.strftime("%Y-%m-%dT%H:%M:%S")


@pytest.mark.parametrize(
    ("created", "expected"),
    [
        (1_000_000_000.0, "2001-09-09T01:46:40.000000Z"),
        (1_000_000_000.123456, "2001-09-09T01:46:40.123456Z"),
        (1_700_000_000.9999996, "2023-11-14T22:13:21.000000Z"),
        (-1.5, "1969-12-31T23:59:58.500000Z"),
    ],
)
def test_bridge_renders_record_creation_time(
    native_memory: None, created: float, expected: str
) -> None:
    # A replayed or queued record keeps the time it was created, not the time
    # the bridge rendered it.
    record = logging.makeLogRecord(
        {"name": "replayed", "msg": "old event", "levelno": logging.INFO, "created": created}
    )
    StructguruHandler().handle(record)
    assert _records()[-1]["timestamp"] == expected


@pytest.mark.parametrize("format", ["json", "console"])
def test_bridge_creation_time_reaches_stream_sinks(format: str) -> None:
    import io

    stream = io.StringIO()
    _runtime.configure(target="memory", format=format, colors=False, stream_sink=stream)
    record = logging.makeLogRecord(
        {"name": "replayed", "msg": "old event", "levelno": logging.INFO, "created": 1e9}
    )
    StructguruHandler().handle(record)
    _runtime.flush()
    [line] = _runtime.drain_messages()
    assert line == stream.getvalue()
    assert "2001-09-09T01:46:40.000000Z" in line


def test_bridge_creation_time_matches_datetime_rounding(native_memory: None) -> None:
    import datetime as dt
    import random

    rng = random.Random(1234)
    created = [rng.uniform(-2e9, 4e9) for _ in range(300)]
    created += [rng.randrange(0, 2**31) + rng.randrange(0, 10**6) / 1e6 for _ in range(300)]
    handler = StructguruHandler()
    for value in created:
        handler.handle(
            logging.makeLogRecord(
                {"name": "svc", "msg": "m", "levelno": logging.INFO, "created": value}
            )
        )
    rendered = [record["timestamp"] for record in _records()]
    assert rendered == [
        dt.datetime.fromtimestamp(value, dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        for value in created
    ]


def test_bridge_keeps_creation_time_through_queue_listener(
    native_memory: None, clean_root: None
) -> None:
    import queue
    from logging.handlers import QueueHandler, QueueListener

    records: queue.SimpleQueue[logging.LogRecord] = queue.SimpleQueue()
    listener = QueueListener(records, StructguruHandler())
    listener.start()
    try:
        QueueHandler(records).handle(
            logging.makeLogRecord(
                {"name": "queued", "msg": "from 2001", "levelno": logging.INFO, "created": 1e9}
            )
        )
    finally:
        listener.stop()
    [record] = _records()
    assert record["message"] == "from 2001"
    assert record["timestamp"] == "2001-09-09T01:46:40.000000Z"


@pytest.mark.parametrize("created", [float("nan"), float("inf"), 1e20])
def test_bridge_unrepresentable_creation_time_uses_current_time(
    native_memory: None, created: float
) -> None:
    import datetime as dt

    record = logging.makeLogRecord(
        {"name": "svc", "msg": "m", "levelno": logging.INFO, "created": created}
    )
    StructguruHandler().handle(record)
    timestamp = _records()[-1]["timestamp"]
    assert timestamp.startswith(f"{dt.datetime.now(dt.UTC).year}-")


def test_bridge_level_filters_child_with_explicit_lower_level(
    native_memory: None, clean_root: None
) -> None:
    child = logging.getLogger("explicit_debug_child")
    saved_level = child.level
    saved_propagate = child.propagate
    try:
        install_stdlib_bridge(level="INFO")
        child.setLevel(logging.DEBUG)
        child.propagate = True
        child.debug("below bridge threshold")
        child.info("at bridge threshold")
        assert [record["message"] for record in _records()] == ["at bridge threshold"]
    finally:
        child.setLevel(saved_level)
        child.propagate = saved_propagate


def test_bridge_ignores_structguru_records(native_memory: None, clean_root: None) -> None:
    # Records from structguru's own loggers are skipped to avoid double-wrapping
    # and any interception loop.
    install_stdlib_bridge(level="DEBUG")
    logging.getLogger("structguru.internal").info("should not route")
    assert _records() == []


def test_install_clears_existing_handlers(clean_root: None) -> None:
    root = logging.getLogger()
    sentinel = logging.NullHandler()
    root.addHandler(sentinel)
    install_stdlib_bridge()
    assert sentinel not in root.handlers
    assert any(isinstance(h, StructguruHandler) for h in root.handlers)


def test_install_keeps_existing_handlers_when_requested(clean_root: None) -> None:
    root = logging.getLogger()
    sentinel = logging.NullHandler()
    root.addHandler(sentinel)
    install_stdlib_bridge(clear_handlers=False)
    assert sentinel in root.handlers


def test_install_returns_removable_handler(clean_root: None) -> None:
    handler = install_stdlib_bridge()
    logging.getLogger().removeHandler(handler)
    assert handler not in logging.getLogger().handlers
    uninstall_stdlib_bridge(handler)


def test_suppress_loggers_sets_level() -> None:
    suppress_loggers("noisy_a", "noisy_b", level="ERROR")
    assert logging.getLogger("noisy_a").level == logging.ERROR
    assert logging.getLogger("noisy_b").level == logging.ERROR


def test_install_suppresses_named_loggers(clean_root: None) -> None:
    install_stdlib_bridge(suppress_loggers=("chatty",), suppress_level="WARNING")
    assert logging.getLogger("chatty").level == logging.WARNING


def test_existing_logger_policy_none_preserves_states(clean_root: None) -> None:
    enabled = logging.getLogger("structguru_test_policy_none_enabled")
    disabled = logging.getLogger("structguru_test_policy_none_disabled")
    enabled.disabled = False
    disabled.disabled = True

    bridge = install_stdlib_bridge(disable_existing_loggers=None)
    try:
        assert not enabled.disabled
        assert disabled.disabled
    finally:
        uninstall_stdlib_bridge(bridge)


def test_install_reads_existing_logger_policy_from_environment(
    clean_root: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    existing = logging.getLogger("structguru_test_policy_default_env")
    existing.disabled = False
    monkeypatch.setenv("STRUCTGURU_STDLIB_DISABLE_EXISTING_LOGGERS", "true")

    bridge = install_stdlib_bridge()
    try:
        assert existing.disabled
    finally:
        uninstall_stdlib_bridge(bridge)


def test_explicit_existing_logger_policy_overrides_environment(
    clean_root: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    existing = logging.getLogger("structguru_test_policy_explicit_override")
    existing.disabled = False
    monkeypatch.setenv("STRUCTGURU_STDLIB_DISABLE_EXISTING_LOGGERS", "true")

    bridge = install_stdlib_bridge(disable_existing_loggers=False)
    try:
        assert not existing.disabled
    finally:
        uninstall_stdlib_bridge(bridge)


def test_regular_install_rejects_invalid_existing_logger_environment(
    clean_root: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = logging.getLogger()
    sentinel = logging.NullHandler()
    root.addHandler(sentinel)
    monkeypatch.setenv("STRUCTGURU_STDLIB_DISABLE_EXISTING_LOGGERS", "maybe")

    with pytest.raises(ValueError, match="STRUCTGURU_STDLIB_DISABLE_EXISTING_LOGGERS"):
        install_stdlib_bridge()

    assert root.handlers[-1] is sentinel


def test_existing_logger_policy_true_disables_and_restores(clean_root: None) -> None:
    existing = logging.getLogger("structguru_test_policy_disable")
    existing.disabled = False

    bridge = install_stdlib_bridge(disable_existing_loggers=True)
    assert existing.disabled

    uninstall_stdlib_bridge(bridge)
    assert not existing.disabled


def test_existing_logger_policy_false_enables_and_restores(clean_root: None) -> None:
    existing = logging.getLogger("structguru_test_policy_enable")
    existing.disabled = True

    bridge = install_stdlib_bridge(disable_existing_loggers=False)
    assert not existing.disabled

    uninstall_stdlib_bridge(bridge)
    assert existing.disabled


def test_existing_logger_policy_does_not_disable_root_or_later_logger(
    clean_root: None,
) -> None:
    root = logging.getLogger()
    root.disabled = False
    existing = logging.getLogger("structguru_test_policy_before")
    existing.disabled = False

    bridge = install_stdlib_bridge(disable_existing_loggers=True)
    try:
        later = logging.getLogger("structguru_test_policy_after")
        assert existing.disabled
        assert not root.disabled
        assert not later.disabled
    finally:
        uninstall_stdlib_bridge(bridge)


def test_existing_logger_policy_ignores_placeholders(clean_root: None) -> None:
    logging.getLogger("structguru_test_placeholder.child")
    placeholder = logging.root.manager.loggerDict["structguru_test_placeholder"]
    assert isinstance(placeholder, logging.PlaceHolder)

    bridge = install_stdlib_bridge(disable_existing_loggers=True)
    try:
        assert logging.root.manager.loggerDict["structguru_test_placeholder"] is placeholder
    finally:
        uninstall_stdlib_bridge(bridge)


def test_uninstall_preserves_change_when_bridge_did_not_change_logger(clean_root: None) -> None:
    existing = logging.getLogger("structguru_test_policy_later_change")
    existing.disabled = True
    bridge = install_stdlib_bridge(disable_existing_loggers=True)
    assert existing.disabled

    existing.disabled = False
    uninstall_stdlib_bridge(bridge)
    assert not existing.disabled


def test_install_rejects_duplicate_active_bridge(clean_root: None) -> None:
    bridge = install_stdlib_bridge()
    try:
        with pytest.raises(RuntimeError) as excinfo:
            install_stdlib_bridge(replace=False)
        assert str(excinfo.value) == "the structguru stdlib bridge is already installed"
    finally:
        uninstall_stdlib_bridge(bridge)


def test_replace_swaps_active_bridge(native_memory: None, clean_root: None) -> None:
    root = logging.getLogger()
    old = install_stdlib_bridge(level="DEBUG")

    new = install_stdlib_bridge(level="DEBUG", replace=True)
    try:
        assert new is not old
        assert old not in root.handlers
        assert root.handlers.count(new) == 1
        logging.getLogger("thirdparty.svc").info("after swap")
        assert [rec["message"] for rec in _records()] == ["after swap"]
    finally:
        uninstall_stdlib_bridge(new)


def test_replace_without_active_bridge_is_plain_install(clean_root: None) -> None:
    bridge = install_stdlib_bridge(replace=True)
    try:
        assert bridge in logging.getLogger().handlers
    finally:
        uninstall_stdlib_bridge(bridge)


def test_replace_with_detached_bridge_releases_stale_state(clean_root: None) -> None:
    existing = logging.getLogger("structguru_test_replace_detached")
    existing.disabled = False
    stale = install_stdlib_bridge(disable_existing_loggers=True)
    logging.getLogger().removeHandler(stale)

    bridge = install_stdlib_bridge(replace=True)
    try:
        assert not existing.disabled
        assert stale not in logging.getLogger().handlers
    finally:
        uninstall_stdlib_bridge(bridge)


def test_replace_restores_old_policy_before_applying_new(clean_root: None) -> None:
    existing = logging.getLogger("structguru_test_replace_policy_true_false")
    existing.disabled = False
    install_stdlib_bridge(disable_existing_loggers=True)

    bridge = install_stdlib_bridge(disable_existing_loggers=False, replace=True)
    assert not existing.disabled

    # Restore-then-apply: the replacing bridge saw the logger already enabled
    # (the old snapshot restored it), so uninstalling must not flip it back.
    uninstall_stdlib_bridge(bridge)
    assert not existing.disabled


def test_replace_policy_false_to_true(clean_root: None) -> None:
    existing = logging.getLogger("structguru_test_replace_policy_false_true")
    existing.disabled = True
    install_stdlib_bridge(disable_existing_loggers=False)
    assert not existing.disabled

    bridge = install_stdlib_bridge(disable_existing_loggers=True, replace=True)
    assert existing.disabled

    uninstall_stdlib_bridge(bridge)
    assert existing.disabled


def test_replace_rereads_policy_environment(
    clean_root: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    existing = logging.getLogger("structguru_test_replace_policy_env")
    existing.disabled = False
    install_stdlib_bridge()

    monkeypatch.setenv("STRUCTGURU_STDLIB_DISABLE_EXISTING_LOGGERS", "true")
    bridge = install_stdlib_bridge(replace=True)
    try:
        assert existing.disabled
    finally:
        uninstall_stdlib_bridge(bridge)


def test_replace_keeps_previous_suppression_levels(clean_root: None) -> None:
    install_stdlib_bridge(suppress_loggers=("noisy_replaced",), suppress_level="ERROR")

    bridge = install_stdlib_bridge(replace=True)
    try:
        assert logging.getLogger("noisy_replaced").level == logging.ERROR
    finally:
        uninstall_stdlib_bridge(bridge)


def test_uninstall_of_replaced_bridge_is_a_noop(native_memory: None, clean_root: None) -> None:
    seen: list[str] = []
    log = Logger()
    log.add(seen.append)
    old = install_stdlib_bridge(level="DEBUG")
    new = install_stdlib_bridge(level="DEBUG", replace=True)

    # Releasing the stale handler must not detach the new bridge or restore
    # raw root delivery for `add()` sinks.
    uninstall_stdlib_bridge(old)
    try:
        assert new in logging.getLogger().handlers
        logging.getLogger("thirdparty.svc").info("still bridged once")
        _runtime.flush()
    finally:
        log.remove()
        uninstall_stdlib_bridge(new)

    assert len(seen) == 1
    assert json.loads(seen[0])["message"] == "still bridged once"


def test_replace_under_concurrent_stdlib_logging(native_memory: None, clean_root: None) -> None:
    # A record logged by another thread during a swap may arrive rendered, raw,
    # or not at all — but never twice, and never by raising into the caller.
    seen: list[str] = []
    log = Logger()
    log.add(seen.append)
    bridge = install_stdlib_bridge(level="DEBUG")
    stop = threading.Event()
    failures: list[BaseException] = []

    def emit() -> None:
        source = logging.getLogger("thirdparty.concurrent")
        seq = 0
        try:
            while not stop.is_set() and seq < 20000:
                source.info("seq=%d", seq)
                seq += 1
        except BaseException as exc:  # noqa: BLE001 - asserted empty below
            failures.append(exc)

    worker = threading.Thread(target=emit)
    worker.start()
    try:
        for _ in range(25):
            bridge = install_stdlib_bridge(level="DEBUG", replace=True)
    finally:
        stop.set()
        worker.join(timeout=10)
        _runtime.flush()
        log.remove()
        uninstall_stdlib_bridge(bridge)

    assert failures == []
    delivered = [json.loads(line)["message"] if line.startswith("{") else line for line in seen]
    assert len(delivered) == len(set(delivered))


def test_install_from_env_replace_swaps_active_bridge(clean_root: None) -> None:
    old = install_stdlib_bridge()

    bridge = install_stdlib_bridge_from_env({"STRUCTGURU_STDLIB_REPLACE": "true"})
    try:
        assert old not in logging.getLogger().handlers
        assert bridge in logging.getLogger().handlers
    finally:
        uninstall_stdlib_bridge(bridge)


def test_install_from_env_without_replace_rejects_duplicate(clean_root: None) -> None:
    bridge = install_stdlib_bridge()
    try:
        with pytest.raises(RuntimeError, match="already installed"):
            install_stdlib_bridge_from_env({})
    finally:
        uninstall_stdlib_bridge(bridge)


def test_invalid_replace_env_does_not_change_logging_state(clean_root: None) -> None:
    root = logging.getLogger()
    sentinel = logging.NullHandler()
    root.addHandler(sentinel)

    with pytest.raises(ValueError, match="STRUCTGURU_STDLIB_REPLACE"):
        install_stdlib_bridge_from_env({"STRUCTGURU_STDLIB_REPLACE": "maybe"})

    assert root.handlers[-1] is sentinel


def test_install_from_env_applies_all_options(clean_root: None) -> None:
    root = logging.getLogger()
    sentinel = logging.NullHandler()
    root.addHandler(sentinel)
    existing = logging.getLogger("structguru_test_env_existing")
    existing.disabled = False

    bridge = install_stdlib_bridge_from_env(
        {
            "LOG_LEVEL": "ERROR",
            "STRUCTGURU_STDLIB_LEVEL": "DEBUG",
            "STRUCTGURU_STDLIB_CLEAR_HANDLERS": "false",
            "STRUCTGURU_STDLIB_DISABLE_EXISTING_LOGGERS": "true",
            "STRUCTGURU_STDLIB_SUPPRESS_LOGGERS": " noisy_a, noisy_b ",
            "STRUCTGURU_STDLIB_SUPPRESS_LEVEL": "ERROR",
        }
    )
    try:
        assert sentinel in root.handlers
        assert bridge.level == logging.DEBUG
        assert root.level == logging.DEBUG
        assert existing.disabled
        assert logging.getLogger("noisy_a").level == logging.ERROR
        assert logging.getLogger("noisy_b").level == logging.ERROR
    finally:
        uninstall_stdlib_bridge(bridge)


def test_install_from_injected_env_does_not_fall_back_to_process_env(
    clean_root: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    existing = logging.getLogger("structguru_test_injected_env_isolation")
    existing.disabled = False
    monkeypatch.setenv("STRUCTGURU_STDLIB_DISABLE_EXISTING_LOGGERS", "true")

    bridge = install_stdlib_bridge_from_env({})
    try:
        assert not existing.disabled
    finally:
        uninstall_stdlib_bridge(bridge)


def test_invalid_env_does_not_change_logging_state(clean_root: None) -> None:
    root = logging.getLogger()
    sentinel = logging.NullHandler()
    root.addHandler(sentinel)
    existing = logging.getLogger("structguru_test_env_invalid")
    existing.disabled = False

    with pytest.raises(ValueError, match="STRUCTGURU_STDLIB_CLEAR_HANDLERS"):
        install_stdlib_bridge_from_env({"STRUCTGURU_STDLIB_CLEAR_HANDLERS": "maybe"})

    assert root.handlers[-1] is sentinel
    assert not existing.disabled


def test_bridge_delivers_third_party_records_to_add_sinks_exactly_once(
    native_memory: None, clean_root: None
) -> None:
    # `logger.add()` attaches its sink to the root logger so it also receives
    # third-party records raw. The bridge renders those same records through the
    # native path into the same sink, so both paths together would deliver each
    # record twice — once raw, once rendered.
    seen: list[str] = []
    log = Logger()
    install_stdlib_bridge(level="DEBUG")
    log.add(seen.append)
    try:
        logging.getLogger("thirdparty.svc").info("only once")
        _runtime.flush()
    finally:
        log.remove()

    assert len(seen) == 1
    assert json.loads(seen[0])["message"] == "only once"


def test_bridge_installed_after_add_also_delivers_exactly_once(
    native_memory: None, clean_root: None
) -> None:
    # Same invariant, opposite ordering: installing the bridge must suspend the
    # root attachment of a sink that was registered before it.
    seen: list[str] = []
    log = Logger()
    log.add(seen.append)
    install_stdlib_bridge(level="DEBUG")
    try:
        logging.getLogger("thirdparty.svc").info("still once")
        _runtime.flush()
    finally:
        log.remove()

    assert len(seen) == 1
    assert json.loads(seen[0])["message"] == "still once"


def test_uninstall_restores_raw_root_delivery(native_memory: None, clean_root: None) -> None:
    seen: list[str] = []
    log = Logger()
    log.add(seen.append)
    bridge = install_stdlib_bridge(level="DEBUG")
    uninstall_stdlib_bridge(bridge)
    try:
        logging.getLogger("thirdparty.svc").info("raw again")
        _runtime.flush()
    finally:
        log.remove()

    # Back to the pre-bridge contract: the sink sees the unrendered message.
    assert seen == ["raw again"]


@pytest.mark.parametrize("raise_exceptions", [True, False])
def test_bridge_keeps_record_with_unsupported_extra(
    native_memory: None,
    clean_root: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    raise_exceptions: bool,
) -> None:
    # Django's log_response() attaches the raw request as extra["request"].
    # The event must ship with a marker in its place, not fall back to a
    # "--- Logging error ---" diagnostic — regardless of logging.raiseExceptions.
    monkeypatch.setattr(logging, "raiseExceptions", raise_exceptions)
    install_stdlib_bridge(level="DEBUG")
    try:
        raise RuntimeError("synthetic application failure")
    except RuntimeError:
        logging.getLogger("web.request").error(
            "%s: %s",
            "Internal Server Error",
            "/repro",
            exc_info=True,
            extra={"status_code": 500, "request": object()},
        )

    records = _records()
    assert len(records) == 1
    rec = records[0]
    assert rec["logger"] == "web.request"
    assert rec["level"] == "ERROR"
    assert rec["message"] == "Internal Server Error: /repro"
    assert rec["status_code"] == 500
    assert rec["request"] == "<unsupported: object>"
    assert "RuntimeError: synthetic application failure" in rec["exception"]
    assert "Logging error" not in capsys.readouterr().err


def test_bridge_keeps_record_with_nested_and_cyclic_extras(
    native_memory: None, clean_root: None, capsys: pytest.CaptureFixture[str]
) -> None:
    install_stdlib_bridge(level="DEBUG")
    loop: dict[str, object] = {"n": 1}
    loop["self"] = loop
    logging.getLogger("svc").warning(
        "nested",
        extra={
            "ctx": {"obj": object(), "items": [1, (object(),)]},
            "loop": loop,
            "counts": {200: 3},
        },
    )

    rec = _records()[-1]
    assert rec["ctx"] == {"obj": "<unsupported: object>", "items": [1, ["<unsupported: object>"]]}
    assert rec["loop"] == {"n": 1, "self": "<cycle: dict>"}
    assert rec["counts"] == {"200": 3}
    assert "Logging error" not in capsys.readouterr().err


def test_bridge_leaves_record_and_extras_untouched(native_memory: None, clean_root: None) -> None:
    # Other handlers may need the original objects: the bridge converts a copy
    # and never rewrites the record or the caller's containers.
    seen: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            seen.append(record)

    install_stdlib_bridge(level="DEBUG")
    logging.getLogger().addHandler(Capture())  # runs after the bridge
    request = object()
    ctx: dict[str, object] = {"obj": request, "items": [request]}
    logging.getLogger("svc").info("m", extra={"request": request, "ctx": ctx})

    rec = _records()[-1]
    assert rec["request"] == "<unsupported: object>"
    assert rec["ctx"] == {"obj": "<unsupported: object>", "items": ["<unsupported: object>"]}
    assert seen[0].request is request  # type: ignore[attr-defined]
    assert seen[0].ctx is ctx  # type: ignore[attr-defined]
    assert ctx["obj"] is request
    assert ctx["items"] == [request]


def test_logger_policy_snapshots_take_bridge_lock_before_logging_lock(
    clean_root: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from structguru.integrations import stdlib as integration

    acquisitions: list[str] = []

    class RecordingLock:
        def __init__(self, name: str) -> None:
            self.name = name
            self.lock = threading.RLock()
            self.depth = 0

        @property
        def held(self) -> bool:
            return self.depth > 0

        def acquire(self) -> bool:
            self.lock.acquire()
            self.depth += 1
            acquisitions.append(self.name)
            return True

        def release(self) -> None:
            self.depth -= 1
            self.lock.release()

        def __enter__(self) -> RecordingLock:
            self.acquire()
            return self

        def __exit__(self, *args: object) -> None:
            self.release()

    logging_lock = RecordingLock("logging")
    bridge_lock = RecordingLock("bridge")
    monkeypatch.setattr(logging, "_lock", logging_lock)
    monkeypatch.setattr(integration, "_bridge_lock", bridge_lock)
    original_policy = integration._apply_existing_logger_policy

    def checked_policy(
        disable_existing_loggers: bool | None,
    ) -> list[tuple[logging.Logger, bool, bool]]:
        assert logging_lock.held
        assert bridge_lock.held
        return original_policy(disable_existing_loggers)

    monkeypatch.setattr(integration, "_apply_existing_logger_policy", checked_policy)

    bridge = install_stdlib_bridge(disable_existing_loggers=False)
    assert acquisitions[:2] == ["bridge", "logging"]
    acquisitions.clear()
    bridge = install_stdlib_bridge(disable_existing_loggers=False, replace=True)
    assert acquisitions[:2] == ["bridge", "logging"]
    acquisitions.clear()
    uninstall_stdlib_bridge(bridge)
    assert acquisitions[:2] == ["bridge", "logging"]


def test_root_attachment_takes_logging_lock_before_its_condition(
    clean_root: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    condition = core._root_attach_condition

    @contextmanager
    def checked_condition() -> Iterator[None]:
        assert logging._lock._is_owned()  # type: ignore[attr-defined]
        with condition:
            yield

    handler = logging.NullHandler()
    relay = None
    try:
        with monkeypatch.context() as patch:
            patch.setattr(core, "_root_attach_condition", checked_condition())
            relay = core._attach_to_root(handler, logging.INFO)
        for active in (True, False):
            with monkeypatch.context() as patch:
                patch.setattr(core, "_root_attach_condition", checked_condition())
                core._set_stdlib_bridge_active(active)
        # Retirement also uses the condition, after releasing logging._lock.
        # Keep that separate from the attachment lock-order assertion.
        with monkeypatch.context() as patch:
            patch.setattr(core, "_root_attach_condition", checked_condition())
            patch.setattr(core, "_retire_handler_generation", lambda *args, **kwargs: None)
            core._detach_from_root(relay)
    finally:
        if relay is not None:
            core._detach_from_root(relay)
            core._retire_handler_generation(relay, raw=True)
            core._retire_handler_generation(relay, raw=False)
        core._set_stdlib_bridge_active(False)


def test_bridge_failure_diagnostic_omits_record_and_exception_text(
    capfd: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    class BrokenRecord(logging.LogRecord):
        def getMessage(self) -> str:
            raise ValueError("SYNTHETIC_CANARY")

    monkeypatch.setattr(logging, "raiseExceptions", True)
    StructguruHandler().handle(BrokenRecord("broken", 40, "", 1, "SYNTHETIC_CANARY", (), None))
    error = capfd.readouterr().err
    assert "ValueError" in error and "stdlib emit failed" in error
    assert "SYNTHETIC_CANARY" not in error and "Traceback" not in error
