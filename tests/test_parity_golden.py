"""Golden parity suite: native render path vs the structlog/orjson pipeline.

Every scenario logs the same call through the native path and through
``structlog_reference_line`` (``_structlog_reference.py``): the pre-1.0 structlog processor
chain rendered by ``JSONRenderer(orjson)``, rebuilt without calling structguru.
The emitted JSON lines must be **byte-identical** after normalizing the
volatile timestamp value. Byte comparison (unlike the parsed-dict checks in
``test_native_render.py``) also locks key order and number/string
serialization format, so a native serialization change fails here.

Tiers:

1. **Byte parity** — the common path.
2. **Semantic parity** — exceptions. Known, accepted divergence: the structlog
   path appends ``exception`` after ``message`` (``format_exc_info`` runs in
   the formatter stage, post-``EventRenamer``) while the native path renders it
   with the user fields. Values must still match exactly.
3. **Stack info** — placement parity; the captured frames differ by design.

``test_golden_line_is_pinned`` also pins one representative line as a
committed string, so the reference cannot drift together with the renderer.
"""

from __future__ import annotations

import datetime as dt
import enum
import json
import re
import uuid
from typing import Any

import pytest
from _structlog_reference import structlog_reference_line

import structguru
from structguru import _runtime

pytestmark = pytest.mark.skipif(
    not _runtime.is_available(),
    reason="native extension not built",
)

_TS_RE = re.compile(r'"timestamp":"[^"]*"')


def _normalize_ts(line: str) -> str:
    return _TS_RE.sub('"timestamp":"TS"', line)


def _standard_line(
    method: str,
    msg: Any,
    args: tuple[Any, ...],
    bound: dict[str, Any],
    kwargs: dict[str, Any],
) -> str:
    return structlog_reference_line(
        method, msg, args, logger_name=__name__, bound=bound, kwargs=kwargs
    )


def _native_line(
    method: str,
    msg: Any,
    args: tuple[Any, ...],
    bound: dict[str, Any],
    kwargs: dict[str, Any],
) -> str:
    _runtime.configure(service="svc", target="memory", level="DEBUG")
    try:
        log = structguru.logger.bind(**bound) if bound else structguru.logger
        getattr(log, method)(msg, *args, **kwargs)
        _runtime.flush_native()
        messages = _runtime.drain_messages()
        assert messages, "native path emitted no record"
        return messages[-1].strip()
    finally:
        _runtime.shutdown()


def _assert_byte_parity(
    method: str,
    msg: Any,
    args: tuple[Any, ...] = (),
    bound: dict[str, Any] | None = None,
    kwargs: dict[str, Any] | None = None,
) -> None:
    bound = bound or {}
    kwargs = kwargs or {}
    # kwargs dicts are mutated by _log (consumed format keys); copy per path.
    standard = _standard_line(method, msg, args, dict(bound), dict(kwargs))
    native = _native_line(method, msg, args, dict(bound), dict(kwargs))
    assert _normalize_ts(native) == _normalize_ts(standard)


# -- Tier 1: byte parity -----------------------------------------------------

LEVEL_SCENARIOS = [
    ("trace", "trace maps to debug"),
    ("debug", "debug msg"),
    ("info", "info msg"),
    ("success", "success maps to info"),
    ("warning", "warning msg"),
    ("warn", "warn alias"),
    ("error", "error msg"),
    ("critical", "critical msg"),
    ("fatal", "fatal alias"),
]


@pytest.mark.parametrize(("method", "msg"), LEVEL_SCENARIOS)
def test_parity_all_level_methods(method: str, msg: str) -> None:
    _assert_byte_parity(method, msg)


MESSAGE_SCENARIOS = [
    pytest.param("plain message", (), {}, id="plain"),
    pytest.param("", (), {}, id="empty-message"),
    pytest.param(12345, (), {}, id="non-str-message"),
    pytest.param("hello {name}", (), {"name": "world", "extra": 1}, id="brace-kwarg"),
    pytest.param("n={0} m={1}", (7, "x"), {}, id="brace-positional"),
    pytest.param("{present} and {missing}", (), {"present": "y"}, id="brace-partial-fallback"),
    pytest.param("braces {kept} raw", (), {}, id="brace-no-args"),
    pytest.param("юникод emoji 🚀 done", (), {"поле": "значение"}, id="unicode"),
    pytest.param('quote " backslash \\ newline \n tab \t end', (), {}, id="json-escapes"),
]


