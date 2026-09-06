from __future__ import annotations

import pytest
import structguru._rust as rust

from structguru import _runtime

# orjson is only the reference serializer for the parity checks below; it has no
# free-threaded wheel, so those checks skip on 3.14t instead of blocking the module.
try:
    import orjson
except ImportError:  # pragma: no cover - exercised on free-threaded builds only
    orjson = None  # type: ignore[assignment]


_needs_orjson = pytest.mark.skipif(orjson is None, reason="orjson not installed")


def orjson_serializer(obj: object) -> str:
    assert orjson is not None
    return orjson.dumps(obj).decode()


def test_native_module_exports_core_version() -> None:
    import structguru

    assert rust.version() == structguru.__version__
    assert _runtime.is_available()


def test_native_level_helpers_match_processor_contract() -> None:
    assert rust.normalize_level("warning") == "WARN"
    assert rust.normalize_level("exception") == "ERROR"
    assert rust.normalize_level("notice") == "NOTICE"

    assert rust.syslog_severity("WARN") == 4
    assert rust.normalized_syslog_severity("fatal") == 2
    assert rust.normalized_syslog_severity("notice") == 6


def test_native_converts_nested_values_to_owned_debug_shape() -> None:
    value = {
        "message": "created",
        "ok": True,
        "attempts": 2,
        "ratio": 0.5,
        "none": None,
        "tags": ("api", "write"),
        "context": {"request_id": "req-1", "ids": [1, 2, 3]},
    }

    assert rust._convert_value_debug(value) == {
        "message": "created",
        "ok": True,
        "attempts": 2,
        "ratio": 0.5,
        "none": None,
        "tags": ["api", "write"],
        "context": {"request_id": "req-1", "ids": [1, 2, 3]},
    }


def test_native_conversion_stats_for_realistic_record() -> None:
    record = {
        "timestamp": "2026-07-06T00:00:00Z",
        "level": "INFO",
        "severity": 6,
        "message": "user created",
        "service": "api",
        "user": {"id": 42, "roles": ["admin", "writer"]},
        "context": {"request_id": "req-1", "retry": False},
    }

    assert rust._conversion_stats(record) == {"nodes": 14, "max_depth": 4}


def test_native_replaces_unsupported_objects_with_marker() -> None:
    # A value the renderer cannot represent becomes a type marker — never
    # str()/repr(), which could leak what the object holds — instead of a
    # TypeError that would lose the whole record.
    import decimal
    import pathlib

    unsupported: tuple[object, ...] = (
        object(),
        b"bytes",
        {1, 2},
        frozenset({3}),
        decimal.Decimal("1.5"),
        pathlib.Path("/tmp/secret-name"),
    )
    for value in unsupported:
        assert rust._convert_value_debug(value) == f"<unsupported: {type(value).__name__}>"


def test_native_replaces_unsupported_objects_inside_containers() -> None:
    value = {"a": [1, object(), {"b": (object(),)}], "c": {"d": b"x"}}

    assert rust._convert_value_debug(value) == {
        "a": [1, "<unsupported: object>", {"b": ["<unsupported: object>"]}],
        "c": {"d": "<unsupported: bytes>"},
    }


def test_native_failing_conversion_collapses_to_marker() -> None:
    # A duck-typed leaf whose own conversion raises must not surface as an
    # exception from the log call; it falls back like any unsupported value.
    class BrokenClock:
        def isoformat(self) -> str:
            raise RuntimeError("clock unavailable")

    assert rust._convert_value_debug(BrokenClock()) == "<unsupported: BrokenClock>"


def test_native_dataclass_field_failure_collapses_to_marker() -> None:
    from dataclasses import dataclass

    @dataclass
    class Broken:
        x: int

        def __getattribute__(self, name: str) -> object:
            if name == "x":
                raise RuntimeError("no x")
            return super().__getattribute__(name)

    assert rust._convert_value_debug(Broken(1)) == "<unsupported: Broken>"


def test_native_never_probes_instance_attributes() -> None:
    # Type detection must look at the class only: an instance ``__getattr__``
    # (Django's LazyObject, ORM proxies) could run arbitrary code — or a
    # database query — for every unsupported object that reaches a log call.
    probed: list[str] = []

    class Lazy:
        def __getattr__(self, name: str) -> object:
            probed.append(name)
            raise RuntimeError("must not be evaluated")

    assert rust._convert_value_debug(Lazy()) == "<unsupported: Lazy>"
    assert probed == []


def test_native_propagates_non_exception_base_exceptions() -> None:
    # Recovery is scoped to ``Exception``: a KeyboardInterrupt or SystemExit
    # raised by a conversion still reaches the caller.
    class Interrupting:
        def isoformat(self) -> str:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        rust._convert_value_debug(Interrupting())


def test_native_cyclic_enum_value_hits_depth_marker_not_abort() -> None:
    # An enum whose .value cycles back to itself must hit the depth guard and
    # yield the bounded marker, not recurse forever into a process abort.
    import enum

    class Cyclic(enum.Enum):
        A = 1

    Cyclic.A._value_ = Cyclic.A  # .value now returns the member itself
    assert rust._convert_value_debug(Cyclic.A) == "<max depth exceeded>"


def test_native_converts_datetime_without_orjson() -> None:
    import datetime as dt

    d = dt.datetime(2026, 7, 10, 12, 0, 0, 5)
    result = rust._convert_value_debug(d)
    assert result == "2026-07-10T12:00:00.000005"


