"""Tests for structguru.core."""

from __future__ import annotations

import io
import logging
from pathlib import Path

from structguru.config import configure_structlog
from structguru.core import (
    Logger,
    _CallableHandler,
    _make_handler,
    _safe_format,
)


class TestSafeFormat:
    def test_positional_args(self) -> None:
        msg, consumed = _safe_format("Hello {}", ("world",), {})
        assert msg == "Hello world"
        assert consumed is True

    def test_keyword_args(self) -> None:
        msg, consumed = _safe_format("Hello {name}", (), {"name": "world"})
        assert msg == "Hello world"
        assert consumed is True

    def test_no_placeholders(self) -> None:
        msg, consumed = _safe_format("Hello", ("extra",), {})
        assert msg == "Hello"
        assert consumed is False

    def test_no_args(self) -> None:
        msg, consumed = _safe_format("Hello {}", (), {})
        assert msg == "Hello {}"
        assert consumed is False

    def test_format_key_error_returns_original(self) -> None:
        msg, consumed = _safe_format("Hello {missing}", (), {"other": 1})
        assert msg == "Hello {missing}"
        assert consumed is False

    def test_attribute_error_returns_original(self) -> None:
        msg, consumed = _safe_format("Hello {user.name}", (), {"user": {}})
        assert msg == "Hello {user.name}"
        assert consumed is False

    def test_type_error_returns_original(self) -> None:
        msg, consumed = _safe_format("{0!x}", (42,), {})
        assert msg == "{0!x}"
        assert consumed is False

    def test_non_string_message(self) -> None:
        msg, consumed = _safe_format(42, (), {})
        assert msg == "42"
        assert consumed is False

    def test_mixed_args(self) -> None:
        msg, consumed = _safe_format("{} {name}", ("hi",), {"name": "world"})
        assert msg == "hi world"
        assert consumed is True


class TestMakeHandler:
    def test_logging_handler_passthrough(self) -> None:
        h = logging.StreamHandler()
        assert _make_handler(h) is h

    def test_file_path_string(self, tmp_path: Path) -> None:
        p = tmp_path / "test.log"
        handler = _make_handler(str(p))
        assert isinstance(handler, logging.FileHandler)
        handler.close()

    def test_file_path_object(self, tmp_path: Path) -> None:
        p = tmp_path / "test.log"
        handler = _make_handler(p)
        assert isinstance(handler, logging.FileHandler)
        handler.close()

    def test_file_like_object(self) -> None:
        buf = io.StringIO()
        handler = _make_handler(buf)  # type: ignore[arg-type]
        assert isinstance(handler, logging.StreamHandler)

    def test_callable_sink(self) -> None:
        messages: list[str] = []
        handler = _make_handler(messages.append)
        assert isinstance(handler, _CallableHandler)

    def test_unsupported_type_raises(self) -> None:
        import pytest

        with pytest.raises(TypeError, match="Unsupported sink type"):
            _make_handler(42)  # type: ignore[arg-type]


class TestLoggerBind:
    def test_returns_new_logger(self) -> None:
        log = Logger()
        child = log.bind(user="alice")
        assert child is not log
        assert child._bound == {"user": "alice"}
        assert log._bound == {}

    def test_merges_context(self) -> None:
        log = Logger().bind(a=1)
        child = log.bind(b=2)
        assert child._bound == {"a": 1, "b": 2}

    def test_overrides_existing_key(self) -> None:
        log = Logger().bind(a=1)
        child = log.bind(a=2)
        assert child._bound == {"a": 2}


class TestLoggerContextualize:
    def test_context_manager(self) -> None:
        log = Logger()
        with log.contextualize(request_id="abc") as ctx_logger:
            assert ctx_logger is log


class TestLoggerOpt:
    def test_sets_exc_info(self) -> None:
        log = Logger()
        child = log.opt(exception=True)
        assert child._opt_exc_info is True
        assert log._opt_exc_info is None

    def test_sets_stack_info(self) -> None:
        log = Logger()
        child = log.opt(stack_info=True)
        assert child._opt_stack_info is True
        assert log._opt_stack_info is False


