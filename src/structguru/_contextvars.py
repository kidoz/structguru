"""Request-scoped context for structguru.

A lightweight replacement for ``structlog.contextvars``, backed by a single
``contextvars.ContextVar[dict]``. Context is copy-on-write: ``bind_contextvars``
merges into a new dict and sets a token; ``bound_contextvars`` restores on exit.

This is request-scoped (via Python ``contextvars``), not thread-scoped. An
``asyncio`` task inherits a copy of the context active when it was created; a
new thread starts with an empty one and must propagate context explicitly.
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

_ctx: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "structguru_context", default={}
)


def bind_contextvars(**kwargs: Any) -> None:
    """Merge *kwargs* into the current context (copy-on-write)."""
    current = _ctx.get()
    _ctx.set({**current, **kwargs})


def clear_contextvars() -> None:
    """Reset the context to empty."""
    _ctx.set({})


def get_contextvars() -> dict[str, Any]:
    """Return a snapshot of the current context."""
    return dict(_ctx.get())


@contextmanager
def bound_contextvars(**kwargs: Any) -> Iterator[None]:
    """Bind *kwargs* for the duration of the ``with`` block, then restore."""
    token = _ctx.set({**_ctx.get(), **kwargs})
    try:
        yield
    finally:
        _ctx.reset(token)
