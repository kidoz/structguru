"""Tests for the ``format=`` renderer selector on :func:`configure`.

Covers:
- ``format="json"`` (default) and ``format="console"`` acceptance
- the default (omitting ``format=``) selects JSON
- each format renders the expected shape
- validation (invalid format name)
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

import structguru
from structguru import _runtime

pytestmark = pytest.mark.skipif(
    not _runtime.is_available(),
    reason="native extension not built",
)


def _drain_last_line() -> str:
    _runtime.flush_native()
    return _runtime.drain_messages()[-1].rstrip("\n")


@pytest.mark.parametrize("fmt", ["json", "console"])
@pytest.mark.parametrize("redact_key", [False, True])
@pytest.mark.parametrize("backtracking", [False, True])
def test_stack_is_redacted(fmt: str, redact_key: bool, backtracking: bool) -> None:
    _runtime.configure(
        target="memory",
        format=fmt,
        colors=False,
        sensitive_keys=["STACK"] if redact_key else None,
        sensitive_patterns=[r"(?<=token=)\w+" if backtracking else r"(token=)\w+"],
        pattern_replacement="[MASKED]" if backtracking else "$1[MASKED]",
        allow_backtracking_patterns=backtracking,
    )
    structguru.logger.info("trace", stack_info="frame\ntoken=REVIEW_SENTINEL")
    line = _drain_last_line()
    expected = "[REDACTED]" if redact_key else "frame\ntoken=[MASKED]"
    assert "REVIEW_SENTINEL" not in line
    if fmt == "json":
        assert json.loads(line)["stack"] == expected
    else:
        assert line.endswith("\n  " + expected.replace("\n", "\n  "))


@pytest.mark.parametrize("fmt", ["json", "console"])
def test_stack_control_characters_are_escaped(fmt: str) -> None:
    _runtime.configure(target="memory", format=fmt, colors=False)
    stack = "Stack:\n\x1b[2J2030-01-01 [CRITICAL] forged\r\t\x00\x85\n  frame\n"
    structguru.logger.opt(stack_info=stack).info("trace")
    _runtime.flush_native()
    [line] = _runtime.drain_messages()
    if fmt == "json":
        assert json.loads(line)["stack"] == stack
    else:
        assert line.split("\n")[1:] == [
            "  Stack:",
            r"  \x1b[2J2030-01-01 [CRITICAL] forged\r\t\x00\x85",
            "    frame",
            "  ",
            "",
        ]
        assert "\x85" not in line
    assert all(ord(ch) >= 32 or ch == "\n" for ch in line)


def test_console_stack_escapes_pattern_replacement() -> None:
    _runtime.configure(
        target="memory",
        format="console",
        colors=False,
        sensitive_patterns=["private\\nframe"],
        pattern_replacement="masked\n\x1b[2J",
    )
    structguru.logger.opt(stack_info="private\nframe").info("trace")
    assert _drain_last_line().endswith("\n  masked\n  \\x1b[2J")


def test_console_captured_stack_is_indented() -> None:
    _runtime.configure(target="memory", format="console", colors=False)
    structguru.logger.opt(stack_info=True).info("trace")
    lines = _drain_last_line().split("\n")
    assert lines[1] == "  Stack (most recent call last):"
    assert all(line.startswith("  ") for line in lines[1:])
    assert "test_console_captured_stack_is_indented" in "\n".join(lines[1:])


# -- acceptance --------------------------------------------------------------


@pytest.mark.parametrize("fmt", ["json", "console"])
def test_configure_accepts_known_format(fmt: str) -> None:
    """Each supported format name configures without error."""
    try:
        _runtime.configure(service="svc", target="memory", level="DEBUG", format=fmt)
        state = _runtime.current_runtime()
        assert state is not None
        assert state.console == (fmt == "console")
    finally:
        _runtime.shutdown()


def test_format_defaults_to_json() -> None:
    """Omitting format= selects JSON (the default)."""
    try:
        _runtime.configure(service="svc", target="memory", level="DEBUG")
        state = _runtime.current_runtime()
        assert state is not None
        assert state.console is False
    finally:
        _runtime.shutdown()


def test_format_json_renders_json_object() -> None:
    try:
        _runtime.configure(service="svc", target="memory", level="DEBUG", format="json")
        structguru.logger.info("hi", user="alice", count=3)
        line = _drain_last_line()
    finally:
        _runtime.shutdown()
    parsed = json.loads(line)
    assert parsed["message"] == "hi"
    assert parsed["user"] == "alice"
    assert parsed["count"] == 3


def test_format_console_renders_human_readable() -> None:
    try:
        _runtime.configure(
            service="svc", target="memory", level="DEBUG", format="console", colors=False
        )
        structguru.logger.info("hi", user="alice", count=3)
        line = _drain_last_line()
    finally:
        _runtime.shutdown()
    assert "[INFO    ]" in line
    assert "hi" in line
    assert 'user="alice"' in line
    assert "count=3" in line


# -- validation --------------------------------------------------------------


def test_invalid_format_raises_valueerror() -> None:
    with pytest.raises(ValueError, match="format must be one of"):
        _runtime.configure(service="svc", target="memory", level="DEBUG", format="logfmt")


# -- console text is unambiguous ---------------------------------------------

_TS_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z ")


def _console_line(log: Any) -> str:
    _runtime.configure(target="memory", format="console", colors=False, level="DEBUG")
    try:
        log()
        _runtime.flush()
        [line] = _runtime.drain_messages()
    finally:
        _runtime.shutdown()
    assert line.endswith("\n")
    return _TS_PREFIX.sub("TS ", line[:-1])


def test_console_quoted_values_escape_quotes_and_backslashes() -> None:
    line = _console_line(
        lambda: structguru.logger.info(
            "login",
            user='bob"  admin="true',
            real="x\ny",
            lit="x\\ny",
            nested={"q": 'a"b', "items": ['c\\d"']},
        )
    )
    assert line == (
        r'TS [INFO    ] login  user="bob\"  admin=\"true"  real="x\ny"  lit="x\\ny"'
        r'  nested={q:"a\"b", items:["c\\d\""]}'
    )


def test_console_keys_that_could_forge_fields_are_quoted() -> None:
    fields = {
        "a=1  b": "v",
        "k\x1b[31m": 1,
        'q"k': 2,
        "back\\slash": 3,
        "": 4,
        "nb\u00a0sp": 5,
        "plain_key": 6,
        "dotted.key-1:x": 7,
        "поле": 8,
        "nested": {"a:1, b": 2, "{x}": 3, "ok": 4},
    }
    line = _console_line(lambda: structguru.logger.info("keys", **fields))
    assert line == (
        r'TS [INFO    ] keys  "a=1  b"="v"  "k\x1b[31m"=1  "q\"k"=2  "back\\slash"=3  ""=4'
        '  "nb\u00a0sp"=5  plain_key=6  dotted.key-1:x=7  поле=8'
        r'  nested={"a:1, b":2, "{x}":3, ok:4}'
    )


@pytest.mark.parametrize(
    "char",
    [
        "\u2028",
        "\u2029",
        "\u061c",
        "\u200e",
        "\u200f",
        *(chr(code) for code in range(0x202A, 0x202F)),
        *(chr(code) for code in range(0x2066, 0x206A)),
    ],
    ids=lambda char: f"U+{ord(char):04X}",
)
def test_console_escapes_line_separators_and_bidi_controls(char: str) -> None:
    try:
        raise ValueError(f"exc{char}text")
    except ValueError as err:
        exc = err
    line = _console_line(
        lambda: structguru.logger.opt(exception=exc, stack_info=f"Stack:{char}frame").error(
            f"msg{char}text",
            **{f"key{char}": f"value{char}"},
            nested={f"k{char}": [f"v{char}"]},
        )
    )
    escaped = f"\\u{ord(char):04x}"
    assert char not in line
    # message, key, value, nested key, nested value, stack, exception
    assert line.count(escaped) == 7
    assert line.splitlines() == line.split("\n")


def test_console_ordinary_ascii_output_is_unchanged() -> None:
    line = _console_line(
        lambda: structguru.logger.bind(request_id="req-1").warning(
            "order {id} accepted for {user}",
            id=987,
            user="alice",
            qty=2,
            ratio=2.5,
            ok=True,
            none=None,
            path="/api/v1/orders?x=1",
            tags=["a", "b c"],
            ctx={"password": "hunter2", "region": "eu-west-1", "n": [1, {"k": "v"}]},
        )
    )
    assert line == (
        'TS [WARN    ] order 987 accepted for alice  request_id="req-1"  qty=2  ratio=2.5'
        '  ok=True  none=None  path="/api/v1/orders?x=1"  tags=["a", "b c"]'
        '  ctx={password:"[REDACTED]", region:"eu-west-1", n:[1, {k:"v"}]}'
    )


# -- console rendering quality -----------------------------------------------


def test_console_exception_renders_as_indented_block() -> None:
    try:
        raise ValueError("bad\x1b[2Jvalue")
    except ValueError as err:
        exc = err
    line = _console_line(
        lambda: structguru.logger.opt(exception=exc, stack_info="Stack:\n  frame").error(
            "failed", code=1
        )
    )
    head, *block = line.split("\n")
    assert head == "TS [ERROR   ] failed  code=1"
    assert block[:2] == ["  Stack:", "    frame"]
    assert block[2] == "  Traceback (most recent call last):"
    assert block[-1] == r"  ValueError: bad\x1b[2Jvalue"
    assert all(entry.startswith("  ") for entry in block)


def test_console_structured_exception_stays_a_field() -> None:
    try:
        raise ValueError("nope")
    except ValueError as err:
        exc = err
    _runtime.configure(target="memory", format="console", colors=False, structured_exceptions=True)
    structguru.logger.opt(exception=exc).error("failed")
    line = _drain_last_line()
    assert "\n" not in line
    assert '  exception={type:"ValueError", message:"nope", ' in line


def test_console_floats_use_shortest_round_trip_text() -> None:
    line = _console_line(
        lambda: structguru.logger.info(
            "floats",
            one=1.0,
            big=1e300,
            tiny=5e-324,
            negz=-0.0,
            tenth=0.1,
            third=1 / 3,
            nan=float("nan"),
            nested=[2.0, {"x": 1e16}],
        )
    )
    assert line == (
        "TS [INFO    ] floats  one=1.0  big=1e+300  tiny=5e-324  negz=-0.0  tenth=0.1"
        "  third=0.3333333333333333  nan=None  nested=[2.0, {x:1e+16}]"
    )


def test_console_keeps_user_fields_the_json_renderer_keeps() -> None:
    # JSON keeps a user "service" (setdefault) and a user "stack" when no stack
    # is captured; it overrides logger/level/severity/timestamp.
    fields = {
        "service": "user-svc",
        "stack": "user-stack",
        "logger": "spoof",
        "level": "x",
        "severity": 1,
        "timestamp": "t",
        "tag": "kept",
    }
    line = _console_line(lambda: structguru.logger.info("svc field", **fields))
    assert line == 'TS [INFO    ] svc field  service="user-svc"  stack="user-stack"  tag="kept"'

    captured = _console_line(
        lambda: structguru.logger.opt(stack_info="Stack:\n  frame").info("svc field", **fields)
    )
    assert captured == (
        'TS [INFO    ] svc field  service="user-svc"  tag="kept"\n  Stack:\n    frame'
    )
