"""Tests for structguru.exceptions.build_exception_dict."""

from __future__ import annotations

import argparse
import enum
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from types import MappingProxyType, SimpleNamespace
from typing import Any, NamedTuple

import pytest

from structguru.exceptions import build_exception_dict


def test_structured_logging_snapshots_locals_before_repr_mutates_them() -> None:
    from structguru import _runtime
    from structguru.core import Logger

    namespace = {"logger": Logger()}

    class MutatingRepr:
        def __repr__(self) -> str:
            namespace["added_during_repr"] = True
            return "safe representation"

    namespace["value"] = MutatingRepr()
    _runtime.configure(target="memory", structured_exceptions=True, exception_include_locals=True)
    exec(
        "try:\n    raise ValueError('original')\n"
        "except ValueError:\n    logger.exception('caught')",
        namespace,
    )
    _runtime.flush()
    result = json.loads(_runtime.drain_messages()[-1])["exception"]
    assert result["message"] == "original"
    assert namespace["added_during_repr"] is True
    assert result["frames"][-1]["locals"]["value"] == "safe representation"
    assert "added_during_repr" not in result["frames"][-1]["locals"]


def test_broken_exception_messages_preserve_the_record_and_cause() -> None:
    from structguru import _runtime
    from structguru.core import Logger

    class BrokenMessage(Exception):
        def __str__(self) -> str:
            raise RuntimeError("private conversion detail")

    error = BrokenMessage()
    error.__cause__ = BrokenMessage()
    _runtime.configure(target="memory", structured_exceptions=True)
    Logger().error("original log", exc_info=error)
    _runtime.flush()
    line = _runtime.drain_messages()[-1]
    result = json.loads(line)
    assert result["message"] == "original log"
    assert result["exception"]["message"] == "<str failed: RuntimeError>"
    assert result["exception"]["cause"]["message"] == "<str failed: RuntimeError>"
    assert "private conversion detail" not in line


@pytest.mark.parametrize("include_locals", [False, True])
def test_nested_exception_groups_preserve_members_and_redact_them(include_locals: bool) -> None:
    from structguru import _runtime
    from structguru.core import Logger

    def make_error() -> ValueError:
        password = "private-value"
        try:
            raise ValueError(password)
        except ValueError as exc:
            return exc

    original = make_error()
    original.__cause__ = TypeError("cause private-value")
    group = ExceptionGroup(
        "group private-value",
        [
            original,
            ExceptionGroup("nested", [TypeError("nested private-value")]),
        ],
    )
    _runtime.configure(
        target="memory",
        structured_exceptions=True,
        exception_include_locals=include_locals,
        exception_max_frames=1,
        sensitive_patterns=["private-value"],
    )
    Logger().error("group", exc_info=group)
    _runtime.flush()
    line = _runtime.drain_messages()[-1]
    result = json.loads(line)["exception"]
    assert "private-value" not in line
    first, nested = result["exceptions"]
    assert first["type"] == "ValueError"
    assert first["message"] == "[REDACTED]"
    assert first["cause"]["message"] == "cause [REDACTED]"
    assert len(first["frames"]) == 1
    if include_locals:
        assert first["frames"][0]["locals"]["password"] == "[REDACTED]"
    assert nested["exceptions"][0]["type"] == "TypeError"
    assert nested["exceptions"][0]["message"] == "nested [REDACTED]"


def test_base_exception_groups_preserve_interrupt_members_as_data() -> None:
    result = build_exception_dict(BaseExceptionGroup("interrupts", [KeyboardInterrupt("stop")]))
    assert result is not None
    assert result["exceptions"][0]["type"] == "KeyboardInterrupt"
    assert result["exceptions"][0]["message"] == "stop"


