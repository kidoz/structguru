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
    """Return a shallow snapshot of the current context.

    Mutating the returned dictionary does not change the bound context. Values
    within the dictionary are not copied.
    """
    return dict(_ctx.get())


@contextmanager
def bound_contextvars(**kwargs: Any) -> Iterator[None]:
    """Bind *kwargs* for the duration of the ``with`` block, then restore.

    Exiting in the context the block entered restores that context exactly.
    A block can also exit in another context: a generator resumed under a
    fresh context copy per ``next()`` (Starlette's ``iterate_in_threadpool``),
    or an async generator finalized by the event loop. Exit never raises
    there. It restores the entry context only if the context it exits in
    still holds exactly the mapping the block installed (a copy of the
    entering context); any other context, such as another request's, is left
    untouched.
    """
    previous = _ctx.get()
    installed = {**previous, **kwargs}
    token = _ctx.set(installed)
    try:
        yield
    finally:
        try:
            _ctx.reset(token)
        except (ValueError, RuntimeError):
            if _ctx.get() is installed:
                _ctx.set(previous)
