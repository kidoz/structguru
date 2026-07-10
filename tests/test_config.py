"""Tests for structguru.config (v1.0 native-shim API)."""

from __future__ import annotations

import io
import logging
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import structguru
from structguru import _native
from structguru.config import (
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
        with pytest.warns(UserWarning, match="Unknown log level"):
            assert _to_logging_level("CUSTOM") == logging.INFO


class TestConfigureStructlog:
    def test_json_output(self) -> None:
        buf = io.StringIO()
        configure_structlog(service="testsvc", level="DEBUG", json_logs=True, stream=buf)

        structguru.logger.info("hello")
        _native.flush_native()
        output = buf.getvalue()
        assert '"message"' in output
        assert '"testsvc"' in output
        _native.disable_native()

    def test_console_output(self) -> None:
        buf = io.StringIO()
        configure_structlog(service="testsvc", level="DEBUG", json_logs=False, stream=buf)

        structguru.logger.info("hello")
        _native.flush_native()
        assert "hello" in buf.getvalue()
        _native.disable_native()

    def test_level_filtering(self) -> None:
        buf = io.StringIO()
        configure_structlog(service="app", level="WARNING", json_logs=True, stream=buf)

        structguru.logger.info("should not appear")
        _native.flush_native()
        assert buf.getvalue() == ""

        structguru.logger.warning("should appear")
        _native.flush_native()
        assert "should appear" in buf.getvalue()
        _native.disable_native()


class TestSetupStructlog:
    def test_suppresses_loggers(self) -> None:
        with patch.dict("os.environ", {"LOG_LEVEL": "INFO", "JSON_LOGS": "1"}, clear=True):
            setup_structlog(service="myapp", suppress_loggers=("noisy_lib",))
        try:
            assert logging.getLogger("noisy_lib").level == logging.WARNING
        finally:
            _native.disable_native()

    def test_env_log_level(self) -> None:
        with patch.dict("os.environ", {"LOG_LEVEL": "DEBUG", "JSON_LOGS": "1"}, clear=True):
            setup_structlog(service="myapp")
        try:
            assert _native.is_native_enabled()
        finally:
            _native.disable_native()

    def test_log_path_creates_native_file_sink(self, tmp_path: Path) -> None:
        log_file = tmp_path / "app.log"
        env = {"LOG_LEVEL": "INFO", "JSON_LOGS": "1", "LOG_PATH": str(log_file)}
        with patch.dict("os.environ", env, clear=True):
            setup_structlog(service="myapp")
            try:
                structguru.logger.info("hello-from-log-path")
                _native.flush_native()
                contents = log_file.read_text(encoding="utf-8")
                assert "hello-from-log-path" in contents
                assert '"service":"myapp"' in contents
            finally:
                _native.disable_native()

    def test_excepthook_logs_uncaught_exception(self) -> None:
        with patch.dict("os.environ", {"LOG_LEVEL": "ERROR", "JSON_LOGS": "1"}, clear=True):
            setup_structlog(service="myapp")
        try:
            try:
                raise RuntimeError("kaboom")
            except RuntimeError:
                sys.excepthook(*sys.exc_info())  # type: ignore[misc]
        finally:
            _native.disable_native()

    def test_excepthook_passthrough_on_keyboard_interrupt(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            setup_structlog(service="myapp")
        _native.disable_native()

        called: list[tuple[type[BaseException], BaseException, object]] = []
        original = sys.__excepthook__
        sys.__excepthook__ = lambda et, ev, tb: called.append((et, ev, tb))  # type: ignore[assignment]
        try:
            exc = KeyboardInterrupt()
            sys.excepthook(KeyboardInterrupt, exc, None)  # type: ignore[arg-type]
        finally:
            sys.__excepthook__ = original  # type: ignore[assignment]

        assert called and called[0][0] is KeyboardInterrupt
