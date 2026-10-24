"""Compatibility and redaction boundaries of native local representations."""

from __future__ import annotations

from collections import UserDict
from collections.abc import Iterator, Mapping
from typing import Any

import pytest

from structguru.exceptions import _format_locals
from structguru.redaction import DEFAULT_SENSITIVE_KEYS


def test_local_repr_preserves_python_characters_and_lone_surrogates() -> None:
    class Raw:
        def __repr__(self) -> str:
            return "\udcff🌍é"

    assert _format_locals({"value": Raw()}, DEFAULT_SENSITIVE_KEYS, 2)["value"] == (
        "\udcff🌍...<1 more>"
    )
    assert _format_locals({"value": "🌍" * 10}, DEFAULT_SENSITIVE_KEYS, 4)["value"] == (
        "'🌍🌍🌍...<8 more>"
    )
    assert _format_locals({"value": Raw()}, DEFAULT_SENSITIVE_KEYS, 2**100)["value"] == (
        "\udcff🌍é"
    )


@pytest.mark.parametrize("container", ["mapping", "sequence"])
def test_local_repr_snapshots_before_callbacks_mutate_containers(container: str) -> None:
    value: Any

    class Mutator:
        def __repr__(self) -> str:
            value.clear()
            return "changed"

    if container == "mapping":
        value = {"first": Mutator(), "password": "CANARY", "last": "safe"}
        expected = "{'first': changed, 'password': [REDACTED], 'last': 'safe'}"
    else:
        value = [Mutator(), {"password": "CANARY"}, "safe"]
        expected = "[changed, {'password': [REDACTED]}, 'safe']"
    rendered = _format_locals({"value": value}, DEFAULT_SENSITIVE_KEYS, 200)["value"]
    assert rendered == expected
    assert not value


def test_local_repr_consumes_only_the_bounded_snapshot() -> None:
    seen: list[int] = []

    class Counted(list[int]):
        def __iter__(self) -> Iterator[int]:
            for index in range(10_000):
                seen.append(index)
                yield index

    rendered = _format_locals({"value": Counted()}, DEFAULT_SENSITIVE_KEYS, 100_000)["value"]
    assert seen == list(range(101))
    assert rendered.endswith(", 99, ...])")


def test_local_repr_accepts_mapping_items_as_generic_pairs() -> None:
    class Pairs(Mapping[str, Any]):
        def __getitem__(self, key: str) -> Any:
            raise KeyError(key)

        def __iter__(self) -> Iterator[str]:
            return iter(())

        def __len__(self) -> int:
            return 0

        def items(self) -> Any:
            return iter([["password", "CANARY"], ["shown", 42]])

    rendered = _format_locals({"value": Pairs()}, DEFAULT_SENSITIVE_KEYS, 1000)["value"]
    assert rendered == f"{Pairs.__qualname__}({{'password': [REDACTED], 'shown': 42}})"


def test_local_repr_retains_container_type_across_callbacks() -> None:
    class After(UserDict[str, Any]):
        pass

    class Before(After):
        def items(self) -> Any:
            self.__class__ = After
            return self.data.items()

    value = Before(password="CANARY", shown=42)
    rendered = _format_locals({"value": value}, DEFAULT_SENSITIVE_KEYS, 1000)["value"]
    assert type(value) is After
    assert rendered == f"{Before.__qualname__}({{'password': [REDACTED], 'shown': 42}})"


@pytest.mark.parametrize("error", [KeyboardInterrupt, SystemExit])
def test_local_repr_preserves_control_flow_exceptions(error: type[BaseException]) -> None:
    class Interrupted:
        def __repr__(self) -> str:
            raise error

    with pytest.raises(error):
        _format_locals({"value": [Interrupted()]}, DEFAULT_SENSITIVE_KEYS, 200)


def test_local_repr_shared_references_are_not_cycles() -> None:
    shared = {"token": "CANARY", "visible": 1}
    value = {"left": shared, "right": shared}
    rendered = _format_locals({"value": value}, DEFAULT_SENSITIVE_KEYS, 1000)["value"]
    assert rendered.count("'visible': 1") == 2
    assert rendered.count("[REDACTED]") == 2
    assert "..." not in rendered


def test_local_repr_preserves_custom_sensitive_key_membership() -> None:
    class KeyPolicy(frozenset[str]):
        def __contains__(self, key: object) -> bool:
            return key == "pin"

    value = {"pin": "CANARY", "password": "shown"}
    rendered = _format_locals({"value": value}, KeyPolicy({"password"}), 200)["value"]
    assert rendered == "{'pin': [REDACTED], 'password': 'shown'}"


def test_local_repr_error_markers_use_python_type_name_lookup() -> None:
    class ErrorType(type):
        def __getattribute__(cls, name: str) -> Any:
            return "MaskedError" if name == "__name__" else super().__getattribute__(name)

    class Error(Exception, metaclass=ErrorType):
        pass

    class Broken:
        def __repr__(self) -> str:
            raise Error

    rendered = _format_locals({"value": Broken()}, DEFAULT_SENSITIVE_KEYS, 200)["value"]
    assert rendered == "<repr failed: MaskedError>"
