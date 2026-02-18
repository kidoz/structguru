"""Tests for structguru.integrations.sentry."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

from structguru.integrations.sentry import SentryProcessor


class TestSentryProcessor:
    def test_no_op_without_sentry_sdk(self) -> None:
        with patch.dict("sys.modules", {"sentry_sdk": None}):
            proc = SentryProcessor()
            ed: dict = {"event": "test"}
            result = proc(None, "error", ed)
            assert result is ed

    def test_adds_breadcrumb_at_info(self) -> None:
        mock_sentry = MagicMock()

        with patch.dict("sys.modules", {"sentry_sdk": mock_sentry}):
            proc = SentryProcessor(breadcrumb_level=logging.INFO, event_level=logging.CRITICAL)
            proc(None, "info", {"event": "breadcrumb test", "key": "val"})

        mock_sentry.add_breadcrumb.assert_called_once()
        bc_call = mock_sentry.add_breadcrumb.call_args
        assert bc_call.kwargs["message"] == "breadcrumb test"
        assert bc_call.kwargs["category"] == "structguru"

    def test_captures_event_at_error(self) -> None:
        mock_sentry = MagicMock()
        mock_scope = MagicMock()
        mock_sentry.new_scope.return_value.__enter__ = MagicMock(return_value=mock_scope)
        mock_sentry.new_scope.return_value.__exit__ = MagicMock(return_value=False)

        with patch.dict("sys.modules", {"sentry_sdk": mock_sentry}):
            proc = SentryProcessor(event_level=logging.ERROR)
            proc(None, "error", {"event": "something broke", "service": "myapp"})

        mock_sentry.capture_message.assert_called_once()

    def test_captures_exception_with_exc_info(self) -> None:
        mock_sentry = MagicMock()
        mock_scope = MagicMock()
        mock_sentry.new_scope.return_value.__enter__ = MagicMock(return_value=mock_scope)
        mock_sentry.new_scope.return_value.__exit__ = MagicMock(return_value=False)

        exc = RuntimeError("boom")

        with patch.dict("sys.modules", {"sentry_sdk": mock_sentry}):
            proc = SentryProcessor(event_level=logging.ERROR)
            proc(None, "error", {"event": "fail", "exc_info": exc})

        mock_sentry.capture_exception.assert_called_once_with(exc)

    def test_sets_tags(self) -> None:
        mock_sentry = MagicMock()
        mock_scope = MagicMock()
        mock_sentry.new_scope.return_value.__enter__ = MagicMock(return_value=mock_scope)
        mock_sentry.new_scope.return_value.__exit__ = MagicMock(return_value=False)

        with patch.dict("sys.modules", {"sentry_sdk": mock_sentry}):
            proc = SentryProcessor(
                event_level=logging.ERROR,
                tag_keys=frozenset({"service"}),
            )
            proc(None, "error", {"event": "fail", "service": "myapp"})

        mock_scope.set_tag.assert_called_with("service", "myapp")

    def test_below_breadcrumb_level_no_op(self) -> None:
        mock_sentry = MagicMock()

        with patch.dict("sys.modules", {"sentry_sdk": mock_sentry}):
            proc = SentryProcessor(
                breadcrumb_level=logging.WARNING,
                event_level=logging.ERROR,
            )
            proc(None, "debug", {"event": "quiet"})

        mock_sentry.add_breadcrumb.assert_not_called()
        mock_sentry.capture_message.assert_not_called()
