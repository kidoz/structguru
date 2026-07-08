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

import structguru
from structguru import _native
from structguru.config import configure_structlog

pytestmark = pytest.mark.skipif(
    not _native.native_available(),
    reason="native extension not built",
)


def _standard_json(bound: dict[str, Any], method: str, msg: str, **kwargs: Any) -> dict[str, Any]:
    buf = io.StringIO()
    configure_structlog(service="svc", level="DEBUG", json_logs=True, stream=buf)
    log = structguru.logger.bind(**bound)
    getattr(log, method)(msg, **kwargs)
    line = buf.getvalue().strip().splitlines()[-1]
    return json.loads(line)


def _native_json(bound: dict[str, Any], method: str, msg: str, **kwargs: Any) -> dict[str, Any]:
    _native.enable_native(service="svc", target="memory")
    try:
        log = structguru.logger.bind(**bound)
        getattr(log, method)(msg, **kwargs)
        _native.flush_native()
        line = _native.drain_messages()[-1]
        return json.loads(line)
    finally:
        _native.disable_native()


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


def test_exception_logs_bypass_native_path() -> None:
    """exc_info must fall through to the structlog path, not the native writer."""
    _native.enable_native(service="svc", target="memory")
    try:
        before = _native.native_metrics()["enqueued"]
        try:
            raise ValueError("nope")
        except ValueError:
            structguru.logger.exception("failed")
        _native.flush_native()
        after = _native.native_metrics()["enqueued"]
        assert after == before  # nothing enqueued natively
    finally:
        _native.disable_native()
