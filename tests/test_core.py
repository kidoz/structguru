"""Tests for structguru.core."""

from __future__ import annotations

import io
import json
import logging
import os
import stat
import sys
import threading
import warnings
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import configure

from structguru import _runtime, core
from structguru.core import (
    Logger,
    _CallableHandler,
    _make_handler,
    _safe_format,
    _warn_format_failure,
)


@pytest.mark.parametrize(
    "level, canonical",
    [(level, "ERROR") for level in ("ERROR", "Error", "error", "EXCEPTION")]
    + [(level, "DEBUG") for level in ("TRACE", "Trace", "trace")],
)
def test_catch_normalizes_levels_before_filtering(level: str, canonical: str) -> None:
    stream = io.StringIO()
    configure(level=canonical, stream=stream)
    with Logger().catch(level=level):
        raise ValueError("caught")
    record = json.loads(stream.getvalue())
    assert record["level"] == canonical
    assert "ValueError: caught" in record["exception"]


@pytest.mark.parametrize("threshold", ["TRACE", "DEBUG", "INFO"])
@pytest.mark.parametrize("sink_threshold", ["TRACE", "DEBUG", "INFO"])
def test_catch_trace_matches_trace_thresholds(threshold: str, sink_threshold: str) -> None:
    _runtime.configure(target="memory", level=threshold)
    log = Logger()
    delivered: list[str] = []
    token = log.add(delivered.append, level=sink_threshold)
    try:
        log.trace("direct")
        with log.catch(level="TrAcE", message="caught"):
            raise ValueError("trace failure")
        _runtime.flush()
        records = [json.loads(line) for line in _runtime.drain_messages()]
        expected = [] if threshold == "INFO" else ["direct", "caught"]
        assert [record["message"] for record in records] == expected
        assert all(record["level"] == "DEBUG" for record in records)
        sink_records = [json.loads(line) for line in delivered]
        assert [record["message"] for record in sink_records] == (
            [] if sink_threshold == "INFO" else expected
        )
    finally:
        log.remove(token)


@pytest.mark.parametrize("reraise", [False, True])
@pytest.mark.parametrize("threshold", ["DEBUG", "INFO"])
def test_catch_trace_decorator_preserves_exception_behavior(reraise: bool, threshold: str) -> None:
    stream = io.StringIO()
    configure(level=threshold, stream=stream)
    failure = ValueError("sync failure")

    @Logger().catch(ValueError, level="trace", reraise=reraise)
    def operation(fail: bool) -> int:
        if fail:
            raise failure
        return 42

    assert operation(False) == 42
    if reraise:
        with pytest.raises(ValueError) as caught:
            operation(True)
        assert caught.value is failure
    else:
        assert operation(True) is None
    if threshold == "DEBUG":
        record = json.loads(stream.getvalue())
        assert record["level"] == "DEBUG"
        assert "ValueError: sync failure" in record["exception"]
    else:
        assert stream.getvalue() == ""


@pytest.mark.parametrize("level", ["TYPO", "", 40, None])
def test_catch_rejects_invalid_levels_before_entering(level: object) -> None:
    with pytest.raises(ValueError, match="catch level"):
        Logger().catch(level=level)  # type: ignore[arg-type]


@pytest.mark.parametrize("format", ["json", "console"])
@pytest.mark.parametrize("method, expected", [("debug", 10), ("error", 40), ("critical", 50)])
def test_handler_receives_original_severity(format: str, method: str, expected: int) -> None:
    _runtime.configure(target="null", level="DEBUG", format=format, colors=True)
    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    log = Logger(name="source")
    token = log.add(Capture())
    try:
        getattr(log, method)("severity probe")
        _runtime.flush()
        assert len(records) == 1
        assert records[0].levelno == expected
        assert "severity probe" in records[0].getMessage()
    finally:
        log.remove(token)


@pytest.fixture(autouse=True)
def _reset_format_warning_cache() -> None:  # type: ignore[misc]
    _warn_format_failure.cache_clear()
    yield  # type: ignore[misc]
    _warn_format_failure.cache_clear()


