"""The level table is owned by the Rust core and mirrored once into Python."""

from __future__ import annotations

import logging

import structguru._rust as rust

from structguru import _levels
from structguru.config import _to_logging_level
from structguru.settings import _level_number


def test_python_lookups_mirror_the_native_table() -> None:
    table = rust.level_table()

    assert _levels.METHOD_LEVELS == dict(table)
    assert _levels.LEVELS == {"NOTSET": 0, **{name.upper(): num for name, num in table}}
    assert list(_levels.LEVELS)[:4] == ["NOTSET", "TRACE", "DEBUG", "INFO"]


def test_method_levels_match_the_logging_module() -> None:
    assert _levels.METHOD_LEVELS["debug"] == logging.DEBUG
    assert _levels.METHOD_LEVELS["info"] == _levels.METHOD_LEVELS["success"] == logging.INFO
    assert _levels.METHOD_LEVELS["warning"] == _levels.METHOD_LEVELS["warn"] == logging.WARNING
    assert _levels.METHOD_LEVELS["error"] == _levels.METHOD_LEVELS["exception"] == logging.ERROR
    assert _levels.METHOD_LEVELS["critical"] == _levels.METHOD_LEVELS["fatal"] == logging.CRITICAL
    assert _levels.METHOD_LEVELS["trace"] < logging.DEBUG
    assert "notset" not in _levels.METHOD_LEVELS


def test_settings_and_config_helpers_read_the_same_table() -> None:
    for name, number in rust.level_table():
        assert _level_number(name) == number
        assert _level_number(name.upper()) == number
        assert _to_logging_level(name) == number


def test_method_for_level_number_prefers_the_canonical_method() -> None:
    assert _levels.method_for_level_number(logging.CRITICAL + 10) == "critical"
    assert _levels.method_for_level_number(logging.CRITICAL) == "critical"
    assert _levels.method_for_level_number(logging.ERROR + 5) == "error"
    assert _levels.method_for_level_number(logging.WARNING) == "warning"
    assert _levels.method_for_level_number(logging.INFO + 1) == "info"
    assert _levels.method_for_level_number(logging.DEBUG) == "debug"
    assert _levels.method_for_level_number(5) == "trace"
    assert _levels.method_for_level_number(1) == "debug"
