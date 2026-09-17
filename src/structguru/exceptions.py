"""Structured exception serialization.

Converts ``exc_info`` into a JSON-serializable dictionary with type, message,
module, traceback frames, and optional chained-cause information. Used by the
native renderer when ``structured_exceptions=True`` is configured.
"""

from __future__ import annotations

import sys
import traceback
from typing import Any

from structguru.redaction import DEFAULT_SENSITIVE_KEYS

_MAX_GROUP_DEPTH = 10
_MAX_EXCEPTION_NODES = 100


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
) -> dict[str, Any] | None:
    """Normalize *exc_info* and convert it to a structured dictionary.

    Accepts ``True`` (use the current exception), a ``BaseException`` instance,
    or a ``(type, value, tb)`` tuple; returns ``None`` when *exc_info* does not
    resolve to an active exception. Frame walking, ``f_locals`` access, and
    ``repr`` are Python-owned — the native renderer receives this dict and only
    serializes it. Exception groups include an ``exceptions`` list, bounded to
    ten nesting levels and 100 exception nodes per record. Groups report omitted
    direct children in ``exceptions_truncated`` when either limit is reached.
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
            "frames": _build_frames(tb, include_locals, max_frames, max_local_repr, keys),
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
            frame_info["locals"] = _format_locals(raw_frames[index].f_locals, keys, max_local_repr)
        frames.append(frame_info)

    return frames


def _format_locals(
    raw: dict[str, Any],
    sensitive_keys: frozenset[str],
    limit: int,
) -> dict[str, str]:
    """Redact sensitive names and cap ``repr`` length for each local."""
    out: dict[str, str] = {}
    # repr() may execute arbitrary Python and mutate the containing namespace.
    for name, value in list(raw.items()):
        if isinstance(name, str) and name.lower() in sensitive_keys:
            out[name] = "[REDACTED]"
            continue
        try:
            rendered = repr(value)
        except Exception as exc:  # noqa: BLE001
            rendered = f"<repr failed: {type(exc).__name__}>"
        if len(rendered) > limit:
            remaining = len(rendered) - limit
            rendered = f"{rendered[:limit]}...<{remaining} more>"
        out[name] = rendered
    return out
