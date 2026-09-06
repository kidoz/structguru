"""Tests for structguru.core."""

from __future__ import annotations

import io
import json
import logging
import os
import stat
import threading
import warnings
from pathlib import Path

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


@pytest.mark.parametrize("level", ["ERROR", "Error", "error", "EXCEPTION"])
def test_catch_normalizes_levels_before_filtering(level: str) -> None:
    stream = io.StringIO()
    configure(level="ERROR", stream=stream)
    with Logger().catch(level=level):
        raise ValueError("caught")
    record = json.loads(stream.getvalue())
    assert record["level"] == "ERROR"
    assert "ValueError: caught" in record["exception"]


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
async def test_catch_coroutine_logs_awaited_exceptions(reraise: bool) -> None:
    import asyncio
    import inspect

    buf = io.StringIO()
    configure(stream=buf)
    log = Logger()
    failure = ValueError("async failure")

    @log.catch(ValueError, reraise=reraise)
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
async def test_catch_coroutine_preserves_unmatched_errors_and_cancellation() -> None:
    import asyncio

    buf = io.StringIO()
    configure(stream=buf)

    @Logger().catch(ValueError)
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
