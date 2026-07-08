from __future__ import annotations

import pytest
import structguru._rust as rust

from structguru import _native
from structguru.config import orjson_serializer


def test_native_module_exports_core_version() -> None:
    assert rust.version() == "0.2.0"
    assert _native.native_available()


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
    # Exotic leaves are delegated to orjson; genuinely unserializable objects
    # still raise TypeError (orjson.JSONEncodeError), matching the live renderer.
    with pytest.raises(TypeError):
        rust._convert_value_debug(object())


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
