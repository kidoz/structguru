"""Tests for structguru.config."""

from __future__ import annotations

import logging

import pytest

import structguru
import structguru.config as config_module
from structguru.config import _to_logging_level


def test_configure_structlog_is_fully_removed() -> None:
    assert not hasattr(config_module, "configure_structlog")
    assert not hasattr(structguru, "configure_structlog")
    assert "configure_structlog" not in structguru.__all__


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
