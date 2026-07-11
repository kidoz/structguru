"""Tests for structguru.exceptions.build_exception_dict."""

from __future__ import annotations

import sys

import pytest

from structguru.exceptions import build_exception_dict


def _make_exc_info() -> tuple:
    try:
        raise ValueError("boom")
    except ValueError:
        return sys.exc_info()


class TestBuildExceptionDict:
    def test_converts_exc_info_tuple(self) -> None:
        exc_info = _make_exc_info()
        result = build_exception_dict(exc_info)
        assert result is not None
        assert result["type"] == "ValueError"
        assert result["message"] == "boom"
        assert result["module"] == "builtins"
        assert len(result["frames"]) > 0

    def test_converts_exc_info_true(self) -> None:
        try:
            raise RuntimeError("test")
        except RuntimeError:
            result = build_exception_dict(True)
        assert result is not None
        assert result["type"] == "RuntimeError"

    def test_converts_exception_instance(self) -> None:
        try:
            raise TypeError("oops")
        except TypeError as e:
            result = build_exception_dict(e)
        assert result is not None
        assert result["type"] == "TypeError"

    def test_returns_none_for_no_exception(self) -> None:
        assert build_exception_dict(False) is None

    def test_chained_cause(self) -> None:
        try:
            try:
                raise KeyError("original")
            except KeyError as cause:
                raise ValueError("wrapper") from cause
        except ValueError:
            exc_info = sys.exc_info()

        result = build_exception_dict(exc_info)
        assert result is not None
        assert result["cause"]["type"] == "KeyError"
        assert result["cause"]["message"] == "'original'"

    def test_max_frames(self) -> None:
        exc_info = _make_exc_info()
        result = build_exception_dict(exc_info, max_frames=1)
        assert result is not None
        assert len(result["frames"]) <= 1

    def test_zero_max_frames_captures_no_frames_or_locals(self) -> None:
        exc_info = _make_exc_info()
        result = build_exception_dict(exc_info, include_locals=True, max_frames=0)
        assert result is not None
        assert result["frames"] == []

    @pytest.mark.parametrize(
        ("kwargs", "match"),
        [
            ({"max_frames": -1}, "max_frames"),
            ({"max_local_repr": -1}, "max_local_repr"),
        ],
    )
    def test_negative_limits_are_rejected(self, kwargs: dict[str, int], match: str) -> None:
        with pytest.raises(ValueError, match=match):
            build_exception_dict(_make_exc_info(), **kwargs)

    def test_include_locals_captures_variables(self) -> None:
        def inner() -> None:
            local_val = 123
            _ = local_val
            raise ValueError("test")

        try:
            inner()
        except ValueError:
            exc_info = sys.exc_info()

        result = build_exception_dict(exc_info, include_locals=True)
        assert result is not None
        inner_frames = [f for f in result["frames"] if f["name"] == "inner"]
        assert len(inner_frames) == 1
        assert "locals" in inner_frames[0]
        assert inner_frames[0]["locals"]["local_val"] == "123"

    def test_malformed_exc_info_tuple_short(self) -> None:
        assert build_exception_dict((ValueError("x"),)) is None

    def test_malformed_exc_info_empty_tuple(self) -> None:
        assert build_exception_dict(()) is None

    def test_implicit_chaining_via_context(self) -> None:
        try:
            try:
                raise KeyError("original")
            except KeyError:
                raise ValueError("wrapper")  # noqa: B904
        except ValueError:
            exc_info = sys.exc_info()

        result = build_exception_dict(exc_info)
        assert result is not None
        assert result["cause"]["type"] == "KeyError"

    def test_include_locals_redacts_sensitive_names(self) -> None:
        def inner() -> None:
            password = "hunter2"  # noqa: S105
            api_key = "sk-live-abc"
            safe = "ok"
            _ = (password, api_key, safe)
            raise ValueError("x")

        try:
            inner()
        except ValueError:
            exc_info = sys.exc_info()

        result = build_exception_dict(exc_info, include_locals=True)
        assert result is not None
        locals_ = next(f for f in result["frames"] if f["name"] == "inner")["locals"]
        assert locals_["password"] == "[REDACTED]"
        assert locals_["api_key"] == "[REDACTED]"
        assert locals_["safe"] == "'ok'"

    def test_include_locals_truncates_long_repr(self) -> None:
        def inner() -> None:
            big = "x" * 500
            _ = big
            raise ValueError("x")

        try:
            inner()
        except ValueError:
            exc_info = sys.exc_info()

        result = build_exception_dict(exc_info, include_locals=True, max_local_repr=32)
        assert result is not None
        rendered = next(f for f in result["frames"] if f["name"] == "inner")["locals"]["big"]
        assert len(rendered) < 100
        assert rendered.endswith("more>")

    def test_include_locals_handles_broken_repr(self) -> None:
        class Bad:
            def __repr__(self) -> str:
                raise RuntimeError("nope")

        def inner() -> None:
            weird = Bad()
            _ = weird
            raise ValueError("x")

        try:
            inner()
        except ValueError:
            exc_info = sys.exc_info()

        result = build_exception_dict(exc_info, include_locals=True)
        assert result is not None
        rendered = next(f for f in result["frames"] if f["name"] == "inner")["locals"]["weird"]
        assert "repr failed" in rendered

    def test_suppress_context_via_raise_from_none(self) -> None:
        try:
            try:
                raise KeyError("original")
            except KeyError:
                raise ValueError("wrapper") from None
        except ValueError:
            exc_info = sys.exc_info()

        result = build_exception_dict(exc_info)
        assert result is not None
        assert "cause" not in result