def test_exception_groups_bound_depth_and_total_nodes() -> None:
    wide = build_exception_dict(ExceptionGroup("wide", [ValueError(str(i)) for i in range(200)]))
    assert wide is not None
    assert len(wide["exceptions"]) == 99
    assert wide["exceptions_truncated"] == 101
    error: BaseException = ValueError("leaf")
    for _ in range(30):
        error = ExceptionGroup("deep", [error])
    deep = build_exception_dict(error)
    assert deep is not None
    depth = 0
    while deep["exceptions"]:
        deep = deep["exceptions"][0]
        depth += 1
    assert depth == 10
    assert deep["exceptions_truncated"] == 1


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

    def test_only_kept_frames_are_extracted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A deep traceback must not pay for source lookup on frames it discards."""
        import traceback

        from structguru import exceptions

        def recurse(n: int) -> int:
            if n == 0:
                raise ValueError("bottom")
            return recurse(n - 1)

        try:
            recurse(200)
        except ValueError:
            exc_info = sys.exc_info()

        extracted_depths: list[int] = []
        original = traceback.extract_tb

        def counting_extract_tb(tb: object, limit: int | None = None) -> object:
            depth = 0
            node = tb
            while node is not None:
                depth += 1
                node = node.tb_next  # type: ignore[attr-defined]
            extracted_depths.append(depth)
            return original(tb, limit)  # type: ignore[arg-type]

        monkeypatch.setattr(exceptions.traceback, "extract_tb", counting_extract_tb)
        result = build_exception_dict(exc_info, max_frames=5)
        assert result is not None
        assert len(result["frames"]) == 5
        assert extracted_depths == [5], "extract_tb saw frames that were then discarded"
        assert all(frame["name"] == "recurse" for frame in result["frames"])

    def test_locals_and_source_line_are_both_captured(self) -> None:
        """Enabling locals must not drop the source line the default path reports."""

        def inner() -> None:
            answer = 41
            raise ValueError(f"value {answer}")

        try:
            inner()
        except ValueError:
            exc_info = sys.exc_info()

        with_locals = build_exception_dict(exc_info, include_locals=True)
        without = build_exception_dict(exc_info, include_locals=False)
        assert with_locals is not None and without is not None
        frame = with_locals["frames"][-1]
        assert frame["line"] == 'raise ValueError(f"value {answer}")'
        assert frame["locals"]["answer"] == "41"
        stripped = [{k: v for k, v in f.items() if k != "locals"} for f in with_locals["frames"]]
        assert stripped == without["frames"]


@dataclass
class _Account:
    name: str
    password: str


@dataclass
class _MaskedDsn:
    dsn: str

    def __repr__(self) -> str:
        return "_MaskedDsn(<hidden>)"


@dataclass
class _PartialRepr:
    name: str
    internal: str = field(repr=False)


class _Credentials(NamedTuple):
    user: str
    token: str


class _LoudDict(dict[str, Any]):
    def __repr__(self) -> str:
        raise AssertionError("a walked container's own __repr__ must not run")


class _Field(enum.Enum):
    PASSWORD = "pw"  # matched by name
    CREDENTIAL = "token"  # matched by value
    USER = "user"


class _Flag(enum.Flag):
    A = 1
    B = 2


def _frame_locals(fn: Callable[[], None], **kwargs: Any) -> dict[str, str]:
    try:
        fn()
    except ValueError as exc:
        result = build_exception_dict(exc, include_locals=True, **kwargs)
    else:
        raise AssertionError("fn did not raise")
    assert result is not None
    locals_: dict[str, str] = result["frames"][-1]["locals"]
    return locals_


class TestLocalRedaction:
    """Values under sensitive keys stay redacted however deeply a local nests them."""

    def test_sensitive_keys_inside_values_are_redacted(self) -> None:
        def handler() -> None:
            creds = {"password": "CANARY-PW", "user": "alice"}
            headers = {"Authorization": "Bearer CANARY-HDR", "Accept": "json"}
            nested = [({"inner": {"api_key": "CANARY-KEY"}},), "visible"]
            account = _Account("alice", "CANARY-DC")
            namespace = SimpleNamespace(token="CANARY-NS", region="eu")
            pair = _Credentials("alice", "CANARY-NT")
            proxy = MappingProxyType({"secret": "CANARY-MP"})
            loud = _LoudDict(cookie="CANARY-LD")
            _ = (creds, headers, nested, account, namespace, pair, proxy, loud)
            raise ValueError("x")

        locals_ = _frame_locals(handler)
        assert "CANARY" not in json.dumps(locals_)
        assert locals_["creds"] == "{'password': [REDACTED], 'user': 'alice'}"
        assert locals_["headers"] == "{'Authorization': [REDACTED], 'Accept': 'json'}"
        assert locals_["nested"] == "[({'inner': {'api_key': [REDACTED]}},), 'visible']"
        assert locals_["account"] == "_Account(name='alice', password=[REDACTED])"
        assert locals_["namespace"] == "namespace(token=[REDACTED], region='eu')"
        assert locals_["pair"] == "_Credentials(user='alice', token=[REDACTED])"
        assert locals_["proxy"] == "mappingproxy({'secret': [REDACTED]})"
        assert locals_["loud"] == "_LoudDict({'cookie': [REDACTED]})"

    def test_tuple_subclass_pairs_are_redacted(self) -> None:
        class Pair(tuple):
            pass

        def handler() -> None:
            headers = [Pair((b"Authorization", "CANARY-TUPLE"))]
            _ = headers
            raise ValueError("x")

        locals_ = _frame_locals(handler)
        assert "CANARY-TUPLE" not in json.dumps(locals_)
        assert locals_["headers"] == "[(b'Authorization', [REDACTED])]"

    def test_namespaces_byte_and_enum_keys_and_pairs_are_redacted(self) -> None:
        def handler() -> None:
            args = argparse.Namespace(user="alice", password="CANARY-AP")
            raw = {b"Token": "CANARY-BK", b"\xff": "kept", b"user": "alice"}
            by_enum = {
                _Field.PASSWORD: "CANARY-EN",
                _Field.CREDENTIAL: "CANARY-EV",
                _Field.USER: 1,
            }
            flags = {_Flag.A | _Flag.B: "kept"}
            headers = [("Authorization", "CANARY-PR"), ("Accept", "json"), ("x", "y", "z")]
            parser = argparse.ArgumentParser(prog="tool", add_help=False)
            _ = (args, raw, by_enum, flags, headers, parser)
            raise ValueError("x")

        locals_ = _frame_locals(handler)
        assert "CANARY" not in json.dumps(locals_)
        assert locals_["args"] == "Namespace(user='alice', password=[REDACTED])"
        assert locals_["raw"] == "{b'Token': [REDACTED], b'\\xff': 'kept', b'user': 'alice'}"
        assert locals_["by_enum"] == (
            "{<_Field.PASSWORD: 'pw'>: [REDACTED], <_Field.CREDENTIAL: 'token'>: [REDACTED], "
            "<_Field.USER: 'user'>: 1}"
        )
        assert locals_["flags"].endswith(": 'kept'}")
        assert locals_["headers"] == (
            "[('Authorization', [REDACTED]), ('Accept', 'json'), ('x', 'y', 'z')]"
        )
        assert locals_["parser"].startswith("ArgumentParser(prog='tool'")

    def test_custom_sensitive_keys_replace_the_defaults(self) -> None:
        def handler() -> None:
            payload = {"pin": "CANARY", "password": "shown"}
            _ = payload
            raise ValueError("x")

        locals_ = _frame_locals(handler, sensitive_keys=frozenset({"pin"}))
        assert locals_["payload"] == "{'pin': [REDACTED], 'password': 'shown'}"

    def test_dataclass_repr_choices_are_honoured(self) -> None:
        def handler() -> None:
            masked = _MaskedDsn("postgres://user:CANARY@db")
            partial = _PartialRepr("visible", "CANARY")
            _ = (masked, partial)
            raise ValueError("x")

        locals_ = _frame_locals(handler)
        assert locals_["masked"] == "_MaskedDsn(<hidden>)"
        assert locals_["partial"] == "_PartialRepr(name='visible')"

    def test_walk_is_cycle_safe_and_bounded(self) -> None:
        def handler() -> None:
            loop: dict[str, Any] = {"token": "CANARY"}
            loop["self"] = loop
            chain: list[Any] = []
            chain.append(chain)
            deep: dict[str, Any] = {}
            current = deep
            for _ in range(100):
                current["next"] = current = {}
            wide = list(range(10_000))
            _ = (loop, chain, deep, wide)
            raise ValueError("x")

        locals_ = _frame_locals(handler, max_local_repr=100_000)
        assert locals_["loop"] == "{'token': [REDACTED], 'self': ...}"
        assert locals_["chain"] == "[...]"
        assert locals_["deep"].count("{") < 20
        assert locals_["deep"].endswith("...}}}}}}}}")
        assert locals_["wide"].endswith(", ...]")
        assert len(locals_["wide"]) < 1_000

    def test_oversized_values_are_truncated_without_rendering_them_whole(self) -> None:
        def handler() -> None:
            text = "x" * 1_000_000
            _ = text
            raise ValueError("x")

        rendered = _frame_locals(handler, max_local_repr=10)["text"]
        assert rendered.startswith("'xxxxxxxxx...<")
        assert rendered.endswith("+ more>")

    def test_patterns_see_the_whole_repr_before_truncation(self) -> None:
        seen: list[str] = []

        def redact(text: str) -> str:
            seen.append(text)
            return text.replace("CANARY-SECRET", "[REDACTED]")

        def handler() -> None:
            note = "prefix.. CANARY-SECRET and more text after it"
            _ = note
            raise ValueError("x")

        rendered = _frame_locals(handler, max_local_repr=16, redact=redact)["note"]
        assert "'prefix.. CANARY-SECRET and more text after it'" in seen
        assert rendered == "'prefix.. [REDAC...<28 more>"

    def test_failing_pattern_callback_fails_closed(self) -> None:
        def redact(text: str) -> str:
            raise RuntimeError("boom")

        def handler() -> None:
            note = "CANARY"
            _ = note
            raise ValueError("x")

        assert _frame_locals(handler, redact=redact)["note"] == "[REDACTED]"


def test_runtime_locals_redact_patterns_before_truncation_and_hide_bound_loggers() -> None:
    from structguru import _runtime
    from structguru.core import Logger

    _runtime.configure(
        target="memory",
        structured_exceptions=True,
        exception_include_locals=True,
        exception_max_local_repr=30,
        sensitive_patterns=[r"tok_[A-Za-z0-9]{24}"],
    )

    def handler() -> None:
        log = Logger().bind(access_token="CANARY-BOUND")
        creds = {"password": "CANARY-PW"}
        note = "prefix........ tok_ABCDEFGHIJKLMNOPQRSTUVWX"
        _ = (log, creds, note)
        raise ValueError("x")

    try:
        handler()
    except ValueError:
        Logger().exception("boom")
    _runtime.flush()
    line = _runtime.drain_messages()[-1]
    frame_locals = json.loads(line)["exception"]["frames"][-1]["locals"]
    assert "CANARY" not in line
    assert "tok_ABC" not in line
    assert frame_locals["note"] == "'prefix........ [REDACTED]'"
    assert frame_locals["creds"] == "{'password': [REDACTED]}"


def test_locals_pattern_pass_is_wired_only_when_patterns_are_configured() -> None:
    from structguru import _runtime

    _runtime.configure(target="null", structured_exceptions=True, exception_include_locals=True)
    state = _runtime.current_runtime()
    assert state is not None and state.exception_config is not None
    assert state.exception_config["redact"] is None
    _runtime.update(sensitive_patterns=[r"tok_\w+"], pattern_replacement="<tok>")
    state = _runtime.current_runtime()
    assert state is not None and state.exception_config is not None
    assert state.exception_config["redact"]("a tok_123 b") == "a <tok> b"
