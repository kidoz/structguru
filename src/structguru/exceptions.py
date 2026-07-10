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
    serializes it.
    """
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

    frames = []
    if include_locals:
        # Walk raw traceback to capture local variables, since
        # traceback.extract_tb() does not populate FrameSummary.locals.
        raw_frames: list[tuple[Any, int]] = []
        tb = exc_tb
        while tb is not None:
            raw_frames.append((tb.tb_frame, tb.tb_lineno))
            tb = tb.tb_next
        for frame_obj, lineno in raw_frames[-max_frames:]:
            frame_info: dict[str, Any] = {
                "filename": frame_obj.f_code.co_filename,
                "lineno": lineno,
                "name": frame_obj.f_code.co_name,
                "line": None,
                "locals": _format_locals(frame_obj.f_locals, keys, max_local_repr),
            }
            frames.append(frame_info)
    else:
        for fs in traceback.extract_tb(exc_tb)[-max_frames:]:
            frame_info = {
                "filename": fs.filename,
                "lineno": fs.lineno,
                "name": fs.name,
                "line": fs.line,
            }
            frames.append(frame_info)

    exception_dict: dict[str, Any] = {
        "type": exc_type.__qualname__,
        "message": str(exc_value),
        "module": exc_type.__module__,
        "frames": frames,
    }

    cause = exc_value.__cause__
    if cause is None and not exc_value.__suppress_context__:
        cause = exc_value.__context__
    if cause is not None:
        exception_dict["cause"] = {
            "type": type(cause).__qualname__,
            "message": str(cause),
        }

    return exception_dict


def _format_locals(
    raw: dict[str, Any],
    sensitive_keys: frozenset[str],
    limit: int,
) -> dict[str, str]:
    """Redact sensitive names and cap ``repr`` length for each local."""
    out: dict[str, str] = {}
    for name, value in raw.items():
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
