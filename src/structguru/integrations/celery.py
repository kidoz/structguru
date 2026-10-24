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
import os
import threading
import uuid
import warnings
from collections.abc import Callable, Mapping, Sequence
from contextvars import ContextVar
from itertools import islice
from pathlib import PurePath
from typing import Any, cast

from structguru import _runtime
from structguru._contextvars import bind_contextvars, clear_contextvars
from structguru.integrations._util import REDACTED, TRUNCATED_SUFFIX
from structguru.redaction import DEFAULT_SENSITIVE_KEYS

_HEADER_KEY = "structguru_context"
_setup_done = False
_active_options: tuple[bool, frozenset[str] | None] | None = None
_setup_lock = threading.RLock()


def _after_fork() -> None:
    """Replace setup synchronization inherited from vanished threads."""
    global _setup_lock  # noqa: PLW0603
    _setup_lock = threading.RLock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


# Bounds for context exported into message headers. Brokers cap header sizes
# (and AMQP table keys at 255 bytes), and every task carries the header.
_MAX_DEPTH = 8
_MAX_ITEMS = 100
_MAX_STR = 1024
_MAX_KEY_BYTES = 255
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1


def _export_key(key: object, depth: int = 0) -> str | None:
    """Return a header-safe mapping key, or ``None`` to drop the entry."""
    if issubclass(type(key), enum.Enum):
        return _export_key(cast(enum.Enum, key).value, depth + 1) if depth < _MAX_DEPTH else None
    if issubclass(type(key), bytes):
        key = cast(bytes, key)
        if bytes.__len__(key) > 128:
            return None
        try:
            text = bytes.__bytes__(key).decode("utf-8")
        except UnicodeDecodeError:
            return None
    elif issubclass(type(key), str):
        text = str.__str__(cast(str, key))
    elif key is None or type(key) is bool:
        text = str(key)
    elif issubclass(type(key), int):
        text = int.__repr__(cast(int, key))
    elif issubclass(type(key), float):
        text = float.__repr__(cast(float, key))
    else:
        return None
    return text if len(text.encode("utf-8", "surrogatepass")) <= _MAX_KEY_BYTES else None


def _sensitive_export_key(key: object, sensitive: frozenset[str]) -> bool:
    if issubclass(type(key), enum.Enum):
        member = cast(enum.Enum, key)
        return any(
            issubclass(type(name), str) and str.lower(name) in sensitive
            for name in (member._name_, member._value_)
        )
    text = _export_key(key)
    return text is not None and text.lower() in sensitive


def _pattern_text(text: str, redact: Callable[[str], str] | None) -> str:
    if redact is None:
        return text
    try:
        return redact(text)
    except Exception:  # noqa: BLE001 - header patterns must fail closed
        return REDACTED


def _export_value(
    value: Any, sensitive: frozenset[str], depth: int, redact: Callable[[str], str] | None = None
) -> Any:
    """Coerce *value* into JSON- and AMQP-safe scalars and containers.

    Mirrors what the renderer would log: supported value types keep their
    meaning, anything else becomes the renderer's ``<unsupported: Type>``
    marker rather than its ``str()``/``repr()`` (which could expose internals).
    Strings, containers, integers, and nesting depth are bounded.
    """
    cls = type(value)
    if value is None or cls is bool:
        return value
    if issubclass(cls, str):
        text = _pattern_text(str.__str__(value), redact)
        return text if len(text) <= _MAX_STR else text[:_MAX_STR] + TRUNCATED_SUFFIX
    if issubclass(cls, int) and not issubclass(cls, enum.Enum):
        number = int.__int__(value)
        text = str(number)
        redacted = _pattern_text(text, redact)
        if redacted != text or not _INT64_MIN <= number <= _INT64_MAX:
            return (
                redacted if len(redacted) <= _MAX_STR else redacted[:_MAX_STR] + TRUNCATED_SUFFIX
            )
        return number
    if issubclass(cls, float) and not issubclass(cls, enum.Enum):
        real = float.__float__(value)
        return real if math.isfinite(real) else None
    if issubclass(cls, enum.Enum):
        return (
            _export_value(value.value, sensitive, depth + 1, redact)
            if depth < _MAX_DEPTH
            else "<max depth exceeded>"
        )
    if issubclass(cls, (datetime.datetime, datetime.date, datetime.time)):
        return (
            _export_value(value.isoformat(), sensitive, depth + 1, redact)
            if depth < _MAX_DEPTH
            else "<max depth exceeded>"
        )
    if issubclass(cls, (uuid.UUID, decimal.Decimal, PurePath)):
        return _export_value(str(value), sensitive, depth, redact)
    if depth >= _MAX_DEPTH:
        return _pattern_text("<max depth exceeded>", redact)
    if dataclasses.is_dataclass(cls) and not issubclass(cls, type):
        value = {field.name: getattr(value, field.name) for field in dataclasses.fields(value)}
    if issubclass(type(value), Mapping):
        exported: dict[str, Any] = {}
        for raw_key, item in list(islice(value.items(), _MAX_ITEMS)):
            if len(exported) >= _MAX_ITEMS:
                break
            key = _export_key(raw_key)
            if key is None:
                continue
            sensitive_key = _sensitive_export_key(raw_key, sensitive) or key.lower() in sensitive
            key = _pattern_text(key, redact)
            if len(key.encode("utf-8", "surrogatepass")) > _MAX_KEY_BYTES:
                continue
            exported[key] = (
                REDACTED if sensitive_key else _export_value(item, sensitive, depth + 1, redact)
            )
        return exported
    if issubclass(type(value), (list, tuple, set, frozenset)):
        items: list[Any] = []
        for item in list(islice(value, _MAX_ITEMS)):
            if len(items) >= _MAX_ITEMS:
                break
            items.append(_export_value(item, sensitive, depth + 1, redact))
        return items
    return _export_value(f"<unsupported: {type(value).__name__}>", sensitive, depth, redact)


def _export_context(context: Mapping[str, Any]) -> dict[str, Any]:
    """Return *context* redacted and coerced for a message header."""
    _runtime._ensure_process()
    state = _runtime.current_runtime()
    keys = state.settings.sensitive_keys if state is not None else None
    sensitive = DEFAULT_SENSITIVE_KEYS if keys is None else frozenset(key.lower() for key in keys)
    redact = (
        state.config.redact_patterns if state is not None and state.config.pattern_count else None
    )
    exported = _export_value(context, sensitive, 0, redact)
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
    ``[REDACTED]`` before the header reaches the broker. Active sensitive patterns
    apply to exported keys, strings, and decimal integers before truncation.
    If the context cannot be exported, the header is omitted; publishing never
    fails because of it.

    Eager tasks inherit the selected caller context when propagation is enabled
    and restore the caller context on exit. Worker tasks start with clean context;
    nested tasks restore their parent's context when they finish.
    """
    global _setup_done, _active_options  # noqa: PLW0603
    selected_keys = _normalize_keys(context_keys)
    options = (propagate_context, selected_keys)
    from celery.signals import before_task_publish, task_postrun, task_prerun

    from structguru._contextvars import get_contextvars

    with _setup_lock:
        if _setup_done:
            if options != _active_options:
                warnings.warn(
                    "setup_celery_logging() was already called with different options; "
                    "the first configuration stays active.",
                    RuntimeWarning,
                    stacklevel=2,
                )
            return

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
                except Exception:  # noqa: BLE001 - propagation must not break publishing
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
