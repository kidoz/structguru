"""Tests for structguru.config."""

from __future__ import annotations

import io
import logging
from unittest.mock import patch

import structlog

from structguru.config import (
    _install_exc_info_record_factory,
    _stream_isatty,
    _StructlogMsgFixer,
    _to_logging_level,
    configure_structlog,
    setup_structlog,
)


class TestToLoggingLevel:
    def test_standard_levels(self) -> None:
        assert _to_logging_level("DEBUG") == logging.DEBUG
        assert _to_logging_level("INFO") == logging.INFO
        assert _to_logging_level("WARNING") == logging.WARNING
        assert _to_logging_level("ERROR") == logging.ERROR
        assert _to_logging_level("CRITICAL") == logging.CRITICAL

    def test_warn_alias(self) -> None:
        assert _to_logging_level("WARN") == logging.WARNING
        assert _to_logging_level("warn") == logging.WARNING

    def test_case_insensitive(self) -> None:
        assert _to_logging_level("debug") == logging.DEBUG
        assert _to_logging_level("Info") == logging.INFO

    def test_unknown_defaults_to_info(self) -> None:
        assert _to_logging_level("CUSTOM") == logging.INFO


class TestStreamIsatty:
    def test_tty_stream(self) -> None:
        class FakeTTY:
            def isatty(self) -> bool:
                return True

        assert _stream_isatty(FakeTTY()) is True

    def test_non_tty_stream(self) -> None:
        assert _stream_isatty(io.StringIO()) is False

    def test_no_isatty_method(self) -> None:
        assert _stream_isatty(object()) is False

    def test_value_error_from_isatty(self) -> None:
        class BrokenStream:
            def isatty(self) -> bool:
                raise ValueError("closed")

        assert _stream_isatty(BrokenStream()) is False


class TestStructlogMsgFixer:
    def test_fixes_dict_msg_with_message_key(self) -> None:
        handler = _StructlogMsgFixer()
        record = logging.LogRecord("test", logging.INFO, "", 0, {"message": "hello"}, None, None)
        handler.emit(record)
        assert record.msg == "hello"

    def test_fixes_dict_msg_with_event_key(self) -> None:
        handler = _StructlogMsgFixer()
        record = logging.LogRecord("test", logging.INFO, "", 0, {"event": "world"}, None, None)
        handler.emit(record)
        assert record.msg == "world"

    def test_leaves_string_msg_alone(self) -> None:
        handler = _StructlogMsgFixer()
        record = logging.LogRecord("test", logging.INFO, "", 0, "already string", None, None)
        handler.emit(record)
        assert record.msg == "already string"


class TestInstallExcInfoRecordFactory:
    def test_idempotent(self) -> None:
        _install_exc_info_record_factory()
        first = logging.getLogRecordFactory()
        _install_exc_info_record_factory()
        second = logging.getLogRecordFactory()
        assert first is second

    def test_propagates_exc_info_true(self) -> None:
        _install_exc_info_record_factory()
        factory = logging.getLogRecordFactory()
        try:
            raise RuntimeError("test")
        except RuntimeError:
            record = factory("test", logging.ERROR, "", 0, {"exc_info": True}, None, None)
            assert record.exc_info is not None
            assert record.exc_info[0] is RuntimeError

    def test_propagates_exception_instance(self) -> None:
        _install_exc_info_record_factory()
        factory = logging.getLogRecordFactory()
        exc = ValueError("oops")
        record = factory("test", logging.ERROR, "", 0, {"exc_info": exc}, None, None)
        assert record.exc_info is not None
        assert record.exc_info[1] is exc

    def test_no_exc_info_no_change(self) -> None:
        _install_exc_info_record_factory()
        factory = logging.getLogRecordFactory()
        record = factory("test", logging.INFO, "", 0, "plain msg", None, None)
        assert record.exc_info is None


class TestConfigureStructlog:
    def test_json_output(self) -> None:
        buf = io.StringIO()
        configure_structlog(service="testsvc", level="DEBUG", json_logs=True, stream=buf)

        log = structlog.get_logger("test")
        log.info("hello")
        output = buf.getvalue()
        assert '"message"' in output
        assert '"testsvc"' in output

    def test_console_output(self) -> None:
        buf = io.StringIO()
        configure_structlog(service="testsvc", level="DEBUG", json_logs=False, stream=buf)

        log = structlog.get_logger("test")
        log.info("hello")
        output = buf.getvalue()
        assert "hello" in output

    def test_level_filtering(self) -> None:
        buf = io.StringIO()
        configure_structlog(service="app", level="WARNING", json_logs=True, stream=buf)

        log = structlog.get_logger("test")
        log.info("should not appear")
        assert buf.getvalue() == ""

        log.warning("should appear")
        assert "should appear" in buf.getvalue()

    def test_sets_root_logger_level(self) -> None:
        buf = io.StringIO()
        configure_structlog(service="app", level="ERROR", stream=buf)
        assert logging.getLogger().level == logging.ERROR


class TestSetupStructlog:
    def test_default_setup(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            setup_structlog(service="myapp")
        root = logging.getLogger()
        assert root.level == logging.INFO

    def test_suppresses_loggers(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            setup_structlog(service="myapp", suppress_loggers=("noisy_lib",))
        assert logging.getLogger("noisy_lib").level == logging.WARNING

    def test_env_log_level(self) -> None:
        with patch.dict("os.environ", {"LOG_LEVEL": "DEBUG"}, clear=True):
            setup_structlog(service="myapp")
        assert logging.getLogger().level == logging.DEBUG
