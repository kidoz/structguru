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
from structguru import _runtime

pytestmark = pytest.mark.skipif(
    not _runtime.is_available(),
    reason="native extension not built",
)


def _drain_last() -> dict[str, Any]:
    """Flush the writer and return the last rendered record as a dict."""
    _runtime.flush_native()
    return json.loads(_runtime.drain_messages()[-1])


# -- value-pattern redaction -------------------------------------------------


def test_pattern_redacts_matching_substring_in_string_value() -> None:
    email = r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
    _runtime.configure(service="svc", target="memory", level="DEBUG", sensitive_patterns=[email])
    try:
        structguru.logger.info("msg", body="Contact user@example.com for details")
        record = _drain_last()
        assert record["body"] == "Contact [REDACTED] for details"
        assert "user@example.com" not in record["body"]
    finally:
        _runtime.shutdown()


def test_pattern_redacts_matching_substring_in_message() -> None:
    _runtime.configure(
        service="svc",
        target="memory",
        level="DEBUG",
        sensitive_patterns=[r"secret=\w+"],
    )
    try:
        structguru.logger.info("token secret=abc")
        record = _drain_last()
    finally:
        _runtime.shutdown()

    assert record["message"] == "token [REDACTED]"


def test_pattern_redaction_descends_into_nested_maps_and_lists() -> None:
    ssn = r"\b\d{3}-\d{2}-\d{4}\b"
    _runtime.configure(service="svc", target="memory", level="DEBUG", sensitive_patterns=[ssn])
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
        _runtime.shutdown()


def test_pattern_redaction_skips_non_string_values() -> None:
    # A pattern that would match anything; non-strings must be untouched.
    _runtime.configure(service="svc", target="memory", level="DEBUG", sensitive_patterns=[r".+"])
    try:
        structguru.logger.info("m", count=42, flag=True, ratio=1.5, nothing=None)
        record = _drain_last()
        assert record["count"] == 42
        assert record["flag"] is True
        assert record["ratio"] == 1.5
        assert record["nothing"] is None
    finally:
        _runtime.shutdown()