def test_native_converts_date_without_orjson() -> None:
    import datetime as dt

    d = dt.date(2026, 7, 10)
    result = rust._convert_value_debug(d)
    assert result == "2026-07-10"


def test_native_converts_tz_aware_datetime() -> None:
    import datetime as dt

    d = dt.datetime(2026, 7, 10, 12, 0, 0, tzinfo=dt.UTC)
    result = rust._convert_value_debug(d)
    assert "+00:00" in result


def test_native_converts_uuid_without_orjson() -> None:
    import uuid

    u = uuid.UUID("12345678-1234-5678-1234-567812345678")
    result = rust._convert_value_debug(u)
    assert result == "12345678-1234-5678-1234-567812345678"


def test_native_converts_enum_without_orjson() -> None:
    import enum

    class _Color(enum.Enum):
        RED = "red"
        GREEN = 2

    assert rust._convert_value_debug(_Color.RED) == "red"
    assert rust._convert_value_debug(_Color.GREEN) == 2


def test_native_converts_dataclass_without_orjson() -> None:
    from dataclasses import dataclass

    @dataclass
    class Point:
        x: int
        y: int

    result = rust._convert_value_debug(Point(3, 4))
    assert result == {"x": 3, "y": 4}


def test_native_converts_slots_dataclass_without_orjson() -> None:
    from dataclasses import dataclass

    @dataclass(slots=True)
    class Point:
        x: int
        y: int

    assert rust._convert_value_debug(Point(3, 4)) == {"x": 3, "y": 4}


def test_native_converts_non_string_map_keys() -> None:
    import datetime as dt
    import enum

    class Color(enum.Enum):
        RED = "red"

    value = {
        2: "int",
        None: "none",
        True: "bool",
        1.5: "float",
        2**70: "big",
        Color.RED: "enum",
        dt.date(2026, 1, 2): "date",
        ("t",): "tuple",
    }

    assert rust._convert_value_debug(value) == {
        "2": "int",
        "null": "none",
        "true": "bool",
        "1.5": "float",
        "1180591620717411303424": "big",
        "red": "enum",
        "2026-01-02": "date",
        "<unsupported: tuple>": "tuple",
    }


def test_native_converts_integer_overflow_losslessly() -> None:
    assert rust._convert_value_debug(2**100) == 2**100
    assert rust._convert_value_debug({"n": -(2**70)}) == {"n": -(2**70)}


def test_native_integer_over_str_digit_limit_falls_back_to_marker() -> None:
    import sys

    previous = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(640)
    try:
        assert rust._convert_value_debug(10**5000) == "<unsupported: int>"
    finally:
        sys.set_int_max_str_digits(previous)


def test_native_replaces_cycles_with_marker() -> None:
    values: list[object] = []
    values.append(values)
    mapping: dict[str, object] = {"ok": 1}
    mapping["self"] = mapping

    assert rust._convert_value_debug(values) == ["<cycle: list>"]
    assert rust._convert_value_debug(mapping) == {"ok": 1, "self": "<cycle: dict>"}


def test_native_shared_references_are_not_cycles() -> None:
    # The same object reachable twice on different paths is ordinary data.
    shared = {"x": 1}

    assert rust._convert_value_debug([shared, shared, {"n": [shared]}]) == [
        {"x": 1},
        {"x": 1},
        {"n": [{"x": 1}]},
    ]


def test_native_depth_limit_yields_marker() -> None:
    deep: object = "leaf"
    for _ in range(70):
        deep = [deep]

    converted = rust._convert_value_debug(deep)
    levels = 0
    while isinstance(converted, list):
        converted = converted[0]
        levels += 1
    assert levels == 64
    assert converted == "<max depth exceeded>"


def test_native_lone_surrogates_are_replaced_not_rejected() -> None:
    converted = rust._convert_value_debug("a\udcffb")
    assert converted.startswith("a") and converted.endswith("b")
    assert "\udcff" not in converted and "\ufffd" in converted

    key = next(iter(rust._convert_value_debug({"k\udcff": 1})))
    assert key.startswith("k") and "\udcff" not in key


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        False,
        42,
        -7,
        0.5,
        "created",
        ["api", 1, None],
        ("tuple", 2),
        {
            "message": "created",
            "level": "INFO",
            "severity": 6,
            "context": {"request_id": "req-1", "ids": [1, 2, 3]},
            "ok": True,
            "none": None,
        },
    ],
)
@_needs_orjson
def test_native_json_render_matches_orjson_serializer(value: object) -> None:
    assert rust._render_json_debug(value) == orjson_serializer(value)


def test_native_json_render_replaces_unsupported_objects() -> None:
    assert rust._render_json_debug({"o": object()}) == '{"o":"<unsupported: object>"}'


def test_native_json_render_converts_non_string_map_keys() -> None:
    assert rust._render_json_debug({1: "one", None: 2}) == '{"1":"one","null":2}'


def test_native_json_render_emits_big_integers_as_numbers() -> None:
    assert rust._render_json_debug(2**100) == str(2**100)
    assert rust._render_json_debug({"n": -(2**70)}) == f'{{"n":{-(2**70)}}}'


def test_native_json_render_replaces_cycles() -> None:
    values: list[object] = []
    values.append(values)

    assert rust._render_json_debug(values) == '["<cycle: list>"]'
