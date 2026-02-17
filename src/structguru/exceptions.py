"""Structured exception serialization processor.

Converts ``exc_info`` into a JSON-serializable dictionary with type, message,
module, traceback frames, and optional chained-cause information.
"""

from __future__ import annotations

import sys
import traceback
from typing import Any


class ExceptionDictProcessor:
    """Convert ``exc_info`` to a structured dictionary.

    Parameters
    ----------
    include_locals:
        If ``True``, include local variables in each frame (as ``repr``).
    max_frames:
        Maximum number of traceback frames to include.
    """

    def __init__(
        self,
        *,
        include_locals: bool = False,
        max_frames: int = 20,
    ) -> None:
        self._include_locals = include_locals
        self._max_frames = max_frames

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

        if not isinstance(exc_info, tuple) or exc_info[0] is None:
            return event_dict

        exc_type, exc_value, exc_tb = exc_info

        frames = []
        for fs in traceback.extract_tb(exc_tb)[-self._max_frames :]:
            frame_info: dict[str, Any] = {
                "filename": fs.filename,
                "lineno": fs.lineno,
                "name": fs.name,
                "line": fs.line,
            }
            if self._include_locals and fs.locals:
                frame_info["locals"] = {k: repr(v) for k, v in fs.locals.items()}
            frames.append(frame_info)

        exception_dict: dict[str, Any] = {
            "type": exc_type.__qualname__,
            "message": str(exc_value),
            "module": exc_type.__module__,
            "frames": frames,
        }

        cause = exc_value.__cause__ or exc_value.__context__
        if cause is not None:
            exception_dict["cause"] = {
                "type": type(cause).__qualname__,
                "message": str(cause),
            }

        event_dict["exception"] = exception_dict
        event_dict.pop("exc_info", None)
        return event_dict
