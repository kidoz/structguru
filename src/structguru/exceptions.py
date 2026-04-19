"""Structured exception serialization processor.

Converts ``exc_info`` into a JSON-serializable dictionary with type, message,
module, traceback frames, and optional chained-cause information.
"""

from __future__ import annotations

import sys
import traceback
from typing import Any

from structguru.redaction import DEFAULT_SENSITIVE_KEYS


class ExceptionDictProcessor:
    """Convert ``exc_info`` to a structured dictionary.

    Parameters
    ----------
    include_locals:
        If ``True``, include local variables in each frame (as ``repr``).
        Values are truncated to *max_local_repr* characters, and locals whose
        names match *sensitive_keys* are replaced with ``"[REDACTED]"``.
    max_frames:
        Maximum number of traceback frames to include.
    max_local_repr:
        Maximum length of each local variable ``repr``; longer values are
        truncated with a trailing ``"...<N more>"`` marker.
    sensitive_keys:
        Local-variable names (lower-cased) to redact when *include_locals* is
        ``True``.  Defaults to :data:`~structguru.redaction.DEFAULT_SENSITIVE_KEYS`.
    """

    def __init__(
        self,
        *,
        include_locals: bool = False,
        max_frames: int = 20,
        max_local_repr: int = 200,
        sensitive_keys: frozenset[str] | None = None,
    ) -> None:
        self._include_locals = include_locals
        self._max_frames = max_frames
        self._max_local_repr = max_local_repr
        self._sensitive_keys = (
            sensitive_keys if sensitive_keys is not None else DEFAULT_SENSITIVE_KEYS
        )

    def __call__(
        self,
        _logger: Any,
        _method_name: str,
        event_dict: dict[str, Any],
    ) -> dict[str, Any]:
        exc_info = event_dict.get("exc_info")
        if not exc_info:
            return event_dict

        if isinstance(exc_info, BaseException):
            exc_info = (type(exc_info), exc_info, exc_info.__traceback__)
        elif exc_info is True:
            exc_info = sys.exc_info()

        if not isinstance(exc_info, tuple) or len(exc_info) != 3 or exc_info[0] is None:
            return event_dict

        exc_type, exc_value, exc_tb = exc_info

        frames = []
        if self._include_locals:
            # Walk raw traceback to capture local variables, since
            # traceback.extract_tb() does not populate FrameSummary.locals.
            raw_frames: list[tuple[Any, int]] = []
            tb = exc_tb
            while tb is not None:
                raw_frames.append((tb.tb_frame, tb.tb_lineno))
                tb = tb.tb_next
            for frame_obj, lineno in raw_frames[-self._max_frames :]:
                frame_info: dict[str, Any] = {
                    "filename": frame_obj.f_code.co_filename,
                    "lineno": lineno,
                    "name": frame_obj.f_code.co_name,
                    "line": None,
                    "locals": self._format_locals(frame_obj.f_locals),
                }
                frames.append(frame_info)
        else:
            for fs in traceback.extract_tb(exc_tb)[-self._max_frames :]:
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

        event_dict["exception"] = exception_dict
        event_dict.pop("exc_info", None)
        return event_dict

    def _format_locals(self, raw: dict[str, Any]) -> dict[str, str]:
        """Redact sensitive names and cap ``repr`` length for each local."""
        out: dict[str, str] = {}
        limit = self._max_local_repr
        for name, value in raw.items():
            if isinstance(name, str) and name.lower() in self._sensitive_keys:
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