class TestLoggerLevelMethods:
    def _make_capturing_logger(self) -> tuple[Logger, io.StringIO]:
        buf = io.StringIO()
        configure_structlog(service="test", level="DEBUG", json_logs=True, stream=buf)
        return Logger(), buf

    def test_debug(self) -> None:
        log, buf = self._make_capturing_logger()
        log.debug("test debug")
        assert "test debug" in buf.getvalue()

    def test_info(self) -> None:
        log, buf = self._make_capturing_logger()
        log.info("test info")
        assert "test info" in buf.getvalue()

    def test_warning(self) -> None:
        log, buf = self._make_capturing_logger()
        log.warning("test warning")
        assert "test warning" in buf.getvalue()

    def test_error(self) -> None:
        log, buf = self._make_capturing_logger()
        log.error("test error")
        assert "test error" in buf.getvalue()

    def test_critical(self) -> None:
        log, buf = self._make_capturing_logger()
        log.critical("test critical")
        assert "test critical" in buf.getvalue()

    def test_brace_formatting(self) -> None:
        log, buf = self._make_capturing_logger()
        log.info("Hello {name}", name="world")
        output = buf.getvalue()
        assert "Hello world" in output

    def test_positional_args_no_placeholders_does_not_crash(self) -> None:
        log, buf = self._make_capturing_logger()
        log.info("Hello", "extra")
        assert "Hello" in buf.getvalue()

    def test_warn_alias(self) -> None:
        log, buf = self._make_capturing_logger()
        log.warn("test warn")
        assert "test warn" in buf.getvalue()

    def test_fatal_alias(self) -> None:
        log, buf = self._make_capturing_logger()
        log.fatal("test fatal")
        assert "test fatal" in buf.getvalue()


class TestLoggerAddRemove:
    def test_add_and_remove(self) -> None:
        configure_structlog(service="test", level="DEBUG", json_logs=True, stream=io.StringIO())
        log = Logger()
        messages: list[str] = []
        hid = log.add(messages.append, level="DEBUG")
        assert isinstance(hid, int)

        log.remove(hid)

    def test_remove_all(self) -> None:
        configure_structlog(service="test", level="DEBUG", json_logs=True, stream=io.StringIO())
        log = Logger()
        messages: list[str] = []
        log.add(messages.append, level="DEBUG")
        log.add(messages.append, level="DEBUG")
        log.remove()
        assert len(log._handlers) == 0

    def test_add_callable_receives_messages(self) -> None:
        buf = io.StringIO()
        configure_structlog(service="test", level="DEBUG", json_logs=True, stream=buf)
        log = Logger()
        messages: list[str] = []
        log.add(messages.append, level="DEBUG")
        log.info("captured")
        assert any("captured" in m for m in messages)

    def test_remove_closes_handler(self, tmp_path: Path) -> None:
        configure_structlog(service="test", level="DEBUG", json_logs=True, stream=io.StringIO())
        log = Logger()
        hid = log.add(tmp_path / "test.log", level="DEBUG")
        # Grab the stream before close() sets it to None
        root = logging.getLogger()
        file_handlers = [h for h in root.handlers if isinstance(h, logging.FileHandler)]
        assert len(file_handlers) == 1
        stream = file_handlers[0].stream
        assert stream is not None
        log.remove(hid)
        # FileHandler.close() closes the stream then sets it to None
        assert stream.closed

    def test_remove_all_closes_handlers(self, tmp_path: Path) -> None:
        configure_structlog(service="test", level="DEBUG", json_logs=True, stream=io.StringIO())
        log = Logger()
        log.add(tmp_path / "a.log", level="DEBUG")
        log.add(tmp_path / "b.log", level="DEBUG")
        log.remove()
        # handlers dict is cleared, no leaked file descriptors

    def test_unique_ids_across_instances(self) -> None:
        configure_structlog(service="test", level="DEBUG", json_logs=True, stream=io.StringIO())
        log1 = Logger()
        log2 = Logger()
        id1 = log1.add(io.StringIO(), level="DEBUG")
        id2 = log2.add(io.StringIO(), level="DEBUG")
        assert id1 != id2
        log1.remove()
        log2.remove()


class TestLoggerIntegration:
    def test_bind_with_output(self) -> None:
        buf = io.StringIO()
        configure_structlog(service="test", level="DEBUG", json_logs=True, stream=buf)
        log = Logger().bind(user="alice")
        log.info("action")
        output = buf.getvalue()
        assert "alice" in output
        assert "action" in output

    def test_clear_handlers_false_preserves_existing(self) -> None:
        buf1 = io.StringIO()
        configure_structlog(service="test", level="DEBUG", json_logs=True, stream=buf1)
        root = logging.getLogger()
        handler_count_before = len(root.handlers)

        buf2 = io.StringIO()
        configure_structlog(
            service="test", level="DEBUG", json_logs=True, stream=buf2, clear_handlers=False
        )
        # New handlers are added on top of existing ones
        assert len(root.handlers) > handler_count_before

    def test_contextualize_with_output(self) -> None:
        buf = io.StringIO()
        configure_structlog(service="test", level="DEBUG", json_logs=True, stream=buf)
        log = Logger()
        with log.contextualize(request_id="req-123"):
            log.info("in context")
        output = buf.getvalue()
        assert "req-123" in output
        assert "in context" in output
