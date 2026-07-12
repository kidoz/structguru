"""Golden-parity tests: native render path vs the structlog JSON output.

Compares parsed dicts (ignoring the volatile ``timestamp`` value) so field
order is not asserted here — only that the native path produces the same keys
and values as the current structlog pipeline for the common, non-exception case.
"""

from __future__ import annotations

import datetime as dt
import enum
import io
import json
import re
import uuid
from typing import Any

import pytest
from conftest import configure

import structguru
from structguru import _native

pytestmark = pytest.mark.skipif(
    not _native.is_available(),
    reason="native extension not built",
)


def _standard_json(bound: dict[str, Any], method: str, msg: str, **kwargs: Any) -> dict[str, Any]:
    buf = io.StringIO()
    configure(service="svc", level="DEBUG", json=True, stream=buf)
    log = structguru.logger.bind(**bound)
    getattr(log, method)(msg, **kwargs)
    line = buf.getvalue().strip().splitlines()[-1]
    return json.loads(line)


def _native_json(bound: dict[str, Any], method: str, msg: str, **kwargs: Any) -> dict[str, Any]:
    _native.configure(service="svc", target="memory", level="DEBUG")
    try:
        log = structguru.logger.bind(**bound)
        getattr(log, method)(msg, **kwargs)
        _native.flush_native()
        line = _native.drain_messages()[-1]
        return json.loads(line)
    finally:
        _native.shutdown()


def _without_ts(record: dict[str, Any]) -> dict[str, Any]:
    record.pop("timestamp", None)
    return record


@pytest.mark.parametrize(
    ("method", "msg", "bound", "kwargs"),
    [
        ("info", "plain message", {}, {}),
        ("info", "order {id} accepted", {"request_id": "req-1"}, {"id": 987, "qty": 2}),
        ("warning", "careful", {}, {"code": 42}),
        ("error", "boom", {"service_area": "checkout"}, {}),
        ("debug", "d", {}, {"password": "hunter2", "api_key": "abc"}),
        ("info", "nested", {}, {"ctx": {"token": "t", "path": "/x"}, "flags": ["a", "b"]}),
        ("critical", "down", {}, {"n": 3.5, "ok": False, "none": None}),
        # exotic leaves delegated to orjson for exact parity:
        ("info", "when", {}, {"at": dt.datetime(2026, 7, 8, 16, 19, 24, 616660)}),
        ("info", "day", {}, {"d": dt.date(2026, 7, 8)}),
        ("info", "id", {}, {"uid": uuid.UUID("12345678-1234-5678-1234-567812345678")}),
        ("info", "non-finite", {}, {"bad": float("nan"), "worse": float("inf")}),
    ],
)
def test_native_matches_structlog_json(
    method: str,
    msg: str,
    bound: dict[str, Any],
    kwargs: dict[str, Any],
) -> None:
    standard = _without_ts(_standard_json(bound, method, msg, **kwargs))
    native = _without_ts(_native_json(bound, method, msg, **kwargs))
    assert native == standard


class _Color(enum.Enum):
    RED = "red"
    GREEN = 2


@pytest.mark.parametrize("value", [_Color.RED, _Color.GREEN])
def test_native_enum_matches_structlog(value: _Color) -> None:
    standard = _without_ts(_standard_json({}, "info", "e", c=value))
    native = _without_ts(_native_json({}, "info", "e", c=value))
    assert native == standard


def test_native_timestamp_is_rust_iso8601() -> None:
    native = _native_json({}, "info", "t")
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z", native["timestamp"])


def test_native_redaction_applied() -> None:
    native = _native_json({}, "info", "login", password="secret", user="alice")
    assert native["password"] == "[REDACTED]"
    assert native["user"] == "alice"


def test_native_brace_formatting_consumes_kwarg() -> None:
    native = _native_json({}, "info", "hi {name}", name="world", extra=1)
    assert native["message"] == "hi world"
    assert "name" not in native  # consumed by the template
    assert native["extra"] == 1


def test_native_exception_matches_structlog() -> None:
    """logger.error(exc_info=...) renders natively with a matching traceback string.

    The *same* captured exception is logged both ways so the traceback (file, line,
    function) is identical — only the renderer differs.
    """
    try:
        raise ValueError("nope")
    except ValueError as err:
        exc = err

    _native.configure(service="svc", target="memory", level="DEBUG")
    try:
        structguru.logger.error("failed", code=1, exc_info=exc)
        _native.flush_native()
        native = json.loads(_native.drain_messages()[-1])
    finally:
        _native.shutdown()

    buf = io.StringIO()
    configure(service="svc", level="DEBUG", json=True, stream=buf)
    structguru.logger.error("failed", code=1, exc_info=exc)
    standard = json.loads(buf.getvalue().strip().splitlines()[-1])

    assert native["exception"] == standard["exception"]
    assert native["level"] == "ERROR"
    assert native["code"] == 1


def test_malformed_exc_info_does_not_break_logging() -> None:
    _native.configure(service="svc", target="memory", level="DEBUG")
    try:
        structguru.logger.error("bad metadata", exc_info="invalid")
        _native.flush_native()
        record = json.loads(_native.drain_messages()[-1])
    finally:
        _native.shutdown()

    assert record["message"] == "bad metadata"
    assert "exception" not in record


def test_native_level_filtering_drops_below_threshold() -> None:
    _native.configure(service="svc", target="memory", level="WARNING")
    try:
        structguru.logger.info("dropped")
        structguru.logger.warning("kept")
        _native.flush_native()
        lines = _native.drain_messages()
        assert len(lines) == 1
        assert json.loads(lines[0])["message"] == "kept"
    finally:
        _native.shutdown()


def test_native_reserved_key_collision_matches_structlog() -> None:
    """User fields colliding with standard keys must not duplicate keys, and must
    match structlog's precedence (canonical level; user-provided service wins)."""
    standard = _without_ts(_standard_json({}, "warning", "m", level="bogus", tag="t"))
    native = _without_ts(_native_json({}, "warning", "m", level="bogus", tag="t"))
    assert native == standard
    assert native["level"] == "WARN"
    assert native["tag"] == "t"


def test_native_custom_sensitive_keys() -> None:
    _native.configure(service="svc", target="memory", sensitive_keys=["secret_sauce"])
    try:
        structguru.logger.info("m", secret_sauce="x", ssn="123")
        _native.flush_native()
        record = json.loads(_native.drain_messages()[-1])
        assert record["secret_sauce"] == "[REDACTED]"
        assert record["ssn"] == "123"  # default key, not in the custom set
    finally:
        _native.shutdown()
