"""Logging configuration for structguru.

Since v1.0, structguru uses the native Rust renderer as its only logging path.
The remaining compatibility shim configures the native renderer to match the
pre-1.0 observable stream contract.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from structguru._native import configure as _configure


def _to_logging_level(level_name: str) -> int:
    """Convert a human-readable level name to its :mod:`logging` constant."""
    upper_level = level_name.upper()
    if upper_level == "WARN":
        return logging.WARNING
    result: int | None = getattr(logging, upper_level, None)
    if not isinstance(result, int):
        import warnings

        warnings.warn(
            f"Unknown log level {level_name!r}, falling back to INFO",
            stacklevel=2,
        )
        return logging.INFO
    return result


def configure_structlog(
    *,
    service: str = "app",
    level: str = "INFO",
    json_logs: bool = True,
    stream: Any = None,
    clear_handlers: bool = True,
) -> None:
    """Configure the native logger.

    .. deprecated:: 1.0
        Use :func:`structguru.configure` instead. This compatibility wrapper
        will be removed in v2.0.

    Since v1.0, this is a compatibility shim that wires the native Rust renderer
    synchronously to the given *stream*, preserving the pre-1.0 contract
    that ``logger`` output lands on the configured stream. The native renderer
    handles all JSON/console formatting, redaction, and level filtering.

    Parameters
    ----------
    service:
        Application/service name added to every log record.
    level:
        Minimum log level (e.g. ``"DEBUG"``, ``"INFO"``).
    json_logs:
        ``True`` for JSON output, ``False`` for colored console output.
    stream:
        Output stream. Defaults to ``sys.stdout`` and is the sole output target.
    clear_handlers:
        Kept for backward compatibility; the native path does not use root
        logger handlers.
    """
    import warnings

    warnings.warn(
        "configure_structlog() is deprecated; use configure() instead. "
        "It will be removed in v2.0.",
        DeprecationWarning,
        stacklevel=2,
    )

    if stream is None:
        stream = sys.stdout

    # Wire the stream as a synchronous sink so logger output lands on it
    # immediately (preserving the pre-1.0 contract that output is available
    # right after the logger call, no flush needed).
    _configure(
        service=service,
        level=level,
        json=json_logs,
        target="null",
        stream_sink=stream,
    )
