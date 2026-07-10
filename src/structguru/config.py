"""Logging configuration for structguru.

Since v1.0, structguru uses the native Rust renderer as its only logging path.
These functions are compatibility shims that configure the native renderer to
match the pre-1.0 observable contract (output lands on the configured stream).
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Sequence
from typing import Any

from structguru._native import enable_native as _enable_native


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

    Since v1.0, this is a compatibility shim that wires the native Rust renderer
    to the given *stream* (via a callable sink), preserving the pre-1.0 contract
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
        Output stream.  Defaults to ``sys.stdout``. Wired as a callable sink.
    clear_handlers:
        Kept for backward compatibility; the native path does not use root
        logger handlers.
    """
    if stream is None:
        stream = sys.stdout

    # Wire the stream as a synchronous sink so logger output lands on it
    # immediately (preserving the pre-1.0 contract that output is available
    # right after the logger call, no flush needed).
    _enable_native(
        service=service,
        level=level,
        json=json_logs,
        stream_sink=stream,
    )


def setup_structlog(
    *,
    service: str = "app",
    suppress_loggers: Sequence[str] = (),
) -> None:
    """Application-level logging setup.

    Reads environment variables:

    - ``LOG_LEVEL`` (default: ``"INFO"``)
    - ``JSON_LOGS`` (``"0"`` = console, default: ``"1"`` = JSON)
    - ``LOG_PATH`` (optional file sink with 50 MB rotation)

    Parameters
    ----------
    service:
        Application/service name added to every log record.
    suppress_loggers:
        Logger names to suppress to WARNING level.
    """
    level = os.environ.get("LOG_LEVEL", "INFO")
    json_logs = os.environ.get("JSON_LOGS", "1") != "0"

    kwargs: dict[str, Any] = {
        "service": service,
        "level": level,
        "json": json_logs,
    }

    log_path = os.environ.get("LOG_PATH")
    if log_path:
        kwargs["file_path"] = log_path

    _enable_native(**kwargs)

    for name in suppress_loggers:
        logging.getLogger(name).setLevel(logging.WARNING)

    def _log_exception(
        exc_type: type[BaseException],
        exc_value: BaseException,
        exc_traceback: Any,
    ) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        logging.getLogger().error(
            "Uncaught exception",
            exc_info=(exc_type, exc_value, exc_traceback),
        )

    sys.excepthook = _log_exception
