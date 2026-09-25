"""Celery integration for structguru.

Provides automatic ``task_id`` / ``task_name`` binding and optional
context propagation from producer to consumer via task headers.

Usage::

    from structguru.integrations.celery import setup_celery_logging

    setup_celery_logging()
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import enum
import math
import uuid
import warnings
from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from pathlib import PurePath
from typing import Any

from structguru._contextvars import bind_contextvars, clear_contextvars
from structguru.integrations._util import REDACTED, TRUNCATED_SUFFIX, active_sensitive_keys

_HEADER_KEY = "structguru_context"
_setup_done = False
_active_options: tuple[bool, frozenset[str] | None] | None = None

# Bounds for context exported into message headers. Brokers cap header sizes
# (and AMQP table keys at 255 bytes), and every task carries the header.
_MAX_DEPTH = 8
_MAX_ITEMS = 100
_MAX_STR = 1024
_MAX_KEY_BYTES = 255
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1


def _export_key(key: object) -> str | None:
    """Return a header-safe mapping key, or ``None`` to drop the entry."""
    if isinstance(key, str):
        text = str.__str__(key)
    elif key is None or isinstance(key, (bool, int, float)):
        text = str(key)  # JSON stringifies scalar keys the same way
    else:
        return None
    return text if len(text.encode("utf-8", "surrogatepass")) <= _MAX_KEY_BYTES else None


def _export_value(value: Any, sensitive: frozenset[str], depth: int) -> Any:
    """Coerce *value* into JSON- and AMQP-safe scalars and containers.

    Mirrors what the renderer would log: supported value types keep their
    meaning, anything else becomes the renderer's ``<unsupported: Type>``
    marker rather than its ``str()``/``repr()`` (which could expose internals).
    Strings, containers, integers, and nesting depth are bounded.
    """
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = str.__str__(value)
        return text if len(text) <= _MAX_STR else text[:_MAX_STR] + TRUNCATED_SUFFIX
    if isinstance(value, int) and not isinstance(value, enum.Enum):
        number = int.__int__(value)
        return number if _INT64_MIN <= number <= _INT64_MAX else str(number)
    if isinstance(value, float):
        real = float.__float__(value)
        return real if math.isfinite(real) else None
    if isinstance(value, enum.Enum):
        return _export_value(value.value, sensitive, depth)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, (uuid.UUID, decimal.Decimal, PurePath)):
        return _export_value(str(value), sensitive, depth)
    if depth >= _MAX_DEPTH:
        return "<max depth exceeded>"
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        value = {field.name: getattr(value, field.name) for field in dataclasses.fields(value)}
    if isinstance(value, Mapping):
        exported: dict[str, Any] = {}
        for raw_key, item in value.items():
            if len(exported) >= _MAX_ITEMS:
                break
            key = _export_key(raw_key)
            if key is None:
                continue
            exported[key] = (
                REDACTED if key.lower() in sensitive else _export_value(item, sensitive, depth + 1)
            )
        return exported
    if isinstance(value, (list, tuple, set, frozenset)):
        items: list[Any] = []
        for item in value:
            if len(items) >= _MAX_ITEMS:
                break
            items.append(_export_value(item, sensitive, depth + 1))
        return items
    return f"<unsupported: {type(value).__name__}>"


def _export_context(context: Mapping[str, Any]) -> dict[str, Any]:
    """Return *context* redacted and coerced for a message header."""
    exported = _export_value(context, active_sensitive_keys(), 0)
    return exported if isinstance(exported, dict) else {}


def _normalize_keys(context_keys: Sequence[str] | None) -> frozenset[str] | None:
    if context_keys is None:
        return None
    if isinstance(context_keys, str):
        return frozenset({context_keys})
    return frozenset(context_keys)


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

    Warns
    -----
    RuntimeWarning
        When called again with options that differ from the active ones. The
        first successful configuration stays in effect; repeated calls never
        connect duplicate handlers.

    Notes
    -----
    Propagated context is exported the way the renderer would log it: values
    are coerced to JSON-safe scalars, lists, and string-keyed mappings (other
    objects become ``<unsupported: Type>`` markers), strings and containers are
    bounded, and keys the active runtime treats as sensitive are replaced by
    ``[REDACTED]`` before the header reaches the broker. If the context cannot
    be exported, the header is omitted; publishing never fails because of it.

    Eager tasks inherit the selected caller context when propagation is enabled
    and restore the caller context on exit. Worker tasks start with clean context;
    nested tasks restore their parent's context when they finish.
    """
    global _setup_done, _active_options  # noqa: PLW0603
    selected_keys = _normalize_keys(context_keys)
    options = (propagate_context, selected_keys)
    if _setup_done:
        if options != _active_options:
            warnings.warn(
                "setup_celery_logging() was already called with different options; "
                "the first configuration stays active.",
                RuntimeWarning,
                stacklevel=2,
            )
        return

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
            try:
                ctx = get_contextvars()
                if selected_keys is not None:
                    ctx = {k: v for k, v in ctx.items() if k in selected_keys}
                exported = _export_context(ctx)
            except Exception:  # noqa: BLE001 - context propagation must never break publishing
                return
            headers[_HEADER_KEY] = exported

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
                if selected_keys is not None:
                    inherited = {k: v for k, v in inherited.items() if k in selected_keys}
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
                bind_contextvars(**{k: v for k, v in ctx.items() if isinstance(k, str)})

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

    _active_options = options
    _setup_done = True
