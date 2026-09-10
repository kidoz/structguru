"""Tests for the ``format=`` renderer selector on :func:`configure`.

Covers:
- ``format="json"`` (default) and ``format="console"`` acceptance
- the default (omitting ``format=``) selects JSON
- each format renders the expected shape
- validation (invalid format name)
"""

from __future__ import annotations

import json

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
