"""Tests for structguru.integrations.sentry."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

from structguru.integrations import sentry as sentry_mod
from structguru.integrations.sentry import SentryProcessor
from structguru.redaction import REDACTED_MARKER_KEY


def _mock_sentry() -> MagicMock:
    mock = MagicMock()
    scope = MagicMock()
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=scope)
    cm.__exit__ = MagicMock(return_value=False)
    mock.new_scope.return_value = cm
    mock._scope = scope
    return mock


class TestSentryProcessor:
    def test_no_op_without_sentry_sdk(self) -> None:
        with patch.object(sentry_mod, "_sentry_sdk", None):
            proc = SentryProcessor()
            ed: dict = {"event": "test"}
            result = proc(None, "error", ed)
            assert result is ed

    def test_adds_breadcrumb_at_info(self) -> None:
        mock_sentry = _mock_sentry()
        with patch.object(sentry_mod, "_sentry_sdk", mock_sentry):
            proc = SentryProcessor(breadcrumb_level=logging.INFO, event_level=logging.CRITICAL)
            proc(None, "info", {"event": "breadcrumb test", "key": "val"})

        mock_sentry.add_breadcrumb.assert_called_once()
        call = mock_sentry.add_breadcrumb.call_args
        assert call.kwargs["message"] == "breadcrumb test"
        assert call.kwargs["category"] == "structguru"
        # The redaction marker must not leak into the breadcrumb payload.
        assert REDACTED_MARKER_KEY not in call.kwargs["data"]

    def test_error_without_exc_info_does_not_capture_by_default(self) -> None:
        mock_sentry = _mock_sentry()
        with patch.object(sentry_mod, "_sentry_sdk", mock_sentry):
            proc = SentryProcessor(event_level=logging.ERROR, require_redaction=False)
            proc(None, "error", {"event": "user blocked"})

        mock_sentry.capture_message.assert_not_called()
        mock_sentry.capture_exception.assert_not_called()

    def test_error_without_exc_info_captures_message_when_opted_in(self) -> None:
        mock_sentry = _mock_sentry()
        with patch.object(sentry_mod, "_sentry_sdk", mock_sentry):
            proc = SentryProcessor(
                event_level=logging.ERROR,
                capture_messages=True,
                require_redaction=False,
            )
            proc(None, "error", {"event": "something broke", "service": "myapp"})

        mock_sentry.capture_message.assert_called_once()

    def test_captures_exception_with_exc_info(self) -> None:
        mock_sentry = _mock_sentry()
        exc = RuntimeError("boom")
        with patch.object(sentry_mod, "_sentry_sdk", mock_sentry):
            proc = SentryProcessor(event_level=logging.ERROR, require_redaction=False)
            proc(None, "error", {"event": "fail", "exc_info": exc})

        mock_sentry.capture_exception.assert_called_once_with(exc)

    def test_captures_exception_with_exc_info_true(self) -> None:
        mock_sentry = _mock_sentry()
        with patch.object(sentry_mod, "_sentry_sdk", mock_sentry):
            try:
                raise ValueError("boom")
            except ValueError:
                proc = SentryProcessor(event_level=logging.ERROR, require_redaction=False)
                proc(None, "error", {"event": "fail", "exc_info": True})

        assert mock_sentry.capture_exception.call_count == 1
        captured_exc = mock_sentry.capture_exception.call_args.args[0]
        assert isinstance(captured_exc, ValueError)

    def test_captures_exception_from_tuple(self) -> None:
        mock_sentry = _mock_sentry()
        exc = KeyError("k")
        with patch.object(sentry_mod, "_sentry_sdk", mock_sentry):
            proc = SentryProcessor(event_level=logging.ERROR, require_redaction=False)
            proc(None, "error", {"event": "fail", "exc_info": (type(exc), exc, None)})

        mock_sentry.capture_exception.assert_called_once_with(exc)

    def test_sets_tags(self) -> None:
        mock_sentry = _mock_sentry()
        with patch.object(sentry_mod, "_sentry_sdk", mock_sentry):
            proc = SentryProcessor(
                event_level=logging.ERROR,
                tag_keys=frozenset({"service"}),
                capture_messages=True,
                require_redaction=False,
            )
            proc(None, "error", {"event": "fail", "service": "myapp"})

        mock_sentry._scope.set_tag.assert_called_with("service", "myapp")

    def test_below_breadcrumb_level_no_op(self) -> None:
        mock_sentry = _mock_sentry()
        with patch.object(sentry_mod, "_sentry_sdk", mock_sentry):
            proc = SentryProcessor(
                breadcrumb_level=logging.WARNING,
                event_level=logging.ERROR,
            )
            proc(None, "debug", {"event": "quiet"})

        mock_sentry.add_breadcrumb.assert_not_called()
        mock_sentry.capture_message.assert_not_called()

    def test_skips_extras_when_redaction_required_and_missing(self) -> None:
        mock_sentry = _mock_sentry()
        with patch.object(sentry_mod, "_sentry_sdk", mock_sentry):
            proc = SentryProcessor(
                event_level=logging.ERROR,
                capture_messages=True,
                require_redaction=True,
            )
            proc(None, "error", {"event": "fail", "password": "hunter2"})

        mock_sentry._scope.set_extra.assert_not_called()

    def test_includes_extras_when_marker_present(self) -> None:
        mock_sentry = _mock_sentry()
        with patch.object(sentry_mod, "_sentry_sdk", mock_sentry):
            proc = SentryProcessor(
                event_level=logging.ERROR,
                capture_messages=True,
                require_redaction=True,
            )
            proc(
                None,
                "error",
                {"event": "fail", "safe": "ok", REDACTED_MARKER_KEY: True},
            )

        mock_sentry._scope.set_extra.assert_called_once()
        extras = mock_sentry._scope.set_extra.call_args.args[1]
        assert "safe" in extras
        assert REDACTED_MARKER_KEY not in extras
