"""Tests for the public request-scoped context API."""

from __future__ import annotations

import asyncio
import contextvars
import gc
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest

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


def test_exit_in_an_unrelated_context_leaves_it_untouched() -> None:
    structguru.clear_contextvars()
    structguru.bind_contextvars(request_id="outer", base=1)
    try:
        block = structguru.bound_contextvars(request_id="inner")
        contextvars.copy_context().run(block.__enter__)
        elsewhere = contextvars.copy_context()
        elsewhere.run(structguru.bind_contextvars, request_id="inner", extra="kept")
        elsewhere.run(block.__exit__, None, None, None)
        assert elsewhere.run(structguru.get_contextvars) == {
            "request_id": "inner",
            "base": 1,
            "extra": "kept",
        }
        assert structguru.get_contextvars() == {"request_id": "outer", "base": 1}
    finally:
        structguru.clear_contextvars()


def test_exit_in_a_copy_of_the_entering_context_restores_it() -> None:
    structguru.clear_contextvars()
    structguru.bind_contextvars(request_id="outer")
    try:
        block = structguru.bound_contextvars(request_id="inner", user="u1")
        entered = contextvars.copy_context()
        entered.run(block.__enter__)
        copy = entered.run(contextvars.copy_context)
        copy.run(block.__exit__, None, None, None)
        assert copy.run(structguru.get_contextvars) == {"request_id": "outer"}
    finally:
        structguru.clear_contextvars()


def test_generator_finalized_during_another_request_keeps_its_context() -> None:
    # A generator entered contextualize() in request A and is finalized by the
    # garbage collector while request B runs: B's context must not change.
    def stream() -> Iterator[int]:
        with structguru.logger.contextualize(user="gen-scope", extra="x"):
            yield 1
            yield 2

    holder: dict[str, Iterator[int]] = {}

    def request_a() -> None:
        structguru.bind_contextvars(request_id="A", user="alice")
        iterator = stream()
        next(iterator)
        holder["iterator"] = iterator

    def request_b() -> dict[str, Any]:
        structguru.bind_contextvars(request_id="B", user="bob", extra="b-extra")
        del holder["iterator"]
        gc.collect()
        return structguru.get_contextvars()

    structguru.clear_contextvars()
    contextvars.copy_context().run(request_a)
    assert contextvars.copy_context().run(request_b) == {
        "request_id": "B",
        "user": "bob",
        "extra": "b-extra",
    }
    assert structguru.get_contextvars() == {}


def test_generator_resumed_in_fresh_context_copies_exits_cleanly() -> None:
    # Starlette's iterate_in_threadpool runs every next() in a new copy of
    # the caller's context, so the block exits in a context it did not enter.
    structguru.clear_contextvars()

    def stream() -> Iterator[str]:
        with structguru.logger.contextualize(request_id="r1"):
            yield "a"
            yield "b"

    iterator = stream()
    first = contextvars.copy_context()
    assert first.run(next, iterator) == "a"
    assert first.run(structguru.get_contextvars) == {"request_id": "r1"}
    assert contextvars.copy_context().run(next, iterator) == "b"
    last = contextvars.copy_context()
    with pytest.raises(StopIteration):
        last.run(next, iterator)
    assert last.run(structguru.get_contextvars) == {}
    assert structguru.get_contextvars() == {}


def test_async_generator_finalized_by_gc_does_not_raise() -> None:
    errors: list[dict[str, Any]] = []

    async def agen() -> AsyncIterator[int]:
        with structguru.bound_contextvars(request_id="r1"):
            yield 1
            yield 2

    async def main() -> None:
        generator = agen()
        await generator.__anext__()
        del generator
        gc.collect()
        await asyncio.sleep(0.05)

    loop = asyncio.new_event_loop()
    loop.set_exception_handler(lambda _loop, context: errors.append(context))
    try:
        loop.run_until_complete(main())
    finally:
        loop.close()
        structguru.clear_contextvars()
    assert errors == []
