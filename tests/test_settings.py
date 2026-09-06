from __future__ import annotations

import io
import json
import logging
import os
import subprocess
import sys
import threading
from dataclasses import FrozenInstanceError, replace
from typing import Any

import pytest

from structguru import (
    Settings,
    _runtime,
    configure,
    flush,
    get_config,
    logger,
    set_level,
    shutdown,
    update,
)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in os.environ:
        if name.startswith("STRUCTGURU_") or name == "LOG_LEVEL":
            monkeypatch.delenv(name)


def test_environment_precedence_and_explicit_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "ERROR")
    monkeypatch.setenv("STRUCTGURU_LEVEL", "DEBUG")
    monkeypatch.setenv("STRUCTGURU_SERVICE", "environment")
    monkeypatch.setenv("STRUCTGURU_TARGET", "memory")
    monkeypatch.setenv("STRUCTGURU_NATIVE_TARGET", "invalid")
    configure(service="application")
    assert get_config() == Settings(service="application", target="memory", level="DEBUG")
    configure(level="INFO", service="app")
    assert get_config() == Settings(target="memory")


def test_explicit_overrides_skip_invalid_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STRUCTGURU_LEVEL", "typo")
    monkeypatch.setenv("STRUCTGURU_SAMPLE_RATE", "bad")
    monkeypatch.setenv("STRUCTGURU_RATE_LIMIT", "bad/bad")
    configure(
        target="memory", level="INFO", sample_rate=1.0, rate_limit_max=None, rate_limit_period=60.0
    )
    assert get_config() == Settings(target="memory")
    with pytest.raises(ValueError, match="level"):
        Settings.from_env({"STRUCTGURU_LEVEL": "typo"})


def test_legacy_environment_names_and_partial_rate_override() -> None:
    settings = Settings.from_env(
        {
            "LOG_LEVEL": "debug",
            "STRUCTGURU_NATIVE_TARGET": "memory",
            "STRUCTGURU_NATIVE_SAMPLE_RATE": "0.5",
            "STRUCTGURU_NATIVE_RATE_LIMIT": "20/2.5",
        },
        rate_limit_max=None,
    )
    assert settings.level == "DEBUG"
    assert settings.target == "memory"
    assert settings.sample_rate == 0.5
    assert settings.rate_limit_max is None
    assert settings.rate_limit_period == 2.5


def test_settings_base_ignores_environment_and_preserves_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STRUCTGURU_SAMPLE_RATE", "invalid")
    keys = ["custom"]
    stream = io.StringIO()
    records: list[str] = []
    callback = records.append
    settings = Settings(
        target="memory", sensitive_keys=keys, stream_sink=stream, callable_sinks=[callback]
    )
    keys.append("changed")
    assert settings.sensitive_keys == ("custom",)
    with pytest.raises(FrozenInstanceError):
        settings.service = "changed"  # type: ignore[misc]
    configure(settings, service="application")
    active = get_config()
    assert active is not None
    assert active.stream_sink is stream
    assert active.callable_sinks == (callback,)
    logger.info("test", custom="private")
    flush()
    assert json.loads(records[0])["custom"] == "[REDACTED]"


def test_configure_replaces_settings_and_explicit_none_and_false_win() -> None:
    settings = Settings(target="memory", service="original", otel=True, sensitive_keys=["custom"])
    configure(settings, otel=False, sensitive_keys=None)
    assert get_config() == replace(settings, otel=False, sensitive_keys=None)
    configure(target="memory")
    assert get_config() == Settings(target="memory")


@pytest.mark.parametrize("level", ["INF", "debugg", "", -1, True, 1.5, None])
def test_invalid_levels_leave_active_runtime_untouched(level: Any) -> None:
    configure(target="memory")
    state = _runtime.current_runtime()
    for change in (configure, update, set_level):
        with pytest.raises(ValueError, match="level"):
            change(level=level)
        assert _runtime.current_runtime() is state


@pytest.mark.parametrize("level", ["warn", "FaTaL", "NOTSET", "TRACE", logging.WARNING, 25])
def test_valid_level_thresholds(level: str | int) -> None:
    configure(target="memory", level=level)
    logger.debug("debug")
    logger.warning("warning")
    flush()
    messages = [json.loads(line)["message"] for line in _runtime.drain_messages()]
    if level == "FaTaL":
        assert messages == []
    elif level in ("NOTSET", "TRACE"):
        assert messages == ["debug", "warning"]
    else:
        assert messages == ["warning"]


def test_update_preserves_options_and_does_not_reread_env(monkeypatch: pytest.MonkeyPatch) -> None:
    configure(
        service="application", target="memory", sensitive_patterns=["private"], rate_limit_max=5
    )
    monkeypatch.setenv("STRUCTGURU_SERVICE", "other")
    monkeypatch.setenv("STRUCTGURU_LEVEL", "invalid")
    update(structured_exceptions=True)
    settings = get_config()
    assert settings is not None
    assert settings.service == "application"
    assert settings.sensitive_patterns == ("private",)
    assert settings.rate_limit_max == 5
    assert settings.structured_exceptions
    update(sensitive_patterns=None)
    assert get_config() == replace(settings, sensitive_patterns=None)


