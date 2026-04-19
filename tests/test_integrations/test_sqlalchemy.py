"""Tests for structguru.integrations.sqlalchemy."""

from __future__ import annotations

import io
from typing import Any
from unittest.mock import MagicMock, patch

from structguru.config import configure_structlog


class TestSetupQueryLogging:
    def test_registers_event_listeners(self) -> None:
        mock_event = MagicMock()
        mock_engine = MagicMock()
        registered: list[tuple[str, str]] = []

        def track_listens_for(target: Any, identifier: str) -> Any:
            def decorator(fn: Any) -> Any:
                registered.append((str(target), identifier))
                return fn

            return decorator

        mock_event.listens_for = track_listens_for

        mock_sqlalchemy = MagicMock()
        mock_sqlalchemy.event = mock_event
        modules = {"sqlalchemy": mock_sqlalchemy, "sqlalchemy.event": mock_event}
        with patch.dict("sys.modules", modules):
            from structguru.integrations.sqlalchemy import setup_query_logging

            setup_query_logging(mock_engine, slow_threshold_ms=50)

        assert any("before_cursor_execute" in r[1] for r in registered)
        assert any("after_cursor_execute" in r[1] for r in registered)

    def test_registers_handle_error_listener(self) -> None:
        mock_event = MagicMock()
        mock_engine = MagicMock()
        registered: list[tuple[str, str]] = []

        def track_listens_for(target: Any, identifier: str) -> Any:
            def decorator(fn: Any) -> Any:
                registered.append((str(target), identifier))
                return fn

            return decorator

        mock_event.listens_for = track_listens_for
        mock_sqlalchemy = MagicMock()
        mock_sqlalchemy.event = mock_event
        modules = {"sqlalchemy": mock_sqlalchemy, "sqlalchemy.event": mock_event}
        with patch.dict("sys.modules", modules):
            from structguru.integrations.sqlalchemy import setup_query_logging

            setup_query_logging(mock_engine)

        assert any("handle_error" in r[1] for r in registered)

    def test_handle_error_evicts_start_entry(self) -> None:
        listeners: dict[str, Any] = {}

        def mock_listens_for(target: Any, identifier: str) -> Any:
            def decorator(fn: Any) -> Any:
                listeners[identifier] = fn
                return fn

            return decorator

        mock_event = MagicMock()
        mock_event.listens_for = mock_listens_for
        mock_engine = MagicMock()
        mock_sqlalchemy = MagicMock()
        mock_sqlalchemy.event = mock_event

        modules = {"sqlalchemy": mock_sqlalchemy, "sqlalchemy.event": mock_event}
        with patch.dict("sys.modules", modules):
            from structguru.integrations.sqlalchemy import setup_query_logging

            setup_query_logging(mock_engine)

        mock_conn = MagicMock()
        mock_conn.info = {}

        # Simulate two executes that never reach after_cursor_execute.
        ctx_a = object()
        ctx_b = object()
        listeners["before_cursor_execute"](mock_conn, None, "SQL A", None, ctx_a, False)
        listeners["before_cursor_execute"](mock_conn, None, "SQL B", None, ctx_b, False)
        assert len(mock_conn.info["structguru_query_start"]) == 2

        # handle_error fires for ctx_a -> that entry is evicted.
        err_ctx = MagicMock()
        err_ctx.connection = mock_conn
        err_ctx.execution_context = ctx_a
        listeners["handle_error"](err_ctx)

        starts = mock_conn.info["structguru_query_start"]
        assert id(ctx_a) not in starts
        assert id(ctx_b) in starts

    def test_logs_slow_queries(self) -> None:
        buf = io.StringIO()
        configure_structlog(service="test", level="DEBUG", json_logs=True, stream=buf)

        listeners: dict[str, Any] = {}

        def mock_listens_for(target: Any, identifier: str) -> Any:
            def decorator(fn: Any) -> Any:
                listeners[identifier] = fn
                return fn

            return decorator

        mock_event = MagicMock()
        mock_event.listens_for = mock_listens_for

        mock_engine = MagicMock()

        mock_sqlalchemy = MagicMock()
        mock_sqlalchemy.event = mock_event
        modules = {"sqlalchemy": mock_sqlalchemy, "sqlalchemy.event": mock_event}
        with patch.dict("sys.modules", modules):
            from structguru.integrations.sqlalchemy import setup_query_logging

            setup_query_logging(mock_engine, slow_threshold_ms=0.0, log_all=True)

        # Simulate a query
        mock_conn = MagicMock()
        mock_conn.info = {}

        listeners["before_cursor_execute"](mock_conn, None, "SELECT 1", None, None, False)
        listeners["after_cursor_execute"](mock_conn, None, "SELECT 1", None, None, False)

        output = buf.getvalue()
        assert "SELECT 1" in output
