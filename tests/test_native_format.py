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
