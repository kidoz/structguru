"""Tests for structguru.processors."""

from __future__ import annotations

from structguru.processors import (
    _LEVEL_MAP,
    _SEVERITY_MAP,
    add_service,
    add_syslog_severity,
    ensure_event_is_str,
    normalize_level,
)


class TestLevelMap:
    def test_all_loguru_levels_mapped(self) -> None:
        for level in (
            "trace",
            "debug",
            "info",
            "success",
            "warning",
            "warn",
            "error",
            "critical",
            "fatal",
            "exception",
        ):
            assert level in _LEVEL_MAP

    def test_canonical_values(self) -> None:
        assert _LEVEL_MAP["trace"] == "DEBUG"
        assert _LEVEL_MAP["success"] == "INFO"
        assert _LEVEL_MAP["warning"] == "WARN"
        assert _LEVEL_MAP["fatal"] == "CRITICAL"
        assert _LEVEL_MAP["exception"] == "ERROR"


class TestSeverityMap:
    def test_all_canonical_levels(self) -> None:
        assert set(_SEVERITY_MAP.keys()) == {"DEBUG", "INFO", "WARN", "ERROR", "CRITICAL"}

    def test_rfc5424_values(self) -> None:
        assert _SEVERITY_MAP["DEBUG"] == 7
        assert _SEVERITY_MAP["INFO"] == 6
        assert _SEVERITY_MAP["WARN"] == 4
        assert _SEVERITY_MAP["ERROR"] == 3
        assert _SEVERITY_MAP["CRITICAL"] == 2


class TestAddService:
    def test_adds_service_field(self) -> None:
        processor = add_service("myapp")
        event_dict: dict = {}
        result = processor(None, "info", event_dict)
        assert result["service"] == "myapp"

    def test_does_not_overwrite_existing(self) -> None:
        processor = add_service("myapp")
        event_dict: dict = {"service": "other"}
        result = processor(None, "info", event_dict)
        assert result["service"] == "other"


class TestNormalizeLevel:
    def test_normalizes_known_levels(self) -> None:
        for raw, expected in [
            ("debug", "DEBUG"),
            ("warning", "WARN"),
            ("fatal", "CRITICAL"),
            ("exception", "ERROR"),
        ]:
            event_dict: dict = {"level": raw}
            result = normalize_level(None, raw, event_dict)
            assert result["level"] == expected

    def test_falls_back_to_method_name(self) -> None:
        event_dict: dict = {}
        result = normalize_level(None, "info", event_dict)
        assert result["level"] == "INFO"

    def test_unknown_level_uppercased(self) -> None:
        event_dict: dict = {"level": "custom"}
        result = normalize_level(None, "custom", event_dict)
        assert result["level"] == "CUSTOM"


class TestAddSyslogSeverity:
    def test_maps_known_levels(self) -> None:
        for level, code in _SEVERITY_MAP.items():
            event_dict: dict = {"level": level}
            result = add_syslog_severity(None, "info", event_dict)
            assert result["severity"] == code

    def test_defaults_to_6_for_unknown(self) -> None:
        event_dict: dict = {"level": "CUSTOM"}
        result = add_syslog_severity(None, "info", event_dict)
        assert result["severity"] == 6

    def test_defaults_to_info_when_missing(self) -> None:
        event_dict: dict = {}
        result = add_syslog_severity(None, "info", event_dict)
        assert result["severity"] == 6


class TestEnsureEventIsStr:
    def test_converts_non_string(self) -> None:
        event_dict: dict = {"event": 42}
        result = ensure_event_is_str(None, "info", event_dict)
        assert result["event"] == "42"

    def test_preserves_string(self) -> None:
        event_dict: dict = {"event": "hello"}
        result = ensure_event_is_str(None, "info", event_dict)
        assert result["event"] == "hello"

    def test_ignores_none(self) -> None:
        event_dict: dict = {"event": None}
        result = ensure_event_is_str(None, "info", event_dict)
        assert result["event"] is None

    def test_handles_missing_event(self) -> None:
        event_dict: dict = {}
        result = ensure_event_is_str(None, "info", event_dict)
        assert "event" not in result
