"""Celery integration for structguru.

Provides automatic ``task_id`` / ``task_name`` binding and optional
context propagation from producer to consumer via task headers.

Usage::

    from structguru.integrations.celery import setup_celery_logging

    setup_celery_logging()
"""

from __future__ import annotations

from collections.abc import Sequence
from contextvars import ContextVar
from typing import Any

from structguru._contextvars import bind_contextvars, clear_contextvars

_HEADER_KEY = "structguru_context"
_setup_done = False


def setup_celery_logging(
    *,
    propagate_context: bool = True,
    context_keys: Sequence[str] | None = None,
) -> None:
    """Connect Celery signals for structured logging.

    Parameters
    ----------
    propagate_context:
        If ``True``, serialise selected context-var keys into task headers
        so they are available in the worker.
    context_keys:
        If set, only propagate these keys.  ``None`` means propagate all.

    Notes
    -----
    Eager tasks inherit the selected caller context when propagation is enabled
    and restore the caller context on exit. Worker tasks start with clean context;
    nested tasks restore their parent's context when they finish.
    """
    global _setup_done  # noqa: PLW0603
    if _setup_done:
        return
    _setup_done = True

    from celery.signals import before_task_publish, task_postrun, task_prerun

    from structguru._contextvars import get_contextvars

    previous_contexts: ContextVar[tuple[dict[str, Any] | None, ...]] = ContextVar(
        "structguru_celery_context_stack", default=()
    )

    if propagate_context:

        @before_task_publish.connect(weak=False)  # type: ignore[untyped-decorator]
        def _inject_context(
            headers: dict[str, Any] | None = None,
            **_kw: Any,
        ) -> None:
            if headers is None:
                return
            ctx = get_contextvars()
            if context_keys is not None:
                ctx = {k: v for k, v in ctx.items() if k in context_keys}
            headers[_HEADER_KEY] = ctx

    @task_prerun.connect(weak=False)  # type: ignore[untyped-decorator]
    def _bind_task_context(
        task_id: str | None = None,
        task: Any = None,
        **_kw: Any,
    ) -> None:
        stack = previous_contexts.get()
        eager = task is not None and getattr(task.request, "is_eager", False) is True
        previous = get_contextvars() if eager or stack else None
        previous_contexts.set((*stack, previous))
        clear_contextvars()

        if propagate_context and task:
            # Eager execution bypasses before_task_publish, so inherit the same
            # selected fields locally. Explicit task headers still take priority.
            if eager and previous is not None:
                inherited = previous
                if context_keys is not None:
                    inherited = {k: v for k, v in inherited.items() if k in context_keys}
                bind_contextvars(**inherited)
            request = task.request
            ctx: dict[str, Any] | None = None
            if hasattr(request, _HEADER_KEY):
                ctx = getattr(request, _HEADER_KEY, None)
            if ctx is None and hasattr(request, "get"):
                ctx = request.get(_HEADER_KEY)
            if ctx is None:
                headers = getattr(request, "headers", None)
                if isinstance(headers, dict):
                    ctx = headers.get(_HEADER_KEY)
            if isinstance(ctx, dict):
                bind_contextvars(**ctx)

        # A producer may itself be a task. Its propagated identity must never
        # replace the consumer's identity; correlation fields still propagate.
        if task_id:
            bind_contextvars(task_id=task_id)
        if task:
            bind_contextvars(task_name=task.name)

    @task_postrun.connect(weak=False)  # type: ignore[untyped-decorator]
    def _clear_context(**_kw: Any) -> None:
        stack = previous_contexts.get()
        clear_contextvars()
        if stack:
            previous_contexts.set(stack[:-1])
            if stack[-1] is not None:
                bind_contextvars(**stack[-1])
