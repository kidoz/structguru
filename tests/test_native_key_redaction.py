"""Preserve sensitive mapping-key identity through native conversion."""

from __future__ import annotations

import enum
import json
import logging
from typing import Any

import pytest

import structguru
from structguru import _runtime
from structguru.integrations.stdlib import StructguruHandler


class Key(enum.Enum):
    password = 1
    ordinary = "AUTHORIZATION"
    private = 2


class IntegerKey(enum.IntEnum):
    PASSWORD = 3


class StringKey(enum.StrEnum):
    password = "public"


@pytest.mark.parametrize("format", ["json", "console"])
@pytest.mark.parametrize(
    "key", [b"AUTHORIZATION", Key.password, Key.ordinary, IntegerKey.PASSWORD, StringKey.password]
)
@pytest.mark.parametrize("route", ["ordinary", "captured", "stdlib"])
def test_sensitive_original_keys_are_redacted_before_all_outputs(
    format: str, key: object, route: str, recwarn: pytest.WarningsRecorder
) -> None:
    delivered: list[str] = []
    events: list[dict[str, Any]] = []

    def sentry(_logger: Any, _method: str, fields: dict[str, Any]) -> dict[str, Any]:
        events.append(fields)
        return fields

    _runtime.configure(
        target="memory",
        format=format,
        colors=False,
        sentry_processor=sentry if route == "captured" else None,
    )
    sink = structguru.logger.add(delivered.append) if route == "captured" else None
    try:
        payload = [{key: "CANARY", "safe": 42}]
        if route == "stdlib":
            record = logging.LogRecord("foreign", logging.INFO, "", 1, "record", (), None)
            record.payload = payload
            StructguruHandler().handle(record)
        else:
            structguru.logger.info("record", payload=payload)
        _runtime.flush()
        [line] = _runtime.drain_messages()
        assert "CANARY" not in line
        assert "42" in line and "[REDACTED]" in line
        assert all("CANARY" not in str(w.message) for w in recwarn)
        if sink is not None:
            assert len(delivered) == len(events) == 1
            assert "CANARY" not in delivered[0]
            assert "CANARY" not in json.dumps(events)
    finally:
        if sink is not None:
            structguru.logger.remove(sink)


def test_bytes_keys_keep_distinct_names_instead_of_collapsing() -> None:
    _runtime.configure(target="memory")
    structguru.logger.info(
        "record", payload={b"first": "one", b"authorization": "CANARY", b"last": "two"}
    )
    _runtime.flush()
    [line] = _runtime.drain_messages()
    assert json.loads(line)["payload"] == {
        "first": "one",
        "authorization": "[REDACTED]",
        "last": "two",
    }


@pytest.mark.parametrize("keys", [[], ["PRIVATE"]])
def test_original_key_redaction_respects_replaced_sensitive_keys(keys: list[str]) -> None:
    _runtime.configure(target="memory", sensitive_keys=keys)
    structguru.logger.info(
        "record", payload={Key.private: "value", b"PRIVATE": "bytes", "password": "retained"}
    )
    _runtime.flush()
    [line] = _runtime.drain_messages()
    payload = json.loads(line)["payload"]
    assert payload == {
        "2": "[REDACTED]" if keys else "value",
        "PRIVATE": "[REDACTED]" if keys else "bytes",
        "password": "retained",
    }


@pytest.mark.parametrize("sensitive_last", [False, True])
def test_enum_key_collisions_retain_last_value_and_its_policy(sensitive_last: bool) -> None:
    pairs = [(Key.password, "CANARY"), ("1", "visible")]
    if sensitive_last:
        pairs.reverse()
    _runtime.configure(target="memory")
    structguru.logger.info("record", payload=dict(pairs))
    _runtime.flush()
    [line] = _runtime.drain_messages()
    assert json.loads(line)["payload"] == {"1": "[REDACTED]" if sensitive_last else "visible"}


def test_string_enum_top_level_field_uses_its_member_name() -> None:
    _runtime.configure(target="memory")
    structguru.logger.bind(**{StringKey.password: "CANARY"}).info("record")
    _runtime.flush()
    [line] = _runtime.drain_messages()
    assert json.loads(line)["public"] == "[REDACTED]"


def test_custom_isoformat_and_uuid_impostors_are_never_executed() -> None:
    calls: list[str] = []

    class ISO:
        def isoformat(self) -> str:
            calls.append("isoformat")
            return "CANARY"

    class UUID:
        hex = "value"
        int = 1

        def __str__(self) -> str:
            calls.append("str")
            return "CANARY"

    _runtime.configure(target="memory")
    structguru.logger.info("record", iso=ISO(), uuid=UUID(), safe=42)
    _runtime.flush()
    [line] = _runtime.drain_messages()
    assert calls == []
    assert "CANARY" not in line
    assert json.loads(line)["iso"] == "<unsupported: ISO>"
    assert json.loads(line)["uuid"] == "<unsupported: UUID>"
