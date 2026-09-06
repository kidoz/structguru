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
import sys
import uuid
from typing import Any

import pytest
from conftest import configure

import structguru
from structguru import _runtime

pytestmark = pytest.mark.skipif(
    not _runtime.is_available(),
    reason="native extension not built",
)


def _standard_json(bound: dict[str, Any], method: str, msg: str, **kwargs: Any) -> dict[str, Any]:
    buf = io.StringIO()
    configure(service="svc", level="DEBUG", stream=buf)
    log = structguru.logger.bind(**bound)
    getattr(log, method)(msg, **kwargs)
    line = buf.getvalue().strip().splitlines()[-1]
    return json.loads(line)


def _native_json(bound: dict[str, Any], method: str, msg: str, **kwargs: Any) -> dict[str, Any]:
    _runtime.configure(service="svc", target="memory", level="DEBUG")
    try:
        log = structguru.logger.bind(**bound)
        getattr(log, method)(msg, **kwargs)
        _runtime.flush_native()
        line = _runtime.drain_messages()[-1]
        return json.loads(line)
    finally:
        _runtime.shutdown()


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

    _runtime.configure(service="svc", target="memory", level="DEBUG")
    try:
        structguru.logger.error("failed", code=1, exc_info=exc)
        _runtime.flush_native()
        native = json.loads(_runtime.drain_messages()[-1])
    finally:
        _runtime.shutdown()

    buf = io.StringIO()
    configure(service="svc", level="DEBUG", stream=buf)
    structguru.logger.error("failed", code=1, exc_info=exc)
    standard = json.loads(buf.getvalue().strip().splitlines()[-1])

    assert native["exception"] == standard["exception"]
    assert native["level"] == "ERROR"
    assert native["code"] == 1


def test_malformed_exc_info_does_not_break_logging() -> None:
    _runtime.configure(service="svc", target="memory", level="DEBUG")
    try:
        structguru.logger.error("bad metadata", exc_info="invalid")
        _runtime.flush_native()
        record = json.loads(_runtime.drain_messages()[-1])
    finally:
        _runtime.shutdown()

    assert record["message"] == "bad metadata"
    assert "exception" not in record


def test_native_level_filtering_drops_below_threshold() -> None:
    _runtime.configure(service="svc", target="memory", level="WARNING")
    try:
        structguru.logger.info("dropped")
        structguru.logger.warning("kept")
        _runtime.flush_native()
        lines = _runtime.drain_messages()
        assert len(lines) == 1
        assert json.loads(lines[0])["message"] == "kept"
    finally:
        _runtime.shutdown()


def test_native_reserved_key_collision_matches_structlog() -> None:
    """User fields colliding with standard keys must not duplicate keys, and must
    match structlog's precedence (canonical level; user-provided service wins)."""
    standard = _without_ts(_standard_json({}, "warning", "m", level="bogus", tag="t"))
    native = _without_ts(_native_json({}, "warning", "m", level="bogus", tag="t"))
    assert native == standard
    assert native["level"] == "WARN"
    assert native["tag"] == "t"


def test_native_custom_sensitive_keys() -> None:
    _runtime.configure(service="svc", target="memory", sensitive_keys=["secret_sauce"])
    try:
        structguru.logger.info("m", secret_sauce="x", ssn="123")
        _runtime.flush_native()
        record = json.loads(_runtime.drain_messages()[-1])
        assert record["secret_sauce"] == "[REDACTED]"
        assert record["ssn"] == "123"  # default key, not in the custom set
    finally:
        _runtime.shutdown()


def _record_with_unsupported_fields(**configure_kwargs: Any) -> str:
    """Log one record carrying unsupported values and return the rendered line."""
    _runtime.configure(service="svc", target="memory", level="DEBUG", **configure_kwargs)
    try:
        structguru.logger.bind(request=object()).info(
            "kept",
            nested={"items": [1, object()]},
            note="secret=hunter2 ok",
            password=object(),
        )
        _runtime.flush_native()
        return _runtime.drain_messages()[-1]
    finally:
        _runtime.shutdown()


