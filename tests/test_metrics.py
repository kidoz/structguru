"""Tests for structguru.metrics."""

from __future__ import annotations

from structguru.metrics import MetricProcessor


class TestMetricProcessor:
    def test_counter_fires_on_match(self) -> None:
        calls: list[dict] = []
        proc = MetricProcessor()
        proc.counter("user.login", lambda ed: calls.append(ed))

        proc(None, "info", {"event": "user.login succeeded"})
        assert len(calls) == 1

    def test_counter_does_not_fire_on_mismatch(self) -> None:
        calls: list[dict] = []
        proc = MetricProcessor()
        proc.counter("user.login", lambda ed: calls.append(ed))

        proc(None, "info", {"event": "page.view"})
        assert len(calls) == 0

    def test_histogram_fires_with_value(self) -> None:
        observations: list[tuple[float, dict]] = []
        proc = MetricProcessor()
        proc.histogram("db.query", "duration_ms", lambda v, ed: observations.append((v, ed)))

        proc(None, "info", {"event": "db.query executed", "duration_ms": 42.5})
        assert len(observations) == 1
        assert observations[0][0] == 42.5

    def test_histogram_skips_missing_value_key(self) -> None:
        observations: list = []
        proc = MetricProcessor()
        proc.histogram("db.query", "duration_ms", lambda v, ed: observations.append(v))

        proc(None, "info", {"event": "db.query executed"})
        assert len(observations) == 0

    def test_chained_registration(self) -> None:
        proc = MetricProcessor()
        result = proc.counter("a", lambda ed: None).histogram("b", "v", lambda v, ed: None)
        assert result is proc

    def test_event_dict_passed_through(self) -> None:
        proc = MetricProcessor()
        proc.counter("x", lambda ed: None)
        ed: dict = {"event": "x happened", "key": "val"}
        result = proc(None, "info", ed)
        assert result is ed

    def test_histogram_skips_non_numeric_value(self) -> None:
        observations: list = []
        proc = MetricProcessor()
        proc.histogram("db.query", "duration_ms", lambda v, ed: observations.append(v))

        result = proc(None, "info", {"event": "db.query executed", "duration_ms": "not-a-number"})
        assert len(observations) == 0
        assert "event" in result  # event dict still passed through
