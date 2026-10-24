"""Structured exception serialization.

Converts ``exc_info`` into a JSON-serializable dictionary with type, message,
module, traceback frames, and optional chained-cause information. Used by the
native renderer when ``structured_exceptions=True`` is configured.
"""

from __future__ import annotations

import dataclasses
import enum
import importlib
import sys
import traceback
import types
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from typing import Any, cast

from structguru.redaction import DEFAULT_SENSITIVE_KEYS

_render_local_repr = cast(
    "Callable[..., tuple[str, bool, bool, bool]]",
    importlib.import_module("structguru._rust")._render_local_repr,
)

_MAX_GROUP_DEPTH = 10
_MAX_EXCEPTION_NODES = 100

_REDACTED = "[REDACTED]"
# Bounds for rendering one local. Containers nested deeper than this, or items
# past the per-container cap, render as ``...``.
_MAX_LOCAL_DEPTH = 8
_MAX_LOCAL_ITEMS = 100
# Nesting depth walked when a message placeholder interpolates a container,
# and an item cap that never applies (islice accepts at most sys.maxsize).
_MAX_MESSAGE_DEPTH = 32
_UNCAPPED = sys.maxsize - 1
# Longest bytes key decoded to match a sensitive key name.
_MAX_BYTES_KEY = 128
# Per-class answers of _walk_kind(), for at most this many classes.
_MAX_CACHED_KINDS = 512
_walk_kinds: dict[type, str | None] = {}
# Characters rendered beyond ``max_local_repr`` so pattern redaction sees a
# secret that straddles the truncation point whole before the text is cut.
_PATTERN_MARGIN = 4096
_LEAF_TYPES = frozenset({str, bytes, bytearray, int, float, complex, bool, type(None)})
_NAMESPACE_REPR: object = types.SimpleNamespace.__repr__


def _exception_message(value: BaseException) -> str:
    """Convert an exception message without replacing the original failure."""
    try:
        return str(value)
    except Exception as exc:
        return f"<str failed: {type(exc).__name__}>"


def build_exception_dict(
    exc_info: Any,
    *,
    include_locals: bool = False,
    max_frames: int = 20,
    max_local_repr: int = 200,
    sensitive_keys: frozenset[str] | None = None,
    redact: Callable[[str], str] | None = None,
) -> dict[str, Any] | None:
    """Normalize *exc_info* and convert it to a structured dictionary.

    Accepts ``True`` (use the current exception), a ``BaseException`` instance,
    or a ``(type, value, tb)`` tuple; returns ``None`` when *exc_info* does not
    resolve to an active exception. Python owns frame walking and ``f_locals``
    access; a native helper walks local containers and calls Python ``repr``
    for leaves. The record renderer serializes the resulting dict. Exception
    groups include an ``exceptions`` list, bounded to
    ten nesting levels and 100 exception nodes per record. Groups report omitted
    direct children in ``exceptions_truncated`` when either limit is reached.

    With ``include_locals``, a local whose name is in ``sensitive_keys``
    (lower-case names; the defaults when ``None``) renders as ``[REDACTED]``.
    Other locals render like ``repr()``, except that mappings, lists, tuples,
    named tuples, sets, dataclasses with their generated ``__repr__``, and
    ``types.SimpleNamespace`` and ``argparse.Namespace`` objects are walked
    rather than asked for their own ``repr()``, so a value under a sensitive
    key renders as ``[REDACTED]`` at any depth. A key matches as a string, as
    UTF-8 ``bytes``, or as an ``Enum`` member by name or string value, and a
    ``(key, value)`` pair inside a walked sequence counts as a keyed value. The
    walk is cycle-safe and bounded in depth, items, and length. Any other
    object's own ``repr()`` text is covered only by ``redact``.

    ``redact`` receives each local's rendering before it is cut to
    ``max_local_repr`` characters, so a pattern matching text that straddles
    the cut still applies; the runtime passes its compiled
    ``sensitive_patterns``. A failing ``redact`` redacts the whole local.
    """
    if max_frames < 0:
        msg = f"max_frames must be >= 0, got {max_frames}"
        raise ValueError(msg)
    if max_local_repr < 0:
        msg = f"max_local_repr must be >= 0, got {max_local_repr}"
        raise ValueError(msg)

    if isinstance(exc_info, BaseException):
        exc_info = (type(exc_info), exc_info, exc_info.__traceback__)
    elif exc_info is True:
        exc_info = sys.exc_info()

    if not isinstance(exc_info, tuple) or len(exc_info) != 3 or exc_info[0] is None:
        return None

    exc_type, exc_value, exc_tb = exc_info
    if not isinstance(exc_type, type) or not isinstance(exc_value, BaseException):
        return None
    keys = sensitive_keys if sensitive_keys is not None else DEFAULT_SENSITIVE_KEYS

    remaining = _MAX_EXCEPTION_NODES

    def build(value: BaseException, tb: Any, depth: int) -> dict[str, Any]:
        nonlocal remaining
        remaining -= 1
        result: dict[str, Any] = {
            "type": type(value).__qualname__,
            "message": _exception_message(value),
            "module": type(value).__module__,
            "frames": _build_frames(tb, include_locals, max_frames, max_local_repr, keys, redact),
        }
        cause = value.__cause__
        if cause is None and not value.__suppress_context__:
            cause = value.__context__
        if cause is not None:
            result["cause"] = {
                "type": type(cause).__qualname__,
                "message": _exception_message(cause),
            }
        if isinstance(value, BaseExceptionGroup):
            children: list[dict[str, Any]] = []
            if depth < _MAX_GROUP_DEPTH:
                for child in value.exceptions:
                    if remaining == 0:
                        break
                    children.append(build(child, child.__traceback__, depth + 1))
            result["exceptions"] = children
            omitted = len(value.exceptions) - len(children)
            if omitted:
                result["exceptions_truncated"] = omitted
        return result

    result = build(exc_value, exc_tb, 0)
    # Preserve the explicit type supplied by a valid exc_info tuple.
    result["type"] = exc_type.__qualname__
    result["module"] = exc_type.__module__
    return result


