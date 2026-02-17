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
