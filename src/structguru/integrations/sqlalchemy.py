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

from structguru.core import Logger


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
        Name for the structguru logger.
    """
    from sqlalchemy import event

    log = Logger(name=logger_name)
    _START_KEY = "structguru_query_start"

    @event.listens_for(engine, "before_cursor_execute")
    def _before_execute(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        # Key the timestamp by the ExecutionContext so an aborted execute
        # cannot leave orphaned start times behind — a fresh execute either
        # overwrites its own key or SQLAlchemy discards the context entirely.
        starts: dict[int, float] = conn.info.setdefault(_START_KEY, {})
        starts[id(context)] = time.perf_counter()

    @event.listens_for(engine, "after_cursor_execute")
    def _after_execute(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        starts: dict[int, float] | None = conn.info.get(_START_KEY)
        if not starts:
            return
        start_time = starts.pop(id(context), None)
        if start_time is None:
            return
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

    @event.listens_for(engine, "handle_error")
    def _on_error(ctx: Any) -> None:
        # When the cursor raises, `after_cursor_execute` is skipped.
        # Evict the start time so conn.info doesn't grow without bound.
        conn_info = getattr(getattr(ctx, "connection", None), "info", None)
        if conn_info is None:
            return
        starts = conn_info.get(_START_KEY)
        if isinstance(starts, dict):
            starts.pop(id(ctx.execution_context), None)
