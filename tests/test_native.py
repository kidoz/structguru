from __future__ import annotations

import orjson
import pytest
import structguru._rust as rust

from structguru import _runtime


def orjson_serializer(obj: object) -> str:
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


def test_native_rejects_unsupported_objects() -> None:
    # Genuinely unserializable types raise TypeError, matching the orjson
    # rejection contract for bytes/set/Decimal/etc. (no orjson in the path now).
    for unsupported in (object(), b"bytes", {1, 2}, frozenset({3})):
        with pytest.raises(TypeError):
            rust._convert_value_debug(unsupported)


def test_native_cyclic_enum_value_raises_not_aborts() -> None:
    # An enum whose .value cycles back to itself must hit the recursion-depth
    # guard and raise cleanly, not recurse forever into a process abort.
    import enum

    class Cyclic(enum.Enum):
        A = 1

    Cyclic.A._value_ = Cyclic.A  # .value now returns the member itself
    with pytest.raises((RecursionError, ValueError)):
        rust._convert_value_debug(Cyclic.A)


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


def test_native_rejects_non_string_map_keys() -> None:
    with pytest.raises(TypeError, match="map keys must be strings"):
        rust._convert_value_debug({1: "one"})


def test_native_rejects_integer_overflow() -> None:
    with pytest.raises(OverflowError, match="i64"):
        rust._convert_value_debug(2**100)


def test_native_rejects_cycles() -> None:
    values: list[object] = []
    values.append(values)

    with pytest.raises(ValueError, match="cycle detected"):
        rust._convert_value_debug(values)


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
def test_native_json_render_matches_orjson_serializer(value: object) -> None:
    assert rust._render_json_debug(value) == orjson_serializer(value)


def test_native_json_render_rejects_unsupported_objects() -> None:
    with pytest.raises(TypeError):
        rust._render_json_debug(object())


def test_native_json_render_rejects_non_string_map_keys() -> None:
    with pytest.raises(TypeError, match="map keys must be strings"):
        rust._render_json_debug({1: "one"})


def test_native_json_render_rejects_integer_overflow() -> None:
    with pytest.raises(OverflowError, match="i64"):
        rust._render_json_debug(2**100)


def test_native_json_render_rejects_cycles() -> None:
    values: list[object] = []
    values.append(values)

    with pytest.raises(ValueError, match="cycle detected"):
        rust._render_json_debug(values)
