"""Tests for structguru.otel."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from structguru.otel import add_otel_context


class TestAddOtelContext:
    def test_adds_trace_fields_when_span_valid(self) -> None:
        mock_ctx = MagicMock()
        mock_ctx.is_valid = True
        mock_ctx.trace_id = 0x0AF7651916CD43DD8448EB211C80319C
        mock_ctx.span_id = 0x00F067AA0BA902B7
        mock_ctx.trace_flags = 1

        mock_span = MagicMock()
        mock_span.get_span_context.return_value = mock_ctx

        mock_trace = MagicMock()
        mock_trace.get_current_span.return_value = mock_span

        mock_otel = MagicMock()
        mock_otel.trace = mock_trace
        modules = {"opentelemetry": mock_otel, "opentelemetry.trace": mock_trace}
        with patch.dict("sys.modules", modules):
            ed: dict = {"event": "test"}
            result = add_otel_context(None, "info", ed)

        assert result["trace_id"] == "0af7651916cd43dd8448eb211c80319c"
        assert result["span_id"] == "00f067aa0ba902b7"
        assert result["trace_flags"] == 1

    def test_no_op_when_otel_not_installed(self) -> None:
        with patch.dict("sys.modules", {"opentelemetry": None}):
            ed: dict = {"event": "test"}
            result = add_otel_context(None, "info", ed)
        assert "trace_id" not in result

    def test_no_op_when_span_invalid(self) -> None:
        mock_ctx = MagicMock()
        mock_ctx.is_valid = False

        mock_span = MagicMock()
        mock_span.get_span_context.return_value = mock_ctx

        mock_trace = MagicMock()
        mock_trace.get_current_span.return_value = mock_span

        mock_otel = MagicMock()
        mock_otel.trace = mock_trace
        modules = {"opentelemetry": mock_otel, "opentelemetry.trace": mock_trace}
        with patch.dict("sys.modules", modules):
            ed: dict = {"event": "test"}
            result = add_otel_context(None, "info", ed)
        assert "trace_id" not in result