class TestSafeFormat:
    def test_positional_args(self) -> None:
        msg, consumed_keys = _safe_format("Hello {}", ("world",), {})
        assert msg == "Hello world"
        assert consumed_keys == set()  # positional args have no named keys

    def test_keyword_args(self) -> None:
        msg, consumed_keys = _safe_format("Hello {name}", (), {"name": "world"})
        assert msg == "Hello world"
        assert consumed_keys == {"name"}

    def test_no_placeholders(self) -> None:
        msg, consumed_keys = _safe_format("Hello", ("extra",), {})
        assert msg == "Hello"
        assert consumed_keys == set()

    def test_no_args(self) -> None:
        msg, consumed_keys = _safe_format("Hello {}", (), {})
        assert msg == "Hello {}"
        assert consumed_keys == set()

    def test_format_key_error_returns_original(self) -> None:
        with pytest.warns(UserWarning, match="KeyError"):
            msg, consumed_keys = _safe_format("Hello {missing}", (), {"other": 1})
        assert msg == "Hello {missing}"
        assert consumed_keys == set()

    def test_attribute_error_returns_original(self) -> None:
        with pytest.warns(UserWarning, match="AttributeError"):
            msg, consumed_keys = _safe_format("Hello {user.name}", (), {"user": {}})
        assert msg == "Hello {user.name}"
        assert consumed_keys == set()

    def test_type_error_returns_original(self) -> None:
        with pytest.warns(UserWarning, match="ValueError"):
            msg, consumed_keys = _safe_format("{0!x}", (42,), {})
        assert msg == "{0!x}"
        assert consumed_keys == set()

    def test_malformed_braces_warn_and_fallback(self) -> None:
        with pytest.warns(UserWarning, match="ValueError"):
            msg, consumed_keys = _safe_format("Hello {unterminated", (), {"name": "x"})
        assert msg == "Hello {unterminated"
        assert consumed_keys == set()

    def test_warns_only_once_per_template(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            for _ in range(5):
                _safe_format("Hello {missing}", (), {"other": 1})
        format_warnings = [w for w in caught if "failed to format" in str(w.message)]
        assert len(format_warnings) == 1

    def test_different_templates_each_warn(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            _safe_format("A {x}", (), {"other": 1})
            _safe_format("B {y}", (), {"other": 1})
        format_warnings = [w for w in caught if "failed to format" in str(w.message)]
        assert len(format_warnings) == 2

    def test_non_string_message(self) -> None:
        msg, consumed_keys = _safe_format(42, (), {})
        assert msg == "42"
        assert consumed_keys == set()

    def test_mixed_args(self) -> None:
        msg, consumed_keys = _safe_format("{} {name}", ("hi",), {"name": "world"})
        assert msg == "hi world"
        assert consumed_keys == {"name"}


class TestMakeHandler:
    def test_logging_handler_passthrough(self) -> None:
        h = logging.StreamHandler()
        assert _make_handler(h) is h

    def test_file_path_string(self, tmp_path: Path) -> None:
        p = tmp_path / "test.log"
        handler = _make_handler(str(p))
        assert isinstance(handler, logging.FileHandler)
        handler.close()

    def test_file_path_object(self, tmp_path: Path) -> None:
        p = tmp_path / "test.log"
        handler = _make_handler(p)
        assert isinstance(handler, logging.FileHandler)
        handler.close()

    def test_file_like_object(self) -> None:
        buf = io.StringIO()
        handler = _make_handler(buf)  # type: ignore[arg-type]
        assert isinstance(handler, logging.StreamHandler)

    def test_callable_sink(self) -> None:
        messages: list[str] = []
        handler = _make_handler(messages.append)
        assert isinstance(handler, _CallableHandler)

    def test_unsupported_type_raises(self) -> None:
        import pytest

        with pytest.raises(TypeError, match="Unsupported sink type"):
            _make_handler(42)  # type: ignore[arg-type]


class TestLoggerBind:
    def test_returns_new_logger(self) -> None:
        log = Logger()
        child = log.bind(user="alice")
        assert child is not log
        assert child._bound == {"user": "alice"}
        assert log._bound == {}

    def test_merges_context(self) -> None:
        log = Logger().bind(a=1)
        child = log.bind(b=2)
        assert child._bound == {"a": 1, "b": 2}

    def test_overrides_existing_key(self) -> None:
        log = Logger().bind(a=1)
        child = log.bind(a=2)
        assert child._bound == {"a": 2}


class TestLoggerContextualize:
    def test_context_manager(self) -> None:
        log = Logger()
        with log.contextualize(request_id="abc") as ctx_logger:
            assert ctx_logger is log


class TestLoggerOpt:
    def test_sets_exc_info(self) -> None:
        log = Logger()
        child = log.opt(exception=True)
        assert child._opt_exc_info is True
        assert log._opt_exc_info is None

    def test_sets_stack_info(self) -> None:
        log = Logger()
        child = log.opt(stack_info=True)
        assert child._opt_stack_info is True
        assert log._opt_stack_info is False

    def test_flag_persists_across_calls(self) -> None:
        """Matches loguru: opt() is sticky on the returned logger."""
        buf = io.StringIO()
        configure(service="test", level="DEBUG", stream=buf)

        log = Logger()
        errlog = log.opt(exception=True)
        try:
            raise RuntimeError("first")
        except RuntimeError:
            errlog.error("boom1")
        try:
            raise RuntimeError("second")
        except RuntimeError:
            errlog.error("boom2")

        output = buf.getvalue()
        # Both calls must render their traceback (not only the first one).
        assert output.count("Traceback") == 2 or output.count("RuntimeError") >= 2

    def test_opt_does_not_leak_into_parent(self) -> None:
        log = Logger()
        _ = log.opt(exception=True)
        assert log._opt_exc_info is None


class TestLoggerLevelMethods:
    def _make_capturing_logger(self) -> tuple[Logger, io.StringIO]:
        buf = io.StringIO()
        configure(service="test", level="DEBUG", stream=buf)
        return Logger(), buf

    def test_debug(self) -> None:
        log, buf = self._make_capturing_logger()
        log.debug("test debug")
        assert "test debug" in buf.getvalue()

    def test_info(self) -> None:
        log, buf = self._make_capturing_logger()
        log.info("test info")
        assert "test info" in buf.getvalue()

    def test_warning(self) -> None:
        log, buf = self._make_capturing_logger()
        log.warning("test warning")
        assert "test warning" in buf.getvalue()

    def test_error(self) -> None:
        log, buf = self._make_capturing_logger()
        log.error("test error")
        assert "test error" in buf.getvalue()

    def test_critical(self) -> None:
        log, buf = self._make_capturing_logger()
        log.critical("test critical")
        assert "test critical" in buf.getvalue()

    def test_brace_formatting(self) -> None:
        log, buf = self._make_capturing_logger()
        log.info("Hello {name}", name="world")
        output = buf.getvalue()
        assert "Hello world" in output

    def test_positional_args_no_placeholders_does_not_crash(self) -> None:
        log, buf = self._make_capturing_logger()
        log.info("Hello", "extra")
        assert "Hello" in buf.getvalue()

    def test_warn_alias(self) -> None:
        log, buf = self._make_capturing_logger()
        log.warn("test warn")
        assert "test warn" in buf.getvalue()

    def test_fatal_alias(self) -> None:
        log, buf = self._make_capturing_logger()
        log.fatal("test fatal")
        assert "test fatal" in buf.getvalue()


class TestRawStdlibDelivery:
    """Third-party records reach ``add()`` sinks raw through the root logger."""

    def test_records_emitted_by_a_raw_callback_do_not_reenter_sinks(self) -> None:
        configure(service="test", level="DEBUG", stream=io.StringIO())
        log = Logger()
        received: list[str] = []

        def sink(line: str) -> None:
            received.append(line)
            logging.getLogger("third_party").warning("emitted by callback")

        handler_id = log.add(sink)
        try:
            logging.getLogger("third_party").warning("original")
        finally:
            log.remove(handler_id)
        assert received == ["original"]

    def test_raw_records_emitted_by_a_native_callback_do_not_reenter_sinks(self) -> None:
        configure(service="test", level="DEBUG", stream=io.StringIO())
        log = Logger()
        received: list[str] = []

        def sink(line: str) -> None:
            received.append(line)
            logging.getLogger("third_party").warning("emitted by callback")

        handler_id = log.add(sink)
        try:
            log.info("native record")
            _runtime.flush()
        finally:
            log.remove(handler_id)
        assert len(received) == 1
        assert "native record" in received[0]

    def test_native_records_emitted_by_a_raw_callback_bypass_sinks(self) -> None:
        stream = io.StringIO()
        configure(service="test", level="DEBUG", stream=stream)
        log = Logger()
        received: list[str] = []

        def sink(line: str) -> None:
            received.append(line)
            log.info("echo from callback")

        handler_id = log.add(sink)
        try:
            logging.getLogger("third_party").warning("original")
            _runtime.flush()
        finally:
            log.remove(handler_id)
        assert received == ["original"]
        assert "echo from callback" in stream.getvalue()

    def test_remove_waits_for_raw_delivery_in_progress(self) -> None:
        configure(service="test", level="DEBUG", stream=io.StringIO())
        log = Logger()
        entered, release = threading.Event(), threading.Event()
        order: list[str] = []

        def sink(line: str) -> None:
            entered.set()
            assert release.wait(3)
            order.append("delivered")

        handler_id = log.add(sink)
        emitter = threading.Thread(
            target=logging.getLogger("third_party").warning, args=("blocked",)
        )
        emitter.start()
        assert entered.wait(3)

        def remove() -> None:
            log.remove(handler_id)
            order.append("removed")

        remover = threading.Thread(target=remove)
        remover.start()
        remover.join(0.2)
        try:
            assert remover.is_alive()
        finally:
            release.set()
            emitter.join(3)
            remover.join(3)
        assert order == ["delivered", "removed"]

    def test_remove_inside_a_raw_callback_returns_without_deadlock(self) -> None:
        configure(service="test", level="DEBUG", stream=io.StringIO())
        log = Logger()
        received: list[str] = []
        handler_id: int | None = None

        def sink(line: str) -> None:
            received.append(line)
            assert handler_id is not None
            log.remove(handler_id)

        handler_id = log.add(sink)
        emitter = threading.Thread(target=logging.getLogger("third_party").warning, args=("once",))
        emitter.start()
        emitter.join(3)
        assert not emitter.is_alive()
        logging.getLogger("third_party").warning("after removal")
        assert received == ["once"]

    def test_removed_sink_rejects_raw_records_already_offered(self) -> None:
        # A producer that fetched the relay just before removal must not deliver
        # after remove() has returned.
        configure(service="test", level="DEBUG", stream=io.StringIO())
        log = Logger()
        received: list[str] = []
        handler_id = log.add(received.append)
        relay = core._root_relays[log._handlers[handler_id]]
        log.remove(handler_id)
        record = logging.LogRecord("third_party", logging.WARNING, "", 0, "late", (), None)
        assert relay.handle(record) is False
        assert received == []


class TestLoggerAddRemove:
    def test_add_and_remove(self) -> None:
        configure(service="test", level="DEBUG", stream=io.StringIO())
        log = Logger()
        messages: list[str] = []
        hid = log.add(messages.append, level="DEBUG")
        assert isinstance(hid, int)

        log.remove(hid)

    def test_remove_drains_records_queued_before_removal(self) -> None:
        configure(service="test", level="DEBUG", stream=io.StringIO())
        log = Logger()
        messages: list[str] = []
        handler_id = log.add(messages.append)
        log.info("before removal")
        log.remove(handler_id)
        assert any("before removal" in message for message in messages)

    def test_remove_all(self) -> None:
        configure(service="test", level="DEBUG", stream=io.StringIO())
        log = Logger()
        messages: list[str] = []
        log.add(messages.append, level="DEBUG")
        log.add(messages.append, level="DEBUG")
        log.remove()
        assert len(log._handlers) == 0

    def test_add_callable_receives_messages(self) -> None:
        # Callable sinks route through the native dispatch thread and receive
        # rendered log lines.
        configure(service="test", level="DEBUG", stream=io.StringIO())
        log = Logger()
        messages: list[str] = []
        hid = log.add(messages.append, level="DEBUG")
        try:
            log.info("captured")
            _runtime.flush_native()
        finally:
            log.remove(hid)
        assert any("captured" in m for m in messages), f"expected delivery, got {messages}"

    def test_duplicate_callable_registrations_are_removed_by_handler_id(self) -> None:
        configure(service="test", level="DEBUG", stream=io.StringIO())
        log = Logger()
        messages: list[str] = []
        first = log.add(messages.append)
        second = log.add(messages.append)
        log.remove(first)
        try:
            log.info("once")
            _runtime.flush_native()
        finally:
            log.remove(second)
        assert len(messages) == 1

    def test_callable_added_while_disabled_activates_on_configure(self) -> None:
        _runtime.shutdown()
        log = Logger()
        messages: list[str] = []
        handler_id = log.add(messages.append)
        try:
            _runtime.configure(target="memory")
            log.info("activated")
            _runtime.flush_native()
        finally:
            log.remove(handler_id)
        assert any("activated" in message for message in messages)

    def test_runtime_callable_survives_reconfigure(self) -> None:
        _runtime.configure(target="memory")
        log = Logger()
        messages: list[str] = []
        handler_id = log.add(messages.append)
        try:
            _runtime.configure(target="memory", level="DEBUG")
            log.info("after reconfigure")
            _runtime.flush_native()
        finally:
            log.remove(handler_id)
        assert any("after reconfigure" in message for message in messages)

    def test_stream_sink_receives_native_records(self) -> None:
        configure(service="test", level="DEBUG", stream=io.StringIO())
        log = Logger()
        stream = io.StringIO()
        handler_id = log.add(stream)
        try:
            log.info("native stream")
            _runtime.flush_native()
        finally:
            log.remove(handler_id)
        assert "native stream" in stream.getvalue()

    def test_remove_closes_handler(self, tmp_path: Path) -> None:
        configure(service="test", level="DEBUG", stream=io.StringIO())
        log = Logger()
        hid = log.add(tmp_path / "test.log", level="DEBUG")
        handler = log._handlers[hid]
        stream = handler.stream  # type: ignore[attr-defined]
        assert stream is not None
        log.info("native file")
        _runtime.flush_native()
        log.remove(hid)
        assert stream.closed
        assert "native file" in (tmp_path / "test.log").read_text()

    def test_remove_inside_callback_closes_handler_after_queued_deliveries(
        self, tmp_path: Path
    ) -> None:
        # A callback removing a file sink cannot wait for the worker, but the
        # deliveries already queued for that sink must still reach the file
        # before it closes; closing first made FileHandler reopen the file for
        # them and leak the stream.
        configure(service="test", level="DEBUG", stream=io.StringIO())
        log = Logger()
        handler = logging.FileHandler(tmp_path / "test.log")
        file_id = log.add(handler, level="DEBUG")
        entered, proceed = threading.Event(), threading.Event()

        def trigger(line: str) -> None:
            if '"message":"trigger"' in line:
                entered.set()
                assert proceed.wait(3)
                log.remove(file_id)

        trigger_id = log.add(trigger, level="DEBUG")
        try:
            log.info("trigger")
            assert entered.wait(3)
            log.info("queued")  # captured the file sink before its removal
            proceed.set()
            _runtime.flush()
        finally:
            log.remove(trigger_id)
        assert handler.stream is None, "handler closed only after the queued delivery"
        lines = (tmp_path / "test.log").read_text().splitlines()
        assert [json.loads(line)["message"] for line in lines] == ["trigger", "queued"]

    @pytest.mark.skipif(os.name != "posix", reason="Unix permission bits only")
    def test_path_sink_is_created_owner_only(self, tmp_path: Path) -> None:
        configure(service="test", level="DEBUG", stream=io.StringIO())
        log = Logger()
        path = tmp_path / "private.log"
        handler_id = log.add(path)
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        finally:
            log.remove(handler_id)
        assert mode == 0o600

    def test_remove_all_closes_handlers(self, tmp_path: Path) -> None:
        configure(service="test", level="DEBUG", stream=io.StringIO())
        log = Logger()
        log.add(tmp_path / "a.log", level="DEBUG")
        log.add(tmp_path / "b.log", level="DEBUG")
        log.remove()
        # handlers dict is cleared, no leaked file descriptors

    def test_sink_without_level_accepts_all_levels_on_both_paths(self) -> None:
        # `level=None` documents "all levels". Inheriting the root logger's level
        # instead would gate the stdlib delivery path at WARNING (the default on
        # an unconfigured root) while native delivery still passed everything.
        logging.getLogger().setLevel(logging.WARNING)
        configure(service="test", level="DEBUG", stream=io.StringIO())
        log = Logger()
        handler_id = log.add(io.StringIO())
        try:
            assert log._handlers[handler_id].level == logging.NOTSET
        finally:
            log.remove(handler_id)

    @pytest.mark.parametrize(
        "level, threshold, kept, gated",
        [("TRACE", 5, "debug", None), ("EXCEPTION", 40, "error", "info")],
    )
    def test_sink_level_aliases_gate_both_paths(
        self, level: str, threshold: int, kept: str, gated: str | None
    ) -> None:
        # Aliases the rest of the library accepts must not warn and fall back to
        # INFO, which dropped DEBUG below TRACE and admitted INFO to EXCEPTION.
        configure(service="test", level="DEBUG", stream=io.StringIO())
        log = Logger()
        messages: list[str] = []
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            handler_id = log.add(messages.append, level=level)
        try:
            assert log._handlers[handler_id].level == threshold
            getattr(log, kept)("kept")
            if gated is not None:
                getattr(log, gated)("gated")
            _runtime.flush()
        finally:
            log.remove(handler_id)
        assert any("kept" in message for message in messages)
        assert not any("gated" in message for message in messages)

    def test_logger_is_hashable(self) -> None:
        # Loggers are identity-valued handles; users put them in dicts/sets.
        log = Logger(name="svc")
        assert len({log, log, Logger(name="svc")}) == 2

    def test_unique_ids_across_instances(self) -> None:
        configure(service="test", level="DEBUG", stream=io.StringIO())
        log1 = Logger()
        log2 = Logger()
        id1 = log1.add(io.StringIO(), level="DEBUG")
        id2 = log2.add(io.StringIO(), level="DEBUG")
        assert id1 != id2
        log1.remove()
        log2.remove()


class TestLoggerIntegration:
    def test_bind_with_output(self) -> None:
        buf = io.StringIO()
        configure(service="test", level="DEBUG", stream=buf)
        log = Logger().bind(user="alice")
        log.info("action")
        output = buf.getvalue()
        assert "alice" in output
        assert "action" in output

    def test_clear_handlers_false_preserves_existing(self) -> None:
        # Native mode doesn't manage root handlers; reconfiguring doesn't change them.
        buf1 = io.StringIO()
        configure(service="test", level="DEBUG", stream=buf1)
        root = logging.getLogger()
        handler_count_before = len(root.handlers)

        buf2 = io.StringIO()
        configure(service="test", level="DEBUG", stream=buf2)
        # Handler count unchanged (native mode doesn't add/remove root handlers).
        assert len(root.handlers) == handler_count_before

    def test_contextualize_with_output(self) -> None:
        buf = io.StringIO()
        configure(service="test", level="DEBUG", stream=buf)
        log = Logger()
        with log.contextualize(request_id="req-123"):
            log.info("in context")
        output = buf.getvalue()
        assert "req-123" in output
        assert "in context" in output


class TestLoggerCatch:
    def test_catch_context_manager(self) -> None:
        buf = io.StringIO()
        configure(service="test", level="DEBUG", stream=buf)
        log = Logger()

        with log.catch(ValueError, message="caught ValueError"):
            raise ValueError("boom")

        output = buf.getvalue()
        assert "caught ValueError" in output
        assert "ValueError: boom" in output

    def test_catch_decorator(self) -> None:
        buf = io.StringIO()
        configure(service="test", level="DEBUG", stream=buf)
        log = Logger()

        @log.catch(message="decorator error")
        def failing_func() -> None:
            raise RuntimeError("crashed")

        failing_func()
        output = buf.getvalue()
        assert "decorator error" in output
        assert "RuntimeError: crashed" in output

    def test_catch_reraise(self) -> None:
        buf = io.StringIO()
        configure(service="test", level="DEBUG", stream=buf)
        log = Logger()

        with pytest.raises(ValueError):
            with log.catch(ValueError, message="will reraise", reraise=True):
                raise ValueError("boom")

        output = buf.getvalue()
        assert "will reraise" in output

    def test_catch_unmatched_exception_reraises_automatically(self) -> None:
        buf = io.StringIO()
        configure(service="test", level="DEBUG", stream=buf)
        log = Logger()

        with pytest.raises(RuntimeError):
            with log.catch(ValueError):
                raise RuntimeError("boom")


@pytest.mark.parametrize("message", ["DEMO_VALUE {missing}", "DEMO_VALUE {unclosed"])
def test_format_warning_omits_sensitive_template_and_source(message: str) -> None:
    buf = io.StringIO()
    _runtime.configure(stream_sink=buf, target="null", sensitive_patterns=["DEMO_VALUE"])
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        Logger().info(message, unused=True)
    assert len(captured) == 1
    warning = captured[0]
    formatted = warnings.formatwarning(
        warning.message,
        warning.category,
        warning.filename,
        warning.lineno,
    )
    assert "DEMO_VALUE" not in formatted
    assert "DEMO_VALUE" not in buf.getvalue()
    assert "[REDACTED]" in buf.getvalue()


def test_format_warning_omits_exception_details() -> None:
    class BadFormat:
        def __format__(self, spec: str) -> str:
            raise ValueError("DEMO_VALUE")

    with pytest.warns(UserWarning) as captured:
        _safe_format("{}", (BadFormat(),), {})
    assert "DEMO_VALUE" not in str(captured[0].message)


@pytest.mark.asyncio
@pytest.mark.parametrize("reraise", [False, True])
@pytest.mark.parametrize("level", ["error", "TrAcE"])
async def test_catch_coroutine_logs_awaited_exceptions(reraise: bool, level: str) -> None:
    import asyncio
    import inspect

    buf = io.StringIO()
    configure(stream=buf)
    log = Logger()
    failure = ValueError("async failure")

    @log.catch(ValueError, level=level, reraise=reraise)
    async def operation(fail: bool) -> int:
        await asyncio.sleep(0)
        if fail:
            raise failure
        return 42

    assert inspect.iscoroutinefunction(operation)
    assert operation.__name__ == "operation"
    assert await operation(False) == 42
    if reraise:
        with pytest.raises(ValueError) as captured:
            await operation(True)
        assert captured.value is failure
    else:
        assert await operation(True) is None
    assert "async failure" in buf.getvalue()
    assert len(buf.getvalue().splitlines()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("level", ["error", "trace"])
async def test_catch_coroutine_preserves_unmatched_errors_and_cancellation(level: str) -> None:
    import asyncio

    buf = io.StringIO()
    configure(stream=buf)

    @Logger().catch(ValueError, level=level)
    async def operation(error: BaseException) -> None:
        raise error

    for error in (RuntimeError("unmatched"), asyncio.CancelledError()):
        with pytest.raises(type(error)) as captured:
            await operation(error)
        assert captured.value is error
    assert not buf.getvalue()


def test_stream_sink_protocol_accepts_standard_and_custom_streams() -> None:
    from structguru import flush
    from structguru.core import WritableSink

    class Stream:
        def __init__(self) -> None:
            self.lines: list[str] = []

        def write(self, message: str, /) -> None:
            self.lines.append(message)

    custom = Stream()
    streams: list[WritableSink] = [io.StringIO(), custom]
    _runtime.configure(target="null")
    log = Logger()
    tokens = [log.add(stream) for stream in streams]
    log.info("stream record")
    flush()
    assert len(custom.lines) == 1
    for token in tokens:
        log.remove(token)


def test_stream_sink_consumer_types_accept_text_and_reject_binary() -> None:
    api = pytest.importorskip("mypy.api")
    code = """
import io
from structguru import logger
from structguru.core import WritableSink

class Stream:
    def write(self, message: str, /) -> None:
        pass

sink: WritableSink = Stream()
logger.add(sink)
logger.add(io.StringIO())
"""
    stdout, stderr, status = api.run(["--no-incremental", "-c", code])
    assert status == 0, stdout + stderr
    stdout, stderr, status = api.run(
        [
            "--no-incremental",
            "-c",
            "import io; from structguru import logger; logger.add(io.BytesIO())",
        ]
    )
    assert status == 1 and "arg-type" in stdout, stdout + stderr


def _memory_records() -> list[dict[str, Any]]:
    _runtime.flush()
    return [json.loads(line) for line in _runtime.drain_messages()]


def test_logger_repr_hides_bound_and_option_values() -> None:
    log = Logger(name="svc").bind(access_token="CANARY-BOUND").opt(exception=ValueError("CANARY"))
    assert "CANARY" not in repr(log)
    assert "svc" in repr(log)


@dataclass
class _Login:
    user: str
    password: str


@dataclass
class _Named:
    name: str
    password: str

    def __str__(self) -> str:
        return self.name


class _Request:
    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers


class TestPlaceholderRedaction:
    @pytest.mark.parametrize(
        "template, kwargs, expected",
        [
            ("login token={token}", {"token": "CANARY"}, "login token=[REDACTED]"),
            ("auth {Authorization!r}", {"Authorization": "CANARY"}, "auth '[REDACTED]'"),
            ("pw={creds[password]}", {"creds": {"password": "CANARY"}}, "pw=[REDACTED]"),
            ("{user.api_key:>12}", {"user": SimpleNamespace(api_key="CANARY")}, "  [REDACTED]"),
        ],
    )
    def test_sensitive_placeholders_are_interpolated_redacted(
        self, template: str, kwargs: dict[str, Any], expected: str
    ) -> None:
        _runtime.configure(target="memory")
        Logger().info(template, user_id=7, **kwargs)
        _runtime.flush()
        line = _runtime.drain_messages()[-1]
        assert "CANARY" not in line
        record = json.loads(line)
        assert record["message"] == expected
        assert record["user_id"] == 7

    def test_custom_sensitive_keys_replace_the_defaults(self) -> None:
        _runtime.configure(target="memory", sensitive_keys=["PIN"])
        Logger().info("pin={pin} token={token}", pin="1234", token="visible")
        assert _memory_records()[-1]["message"] == "pin=[REDACTED] token=visible"

    @pytest.mark.parametrize(
        "template, args, kwargs, expected",
        [
            ("{}", ({"password": "CANARY"},), {}, "{'password': [REDACTED]}"),
            ("{data}", (), {"data": {"token": "CANARY"}}, "{'token': [REDACTED]}"),
            ("{!r:>26}", ({"secret": "CANARY"},), {}, "    {'secret': [REDACTED]}"),
            ("{!s}", ([{"Cookie": "CANARY"}],), {}, "[{'Cookie': [REDACTED]}]"),
            (
                "{0}",
                ([("Authorization", "CANARY"), ("Accept", "json")],),
                {},
                "[('Authorization', [REDACTED]), ('Accept', 'json')]",
            ),
            ("{req.headers}", (), {"req": _Request({"token": "CANARY"})}, "{'token': [REDACTED]}"),
            ("{}", (_Login("alice", "CANARY"),), {}, "_Login(user='alice', password=[REDACTED])"),
            ("{}", (SimpleNamespace(api_key="CANARY"),), {}, "namespace(api_key=[REDACTED])"),
        ],
    )
    def test_interpolated_containers_are_redacted(
        self, template: str, args: tuple[Any, ...], kwargs: dict[str, Any], expected: str
    ) -> None:
        _runtime.configure(target="memory")
        Logger().info(template, *args, **kwargs)
        _runtime.flush()
        line = _runtime.drain_messages()[-1]
        assert "CANARY" not in line
        assert json.loads(line)["message"] == expected

    def test_containers_without_secrets_format_as_usual(self) -> None:
        _runtime.configure(target="memory")
        wide = list(range(500))
        Logger().info("{} {} {} {}", {"user": "alice"}, wide, _Named("alice", "pw"), 3)
        expected = f"{{'user': 'alice'}} {wide} alice 3"
        assert _memory_records()[-1]["message"] == expected

    def test_container_redaction_follows_configured_keys(self) -> None:
        _runtime.configure(target="memory", sensitive_keys=["PIN"])
        Logger().info("{}", {"pin": "CANARY", "password": "shown"})
        assert _memory_records()[-1]["message"] == "{'pin': [REDACTED], 'password': 'shown'}"
        _runtime.configure(target="memory", sensitive_keys=[])
        Logger().info("{}", {"password": "shown"})
        assert _memory_records()[-1]["message"] == "{'password': 'shown'}"


class TestCatchLoggerName:
    @pytest.fixture
    def target(self) -> dict[str, Any]:
        namespace: dict[str, Any] = {"__name__": "catch_target"}
        exec(
            "def fail():\n    raise ValueError('sync')\n"
            "async def afail():\n    raise ValueError('async')\n",
            namespace,
        )
        return namespace

    def test_decorators_use_the_decorated_function_module(self, target: dict[str, Any]) -> None:
        import asyncio

        _runtime.configure(target="memory")
        Logger().catch()(target["fail"])()
        asyncio.run(Logger().catch()(target["afail"])())
        with Logger().catch():
            raise ValueError("block")
        Logger(name="explicit").catch()(target["fail"])()
        names = [record["logger"] for record in _memory_records()]
        assert names == ["catch_target", "catch_target", __name__, "explicit"]


class TestRemoveById:
    def test_ids_are_process_wide(self) -> None:
        _runtime.configure(target="null")
        received: list[str] = []
        handler_id = Logger(name="mymod").add(received.append)
        Logger().remove(handler_id)
        Logger().info("after remove")
        _runtime.flush()
        assert received == []
        with pytest.raises(
            ValueError, match=f"There is no existing handler with id {handler_id}$"
        ):
            Logger().remove(handler_id)

    def test_unknown_and_invalid_ids_raise(self) -> None:
        with pytest.raises(ValueError, match="There is no existing handler with id 987654321"):
            Logger().remove(987654321)
        with pytest.raises(TypeError, match="Invalid handler id"):
            Logger().remove("1")  # type: ignore[arg-type]

    def test_remove_all_skips_sinks_removed_elsewhere(self) -> None:
        _runtime.configure(target="null")
        owner = Logger()
        removed = owner.add(io.StringIO())
        kept_elsewhere = Logger().add(io.StringIO())
        owner.bind(child=True).remove(removed)
        owner.remove()
        assert owner._handlers == {}
        Logger().remove(kept_elsewhere)


def test_add_leaves_a_user_handler_unmodified() -> None:
    _runtime.configure(target="null")
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    formatter = logging.Formatter("APP %(levelname)s: %(message)s")
    handler.setFormatter(formatter)
    handler.setLevel(logging.ERROR)
    handler_id = Logger().add(handler)
    try:
        assert handler.level == logging.ERROR
        assert handler.formatter is formatter
        logging.getLogger("third.party").warning("raw warning")
        Logger().info("native info")
        _runtime.flush()
    finally:
        Logger().remove(handler_id)
    assert handler.level == logging.ERROR
    assert handler.formatter is formatter
    output = buffer.getvalue()
    assert "APP WARNING: raw warning" in output
    assert 'APP INFO: {"logger"' in output


class TestAddLevelValidation:
    @pytest.mark.parametrize("level, threshold", [(30, 30), ("warning", 30), (0, 0)])
    def test_levels_parse_like_configure(self, level: str | int, threshold: int) -> None:
        _runtime.configure(target="null")
        log = Logger()
        handler_id = log.add(io.StringIO(), level=level)
        try:
            assert log._handlers[handler_id].level == threshold
        finally:
            log.remove(handler_id)

    @pytest.mark.parametrize("level", ["TYPO", -1, True, 2.5])
    def test_invalid_levels_raise_before_registering(self, level: object) -> None:
        log = Logger()
        before = dict(core._sinks)
        with pytest.raises(ValueError, match="level must be"):
            log.add(io.StringIO(), level=level)  # type: ignore[arg-type]
        assert core._sinks == before
        assert log._handlers == {}


class TestFormattingDecision:
    def test_exception_formats_templates_like_error(self) -> None:
        _runtime.configure(target="memory")
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            try:
                raise ValueError("boom")
            except ValueError:
                Logger().exception("json: {{}} and {name}")
                Logger().exception("escaped only {{x}}")
                Logger().error("escaped only {{x}}", exc_info=True, stack_info=False)
                with Logger().catch(message="caught {{x}}"):
                    raise
        records = _memory_records()
        assert [record["message"] for record in records] == [
            "json: {{}} and {name}",
            "escaped only {{x}}",
            "escaped only {{x}}",
            "caught {{x}}",
        ]
        assert all("ValueError: boom" in record["exception"] for record in records)

    def test_control_keywords_alone_do_not_format(self) -> None:
        result = _safe_format("x {{y}}", (), {"exc_info": True, "stack_info": False})
        assert result == ("x {{y}}", frozenset())


class _Unprintable:
    def __str__(self) -> str:
        raise RuntimeError("private detail")


class _BadFormat:
    def __format__(self, spec: str) -> str:
        raise ZeroDivisionError


class TestLoggingNeverRaises:
    def test_unprintable_message_logs_a_placeholder(self) -> None:
        class Unprintable(Exception):
            def __str__(self) -> str:
                raise RuntimeError("private detail")

        _runtime.configure(target="memory")
        with pytest.warns(UserWarning, match=r"convert log message.*RuntimeError") as captured:
            Logger().error(Unprintable())
        assert "private detail" not in str(captured[0].message)
        _runtime.flush()
        line = _runtime.drain_messages()[-1]
        assert json.loads(line)["message"] == "<unprintable Unprintable object>"
        assert "private detail" not in line

    def test_raising_format_argument_keeps_the_template(self) -> None:
        class BadFormat:
            def __format__(self, spec: str) -> str:
                raise RuntimeError("private detail")

        _runtime.configure(target="memory")
        with pytest.warns(UserWarning, match="RuntimeError"):
            Logger().info("value {b}", b=BadFormat())
        assert _memory_records()[-1]["message"] == "value {b}"

    @pytest.mark.parametrize(
        ("message", "args", "kwargs", "expected"),
        [
            pytest.param(_Unprintable(), (), {}, "<unprintable _Unprintable object>", id="str"),
            pytest.param("{", (1,), {}, "{", id="malformed"),
            pytest.param("{a}", (), {"b": 1}, "{a}", id="missing-key"),
            pytest.param("{x}", (), {"x": _BadFormat()}, "{x}", id="raising-format"),
        ],
    )
    def test_warnings_as_errors_go_to_unraisablehook(
        self,
        monkeypatch: pytest.MonkeyPatch,
        message: object,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        expected: str,
    ) -> None:
        core._warn_unprintable_message.cache_clear()
        unraisable: list[BaseException | None] = []
        monkeypatch.setattr(sys, "unraisablehook", lambda hook: unraisable.append(hook.exc_value))
        _runtime.configure(target="memory")
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            Logger().info(message, *args, **kwargs)
        assert _memory_records()[-1]["message"] == expected
        assert len(unraisable) == 1
        assert isinstance(unraisable[0], UserWarning)
        assert "structguru: failed to" in str(unraisable[0])

    def test_reserved_parameter_names_become_fields(self) -> None:
        _runtime.configure(target="memory")
        log = Logger()
        log.info("plain", self=1)
        log.bind(self=2).warning("bound")
        with log.contextualize(self=3):
            log.error("contextual", message="ignored field")
        records = _memory_records()
        assert [(record["message"], record["self"]) for record in records] == [
            ("plain", 1),
            ("bound", 2),
            ("contextual", 3),
        ]


def test_long_templates_are_not_cached() -> None:
    import gc
    import tracemalloc

    core._extract_format_keys.cache_clear()
    tracemalloc.start()
    try:
        before = tracemalloc.get_traced_memory()[0]
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            for index in range(40):
                template = "x" * 100_000 + f" {{missing{index % 2}}}"
                _safe_format(template, (), {"other": index})
        gc.collect()
        retained = tracemalloc.get_traced_memory()[0] - before
    finally:
        tracemalloc.stop()
    assert retained < 1_000_000
    assert core._extract_format_keys.cache_info().currsize == 0
    assert len([w for w in caught if "failed to format" in str(w.message)]) == 2


class TestBraceEdgeCases:
    def test_nested_format_spec_fields_are_consumed(self) -> None:
        assert _safe_format("{x:{width}}", (), {"x": 1, "width": 5}) == (
            "    1",
            frozenset({"x", "width"}),
        )

    def test_escapes_are_formatted_whenever_arguments_are_present(self) -> None:
        assert _safe_format("a }} b", (), {"x": 1}) == ("a } b", frozenset())
        assert _safe_format("a }} b", (), {}) == ("a }} b", frozenset())

    def test_exc_info_placeholder_keeps_the_exception(self) -> None:
        _runtime.configure(target="memory")
        try:
            raise ValueError("kept")
        except ValueError:
            Logger().error("val {exc_info} {x}", exc_info=True, x=1)
        record = _memory_records()[-1]
        assert record["message"] == "val True 1"
        assert "ValueError: kept" in record["exception"]


def test_stack_keeps_frames_of_modules_sharing_the_package_prefix() -> None:
    _runtime.configure(target="memory")
    namespace: dict[str, Any] = {"__name__": "structguru_ext", "logger": Logger()}
    exec("def helper():\n    logger.info('from ext', stack_info=True)\n", namespace)
    namespace["helper"]()
    record = _memory_records()[-1]
    assert record["logger"] == "structguru_ext"
    assert record["stack"].splitlines()[-1].strip() == 'File "<string>", line 2, in helper'
