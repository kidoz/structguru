"""Tests for structguru.processors."""

from __future__ import annotations

from structguru import _native
from structguru.processors import (
    _LEVEL_MAP,
    _SEVERITY_MAP,
    add_syslog_severity,
    normalize_level,
)


class _StringifiesToError:
    def __str__(self) -> str:
        return "ERROR"


def _assert_native_matches_fallback(
    monkeypatch,
    processor,
    method_name: str,
    event_dict: dict,
) -> None:
    native_result = processor(None, method_name, event_dict.copy())
    native_module = _native._RUST

    monkeypatch.setattr(_native, "_RUST", None)
    fallback_result = processor(None, method_name, event_dict.copy())
    monkeypatch.setattr(_native, "_RUST", native_module)

    assert native_result == fallback_result


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

    def test_falls_back_when_native_unavailable(self, monkeypatch) -> None:
        monkeypatch.setattr(_native, "_RUST", None)
        event_dict: dict = {"level": "warning"}
        result = normalize_level(None, "warning", event_dict)
        assert result["level"] == "WARN"

    def test_native_and_fallback_paths_match(self, monkeypatch) -> None:
        cases: list[tuple[str, dict]] = [
            ("info", {}),
            ("debug", {"level": "trace"}),
            ("info", {"level": "success"}),
            ("warning", {"level": "warning"}),
            ("error", {"level": "exception"}),
            ("critical", {"level": "fatal"}),
            ("custom", {"level": "notice"}),
            ("info", {"level": 42}),
            ("info", {"level": _StringifiesToError()}),
        ]
        for method_name, event_dict in cases:
            _assert_native_matches_fallback(monkeypatch, normalize_level, method_name, event_dict)


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

    def test_falls_back_when_native_unavailable(self, monkeypatch) -> None:
        monkeypatch.setattr(_native, "_RUST", None)
        event_dict: dict = {"level": "ERROR"}
        result = add_syslog_severity(None, "info", event_dict)
        assert result["severity"] == 3

    def test_native_and_fallback_paths_match(self, monkeypatch) -> None:
        cases: list[dict] = [
            {},
            {"level": "DEBUG"},
            {"level": "INFO"},
            {"level": "WARN"},
            {"level": "ERROR"},
            {"level": "CRITICAL"},
            {"level": "NOTICE"},
            {"level": 42},
            {"level": _StringifiesToError()},
        ]
        for event_dict in cases:
            _assert_native_matches_fallback(monkeypatch, add_syslog_severity, "info", event_dict)