def _build_frames(
    exc_tb: Any,
    include_locals: bool,
    max_frames: int,
    max_local_repr: int,
    keys: frozenset[str],
    redact: Callable[[str], str] | None,
) -> list[dict[str, Any]]:
    """Collect the last ``max_frames`` traceback frames with the same fields either way.

    The traceback is walked to the first frame that will be kept *before*
    ``traceback.extract_tb()`` runs: it reads and dedents source for every frame
    it visits, so extracting the whole traceback and slicing afterwards paid for
    frames that were then discarded, which made a ``RecursionError`` cost
    hundreds of frames of source lookup for the twenty it reported. Locals, when
    requested, come from the same selected frames, so the ``line`` field is
    present whether or not locals are.
    """
    if max_frames <= 0:
        return []
    depth = 0
    node = exc_tb
    while node is not None:
        depth += 1
        node = node.tb_next
    node = exc_tb
    for _ in range(max(depth - max_frames, 0)):
        node = node.tb_next

    summaries = traceback.extract_tb(node)
    raw_frames: list[Any] = []
    if include_locals:
        # ``FrameSummary`` never carries ``f_locals``; pair each summary with
        # its live frame from the same starting node.
        current = node
        while current is not None:
            raw_frames.append(current.tb_frame)
            current = current.tb_next

    frames: list[dict[str, Any]] = []
    for index, summary in enumerate(summaries):
        frame_info: dict[str, Any] = {
            "filename": summary.filename,
            "lineno": summary.lineno,
            "name": summary.name,
            "line": summary.line,
        }
        if include_locals:
            frame_info["locals"] = _format_locals(
                raw_frames[index].f_locals, keys, max_local_repr, redact
            )
        frames.append(frame_info)

    return frames


def _format_locals(
    raw: dict[str, Any],
    sensitive_keys: frozenset[str],
    limit: int,
    redact: Callable[[str], str] | None = None,
) -> dict[str, str]:
    """Redact sensitive names and values, then cap each rendered local's length."""
    out: dict[str, str] = {}
    # repr() may execute arbitrary Python and mutate the containing namespace.
    for name, value in list(raw.items()):
        if isinstance(name, str) and name.lower() in sensitive_keys:
            out[name] = _REDACTED
            continue
        rendered, truncated, _, _ = _render_local_repr(
            value,
            sensitive_keys,
            min(limit + _PATTERN_MARGIN, sys.maxsize),
            _MAX_LOCAL_ITEMS,
            _MAX_LOCAL_DEPTH,
            _REPR_HOOKS,
        )
        if redact is not None:
            try:
                rendered = redact(rendered)
            except Exception:  # noqa: BLE001 - fail closed rather than emit unchecked text
                out[name] = _REDACTED
                continue
        if truncated or len(rendered) > limit:
            remaining = max(len(rendered) - limit, 0)
            # A rendering cut at the budget reports a lower bound.
            more = f"{remaining}+" if truncated else str(remaining)
            rendered = f"{rendered[:limit]}...<{more} more>"
        out[name] = rendered
    return out


def _has_generated_repr(cls: type) -> bool:
    """True when *cls* uses the ``__repr__`` that ``dataclasses`` generated.

    The generated method wraps a function compiled from source text, so its
    ``__wrapped__`` code comes from ``<string>``; a ``__repr__`` written in the
    class body is kept, because it may hide fields on purpose.
    """
    wrapped = getattr(getattr(cls, "__repr__", None), "__wrapped__", None)
    code = getattr(wrapped, "__code__", None)
    return code is not None and code.co_filename == "<string>"


