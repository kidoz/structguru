"""Bounded background dispatch for Python callable logging sinks.

The dispatcher itself lives in the native extension (``CallableDispatcher``):
sink registry, bounded queue generations, the worker thread, delivery
accounting, and deferred finalizers. This module keeps the Python-facing names
and the callback-scope helpers used by the raw stdlib delivery path in
:mod:`structguru.core`.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from typing import Any, Protocol, cast


class CallableDispatcherProtocol(Protocol):
    """Methods of the native ``CallableDispatcher`` used by the runtime."""

    def add(
        self,
        callback: Callable[[str], None],
        min_level: int = 0,
        *,
        enabled: bool,
        level_callback: Callable[[str, int], None] | None = None,
    ) -> int: ...

    def remove(self, token: int, *, finalizer: Callable[[], None] | None = None) -> bool: ...

    def configure(self, callbacks: Iterable[Callable[[str], None]], *, maxsize: int) -> None: ...

    def disable(self) -> None: ...

    def stop(self, *, drain: bool, stall_timeout: float | None = None) -> int: ...

    def flush(self, stall_timeout: float | None = None) -> int: ...

    def idle(self) -> bool: ...

    def enqueue(self, line: str, level: int, *, overflow: str) -> bool: ...

    def metrics(self) -> dict[str, int]: ...

    def reset_drop_count(self) -> None: ...

    def fork_child(self, *, enabled: bool) -> CallableDispatcherProtocol: ...


_rust = importlib.import_module("structguru._rust")

# Python-facing names for the native classes and the callback-scope probe.
CallableSinkDispatcher: Any = _rust.CallableDispatcher
DispatchChannel: Any = _rust.DispatchChannel
in_callback: Callable[[], bool] = _rust.in_callback
_enter_callback: Callable[[], None] = _rust.enter_callback
_exit_callback: Callable[[], None] = _rust.exit_callback


def new_dispatcher() -> CallableDispatcherProtocol:
    """Create a native dispatcher with no sinks and dispatch stopped."""
    return cast(CallableDispatcherProtocol, _rust.CallableDispatcher())


@contextmanager
def callback_scope() -> Iterator[None]:
    """Mark the current thread as inside a sink callback for the block."""
    _enter_callback()
    try:
        yield
    finally:
        _exit_callback()
