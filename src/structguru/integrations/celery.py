"""Celery integration for structguru.

Provides automatic ``task_id`` / ``task_name`` binding and optional
context propagation from producer to consumer via task headers.

Usage::

    from structguru.integrations.celery import setup_celery_logging

    setup_celery_logging()
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from structlog.contextvars import bind_contextvars, clear_contextvars

_HEADER_KEY = "structguru_context"


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
    """
    from celery.signals import before_task_publish, task_postrun, task_prerun
    from structlog.contextvars import get_contextvars

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
        clear_contextvars()

        if task_id:
            bind_contextvars(task_id=task_id)
        if task:
            bind_contextvars(task_name=task.name)

        if propagate_context and task:
            request = task.request
            ctx: dict[str, Any] | None = None
            if hasattr(request, _HEADER_KEY):
                ctx = getattr(request, _HEADER_KEY, None)
            if ctx is None and hasattr(request, "get"):
                ctx = request.get(_HEADER_KEY)
            if isinstance(ctx, dict):
                bind_contextvars(**ctx)

    @task_postrun.connect(weak=False)  # type: ignore[untyped-decorator]
    def _clear_context(**_kw: Any) -> None:
        clear_contextvars()
