"""Tests for the public request-scoped context API."""

from __future__ import annotations

import structguru


def test_contextvars_helpers_are_exported_from_package_root() -> None:
    names = {
        "bind_contextvars",
        "bound_contextvars",
        "clear_contextvars",
        "get_contextvars",
    }

    assert names <= set(structguru.__all__)
    assert all(callable(getattr(structguru, name)) for name in names)


def test_get_contextvars_returns_a_shallow_snapshot() -> None:
    nested = {"role": "admin"}
    structguru.clear_contextvars()
    try:
        structguru.bind_contextvars(request_id="req-1", user=nested)

        snapshot = structguru.get_contextvars()
        snapshot["request_id"] = "changed"
        snapshot["added"] = True

        assert structguru.get_contextvars() == {
            "request_id": "req-1",
            "user": nested,
        }
        assert snapshot["user"] is nested
    finally:
        structguru.clear_contextvars()