def _argparse_namespace_repr() -> object:
    """``argparse.Namespace.__repr__``, or ``None`` if argparse is not loaded.

    No ``Namespace`` exists before argparse is imported, so the module is
    looked up rather than imported.
    """
    argparse = sys.modules.get("argparse")
    namespace = getattr(argparse, "Namespace", None)
    return getattr(namespace, "__repr__", None) if isinstance(namespace, type) else None


def _walk_kind(cls: type) -> str | None:
    """Name the container walk for *cls*, or return ``None`` for a leaf.

    Only type-level attributes are consulted, so no instance code runs. The
    answer is cached per class: message formatting asks for every argument
    that is not a plain scalar.
    """
    if cls in _walk_kinds:
        return _walk_kinds[cls]
    kind = _classify(cls)
    if len(_walk_kinds) < _MAX_CACHED_KINDS:
        _walk_kinds[cls] = kind
    return kind


def _classify(cls: type) -> str | None:
    """Uncached ``_walk_kind``."""
    if cls in _LEAF_TYPES or issubclass(cls, type):
        return None
    if issubclass(cls, (Mapping, dict)):
        return "_mapping"
    if issubclass(cls, tuple) and isinstance(getattr(cls, "_fields", None), tuple):
        return "_named_tuple"
    if issubclass(cls, (list, tuple, set, frozenset, deque)):
        return "_sequence"
    if dataclasses.is_dataclass(cls) and _has_generated_repr(cls):
        return "_dataclass"
    repr_method = getattr(cls, "__repr__", None)
    if repr_method is _NAMESPACE_REPR:
        return "_namespace"
    if repr_method is not None and repr_method is _argparse_namespace_repr():
        # The shared _AttributeHolder.__repr__: walk only Namespace objects,
        # not parsers or actions.
        namespace = sys.modules["argparse"].Namespace
        return "_namespace" if issubclass(cls, namespace) else None
    return None


def _repr_sensitive(key: object, sensitive_keys: frozenset[str]) -> bool:
    """Match unusual keys with Python's Enum, string, and bytes semantics."""
    if isinstance(key, enum.Enum):
        return any(
            isinstance(name, str) and name.lower() in sensitive_keys
            for name in (key._name_, key._value_)
        )
    if isinstance(key, str):
        return str.lower(key) in sensitive_keys
    if isinstance(key, (bytes, bytearray)) and len(key) <= _MAX_BYTES_KEY:
        try:
            return bytes(key).decode("utf-8").lower() in sensitive_keys
        except UnicodeDecodeError:
            return False
    return False


def _repr_shape(value: Any, kind: str, cls: type) -> tuple[str, str]:
    """Preserve Python container spellings for types beyond exact builtins."""
    if kind == "_mapping":
        return f"{cls.__qualname__}({{", "})"
    if kind == "_named_tuple":
        return f"{cls.__name__}(", ")"
    if kind == "_dataclass":
        return f"{cls.__qualname__}(", ")"
    if kind == "_namespace":
        if cls is types.SimpleNamespace:
            name = "namespace"
        elif getattr(cls, "__repr__", None) is _NAMESPACE_REPR:
            name = cls.__qualname__
        else:
            name = cls.__name__
        return f"{name}(", ")"
    if issubclass(cls, (set, frozenset)):
        return (f"{cls.__qualname__}({{", "})") if value else (f"{cls.__qualname__}(", ")")
    return f"{cls.__qualname__}([", "])"


def _repr_items(value: Any, kind: str, cls: type) -> Iterable[Any]:
    """Expose unusual containers without invoking their own representation."""
    if kind == "_mapping":
        return dict.items(value) if issubclass(cls, dict) else value.items()
    if kind == "_named_tuple":
        return zip(getattr(cls, "_fields"), tuple.__iter__(value), strict=False)
    if kind == "_dataclass":
        fields = [item for item in dataclasses.fields(cls) if item.repr]
        return ((item.name, getattr(value, item.name)) for item in fields)
    if kind == "_namespace":
        return dict.items(vars(value))
    return cast("Iterable[Any]", value)


_REPR_HOOKS = (_walk_kind, _repr_shape, _repr_items, _repr_sensitive)


def _redacted_message_repr(value: Any, sensitive_keys: frozenset[str]) -> str | None:
    """Render a container a message placeholder interpolates, if it needs redaction.

    Returns the native rendering of *value* when it redacted a
    value under one of *sensitive_keys* (lower-case) or hid nesting deeper
    than it walks, and ``None`` otherwise, so the caller formats *value* as
    usual. Items and length are not capped, like ``str()``.
    """
    if _walk_kind(type(value)) is None:
        return None
    rendered, _, redacted, elided = _render_local_repr(
        value, sensitive_keys, sys.maxsize, _UNCAPPED, _MAX_MESSAGE_DEPTH, _REPR_HOOKS
    )
    return rendered if redacted or elided else None