@pytest.mark.parametrize(
    "sensitive_patterns",
    [None, [r"secret=\w+"]],
    ids=["render_line", "render_line_with_config"],
)
def test_native_unsupported_field_keeps_json_record(sensitive_patterns: list[str] | None) -> None:
    # Both JSON branches: the standalone renderer and the one reusing a
    # compiled RedactionConfig. The record ships with markers in place of the
    # unsupported values, and redaction still applies around them.
    record = json.loads(_record_with_unsupported_fields(sensitive_patterns=sensitive_patterns))

    assert record["message"] == "kept"
    assert record["request"] == "<unsupported: object>"
    assert record["nested"] == {"items": [1, "<unsupported: object>"]}
    assert record["password"] == "[REDACTED]"  # key redaction covers markers too
    expected_note = "[REDACTED] ok" if sensitive_patterns else "secret=hunter2 ok"
    assert record["note"] == expected_note


@pytest.mark.parametrize(
    "sensitive_patterns",
    [None, [r"secret=\w+"]],
    ids=["render_line_console", "render_console_with_config"],
)
def test_native_unsupported_field_keeps_console_record(
    sensitive_patterns: list[str] | None,
) -> None:
    line = _record_with_unsupported_fields(
        format="console", colors=False, sensitive_patterns=sensitive_patterns
    )

    assert "kept" in line
    assert 'request="<unsupported: object>"' in line
    assert 'password="[REDACTED]"' in line
    expected_note = "[REDACTED] ok" if sensitive_patterns else "secret=hunter2 ok"
    assert f'note="{expected_note}"' in line


def test_native_unsupported_field_keeps_exception_traceback() -> None:
    try:
        raise RuntimeError("synthetic application failure")
    except RuntimeError as err:
        exc = err

    _runtime.configure(service="svc", target="memory", level="DEBUG")
    try:
        structguru.logger.opt(exception=exc).error("failed", request=object(), status_code=500)
        _runtime.flush_native()
        record = json.loads(_runtime.drain_messages()[-1])
    finally:
        _runtime.shutdown()

    assert record["level"] == "ERROR"
    assert record["status_code"] == 500
    assert record["request"] == "<unsupported: object>"
    assert "RuntimeError: synthetic application failure" in record["exception"]


def _is_caret_line(line: str) -> bool:
    # Exception groups indent their members behind a "|" margin.
    body = line.strip().lstrip("|+ ").strip()
    return bool(body) and set(body) <= set("~^")


def _lookup_missing(data: dict[str, Any]) -> Any:
    return data["a"]["missing"]  # sub-expression failure: carets under ["missing"]


def _capture(raise_fn: Any) -> Any:
    try:
        raise_fn()
    except Exception:
        return sys.exc_info()
    raise AssertionError("expected an exception")


def _raise_chained() -> None:
    try:
        _lookup_missing({"a": {}})
    except KeyError as err:
        raise ValueError("wrapped") from err


def _raise_group() -> None:
    errors = []
    for payload in ({"a": {}}, {"a": {}}):
        try:
            _lookup_missing(payload)
        except KeyError as err:
            errors.append(err)
    raise ExceptionGroup("group", errors)


@pytest.mark.parametrize(
    "raise_fn",
    [lambda: _lookup_missing({"a": {}}), _raise_chained, _raise_group],
    ids=["plain", "chained-cause", "exception-group"],
)
def test_format_exception_without_carets_drops_only_position_markers(raise_fn: Any) -> None:
    exc_info = _capture(raise_fn)
    with_carets = _runtime.format_exception(exc_info)
    without = _runtime.format_exception(exc_info, carets=False)

    assert not any(_is_caret_line(line) for line in without.splitlines())
    expected = "\n".join(line for line in with_carets.splitlines() if not _is_caret_line(line))
    assert without == expected
    assert "Traceback (most recent call last):" in without


