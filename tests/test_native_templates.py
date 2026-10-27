"""Parity and redaction boundaries of native template metadata analysis."""

from __future__ import annotations

from typing import Any

import pytest

from structguru import core


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("user", ("user", [])),
        ("user.name[0].TOKEN", ("user", ["name", "0", "TOKEN"])),
        ("[password]", ("", ["password"])),
        ("user..token", ("user", ["", "token"])),
        ("user[].", ("user", ["", ""])),
        ("user[unclosed.password", ("user", ["password"])),
        ("user[a[b]].name", ("user", ["a[b", "name"])),
        ("π.🌍[ПАРОЛЬ]", ("π", ["🌍", "ПАРОЛЬ"])),
        ("\udcff.password", ("\udcff", ["password"])),
    ],
)
def test_native_field_paths_retain_python_regex_semantics(
    field: str, expected: tuple[str, list[str]]
) -> None:
    assert core._field_path(field) == expected


def test_native_field_path_preserves_string_subclass_methods() -> None:
    calls: list[str] = []

    class Field(str):
        def find(self, value: str, *args: int) -> int:
            calls.append(value)
            return super().find(value, *args)

    assert core._field_path(Field("user.name")) == ("user", ["name"])
    assert calls == [".", "["]


def test_native_analysis_keeps_nested_specs_and_unicode_digit_rules() -> None:
    result = core._parse_template("{x:{width}.{precision}f} {².name} {π.APIKEY}")
    assert result == (
        frozenset({"x", "width", "precision", "π"}),
        frozenset({"x", "width", "precision", "name", "π", "apikey"}),
        True,
        True,
    )


@pytest.mark.parametrize("message", ["{", "}", "{x!}", "{x:{width}"])
def test_native_analysis_returns_parse_errors(message: str) -> None:
    result = core._parse_template(message)
    assert isinstance(result, ValueError)


@pytest.mark.parametrize("error", [KeyboardInterrupt, SystemExit])
def test_native_analysis_does_not_cache_control_flow_exceptions(
    error: type[BaseException],
) -> None:
    def interrupted_parser(_message: str) -> Any:
        raise error

    with pytest.raises(error):
        core._template_native._analyze_template(
            interrupted_parser, core._python_field_path, core.DEFAULT_SENSITIVE_KEYS, "ignored"
        )


@pytest.mark.parametrize("message", ["{data.Password}", "{data[APIKEY]}", "{\udcff.password}"])
def test_native_analysis_redacts_sensitive_paths_before_lookup(message: str) -> None:
    class Unreadable:
        def __getattr__(self, _name: str) -> Any:
            pytest.fail("sensitive attributes must not be read")

        def __getitem__(self, _key: object) -> Any:
            pytest.fail("sensitive items must not be read")

    result = core._safe_format(message, (), {"data": Unreadable(), "\udcff": Unreadable()})
    assert result[0] == "[REDACTED]"


def test_native_analysis_preserves_the_bounded_python_cache() -> None:
    core._extract_format_keys.cache_clear()
    try:
        for index in range(1100):
            core._extract_format_keys(f"event-{index} {{name}}")
        info = core._extract_format_keys.cache_info()
        assert info.maxsize == info.currsize == 1024
        assert info.misses == 1100
    finally:
        core._extract_format_keys.cache_clear()


@pytest.mark.parametrize(
    ("message", "args", "kwargs", "expected", "consumed"),
    [
        ("Hello {}", ("world",), {}, "Hello world", set()),
        ("{0} {1} {0}", ("a", "b"), {}, "a b a", set()),
        ("{{{name}}}", (), {"name": "Ada"}, "{Ada}", {"name"}),
        (
            "{name} {number} {ratio} {flag} {nothing}",
            (),
            {"name": "Ada", "number": 42, "ratio": 1.0, "flag": True, "nothing": None},
            "Ada 42 1.0 True None",
            {"name", "number", "ratio", "flag", "nothing"},
        ),
    ],
)
def test_native_simple_format_matches_python_semantics(
    message: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    expected: str,
    consumed: set[str],
) -> None:
    assert core._safe_format(message, args, kwargs) == (expected, consumed)


def test_native_simple_format_redacts_before_lookup_and_skips_python_analysis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Unreadable:
        def __str__(self) -> str:
            pytest.fail("a sensitive scalar placeholder must not read its value")

    def fail_analysis(_message: str) -> Any:
        pytest.fail("the simple native path must not invoke Python template analysis")

    monkeypatch.setattr(core, "_extract_format_keys", fail_analysis)
    assert core._safe_format("{password}", (), {"password": Unreadable()}) == (
        "[REDACTED]",
        {"password"},
    )
