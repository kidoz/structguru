"""SQLAlchemy slow-query logging integration.

Attaches event listeners to a SQLAlchemy engine to log queries that exceed
a configurable duration threshold.

Usage::

    from structguru.integrations.sqlalchemy import setup_query_logging

    setup_query_logging(engine, slow_threshold_ms=100)
"""

from __future__ import annotations

import time
from typing import Any

import structlog


def setup_query_logging(
    engine: Any,
    *,
    slow_threshold_ms: float = 100.0,
    log_all: bool = False,
    logger_name: str = "structguru.sqlalchemy",
) -> None:
    """Attach query timing listeners to *engine*.

    Parameters
    ----------
    engine:
        A :class:`sqlalchemy.engine.Engine`.
    slow_threshold_ms:
        Log a warning when a query exceeds this duration (milliseconds).
    log_all:
        If ``True``, log every query regardless of duration.
    logger_name:
        Name for the structlog logger.
    """
    from sqlalchemy import event

    log = structlog.get_logger(logger_name)

    @event.listens_for(engine, "before_cursor_execute")  # type: ignore[untyped-decorator]
    def _before_execute(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        conn.info.setdefault("structguru_query_start", []).append(time.perf_counter())

    @event.listens_for(engine, "after_cursor_execute")  # type: ignore[untyped-decorator]
    def _after_execute(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        starts = conn.info.get("structguru_query_start")
        if not starts:
            return
        start_time = starts.pop()
        duration_ms = (time.perf_counter() - start_time) * 1000

        is_slow = duration_ms >= slow_threshold_ms
        if log_all or is_slow:
            log_method = log.warning if is_slow else log.debug
            log_method(
                "Slow query" if is_slow else "Query executed",
                query=statement[:500],
                duration_ms=round(duration_ms, 2),
                slow=is_slow,
            )