def test_level_updates_preserve_queued_records_and_rate_limit_state() -> None:
    records: list[str] = []
    configure(
        target="memory",
        rate_limit_max=1,
        rate_limit_period=3600,
        callable_sinks=[records.append],
    )
    channel = _runtime._callable_dispatcher._channel
    logger.info("same message")
    state = _runtime.current_runtime()
    assert state is not None
    update(level="DEBUG")
    set_level(logging.DEBUG)
    current = _runtime.current_runtime()
    assert current is not None
    assert current.writer is state.writer
    assert current.record_filter is state.record_filter
    assert _runtime._callable_dispatcher._channel is channel
    logger.info("same message")
    flush()
    assert len(_runtime.drain_messages()) == 1
    assert len(records) == 1
    assert get_config() == replace(state.settings, level=logging.DEBUG)
    update()
    assert _runtime.current_runtime() is current


def test_update_retries_when_another_update_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    configure(target="memory")
    ready = threading.Event()
    proceed = threading.Event()
    original = _runtime._RUST
    assert original is not None

    class DelayedWriterFactory:
        def __getattr__(self, name: str) -> Any:
            return getattr(original, name)

        def _NativeStringWriter(self, *args: Any, **kwargs: Any) -> Any:
            writer = original._NativeStringWriter(*args, **kwargs)
            ready.set()
            assert proceed.wait(5)
            return writer

    monkeypatch.setattr(_runtime, "_RUST", DelayedWriterFactory())
    errors: list[BaseException] = []

    def change_service() -> None:
        try:
            update(service="changed")
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=change_service)
    thread.start()
    try:
        assert ready.wait(5)
        update(level="DEBUG")
    finally:
        proceed.set()
        thread.join(5)
    assert not thread.is_alive()
    assert not errors
    assert get_config() == Settings(target="memory", service="changed", level="DEBUG")


@pytest.mark.parametrize(
    "changes",
    [
        {"target": "invalid"},
        {"maxsize": -1},
        {"sample_rate": float("nan")},
        {"rate_limit_period": float("inf")},
        {"file_max_bytes": "50 MB"},
        {"colors": "false"},
        {"sensitive_keys": "password"},
        {"unknown": True},
    ],
)
def test_mapping_validation(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        Settings.from_mapping(changes)


def test_native_failure_preserves_settings_and_writer() -> None:
    configure(target="memory", service="original")
    state = _runtime.current_runtime()
    with pytest.raises(ValueError, match="sensitive_patterns"):
        update(service="failed", sensitive_patterns=["("])
    assert _runtime.current_runtime() is state
    logger.info("still working")
    flush()
    assert json.loads(_runtime.drain_messages()[0])["service"] == "original"


def test_unconfigured_state() -> None:
    shutdown()
    assert get_config() is None
    with pytest.raises(RuntimeError, match="not configured"):
        update(level="DEBUG")
    set_level("DEBUG")
    assert get_config() is None


def test_explicit_configure_works_with_import_opt_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STRUCTGURU_AUTOCONFIGURE", "0")
    monkeypatch.setenv("STRUCTGURU_FORMAT", "console")
    configure(target="memory", colors=False)
    logger.info("visible")
    flush()
    assert "[INFO    ] visible" in _runtime.drain_messages()[0]


@pytest.mark.parametrize(
    "environ, success",
    [
        ({"STRUCTGURU_AUTOCONFIGURE": "0", "STRUCTGURU_LEVEL": "invalid"}, True),
        ({"STRUCTGURU_AUTOCONFIGURE": "0", "STRUCTGURU_LEGACY": "0"}, True),
        (
            {
                "STRUCTGURU_AUTOCONFIGURE": "1",
                "STRUCTGURU_LEGACY": "1",
                "STRUCTGURU_LEVEL": "invalid",
            },
            False,
        ),
        ({"STRUCTGURU_LEVEL": "invalid"}, False),
        ({"STRUCTGURU_AUTOCONFIGURE": "invalid"}, False),
    ],
)
def test_import_configuration_switches(environ: dict[str, str], success: bool) -> None:
    result = subprocess.run(
        [sys.executable, "-B", "-c", "import structguru; assert structguru.get_config() is None"],
        env={**os.environ, **environ},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert (result.returncode == 0) is success, result.stderr
    if not success:
        assert "ValueError" in result.stderr


@pytest.mark.parametrize("change", [update, set_level])
@pytest.mark.parametrize("format", ["json", "console"])
def test_level_change_during_record_preserves_all_deliveries(change: Any, format: str) -> None:
    callbacks: list[str] = []
    sentry: list[dict[str, Any]] = []

    def metrics(*args: Any) -> None:
        # Deterministically cross the level-change boundary mid-record.
        change(level="ERROR")

    configure(
        target="memory",
        format=format,
        colors=False,
        metric_processor=metrics,
        callable_sinks=[callbacks.append],
        sentry_processor=lambda _, method, event: sentry.append(event),
    )
    logger.info("already admitted")
    logger.info("now filtered")
    flush()
    lines = _runtime.drain_messages()
    assert len(lines) == 1
    assert callbacks == lines
    assert [event["event"] for event in sentry] == ["already admitted"]
