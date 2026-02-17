"""Structlog configuration for structured JSON logging.

Configures structlog to produce JSON logs with standardized fields:
- ``timestamp``: ISO 8601 / RFC 3339 in UTC (``Z`` suffix).
- ``service``: application name.
- ``level``: one of ``CRITICAL``, ``ERROR``, ``WARN``, ``INFO``, ``DEBUG``.
- ``severity``: RFC 5424 syslog severity code (``2``–``7``).
- ``message``: the log message as a string.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Sequence
from logging.handlers import RotatingFileHandler
from typing import Any

import orjson
import structlog
from structlog.contextvars import merge_contextvars

from structguru.processors import (
    add_service,
    add_syslog_severity,
    ensure_event_is_str,
    normalize_level,
)


def _orjson_serializer(obj: object, **_kw: object) -> str:
    """Serialize *obj* to a JSON string using orjson."""
    return orjson.dumps(obj).decode()


def _to_logging_level(level_name: str) -> int:
    """Convert a human-readable level name to its :mod:`logging` constant."""
    upper_level = level_name.upper()
    if upper_level == "WARN":
        return logging.WARNING
    result: int = getattr(logging, upper_level, logging.INFO)
    return result


class _StructlogMsgFixer(logging.Handler):
    """Normalize ``record.msg`` from a structlog event dict to a plain string.

    ``wrap_for_formatter`` stores the whole event dict as ``record.msg``.
    ``ProcessorFormatter`` renders on a **shallow copy** of the record, so
    the original ``record.msg`` stays a ``dict``.  Any handler that runs
    *after* ``ProcessorFormatter`` (notably Sentry's ``EventHandler`` in the
    ``finally`` block of ``callHandlers``) would see the raw dict and call
    ``str(dict)`` — producing an ugly repr.

    This handler is added to the root logger **after** the stream handler.
    By the time it runs, ``ProcessorFormatter`` has already finished with
    its copy, so mutating ``record.msg`` here is safe.  Sentry then sees a
    clean string for events, breadcrumbs, and structured logs alike.
    """

    def emit(self, record: logging.LogRecord) -> None:
        if isinstance(record.msg, dict):
            record.msg = record.msg.get("message") or record.msg.get("event") or str(record.msg)


def _install_exc_info_record_factory() -> None:
    """Patch the ``LogRecord`` factory to propagate ``exc_info`` from structlog.

    ``wrap_for_formatter`` passes the event dict as ``record.msg`` but does
    **not** set ``record.exc_info``.  Sentry's ``LoggingIntegration`` reads
    ``record.exc_info`` before ``ProcessorFormatter`` runs, so it would miss
    structured exception data.

    This factory extracts ``exc_info`` from the event dict and passes it to
    the original factory so the ``LogRecord`` is created with ``exc_info``
    intact.
    """
    original = logging.getLogRecordFactory()
    if getattr(original, "_structlog_exc_info_patched", False):
        return

    def factory(
        name: str,
        level: int,
        fn: str,
        lno: int,
        msg: Any,
        args: Any,
        exc_info: Any,
        func: str | None = None,
        sinfo: str | None = None,
    ) -> logging.LogRecord:
        if exc_info is None and isinstance(msg, dict):
            ei = msg.get("exc_info")
            if ei is True:
                exc_info = sys.exc_info()
            elif isinstance(ei, BaseException):
                exc_info = (type(ei), ei, ei.__traceback__)
            elif isinstance(ei, tuple):
                exc_info = ei
        return original(name, level, fn, lno, msg, args, exc_info, func, sinfo)

    factory._structlog_exc_info_patched = True  # type: ignore[attr-defined]
    logging.setLogRecordFactory(factory)


def _stream_isatty(stream: Any) -> bool:
    """Check if *stream* is connected to a terminal."""
    try:
        result: bool = stream.isatty()
        return result
    except (AttributeError, ValueError):
        return False


def _build_shared_processors(
    service: str,
) -> list[structlog.types.Processor]:
    """Build the shared processor chain used by both structlog and stdlib records."""
    processors: list[structlog.types.Processor] = [
        merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        normalize_level,  # type: ignore[list-item]
        add_syslog_severity,  # type: ignore[list-item]
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
        add_service(service),  # type: ignore[list-item]
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
        ensure_event_is_str,  # type: ignore[list-item]
        structlog.processors.EventRenamer("message"),
    ]
    return processors


def _build_formatter_processors(
    renderer: structlog.types.Processor,
    *,
    json_mode: bool = True,
) -> list[structlog.types.Processor]:
    """Build the ``ProcessorFormatter`` processor chain (final rendering stage)."""
    processors: list[structlog.types.Processor] = [
        structlog.stdlib.ProcessorFormatter.remove_processors_meta,
    ]
    if json_mode:
        processors.append(structlog.processors.format_exc_info)
    processors.append(renderer)
    return processors


def configure_structlog(
    *,
    service: str = "app",
    level: str = "INFO",
    json_logs: bool = True,
    stream: Any = None,
    clear_handlers: bool = True,
) -> None:
    """Configure structlog with ``ProcessorFormatter`` for stdlib integration.

    Uses :class:`structlog.stdlib.ProcessorFormatter` so that ``exc_info``
    is preserved on :class:`logging.LogRecord` objects.  This allows Sentry's
    ``LoggingIntegration`` (and any other stdlib-based consumer) to capture
    structured exception data *before* the traceback is rendered to text.

    Parameters
    ----------
    service:
        Application/service name added to every log record.
    level:
        Minimum log level (e.g. ``"DEBUG"``, ``"INFO"``).
    json_logs:
        ``True`` for JSON output, ``False`` for colored console output.
    stream:
        Output stream.  Defaults to ``sys.stdout``.
    clear_handlers:
        If ``True`` (default), remove all existing root logger handlers before
        adding the structlog handler.  Set to ``False`` when embedding in an
        application that manages its own logging pipeline.
    """
    if stream is None:
        stream = sys.stdout

    _install_exc_info_record_factory()

    shared_processors = _build_shared_processors(service)

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(
            _to_logging_level(level),
        ),
        cache_logger_on_first_use=True,
    )

    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer(serializer=_orjson_serializer)
        if json_logs
        else structlog.dev.ConsoleRenderer(
            colors=_stream_isatty(stream),
            event_key="message",
        )
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=_build_formatter_processors(renderer, json_mode=json_logs),
        foreign_pre_chain=shared_processors,
    )

    root = logging.getLogger()
    if clear_handlers:
        root.handlers.clear()
    root.setLevel(_to_logging_level(level))

    handler = logging.StreamHandler(stream)
    handler.setFormatter(formatter)
    root.addHandler(handler)

    root.addHandler(_StructlogMsgFixer())


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

    configure_structlog(service=service, level=level, json_logs=json_logs)

    for name in suppress_loggers:
        logging.getLogger(name).setLevel(logging.WARNING)

    log_path = os.environ.get("LOG_PATH")
    if log_path:
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=50 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        json_renderer = structlog.processors.JSONRenderer(serializer=_orjson_serializer)
        file_formatter = structlog.stdlib.ProcessorFormatter(
            processors=_build_formatter_processors(json_renderer),
            foreign_pre_chain=_build_shared_processors(service),
        )
        file_handler.setFormatter(file_formatter)
        logging.getLogger().addHandler(file_handler)

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