def test_key_and_pattern_redaction_combine() -> None:
    _runtime.configure(
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
        _runtime.shutdown()


def test_multiple_patterns_apply_in_order() -> None:
    _runtime.configure(
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
        _runtime.shutdown()


def test_pattern_replacement_expands_capture_groups() -> None:
    """The look-behind rewrite: `(?<=password=)\\S+` becomes `password=(\\S+)`
    with a group-preserving replacement — same output, linear-time engine."""
    _runtime.configure(
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
        _runtime.shutdown()


def test_pattern_replacement_custom_literal() -> None:
    _runtime.configure(
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
        _runtime.shutdown()


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
        _runtime.configure(
            service="svc",
            target="memory",
            level="DEBUG",
            sensitive_patterns=[pattern],
        )
    assert not _runtime.is_native_enabled()


def test_unsupported_pattern_error_mentions_backtracking_opt_in() -> None:
    with pytest.raises(ValueError, match="allow_backtracking_patterns=True"):
        _runtime.configure(
            service="svc",
            target="memory",
            level="DEBUG",
            sensitive_patterns=[r"(?<=foo)bar"],
        )
    assert not _runtime.is_native_enabled()


@pytest.mark.parametrize(
    "pattern,body,expected",
    [
        pytest.param(
            r"(?<=password=)\S+",
            "login with password=hunter2 ok",
            "login with password=[REDACTED] ok",
            id="lookbehind",
        ),
        pytest.param(
            r"foo(?!bar)\w*",
            "foobar and foobaz",
            "foobar and [REDACTED]",
            id="negative-lookahead",
        ),
        pytest.param(
            r"\b(\w+) \1\b",
            "dup dup unique",
            "[REDACTED] unique",
            id="backreference",
        ),
    ],
)
def test_backtracking_opt_in_redacts_lookaround_and_backreferences(
    pattern: str, body: str, expected: str
) -> None:
    """`allow_backtracking_patterns=True` routes patterns the linear engine
    rejects through a bounded backtracking engine, so they work as written
    (at the cost of the linear-time guarantee for those patterns)."""
    _runtime.configure(
        service="svc",
        target="memory",
        level="DEBUG",
        sensitive_patterns=[pattern],
        allow_backtracking_patterns=True,
    )
    try:
        structguru.logger.info("m", body=body)
        record = _drain_last()
        assert record["body"] == expected
    finally:
        _runtime.shutdown()


def test_backtracking_opt_in_redacts_message() -> None:
    _runtime.configure(
        service="svc",
        target="memory",
        level="DEBUG",
        sensitive_patterns=[r"(?<=secret=)\w+"],
        allow_backtracking_patterns=True,
    )
    try:
        structguru.logger.info("token secret=abc")
        record = _drain_last()
    finally:
        _runtime.shutdown()

    assert record["message"] == "token secret=[REDACTED]"


def test_invalid_pattern_raises_even_with_backtracking_opt_in() -> None:
    with pytest.raises(ValueError, match="invalid sensitive_patterns regex"):
        _runtime.configure(
            service="svc",
            target="memory",
            level="DEBUG",
            sensitive_patterns=[r"(unclosed"],
            allow_backtracking_patterns=True,
        )
    assert not _runtime.is_native_enabled()


def test_huge_rate_limit_period_raises_not_crashes() -> None:
    # A finite-but-enormous period overflows Rust's Duration; it must surface as
    # a ValueError, not an uncatchable panic at configure()/import time.
    with pytest.raises(ValueError, match="rate_limit_period"):
        _runtime.configure(
            service="svc",
            target="memory",
            level="DEBUG",
            rate_limit_max=5,
            rate_limit_period=1e300,
        )
    assert not _runtime.is_native_enabled()


# -- sampling ---------------------------------------------------------------


def test_sampler_rate_one_keeps_all() -> None:
    _runtime.configure(service="svc", target="memory", level="DEBUG", sample_rate=1.0)
    try:
        for _ in range(100):
            structguru.logger.info("kept")
        _runtime.flush_native()
        assert len(_runtime.drain_messages()) == 100
    finally:
        _runtime.shutdown()


def test_sampler_rate_zero_drops_all() -> None:
    _runtime.configure(service="svc", target="memory", level="DEBUG", sample_rate=0.0)
    try:
        for _ in range(50):
            structguru.logger.info("dropped")
        _runtime.flush_native()
        assert len(_runtime.drain_messages()) == 0
        assert _runtime.writer_metrics()["sampled"] == 50
    finally:
        _runtime.shutdown()


def test_sampler_rate_half_is_statistically_bounded() -> None:
    _runtime.configure(service="svc", target="memory", level="DEBUG", sample_rate=0.5)
    try:
        for _ in range(1000):
            structguru.logger.info("stat")
        _runtime.flush_native()
        kept = len(_runtime.drain_messages())
        assert 300 < kept < 700, f"kept={kept}"
    finally:
        _runtime.shutdown()


def test_invalid_sample_rate_raises() -> None:
    for bad in (1.5, -0.1, float("nan")):
        with pytest.raises(ValueError, match="sample_rate"):
            _runtime.configure(sample_rate=bad)
    assert not _runtime.is_native_enabled()


# -- rate limiting ----------------------------------------------------------


def test_rate_limit_drops_over_threshold() -> None:
    _runtime.configure(
        service="svc", target="memory", level="DEBUG", rate_limit_max=3, rate_limit_period=60.0
    )
    try:
        for _ in range(4):
            structguru.logger.info("same")
        _runtime.flush_native()
        lines = _runtime.drain_messages()
        assert len(lines) == 3
        assert _runtime.writer_metrics()["rate_limited"] == 1
    finally:
        _runtime.shutdown()


def test_rate_limit_keys_are_independent() -> None:
    _runtime.configure(
        service="svc", target="memory", level="DEBUG", rate_limit_max=1, rate_limit_period=60.0
    )
    try:
        structguru.logger.info("alpha")
        structguru.logger.info("beta")
        structguru.logger.info("alpha")  # dropped: alpha exhausted
        _runtime.flush_native()
        messages = [json.loads(line)["message"] for line in _runtime.drain_messages()]
        assert messages == ["alpha", "beta"]
        assert _runtime.writer_metrics()["rate_limited"] == 1
    finally:
        _runtime.shutdown()


def test_rate_limit_window_expires() -> None:
    _runtime.configure(
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
        _runtime.flush_native()
        assert len(_runtime.drain_messages()) == 2
    finally:
        _runtime.shutdown()


def test_invalid_rate_limit_raises() -> None:
    with pytest.raises(ValueError, match="rate_limit_max"):
        _runtime.configure(rate_limit_max=0)
    with pytest.raises(ValueError, match="rate_limit_period"):
        _runtime.configure(rate_limit_max=5, rate_limit_period=0.0)
    assert not _runtime.is_native_enabled()


# -- combined ---------------------------------------------------------------


def test_patterns_and_sampling_combine() -> None:
    _runtime.configure(
        service="svc",
        target="memory",
        level="DEBUG",
        sensitive_patterns=[r"pw=\w+"],
        sample_rate=0.0,
    )
    try:
        structguru.logger.info("m", body="auth pw=hunter2 ok")
        _runtime.flush_native()
        # sample_rate=0 drops everything; nothing is rendered.
        assert len(_runtime.drain_messages()) == 0
        assert _runtime.writer_metrics()["sampled"] == 1
    finally:
        _runtime.shutdown()


def test_env_var_sample_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STRUCTGURU_NATIVE_TARGET", "memory")
    monkeypatch.setenv("STRUCTGURU_NATIVE_SAMPLE_RATE", "0.0")
    _runtime.shutdown()
    _runtime._maybe_configure_from_env()
    try:
        assert _runtime.is_native_enabled()
        structguru.logger.info("dropped")
        _runtime.flush_native()
        assert len(_runtime.drain_messages()) == 0
    finally:
        _runtime.shutdown()
        monkeypatch.delenv("STRUCTGURU_NATIVE_SAMPLE_RATE", raising=False)


def test_env_var_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STRUCTGURU_NATIVE_TARGET", "memory")
    monkeypatch.setenv("STRUCTGURU_NATIVE_RATE_LIMIT", "2/60")
    _runtime.shutdown()
    _runtime._maybe_configure_from_env()
    try:
        assert _runtime.is_native_enabled()
        for _ in range(3):
            structguru.logger.info("same")
        _runtime.flush_native()
        assert len(_runtime.drain_messages()) == 2
        assert _runtime.writer_metrics()["rate_limited"] == 1
    finally:
        _runtime.shutdown()
        monkeypatch.delenv("STRUCTGURU_NATIVE_RATE_LIMIT", raising=False)


@pytest.mark.parametrize(
    ("name", "value", "error"),
    [
        ("STRUCTGURU_NATIVE_SAMPLE_RATE", "not-a-number", ValueError),
        ("STRUCTGURU_NATIVE_RATE_LIMIT", "invalid", ValueError),
        ("STRUCTGURU_NATIVE_TARGET", "invalid", ValueError),
    ],
)
def test_invalid_env_config_fails_loudly(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
    error: type[Exception],
) -> None:
    _runtime.shutdown()
    monkeypatch.setenv(name, value)

    with pytest.raises(error):
        _runtime._maybe_configure_from_env()

    assert not _runtime.is_native_enabled()


# -- level-gated sampling ----------------------------------------------------


def test_sample_max_level_gates_sampling() -> None:
    _runtime.configure(
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
        _runtime.flush_native()
        lines = _runtime.drain_messages()
        assert len(lines) == 2
        assert json.loads(lines[0])["level"] == "WARN"
        assert json.loads(lines[1])["level"] == "ERROR"
        metrics = _runtime.writer_metrics()
        assert metrics is not None
        assert metrics["sampled"] == 2
    finally:
        _runtime.shutdown()


def test_invalid_sample_max_level_raises() -> None:
    with pytest.raises(ValueError, match="sample_max_level"):
        _runtime.configure(target="memory", sample_rate=0.5, sample_max_level="bogus")


# -- metric hooks --------------------------------------------------------------


def test_metric_processor_invoked_per_kept_record() -> None:
    from structguru.metrics import MetricProcessor

    seen: list[dict[str, Any]] = []
    values: list[float] = []
    metrics = MetricProcessor()
    metrics.counter("user.login", seen.append)
    metrics.histogram("db.query", "duration_ms", lambda v, _ed: values.append(v))

    _runtime.configure(service="svc", target="memory", metric_processor=metrics)
    try:
        structguru.logger.info("user.login ok", user="alice")
        structguru.logger.info("db.query done", duration_ms=12.5)
        structguru.logger.info("unrelated")
    finally:
        _runtime.shutdown()

    assert len(seen) == 1
    assert seen[0]["event"] == "user.login ok"
    assert seen[0]["user"] == "alice"
    assert values == [12.5]


def test_metric_processor_not_invoked_for_dropped_records() -> None:
    calls: list[str] = []

    def hook(_logger: Any, method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        calls.append(method)
        return event_dict

    _runtime.configure(
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
        _runtime.shutdown()

    assert calls == ["warning"]


def test_metric_processor_errors_are_swallowed() -> None:
    def hook(_logger: Any, _method: str, _event_dict: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("metrics backend down")

    _runtime.configure(service="svc", target="memory", metric_processor=hook)
    try:
        structguru.logger.info("still logged")
        record = _drain_last()
        assert record["message"] == "still logged"
    finally:
        _runtime.shutdown()


def test_non_callable_metric_processor_raises() -> None:
    with pytest.raises(TypeError, match="metric_processor"):
        _runtime.configure(target="memory", metric_processor="not-a-callable")
