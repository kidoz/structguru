"""Tests for structguru.routing."""

from __future__ import annotations

from typing import Any

import pytest

from structguru.routing import ConditionalProcessor


def _add_marker(_logger: Any, _method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    event_dict["marked"] = True
    return event_dict


class TestConditionalProcessor:
    def test_applies_when_level_in_range(self) -> None:
        proc = ConditionalProcessor(_add_marker, min_level="ERROR", max_level="CRITICAL")
        ed: dict = {"event": "test"}
        result = proc(None, "error", ed)
        assert result["marked"] is True

    def test_skips_when_level_below_range(self) -> None:
        proc = ConditionalProcessor(_add_marker, min_level="ERROR", max_level="CRITICAL")
        ed: dict = {"event": "test"}
        result = proc(None, "debug", ed)
        assert "marked" not in result

    def test_skips_when_level_above_range(self) -> None:
        proc = ConditionalProcessor(_add_marker, min_level="DEBUG", max_level="INFO")
        ed: dict = {"event": "test"}
        result = proc(None, "error", ed)
        assert "marked" not in result

    def test_default_range_applies_to_all(self) -> None:
        proc = ConditionalProcessor(_add_marker)
        for method in ("debug", "info", "warning", "error", "critical"):
            ed: dict = {"event": "test"}
            result = proc(None, method, ed)
            assert result["marked"] is True

    def test_warn_level_supported(self) -> None:
        proc = ConditionalProcessor(_add_marker, min_level="WARN", max_level="WARN")
        ed: dict = {"event": "test"}
        result = proc(None, "warning", ed)
        assert result["marked"] is True

    def test_min_greater_than_max_raises(self) -> None:
        with pytest.raises(ValueError, match="min_level"):
            ConditionalProcessor(_add_marker, min_level="ERROR", max_level="DEBUG")
