"""Tests for structguru.integrations.celery."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from structlog.contextvars import bind_contextvars, clear_contextvars, get_contextvars


class TestSetupCeleryLogging:
    def test_binds_task_context_on_prerun(self) -> None:
        clear_contextvars()

        # Mock celery signals
        handlers: dict[str, Any] = {}

        def make_signal(name: str) -> MagicMock:
            sig = MagicMock()

            def connect(fn: Any = None, weak: bool = True) -> Any:
                if fn is None:

                    def decorator(f: Any) -> Any:
                        handlers[name] = f
                        return f

                    return decorator
                handlers[name] = fn
                return fn

            sig.connect = MagicMock(side_effect=connect)
            return sig

        mock_signals = MagicMock()
        mock_signals.before_task_publish = make_signal("before_task_publish")
        mock_signals.task_prerun = make_signal("task_prerun")
        mock_signals.task_postrun = make_signal("task_postrun")

        with patch.dict("sys.modules", {"celery": MagicMock(), "celery.signals": mock_signals}):
            from structguru.integrations.celery import setup_celery_logging

            setup_celery_logging()

        # Simulate task_prerun
        mock_task = MagicMock()
        mock_task.name = "my_app.tasks.send_email"
        mock_task.request = MagicMock()
        mock_task.request.structguru_context = None

        handlers["task_prerun"](task_id="abc-123", task=mock_task)

        ctx = get_contextvars()
        assert ctx["task_id"] == "abc-123"
        assert ctx["task_name"] == "my_app.tasks.send_email"

    def test_clears_context_on_postrun(self) -> None:
        clear_contextvars()
        bind_contextvars(task_id="old")

        handlers: dict[str, Any] = {}

        def make_signal(name: str) -> MagicMock:
            sig = MagicMock()

            def connect(fn: Any = None, weak: bool = True) -> Any:
                if fn is None:

                    def decorator(f: Any) -> Any:
                        handlers[name] = f
                        return f

                    return decorator
                handlers[name] = fn
                return fn

            sig.connect = MagicMock(side_effect=connect)
            return sig

        mock_signals = MagicMock()
        mock_signals.before_task_publish = make_signal("before_task_publish")
        mock_signals.task_prerun = make_signal("task_prerun")
        mock_signals.task_postrun = make_signal("task_postrun")

        with patch.dict("sys.modules", {"celery": MagicMock(), "celery.signals": mock_signals}):
            from structguru.integrations.celery import setup_celery_logging

            setup_celery_logging()

        handlers["task_postrun"]()
        assert get_contextvars() == {}

    def test_context_propagation_via_headers(self) -> None:
        clear_contextvars()
        bind_contextvars(request_id="req-999")

        handlers: dict[str, Any] = {}

        def make_signal(name: str) -> MagicMock:
            sig = MagicMock()

            def connect(fn: Any = None, weak: bool = True) -> Any:
                if fn is None:

                    def decorator(f: Any) -> Any:
                        handlers[name] = f
                        return f

                    return decorator
                handlers[name] = fn
                return fn

            sig.connect = MagicMock(side_effect=connect)
            return sig

        mock_signals = MagicMock()
        mock_signals.before_task_publish = make_signal("before_task_publish")
        mock_signals.task_prerun = make_signal("task_prerun")
        mock_signals.task_postrun = make_signal("task_postrun")

        with patch.dict("sys.modules", {"celery": MagicMock(), "celery.signals": mock_signals}):
            from structguru.integrations.celery import setup_celery_logging

            setup_celery_logging(propagate_context=True)

        # Simulate before_task_publish
        task_headers: dict[str, Any] = {}
        handlers["before_task_publish"](headers=task_headers)

        assert "structguru_context" in task_headers
        assert task_headers["structguru_context"]["request_id"] == "req-999"