@pytest.mark.parametrize(("msg", "args", "kwargs"), MESSAGE_SCENARIOS)
@pytest.mark.filterwarnings("ignore::UserWarning")
def test_parity_message_formatting(
    msg: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> None:
    _assert_byte_parity("info", msg, args=args, kwargs=kwargs)


FIELD_SCENARIOS = [
    pytest.param({}, {"n": 1, "f": 2.5, "b": True, "z": None}, id="scalars"),
    pytest.param({}, {"tiny": 2.5e-7, "huge": 1e20, "tenth": 0.1, "negzero": -0.0}, id="floats"),
    pytest.param({}, {"nan": float("nan"), "inf": float("inf")}, id="non-finite"),
    pytest.param({}, {"ctx": {"deep": {"deeper": [1, {"x": "y"}]}}}, id="nested"),
    pytest.param({}, {"tup": (1, 2), "lst": ["a", ["b"]]}, id="sequences"),
    pytest.param({"request_id": "req-1"}, {"qty": 2}, id="bound-context"),
    pytest.param({"a": 1}, {"a": 2}, id="kwarg-overrides-bound"),
    pytest.param({}, {"at": dt.datetime(2026, 7, 10, 12, 0, 0, 5)}, id="datetime"),
    pytest.param({}, {"d": dt.date(2026, 7, 10)}, id="date"),
    pytest.param({}, {"uid": uuid.UUID("12345678-1234-5678-1234-567812345678")}, id="uuid"),
    pytest.param(
        {},
        {"one": 1.0, "exp": 1e16, "plain": 1e15, "big": 1e300, "small": 5e-324, "long": 1 / 3},
        id="float-formats",
    ),
    pytest.param(
        {},
        {"min": -(2**63), "max": 2**63 - 1, "u64": 2**64 - 1, "neg": -1, "zero": 0},
        id="integer-bounds",
    ),
    pytest.param(
        {},
        {"ctrl": "\x00\x1f\x7f\u2028\u2029\u202e", "empty": "", "list": [], "map": {}},
        id="string-escapes-and-empty-containers",
    ),
]


@pytest.mark.parametrize(("bound", "kwargs"), FIELD_SCENARIOS)
def test_parity_field_values(bound: dict[str, Any], kwargs: dict[str, Any]) -> None:
    _assert_byte_parity("info", "fields", bound=bound, kwargs=kwargs)


class _Color(enum.Enum):
    RED = "red"
    GREEN = 2


@pytest.mark.parametrize("value", [_Color.RED, _Color.GREEN])
def test_parity_enum(value: _Color) -> None:
    _assert_byte_parity("info", "enum", kwargs={"c": value})


REDACTION_SCENARIOS = [
    pytest.param({"password": "hunter2", "user": "alice"}, id="default-key"),
    pytest.param({"Password": "x", "API_KEY": "y"}, id="case-insensitive"),
    pytest.param({"ctx": {"token": "t", "path": "/x"}}, id="nested-key"),
    pytest.param({"creds": [{"secret": "s"}, "plain"]}, id="key-inside-list"),
    pytest.param({"authorization": "Bearer abc", "ssn": "123-45-6789"}, id="more-defaults"),
    # The internal marker key never reaches the rendered output.
    pytest.param({"_structguru_redacted": True, "user": "alice"}, id="marker-stripped"),
]


@pytest.mark.parametrize("kwargs", REDACTION_SCENARIOS)
def test_parity_redaction(kwargs: dict[str, Any]) -> None:
    _assert_byte_parity("info", "redact", kwargs=kwargs)


COLLISION_SCENARIOS = [
    pytest.param({"level": "bogus", "tag": "t"}, id="level"),
    pytest.param({"service": "user-svc", "tag": "t"}, id="service"),
    pytest.param({"severity": 99, "tag": "t"}, id="severity"),
    pytest.param({"logger": "user-logger", "tag": "t"}, id="logger"),
    pytest.param({"timestamp": "user-ts", "tag": "t"}, id="timestamp"),
    # NOTE: a "message" collision is unreachable through the public API — it
    # clashes with the positional parameter name and raises TypeError.
]


@pytest.mark.parametrize("kwargs", COLLISION_SCENARIOS)
def test_parity_reserved_key_collisions(kwargs: dict[str, Any]) -> None:
    _assert_byte_parity("warning", "collision", kwargs=kwargs)


def test_parity_contextualize() -> None:
    context = {"request_id": "ctx-1", "user": "bob"}
    standard = structlog_reference_line(
        "info", "ctx", logger_name=__name__, kwargs={"extra": 1}, contextvars=context
    )

    _runtime.configure(service="svc", target="memory", level="DEBUG")
    try:
        with structguru.logger.contextualize(**context):
            structguru.logger.info("ctx", extra=1)
        _runtime.flush_native()
        native = _runtime.drain_messages()[-1].strip()
    finally:
        _runtime.shutdown()

    assert _normalize_ts(native) == _normalize_ts(standard)


def test_parity_stack_placement() -> None:
    stack = "Stack (most recent call last):\n  frame"
    standard = structlog_reference_line(
        "info", "where", logger_name=__name__, kwargs={"k": 1}, stack=stack
    )

    _runtime.configure(service="svc", target="memory", level="DEBUG")
    try:
        structguru.logger.opt(stack_info=stack).info("where", k=1)
        _runtime.flush_native()
        native = _runtime.drain_messages()[-1].strip()
    finally:
        _runtime.shutdown()

    assert _normalize_ts(native) == _normalize_ts(standard)


def test_golden_line_is_pinned() -> None:
    # A committed line: the reference and the renderer cannot drift together.
    golden = (
        '{"request_id":"req-1","qty":2,"ratio":0.1,"ok":true,"none":null,"tags":["a","b"],'
        '"ctx":{"password":"[REDACTED]","path":"/x"},"rid":"ctx-1",'
        f'"logger":"{__name__}","level":"WARN","severity":4,"timestamp":"TS",'
        '"service":"svc","message":"order 987 accepted"}'
    )
    call = {
        "bound": {"request_id": "req-1"},
        "kwargs": {
            "id": 987,
            "qty": 2,
            "ratio": 0.1,
            "ok": True,
            "none": None,
            "tags": ["a", "b"],
            "ctx": {"password": "hunter2", "path": "/x"},
        },
    }
    standard = structlog_reference_line(
        "warning",
        "order {id} accepted",
        logger_name=__name__,
        contextvars={"rid": "ctx-1"},
        bound=call["bound"],
        kwargs=call["kwargs"],
    )

    _runtime.configure(service="svc", target="memory", level="DEBUG")
    try:
        with structguru.logger.contextualize(rid="ctx-1"):
            structguru.logger.bind(**call["bound"]).warning(
                "order {id} accepted", **dict(call["kwargs"])
            )
        _runtime.flush_native()
        native = _runtime.drain_messages()[-1].strip()
    finally:
        _runtime.shutdown()

    assert _normalize_ts(standard) == golden
    assert _normalize_ts(native) == golden


# -- Tier 2: semantic parity (exceptions) ------------------------------------
# Known divergence: key ORDER of "exception" differs (standard appends it after
# "message"; native renders it with user fields). Values must match exactly.


def _exception_pair(**log_kwargs: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        raise ValueError("nope")
    except ValueError as err:
        exc = err

    standard = json.loads(
        structlog_reference_line(
            "error", "failed", logger_name=__name__, kwargs={"exc_info": exc, **log_kwargs}
        )
    )

    _runtime.configure(service="svc", target="memory", level="DEBUG")
    try:
        structguru.logger.error("failed", exc_info=exc, **log_kwargs)
        _runtime.flush_native()
        native = json.loads(_runtime.drain_messages()[-1])
    finally:
        _runtime.shutdown()

    standard.pop("timestamp", None)
    native.pop("timestamp", None)
    return standard, native


def test_parity_exception_semantic() -> None:
    standard, native = _exception_pair(code=1)
    assert native == standard


def test_parity_exception_with_chained_cause() -> None:
    try:
        try:
            raise KeyError("inner")
        except KeyError as inner:
            raise RuntimeError("outer") from inner
    except RuntimeError as err:
        exc = err

    standard = json.loads(
        structlog_reference_line("error", "failed", logger_name=__name__, kwargs={"exc_info": exc})
    )

    _runtime.configure(service="svc", target="memory", level="DEBUG")
    try:
        structguru.logger.error("failed", exc_info=exc)
        _runtime.flush_native()
        native = json.loads(_runtime.drain_messages()[-1])
    finally:
        _runtime.shutdown()

    assert native["exception"] == standard["exception"]
    assert "inner" in native["exception"] and "outer" in native["exception"]


# -- Structured exceptions: native output must equal build_exception_dict ---


def _native_structured_exception(exc: BaseException, **enable_kwargs: Any) -> dict[str, Any]:
    _runtime.configure(
        service="svc",
        target="memory",
        level="DEBUG",
        structured_exceptions=True,
        **enable_kwargs,
    )
    try:
        structguru.logger.error("failed", exc_info=exc)
        _runtime.flush_native()
        return json.loads(_runtime.drain_messages()[-1])  # type: ignore[no-any-return]
    finally:
        _runtime.shutdown()


def _processor_exception(exc: BaseException, **proc_kwargs: Any) -> dict[str, Any]:
    from structguru.exceptions import build_exception_dict

    result = build_exception_dict(exc, **proc_kwargs)
    return result  # type: ignore[no-any-return]


def test_parity_structured_exception_matches_processor() -> None:
    try:
        raise ValueError("nope")
    except ValueError as err:
        exc = err

    record = _native_structured_exception(exc)
    assert record["exception"] == _processor_exception(exc)
    assert record["exception"]["type"] == "ValueError"
    assert record["exception"]["frames"]


def test_parity_structured_exception_chained_cause() -> None:
    try:
        try:
            raise KeyError("inner")
        except KeyError as inner:
            raise RuntimeError("outer") from inner
    except RuntimeError as err:
        exc = err

    record = _native_structured_exception(exc)
    assert record["exception"] == _processor_exception(exc)
    assert record["exception"]["cause"] == {"type": "KeyError", "message": "'inner'"}


def _capture(fn: Any) -> BaseException:
    """Run *fn* and return the raised exception.

    The try/except lives in this helper (not in the test body) so the
    traceback's outermost frame is settled — extracting locals twice sees
    identical frame state, instead of picking up the test's own variables
    as they are assigned between the two extractions.
    """
    try:
        fn()
    except BaseException as err:  # noqa: BLE001
        return err
    raise AssertionError("fn did not raise")


def test_parity_structured_exception_locals_and_truncation() -> None:
    def _boom() -> None:
        password = "hunter2"  # noqa: F841 - captured via f_locals
        blob = "x" * 500  # noqa: F841
        raise ValueError("with locals")

    exc = _capture(_boom)

    record = _native_structured_exception(
        exc,
        exception_include_locals=True,
        exception_max_local_repr=100,
    )
    expected = _processor_exception(exc, include_locals=True, max_local_repr=100)
    assert record["exception"] == expected

    boom_locals = record["exception"]["frames"][-1]["locals"]
    assert boom_locals["password"] == "[REDACTED]"
    assert boom_locals["blob"].endswith("...<402 more>")


def test_parity_structured_exception_max_frames() -> None:
    def _level1() -> None:
        raise ValueError("deep")

    def _level2() -> None:
        _level1()

    def _level3() -> None:
        _level2()

    try:
        _level3()
    except ValueError as err:
        exc = err

    record = _native_structured_exception(exc, exception_max_frames=2)
    assert record["exception"] == _processor_exception(exc, max_frames=2)
    assert len(record["exception"]["frames"]) == 2


def test_parity_structured_exception_custom_sensitive_keys() -> None:
    def _boom() -> None:
        secret_sauce = "x"  # noqa: F841
        password = "visible with custom keys"  # noqa: F841
        raise ValueError("custom keys")

    exc = _capture(_boom)

    record = _native_structured_exception(
        exc,
        sensitive_keys=["Secret_Sauce"],
        exception_include_locals=True,
    )
    expected = _processor_exception(
        exc,
        include_locals=True,
        sensitive_keys=frozenset({"secret_sauce"}),
    )
    assert record["exception"] == expected
    boom_locals = record["exception"]["frames"][-1]["locals"]
    assert boom_locals["secret_sauce"] == "[REDACTED]"
    assert boom_locals["password"] == "'visible with custom keys'"


# -- Tier 3: stack_info -------------------------------------------------------
# Semantic parity: the stack *content* intentionally diverges — structlog's
# StackInfoRenderer only skips structlog frames, so the standard path's stack
# ends inside structguru internals; the native path also skips structguru
# frames and ends at the user's calling frame. Header, key position, and
# format match exactly.


def test_parity_stack_info_handled_natively() -> None:
    _runtime.configure(service="svc", target="memory", level="DEBUG")
    try:
        structguru.logger.info("where am I", stack_info=True)
        _runtime.flush_native()
        messages = _runtime.drain_messages()
        assert messages, "stack_info call bypassed the native path"
        line = messages[-1]
        record = json.loads(line)
    finally:
        _runtime.shutdown()

    assert record["stack"].startswith("Stack (most recent call last):\n")
    assert "test_parity_golden.py" in record["stack"]  # ends at the user frame
    assert "structguru/core.py" not in record["stack"]  # internals skipped
    assert not record["stack"].endswith("\n")
    # position matches StackInfoRenderer: between "service" and "message"
    assert '"service":"svc","stack":"' in line
    assert "stack_info" not in record  # consumed, like the processor's pop()


def test_parity_stack_info_via_opt() -> None:
    _runtime.configure(service="svc", target="memory", level="DEBUG")
    try:
        structguru.logger.opt(stack_info=True).warning("careful")
        _runtime.flush_native()
        record = json.loads(_runtime.drain_messages()[-1])
    finally:
        _runtime.shutdown()

    assert record["stack"].startswith("Stack (most recent call last):\n")
    assert record["level"] == "WARN"
