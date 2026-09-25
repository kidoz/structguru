"""Structured exception serialization.

Converts ``exc_info`` into a JSON-serializable dictionary with type, message,
module, traceback frames, and optional chained-cause information. Used by the
native renderer when ``structured_exceptions=True`` is configured.
"""

from __future__ import annotations

import dataclasses
import enum
import itertools
import sys
import traceback
import types
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from structguru.redaction import DEFAULT_SENSITIVE_KEYS

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
    resolve to an active exception. Frame walking, ``f_locals`` access, and
    ``repr`` are Python-owned — the native renderer receives this dict and only
    serializes it. Exception groups include an ``exceptions`` list, bounded to
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
        writer = _LocalRepr(sensitive_keys, limit + _PATTERN_MARGIN)
        rendered = writer.render(value)
        if redact is not None:
            try:
                rendered = redact(rendered)
            except Exception:  # noqa: BLE001 - fail closed rather than emit unchecked text
                out[name] = _REDACTED
                continue
        if writer.truncated or len(rendered) > limit:
            remaining = max(len(rendered) - limit, 0)
            # A rendering cut at the budget reports a lower bound.
            more = f"{remaining}+" if writer.truncated else str(remaining)
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
    """Name the ``_LocalRepr`` method that walks *cls*, or ``None`` for a leaf.

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


def _redacted_message_repr(value: Any, sensitive_keys: frozenset[str]) -> str | None:
    """Render a container a message placeholder interpolates, if it needs redaction.

    Returns the ``_LocalRepr`` rendering of *value* when it redacted a
    value under one of *sensitive_keys* (lower-case) or hid nesting deeper
    than it walks, and ``None`` otherwise, so the caller formats *value* as
    usual. Items and length are not capped, like ``str()``.
    """
    if _walk_kind(type(value)) is None:
        return None
    writer = _LocalRepr(sensitive_keys, sys.maxsize, _UNCAPPED, _MAX_MESSAGE_DEPTH)
    rendered = writer.render(value)
    return rendered if writer.redacted or writer.elided else None


class _LocalRepr:
    """Render one local like ``repr()`` with values under sensitive keys redacted.

    Only type-level checks select a walk, so no instance attribute runs to
    decide how to render a value, and a walked container's own ``__repr__``
    never runs. Output stops at ``budget`` characters and sets ``truncated``.
    ``redacted`` records that a value was redacted, and ``elided`` that items
    or nesting past the caps (or past the budget) were left out.
    """

    __slots__ = (
        "_active",
        "_budget",
        "_keys",
        "_max_depth",
        "_max_items",
        "_parts",
        "_size",
        "elided",
        "redacted",
        "truncated",
    )

    def __init__(
        self,
        sensitive_keys: frozenset[str],
        budget: int,
        max_items: int = _MAX_LOCAL_ITEMS,
        max_depth: int = _MAX_LOCAL_DEPTH,
    ) -> None:
        self._keys = sensitive_keys
        self._budget = budget
        self._max_items = max_items
        self._max_depth = max_depth
        self._parts: list[str] = []
        self._size = 0
        self._active: set[int] = set()
        self.truncated = False
        self.redacted = False
        self.elided = False

    def render(self, value: Any) -> str:
        self._value(value, 0)
        return "".join(self._parts)

    def _write(self, text: str) -> None:
        room = self._budget - self._size
        if len(text) > room:
            text = text[:room]
            self.truncated = self.elided = True
        self._parts.append(text)
        self._size += len(text)

    def _write_redacted(self) -> None:
        self.redacted = True
        self._write(_REDACTED)

    def _sensitive(self, key: object) -> bool:
        if type(key) is str:
            return key.lower() in self._keys
        if isinstance(key, enum.Enum):
            # A composite Flag member has no name.
            names = (key._name_, key._value_)
            return any(isinstance(name, str) and name.lower() in self._keys for name in names)
        if isinstance(key, str):
            return str.lower(key) in self._keys
        if isinstance(key, (bytes, bytearray)) and len(key) <= _MAX_BYTES_KEY:
            try:
                return bytes(key).decode("utf-8").lower() in self._keys
            except UnicodeDecodeError:
                return False
        return False

    def _leaf(self, value: Any) -> None:
        if type(value) in (str, bytes, bytearray):
            # Enough characters to overflow the budget, without copying the rest.
            value = value[: self._budget - self._size + 1]
        try:
            text = repr(value)
        except Exception as exc:  # noqa: BLE001
            text = f"<repr failed: {type(exc).__name__}>"
        self._write(text)

    def _value(self, value: Any, depth: int) -> None:
        if self.truncated:
            return
        kind = _walk_kind(type(value))
        if kind is None:
            self._leaf(value)
        else:
            self._walk(value, depth, getattr(self, kind))

    def _walk(self, value: Any, depth: int, render: Callable[[Any, int], None]) -> None:
        if depth >= self._max_depth:
            self.elided = True
            self._write("...")
            return
        if id(value) in self._active:
            self._write("...")
            return
        self._active.add(id(value))
        try:
            render(value, depth + 1)
        except Exception as exc:  # noqa: BLE001 - iteration or attribute access may fail
            self._write(f"<repr failed: {type(exc).__name__}>")
        finally:
            self._active.discard(id(value))

    def _entries(
        self, items: Iterable[tuple[object, Any]], depth: int, separator: str, keyed: bool
    ) -> None:
        # Snapshot first: rendering an item may mutate the container.
        limit = self._max_items
        for index, (key, item) in enumerate(list(itertools.islice(items, limit + 1))):
            if self.truncated:
                return
            if index:
                self._write(", ")
            if index == limit:
                self.elided = True
                self._write("...")
                return
            if keyed:
                self._value(key, depth)
            else:
                self._write(str(key))
            self._write(separator)
            if self._sensitive(key):
                self._write_redacted()
            else:
                self._value(item, depth)

    def _mapping(self, value: Any, depth: int) -> None:
        cls = type(value)
        items = dict.items(value) if issubclass(cls, dict) else value.items()
        wrapped = cls is not dict
        self._write(f"{cls.__qualname__}({{" if wrapped else "{")
        self._entries(items, depth, ": ", keyed=True)
        self._write("})" if wrapped else "}")

    def _sequence(self, value: Any, depth: int) -> None:
        cls = type(value)
        if cls is list:
            opening, closing = "[", "]"
        elif cls is tuple:
            opening, closing = "(", ",)" if len(value) == 1 else ")"
        elif cls is set and value:
            opening, closing = "{", "}"
        elif issubclass(cls, (set, frozenset)):
            opening, closing = (
                (f"{cls.__qualname__}({{", "})") if value else (f"{cls.__qualname__}(", ")")
            )
        else:
            opening, closing = f"{cls.__qualname__}([", "])"
        self._write(opening)
        limit = self._max_items
        for index, item in enumerate(list(itertools.islice(value, limit + 1))):
            if self.truncated:
                return
            if index:
                self._write(", ")
            if index == limit:
                self.elided = True
                self._write("...")
                break
            if type(item) is tuple and len(item) == 2 and self._sensitive(item[0]):
                # A (key, value) pair, as in header or query-parameter lists.
                self._write("(")
                self._value(item[0], depth)
                self._write(", ")
                self._write_redacted()
                self._write(")")
            else:
                self._value(item, depth)
        self._write(closing)

    def _named_tuple(self, value: Any, depth: int) -> None:
        cls = type(value)
        self._write(f"{cls.__name__}(")
        self._entries(zip(cls._fields, tuple.__iter__(value), strict=False), depth, "=", False)
        self._write(")")

    def _dataclass(self, value: Any, depth: int) -> None:
        fields = [item for item in dataclasses.fields(type(value)) if item.repr]
        self._write(f"{type(value).__qualname__}(")
        self._entries(
            ((item.name, getattr(value, item.name)) for item in fields), depth, "=", False
        )
        self._write(")")

    def _namespace(self, value: Any, depth: int) -> None:
        cls = type(value)
        if cls is types.SimpleNamespace:
            name = "namespace"
        elif getattr(cls, "__repr__", None) is _NAMESPACE_REPR:
            name = cls.__qualname__
        else:  # argparse.Namespace names the class without its qualifier.
            name = cls.__name__
        self._write(f"{name}(")
        self._entries(dict.items(vars(value)), depth, "=", keyed=False)
        self._write(")")
