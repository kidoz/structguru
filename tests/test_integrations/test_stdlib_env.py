"""Tests for stdlib integration environment parsing."""

from __future__ import annotations

import pytest

from structguru.integrations._stdlib_env import (
    optional_bool_from_env,
    stdlib_bridge_config_from_env,
)


@pytest.mark.parametrize("value", ["1", "true", "TRUE", " yes ", "On"])
def test_optional_bool_accepts_true_values(value: str) -> None:
    assert optional_bool_from_env({"SETTING": value}, "SETTING") is True


@pytest.mark.parametrize("value", ["0", "false", "FALSE", " no ", "Off"])
def test_optional_bool_accepts_false_values(value: str) -> None:
    assert optional_bool_from_env({"SETTING": value}, "SETTING") is False


def test_optional_bool_returns_none_when_missing() -> None:
    assert optional_bool_from_env({}, "SETTING") is None


@pytest.mark.parametrize("value", ["", "   "])
def test_optional_bool_treats_empty_values_as_missing(value: str) -> None:
    assert optional_bool_from_env({"SETTING": value}, "SETTING") is None


@pytest.mark.parametrize("value", ["maybe", "2"])
def test_optional_bool_rejects_invalid_values(value: str) -> None:
    with pytest.raises(ValueError, match="SETTING must be one of"):
        optional_bool_from_env({"SETTING": value}, "SETTING")


def test_stdlib_env_defaults() -> None:
    config = stdlib_bridge_config_from_env({})

    assert config.level == "INFO"
    assert config.suppress_loggers == ()
    assert config.suppress_level == "WARNING"
    assert config.clear_handlers
    assert config.disable_existing_loggers is None
    assert not config.replace


def test_stdlib_env_uses_log_level_fallback() -> None:
    config = stdlib_bridge_config_from_env({"LOG_LEVEL": "ERROR"})

    assert config.level == "ERROR"


def test_stdlib_env_specific_level_overrides_log_level() -> None:
    config = stdlib_bridge_config_from_env(
        {"LOG_LEVEL": "ERROR", "STRUCTGURU_STDLIB_LEVEL": "DEBUG"}
    )

    assert config.level == "DEBUG"


def test_stdlib_env_parses_and_deduplicates_logger_names() -> None:
    config = stdlib_bridge_config_from_env(
        {"STRUCTGURU_STDLIB_SUPPRESS_LOGGERS": " urllib3, botocore,urllib3,, "}
    )

    assert config.suppress_loggers == ("urllib3", "botocore")


def test_stdlib_env_parses_all_options() -> None:
    config = stdlib_bridge_config_from_env(
        {
            "STRUCTGURU_STDLIB_LEVEL": "DEBUG",
            "STRUCTGURU_STDLIB_SUPPRESS_LEVEL": "ERROR",
            "STRUCTGURU_STDLIB_CLEAR_HANDLERS": "off",
            "STRUCTGURU_STDLIB_DISABLE_EXISTING_LOGGERS": "on",
            "STRUCTGURU_STDLIB_REPLACE": "1",
        }
    )

    assert config.level == "DEBUG"
    assert config.suppress_level == "ERROR"
    assert not config.clear_handlers
    assert config.disable_existing_loggers is True
    assert config.replace
