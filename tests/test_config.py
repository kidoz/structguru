"""Tests for structguru.config (v1.0 native-shim API)."""

from __future__ import annotations

import io
import logging
from unittest.mock import patch

import pytest

import structguru
import structguru.config as config_module
from structguru import _native
from structguru.config import _to_logging_level, configure_structlog


def test_setup_structlog_is_fully_removed() -> None:
    assert not hasattr(config_module, "setup_structlog")
    with pytest.raises(ImportError):
        exec("from structguru import setup_structlog")


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
    def test_emits_deprecation_warning(self) -> None:
        with patch("structguru.config._configure"):
            with pytest.warns(DeprecationWarning, match="removed in v2.0"):
                configure_structlog(stream=io.StringIO())

    def test_delegates_to_configure(self) -> None:
        buf = io.StringIO()
        with patch("structguru.config._configure") as mock_configure:
            configure_structlog(service="svc", level="DEBUG", json_logs=False, stream=buf)

        mock_configure.assert_called_once_with(
            service="svc",
            level="DEBUG",
            json=False,
            target="null",
            stream_sink=buf,
        )

    def test_json_output(self) -> None:
        buf = io.StringIO()
        configure_structlog(service="testsvc", level="DEBUG", json_logs=True, stream=buf)

        structguru.logger.info("hello")
        _native.flush_native()
        output = buf.getvalue()
        assert '"message"' in output
        assert '"testsvc"' in output
        assert len(output.splitlines()) == 1
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
