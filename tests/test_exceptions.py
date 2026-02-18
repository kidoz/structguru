"""Tests for structguru.exceptions."""

from __future__ import annotations

from structguru.exceptions import ExceptionDictProcessor


class TestExceptionDictProcessor:
    def _make_exc_info(self) -> tuple:
        try:
            raise ValueError("boom")
        except ValueError:
            import sys

            return sys.exc_info()

    def test_converts_exc_info_tuple(self) -> None:
        proc = ExceptionDictProcessor()
        exc_info = self._make_exc_info()
        ed: dict = {"event": "fail", "exc_info": exc_info}
        result = proc(None, "error", ed)

        assert "exception" in result
        assert "exc_info" not in result
        assert result["exception"]["type"] == "ValueError"
        assert result["exception"]["message"] == "boom"
        assert result["exception"]["module"] == "builtins"
        assert len(result["exception"]["frames"]) > 0

    def test_converts_exc_info_true(self) -> None:
        proc = ExceptionDictProcessor()
        try:
            raise RuntimeError("test")
        except RuntimeError:
            ed: dict = {"event": "fail", "exc_info": True}
            result = proc(None, "error", ed)
        assert result["exception"]["type"] == "RuntimeError"

    def test_converts_exception_instance(self) -> None:
        proc = ExceptionDictProcessor()
        try:
            raise TypeError("oops")
        except TypeError as e:
            ed: dict = {"event": "fail", "exc_info": e}
            result = proc(None, "error", ed)
        assert result["exception"]["type"] == "TypeError"

    def test_no_exc_info_passthrough(self) -> None:
        proc = ExceptionDictProcessor()
        ed: dict = {"event": "ok"}
        result = proc(None, "info", ed)
        assert "exception" not in result

    def test_chained_cause(self) -> None:
        proc = ExceptionDictProcessor()
        try:
            try:
                raise KeyError("original")
            except KeyError as cause:
                raise ValueError("wrapper") from cause
        except ValueError:
            import sys

            exc_info = sys.exc_info()

        ed: dict = {"event": "fail", "exc_info": exc_info}
        result = proc(None, "error", ed)
        assert result["exception"]["cause"]["type"] == "KeyError"
        assert result["exception"]["cause"]["message"] == "'original'"

    def test_max_frames(self) -> None:
        proc = ExceptionDictProcessor(max_frames=1)
        exc_info = self._make_exc_info()
        ed: dict = {"event": "fail", "exc_info": exc_info}
        result = proc(None, "error", ed)
        assert len(result["exception"]["frames"]) <= 1

    def test_include_locals_captures_variables(self) -> None:
        proc = ExceptionDictProcessor(include_locals=True)

        def inner() -> None:
            local_val = 123
            _ = local_val  # ensure it's used
            raise ValueError("test")

        try:
            inner()
        except ValueError:
            import sys

            exc_info = sys.exc_info()

        ed: dict = {"event": "fail", "exc_info": exc_info}
        result = proc(None, "error", ed)

        inner_frames = [f for f in result["exception"]["frames"] if f["name"] == "inner"]
        assert len(inner_frames) == 1
        assert "locals" in inner_frames[0]
        assert "local_val" in inner_frames[0]["locals"]
        assert inner_frames[0]["locals"]["local_val"] == "123"

    def test_false_exc_info_passthrough(self) -> None:
        proc = ExceptionDictProcessor()
        ed: dict = {"event": "ok", "exc_info": False}
        result = proc(None, "info", ed)
        assert "exception" not in result

    def test_malformed_exc_info_tuple_short(self) -> None:
        proc = ExceptionDictProcessor()
        ed: dict = {"event": "fail", "exc_info": (ValueError("x"),)}
        result = proc(None, "error", ed)
        assert "exception" not in result

    def test_malformed_exc_info_tuple_two_elements(self) -> None:
        proc = ExceptionDictProcessor()
        ed: dict = {"event": "fail", "exc_info": ("x", "y")}
        result = proc(None, "error", ed)
        assert "exception" not in result

    def test_malformed_exc_info_empty_tuple(self) -> None:
        proc = ExceptionDictProcessor()
        ed: dict = {"event": "fail", "exc_info": ()}
        result = proc(None, "error", ed)
        assert "exception" not in result

    def test_implicit_chaining_via_context(self) -> None:
        proc = ExceptionDictProcessor()
        try:
            try:
                raise KeyError("original")
            except KeyError:
                raise ValueError("wrapper")  # noqa: B904
        except ValueError:
            import sys

            exc_info = sys.exc_info()

        ed: dict = {"event": "fail", "exc_info": exc_info}
        result = proc(None, "error", ed)
        assert result["exception"]["cause"]["type"] == "KeyError"
        assert result["exception"]["cause"]["message"] == "'original'"

    def test_suppress_context_via_raise_from_none(self) -> None:
        proc = ExceptionDictProcessor()
        try:
            try:
                raise KeyError("original")
            except KeyError:
                raise ValueError("wrapper") from None
        except ValueError:
            import sys

            exc_info = sys.exc_info()

        ed: dict = {"event": "fail", "exc_info": exc_info}
        result = proc(None, "error", ed)
        assert "cause" not in result["exception"]
