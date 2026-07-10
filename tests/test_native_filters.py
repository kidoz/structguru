"""Native-path parity tests for value-pattern redaction and sampling/rate-limit filters.

Mirrors the cases in ``test_redaction.py`` and ``test_sampling.py`` but exercises
the Rust native fast path via ``configure(target="memory")`` + ``drain_messages()``.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

import structguru
from structguru import _native

pytestmark = pytest.mark.skipif(
    not _native.native_available(),
    reason="native extension not built",
)


def _drain_last() -> dict[str, Any]:
    """Flush the writer and return the last rendered record as a dict."""
    _native.flush_native()
    return json.loads(_native.drain_messages()[-1])


# -- value-pattern redaction -------------------------------------------------


def test_pattern_redacts_matching_substring_in_string_value() -> None:
    email = r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
    _native.configure(service="svc", target="memory", level="DEBUG", sensitive_patterns=[email])
    try:
        structguru.logger.info("msg", body="Contact user@example.com for details")
        record = _drain_last()
        assert record["body"] == "Contact [REDACTED] for details"
        assert "user@example.com" not in record["body"]
    finally:
        _native.disable_native()


def test_pattern_redaction_descends_into_nested_maps_and_lists() -> None:
    ssn = r"\b\d{3}-\d{2}-\d{4}\b"
    _native.configure(service="svc", target="memory", level="DEBUG", sensitive_patterns=[ssn])
    try:
        structguru.logger.info(
            "m",
            ctx={"note": "ssn is 123-45-6789"},
            tags=["ok 111-22-3333", 7],
        )
        record = _drain_last()
        assert record["ctx"]["note"] == "ssn is [REDACTED]"
        assert record["tags"][0] == "ok [REDACTED]"
        assert record["tags"][1] == 7
    finally:
        _native.disable_native()


def test_pattern_redaction_skips_non_string_values() -> None:
    # A pattern that would match anything; non-strings must be untouched.
    _native.configure(service="svc", target="memory", level="DEBUG", sensitive_patterns=[r".+"])
    try:
        structguru.logger.info("m", count=42, flag=True, ratio=1.5, nothing=None)
        record = _drain_last()
        assert record["count"] == 42
        assert record["flag"] is True
        assert record["ratio"] == 1.5
        assert record["nothing"] is None
    finally:
        _native.disable_native()


def test_key_and_pattern_redaction_combine() -> None:
    _native.configure(
        service="svc",
        target="memory",
        level="DEBUG",
        sensitive_patterns=[r"leak@x\.io"],
    )
    try:
        structguru.logger.info("m", token="abc", msg_field="ping leak@x.io now")
        record = _drain_last()
        assert record["token"] == "[REDACTED]"
        assert record["msg_field"] == "ping [REDACTED] now"
    finally:
        _native.disable_native()


def test_multiple_patterns_apply_in_order() -> None:
    _native.configure(
        service="svc",
        target="memory",
        level="DEBUG",
        sensitive_patterns=[r"a@b\.com", r"secret"],
    )
    try:
        structguru.logger.info("m", body="secret email a@b.com here")
        record = _drain_last()
        assert record["body"] == "[REDACTED] email [REDACTED] here"
    finally:
        _native.disable_native()


def test_pattern_replacement_expands_capture_groups() -> None:
    """The look-behind rewrite: `(?<=password=)\\S+` becomes `password=(\\S+)`
    with a group-preserving replacement — same output, linear-time engine."""
    _native.configure(
        service="svc",
        target="memory",
        level="DEBUG",
        sensitive_patterns=[r"(password=)\S+"],
        pattern_replacement="$1[REDACTED]",
    )
    try:
        structguru.logger.info("m", body="login with password=hunter2 ok")
        record = _drain_last()
        assert record["body"] == "login with password=[REDACTED] ok"
        assert "hunter2" not in record["body"]
    finally:
        _native.disable_native()


def test_pattern_replacement_custom_literal() -> None:
    _native.configure(
        service="svc",
        target="memory",
        level="DEBUG",
        sensitive_patterns=[r"\b\d{3}-\d{2}-\d{4}\b"],
        pattern_replacement="***",
    )
    try:
        structguru.logger.info("m", body="ssn is 123-45-6789")
        record = _drain_last()
        assert record["body"] == "ssn is ***"
    finally:
        _native.disable_native()


@pytest.mark.parametrize(
    "pattern",
    [
        pytest.param(r"(?<=foo)bar", id="lookbehind"),
        pytest.param(r"foo(?!bar)", id="negative-lookahead"),
        pytest.param(r"(\w+) \1", id="backreference"),
    ],
)
def test_unsupported_pattern_raises_at_setup(pattern: str) -> None:
    """Backreferences/look-around are rejected loudly at enable time: Rust's
    regex engine guarantees linear-time matching and does not support them,
    and redaction that silently differs from the configuration is worse than
    an error. The message includes rewrite guidance."""
    with pytest.raises(ValueError, match="unsupported sensitive_patterns regex"):
        _native.configure(
            service="svc",
            target="memory",
            level="DEBUG",
            sensitive_patterns=[pattern],
        )
    assert not _native.is_native_enabled()


# -- sampling ---------------------------------------------------------------


def test_sampler_rate_one_keeps_all() -> None:
    _native.configure(service="svc", target="memory", level="DEBUG", sample_rate=1.0)
    try:
        for _ in range(100):
            structguru.logger.info("kept")
        _native.flush_native()
        assert len(_native.drain_messages()) == 100
    finally:
        _native.disable_native()


def test_sampler_rate_zero_drops_all() -> None:
    _native.configure(service="svc", target="memory", level="DEBUG", sample_rate=0.0)
    try:
        for _ in range(50):
            structguru.logger.info("dropped")
        _native.flush_native()
        assert len(_native.drain_messages()) == 0
        assert _native.native_metrics()["sampled"] == 50
    finally:
        _native.disable_native()


def test_sampler_rate_half_is_statistically_bounded() -> None:
    _native.configure(service="svc", target="memory", level="DEBUG", sample_rate=0.5)
    try:
        for _ in range(1000):
            structguru.logger.info("stat")
        _native.flush_native()
        kept = len(_native.drain_messages())
        assert 300 < kept < 700, f"kept={kept}"
    finally:
        _native.disable_native()


def test_invalid_sample_rate_raises() -> None:
    for bad in (1.5, -0.1, float("nan")):
        with pytest.raises(ValueError, match="sample_rate"):
            _native.configure(sample_rate=bad)
    assert not _native.is_native_enabled()


# -- rate limiting ----------------------------------------------------------


def test_rate_limit_drops_over_threshold() -> None:
    _native.configure(
        service="svc", target="memory", level="DEBUG", rate_limit_max=3, rate_limit_period=60.0
    )
    try:
        for _ in range(4):
            structguru.logger.info("same")
        _native.flush_native()
        lines = _native.drain_messages()
        assert len(lines) == 3
        assert _native.native_metrics()["rate_limited"] == 1
    finally:
        _native.disable_native()


def test_rate_limit_keys_are_independent() -> None:
    _native.configure(
        service="svc", target="memory", level="DEBUG", rate_limit_max=1, rate_limit_period=60.0
    )
    try:
        structguru.logger.info("alpha")
        structguru.logger.info("beta")
        structguru.logger.info("alpha")  # dropped: alpha exhausted
        _native.flush_native()
        messages = [json.loads(line)["message"] for line in _native.drain_messages()]
        assert messages == ["alpha", "beta"]
        assert _native.native_metrics()["rate_limited"] == 1
    finally:
        _native.disable_native()


def test_rate_limit_window_expires() -> None:
    _native.configure(
        service="svc",
        target="memory",
        level="DEBUG",
        rate_limit_max=1,
        rate_limit_period=0.05,
    )
    try:
        structguru.logger.info("k")
        structguru.logger.info("k")  # dropped within window
        time.sleep(0.06)
        structguru.logger.info("k")  # allowed after expiry
        _native.flush_native()
        assert len(_native.drain_messages()) == 2
    finally:
        _native.disable_native()


def test_invalid_rate_limit_raises() -> None:
    with pytest.raises(ValueError, match="rate_limit_max"):
        _native.configure(rate_limit_max=0)
    with pytest.raises(ValueError, match="rate_limit_period"):
        _native.configure(rate_limit_max=5, rate_limit_period=0.0)
    assert not _native.is_native_enabled()


# -- combined ---------------------------------------------------------------


def test_patterns_and_sampling_combine() -> None:
    _native.configure(
        service="svc",
        target="memory",
        level="DEBUG",
        sensitive_patterns=[r"pw=\w+"],
        sample_rate=0.0,
    )
    try:
        structguru.logger.info("m", body="auth pw=hunter2 ok")
        _native.flush_native()
        # sample_rate=0 drops everything; nothing is rendered.
        assert len(_native.drain_messages()) == 0
        assert _native.native_metrics()["sampled"] == 1
    finally:
        _native.disable_native()


def test_env_var_sample_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STRUCTGURU_NATIVE", "1")
    monkeypatch.setenv("STRUCTGURU_NATIVE_TARGET", "memory")
    monkeypatch.setenv("STRUCTGURU_NATIVE_SAMPLE_RATE", "0.0")
    _native.disable_native()
    _native._maybe_configure_from_env()
    try:
        assert _native.is_native_enabled()
        structguru.logger.info("dropped")
        _native.flush_native()
        assert len(_native.drain_messages()) == 0
    finally:
        _native.disable_native()
        monkeypatch.delenv("STRUCTGURU_NATIVE", raising=False)
        monkeypatch.delenv("STRUCTGURU_NATIVE_SAMPLE_RATE", raising=False)


def test_env_var_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STRUCTGURU_NATIVE", "1")
    monkeypatch.setenv("STRUCTGURU_NATIVE_TARGET", "memory")
    monkeypatch.setenv("STRUCTGURU_NATIVE_RATE_LIMIT", "2/60")
    _native.disable_native()
    _native._maybe_configure_from_env()
    try:
        assert _native.is_native_enabled()
        for _ in range(3):
            structguru.logger.info("same")
        _native.flush_native()
        assert len(_native.drain_messages()) == 2
        assert _native.native_metrics()["rate_limited"] == 1
    finally:
        _native.disable_native()
        monkeypatch.delenv("STRUCTGURU_NATIVE", raising=False)
        monkeypatch.delenv("STRUCTGURU_NATIVE_RATE_LIMIT", raising=False)


# -- level-gated sampling (ConditionalProcessor analog) -----------------------


def test_sample_max_level_gates_sampling() -> None:
    _native.configure(
        service="svc",
        target="memory",
        level="DEBUG",
        sample_rate=0.0,
        sample_max_level="INFO",
    )
    try:
        structguru.logger.debug("sampled out")
        structguru.logger.info("sampled out too")
        structguru.logger.warning("always kept")
        structguru.logger.error("always kept too")
        _native.flush_native()
        lines = _native.drain_messages()
        assert len(lines) == 2
        assert json.loads(lines[0])["level"] == "WARN"
        assert json.loads(lines[1])["level"] == "ERROR"
        metrics = _native.native_metrics()
        assert metrics is not None
        assert metrics["sampled"] == 2
    finally:
        _native.disable_native()


def test_invalid_sample_max_level_raises() -> None:
    with pytest.raises(ValueError, match="sample_max_level"):
        _native.configure(target="memory", sample_rate=0.5, sample_max_level="bogus")


# -- metric hooks --------------------------------------------------------------


def test_metric_processor_invoked_per_kept_record() -> None:
    from structguru.metrics import MetricProcessor

    seen: list[dict[str, Any]] = []
    values: list[float] = []
    metrics = MetricProcessor()
    metrics.counter("user.login", seen.append)
    metrics.histogram("db.query", "duration_ms", lambda v, _ed: values.append(v))

    _native.configure(service="svc", target="memory", metric_processor=metrics)
    try:
        structguru.logger.info("user.login ok", user="alice")
        structguru.logger.info("db.query done", duration_ms=12.5)
        structguru.logger.info("unrelated")
    finally:
        _native.disable_native()

    assert len(seen) == 1
    assert seen[0]["event"] == "user.login ok"
    assert seen[0]["user"] == "alice"
    assert values == [12.5]


def test_metric_processor_not_invoked_for_dropped_records() -> None:
    calls: list[str] = []

    def hook(_logger: Any, method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        calls.append(method)
        return event_dict

    _native.configure(
        service="svc",
        target="memory",
        level="INFO",
        sample_rate=0.0,
        sample_max_level="INFO",
        metric_processor=hook,
    )
    try:
        structguru.logger.debug("below level threshold")
        structguru.logger.info("sampled out")
        structguru.logger.warning("kept")
    finally:
        _native.disable_native()

    assert calls == ["warning"]


def test_metric_processor_errors_are_swallowed() -> None:
    def hook(_logger: Any, _method: str, _event_dict: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("metrics backend down")

    _native.configure(service="svc", target="memory", metric_processor=hook)
    try:
        structguru.logger.info("still logged")
        record = _drain_last()
        assert record["message"] == "still logged"
    finally:
        _native.disable_native()


def test_non_callable_metric_processor_raises() -> None:
    with pytest.raises(TypeError, match="metric_processor"):
        _native.configure(target="memory", metric_processor="not-a-callable")