def test_configure_exception_carets_false_applies_to_logged_records() -> None:
    exc_info = _capture(lambda: _lookup_missing({"a": {}}))
    if not any(_is_caret_line(line) for line in _runtime.format_exception(exc_info).splitlines()):
        pytest.skip("interpreter emits no position markers (PYTHONNODEBUGRANGES?)")

    def logged_exception(**config: Any) -> str:
        _runtime.configure(service="svc", target="memory", level="DEBUG", **config)
        try:
            structguru.logger.opt(exception=exc_info).error("failed")
            _runtime.flush()
            exception = json.loads(_runtime.drain_messages()[-1])["exception"]
        finally:
            _runtime.shutdown()
        assert isinstance(exception, str)
        return exception

    assert any(_is_caret_line(line) for line in logged_exception().splitlines())
    without = logged_exception(exception_carets=False)
    assert not any(_is_caret_line(line) for line in without.splitlines())
    assert "KeyError: 'missing'" in without


def test_native_logging_call_never_raises_for_field_shape() -> None:
    loop: dict[str, object] = {"n": 1}
    loop["self"] = loop
    deep: object = "leaf"
    for _ in range(80):
        deep = [deep]

    _runtime.configure(service="svc", target="memory", level="DEBUG")
    try:
        structguru.logger.info("shape", loop=loop, deep=deep, counts={200: 3}, big=2**80)
        _runtime.flush_native()
        record = json.loads(_runtime.drain_messages()[-1])
    finally:
        _runtime.shutdown()

    assert record["loop"] == {"n": 1, "self": "<cycle: dict>"}
    assert record["counts"] == {"200": 3}
    assert record["big"] == 2**80
    innermost: object = record["deep"]
    while isinstance(innermost, list):
        innermost = innermost[0]
    assert innermost == "<max depth exceeded>"


@pytest.mark.parametrize("format", ["json", "console"])
@pytest.mark.parametrize("kind", ["isoformat", "dataclass"])
def test_mutating_field_conversion_uses_dictionary_snapshot(format: str, kind: str) -> None:
    from dataclasses import dataclass

    payload: dict[str, Any] = {}

    class ISOValue:
        def isoformat(self) -> str:
            payload.clear()
            payload["replacement"] = "after snapshot"
            return "converted"

    @dataclass
    class DataclassValue:
        value: str

        def __getattribute__(self, name: str) -> Any:
            if name == "value":
                payload.clear()
                payload["replacement"] = "after snapshot"
            return object.__getattribute__(self, name)

    payload.update(
        first=ISOValue() if kind == "isoformat" else DataclassValue("converted"),
        second="snapshot retained",
    )
    _runtime.configure(target="memory", format=format, colors=False)
    structguru.logger.info("conversion survives", payload=payload)
    _runtime.flush()
    line = _runtime.drain_messages()[0]
    assert "converted" in line
    assert "snapshot retained" in line
    assert "after snapshot" not in line
    assert payload == {"replacement": "after snapshot"}


def test_standalone_native_renderer_snapshots_top_level_fields() -> None:
    fields: dict[str, Any] = {}

    class ISOValue:
        def isoformat(self) -> str:
            fields.clear()
            return "converted"

    fields.update(first=ISOValue(), second="retained")
    assert _runtime._RUST is not None
    record = json.loads(_runtime._RUST.render_line(fields, "test", "info", "svc", "message"))
    assert record["first"] == "converted" and record["second"] == "retained"


@pytest.mark.parametrize(
    "redaction",
    [
        {"sensitive_patterns": ["DEMO_VALUE"]},
        {"sensitive_keys": ["LOGGER", "SERVICE"]},
    ],
)
@pytest.mark.parametrize("bound_service", [False, True])
def test_native_metadata_uses_same_redaction_as_user_fields(
    redaction: dict[str, Any],
    bound_service: bool,
) -> None:
    _runtime.configure(target="memory", service="DEMO_VALUE", **redaction)
    log = structguru.Logger(name="DEMO_VALUE")
    if bound_service:
        log = log.bind(service="DEMO_VALUE")
    log.info("metadata", value="ordinary")
    _runtime.flush()
    record = json.loads(_runtime.drain_messages()[0])
    assert record["logger"] == "[REDACTED]"
    assert record["service"] == "[REDACTED]"
    assert record["message"] == "metadata" and record["level"] == "INFO"
