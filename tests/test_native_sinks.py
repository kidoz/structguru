"""Native-path sink tests: rotating-file sink, callable sinks, and console renderer.

Covers Phase 2 of the Rust migration: native file output with rotation,
deadlock-free callable-sink dispatch, and the colored console renderer.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

import structguru
from structguru import _native

pytestmark = pytest.mark.skipif(
    not _native.native_available(),
    reason="native extension not built",
)


# -- rotating-file sink -----------------------------------------------------


def test_file_sink_writes_records_to_file() -> None:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
        path = f.name
    os.unlink(path)  # let the sink create it
    try:
        _native.configure(service="svc", target="memory", level="DEBUG", file_path=path)
        try:
            structguru.logger.info("file sink test", request_id="r1")
            _native.flush_native()
        finally:
            _native.disable_native()

        with open(path) as f:
            content = f.read()
        assert "file sink test" in content
        assert '"request_id":"r1"' in content
    finally:
        if os.path.exists(path):
            os.unlink(path)


def test_file_sink_rotates_at_max_bytes() -> None:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
        path = f.name
    os.unlink(path)
    try:
        # Tiny max_bytes so rotation triggers quickly; backup_count=3.
        _native.configure(
            service="svc",
            target="memory",
            level="DEBUG",
            file_path=path,
            file_max_bytes=200,
            file_backup_count=3,
        )
        try:
            # Each record is ~150 bytes of JSON; write enough to trigger rotations.
            for i in range(10):
                structguru.logger.info("rotation test {n}", n=i)
            _native.flush_native()
        finally:
            _native.disable_native()

        # After rotation, .1 should exist (the first rotated file).
        assert os.path.exists(f"{path}.1"), "backup .1 should exist after rotation"
        # backup_count=3 means at most 3 backups.
        assert not os.path.exists(f"{path}.4"), ".4 should not exist (backup_count=3)"

        # Rotation happens before the threshold-crossing record, matching
        # logging.handlers.RotatingFileHandler.
        with open(path) as f:
            active = f.read()
        with open(f"{path}.1") as f:
            backup1 = f.read()
        assert active or backup1, "at least one file must have content"
    finally:
        for suffix in ["", ".1", ".2", ".3", ".4"]:
            p = f"{path}{suffix}"
            if os.path.exists(p):
                os.unlink(p)


def test_file_sink_and_stdout_both_receive() -> None:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
        path = f.name
    os.unlink(path)
    try:
        _native.configure(
            service="svc",
            target="memory",
            level="DEBUG",
            file_path=path,
            also_stdout=True,
        )
        try:
            structguru.logger.info("mirrored record")
            _native.flush_native()
        finally:
            _native.disable_native()

        with open(path) as f:
            assert "mirrored record" in f.read()
    finally:
        if os.path.exists(path):
            os.unlink(path)


# -- callable sinks ---------------------------------------------------------


def test_callable_sink_receives_rendered_lines() -> None:
    received: list[str] = []
    lock = threading.Lock()

    def collector(line: str) -> None:
        with lock:
            received.append(line)

    _native.configure(service="svc", target="memory", level="DEBUG", callable_sinks=[collector])
    try:
        for i in range(5):
            structguru.logger.info("callable {n}", n=i)
        _native.flush_native()
    finally:
        _native.disable_native()

    assert len(received) == 5
    assert json.loads(received[0])["message"] == "callable 0"


def test_callable_sink_errors_are_swallowed() -> None:
    good: list[str] = []
    lock = threading.Lock()

    def bad_sink(_line: str) -> None:
        raise RuntimeError("boom")

    def good_sink(line: str) -> None:
        with lock:
            good.append(line)

    _native.configure(
        service="svc",
        target="memory",
        level="DEBUG",
        callable_sinks=[bad_sink, good_sink],
    )
    try:
        structguru.logger.info("survives")
        _native.flush_native()
    finally:
        _native.disable_native()

    assert len(good) == 1, "good sink must still receive despite bad_sink raising"


def test_callable_sink_stopped_on_disable() -> None:
    received: list[str] = []
    lock = threading.Lock()

    def collector(line: str) -> None:
        with lock:
            received.append(line)

    _native.configure(service="svc", target="memory", level="DEBUG", callable_sinks=[collector])
    structguru.logger.info("before disable")
    _native.disable_native()

    count_before = len(received)
    assert count_before == 1, "disable_native must drain pending callable deliveries"
    structguru.logger.info("after disable")
    assert len(received) == count_before


def test_reconfigure_drains_old_configured_sinks_before_replacing() -> None:
    first: list[str] = []
    second: list[str] = []
    _native.configure(target="memory", callable_sinks=[first.append])
    structguru.logger.info("old configuration")
    _native.configure(target="memory", callable_sinks=[second.append])
    try:
        structguru.logger.info("new configuration")
        _native.flush_native()
    finally:
        _native.disable_native()

    assert any("old configuration" in line for line in first)
    assert not any("new configuration" in line for line in first)
    assert any("new configuration" in line for line in second)


def test_atexit_drains_callable_sinks(tmp_path: Path) -> None:
    output = tmp_path / "atexit-callback.log"
    code = f"""
from pathlib import Path
import structguru

output = Path({str(output)!r})
def sink(line):
    output.write_text(line, encoding="utf-8")

structguru.configure(target="null", callable_sinks=[sink])
structguru.logger.info("delivered at exit")
"""
    subprocess.run([sys.executable, "-c", code], check=True)
    assert "delivered at exit" in output.read_text(encoding="utf-8")


def test_callable_sink_queue_is_bounded_and_counts_drops() -> None:
    started = threading.Event()
    release = threading.Event()

    def blocking_sink(_line: str) -> None:
        started.set()
        release.wait(timeout=2)

    _native.configure(
        target="memory",
        overflow="drop",
        callable_sinks=[blocking_sink],
        callable_queue_maxsize=1,
    )
    try:
        structguru.logger.info("first")
        assert started.wait(timeout=1)
        structguru.logger.info("second")
        with pytest.warns(UserWarning, match="callable sinks dropped"):
            structguru.logger.info("third")
        metrics = _native.native_metrics()
        assert metrics is not None
        assert metrics["callable_maxsize"] == 1
        assert metrics["callable_dropped"] == 1
    finally:
        release.set()
        _native.disable_native()


def test_invalid_callable_queue_maxsize_raises() -> None:
    with pytest.raises(ValueError, match="callable_queue_maxsize"):
        _native.configure(callable_queue_maxsize=0)


def test_non_callable_sink_raises() -> None:
    with pytest.raises(TypeError, match="callable_sinks"):
        _native.configure(callable_sinks=["not callable"])  # type: ignore[list-item]
    assert not _native.is_native_enabled()


# -- console renderer -------------------------------------------------------


def _drain_last_line() -> str:
    _native.flush_native()
    return _native.drain_messages()[-1].rstrip("\n")


def test_console_renderer_human_readable_no_colors() -> None:
    _native.configure(service="svc", target="memory", level="DEBUG", json=False, colors=False)
    try:
        structguru.logger.info("hello {name}", name="world", count=3)
        line = _drain_last_line()
    finally:
        _native.disable_native()

    # Format: <timestamp> [<LEVEL>] <message>  k=v ...
    assert "[INFO    ]" in line
    assert "hello world" in line
    assert "count=3" in line
    assert "\x1b" not in line  # no ANSI codes


def test_console_renderer_applies_colors() -> None:
    _native.configure(service="svc", target="memory", level="DEBUG", json=False, colors=True)
    try:
        structguru.logger.error("boom")
        line = _drain_last_line()
    finally:
        _native.disable_native()

    assert "\x1b[31m" in line  # ANSI red
    assert "\x1b[0m" in line  # ANSI reset
    assert "[ERROR   ]" in line


def test_console_renderer_redacts_sensitive_keys() -> None:
    _native.configure(service="svc", target="memory", level="DEBUG", json=False, colors=False)
    try:
        structguru.logger.info("login", password="hunter2", user="alice")
        line = _drain_last_line()
    finally:
        _native.disable_native()

    assert 'password="[REDACTED]"' in line
    assert "hunter2" not in line
    assert 'user="alice"' in line


def test_console_renderer_warn_level_uses_yellow() -> None:
    _native.configure(service="svc", target="memory", level="DEBUG", json=False, colors=True)
    try:
        structguru.logger.warning("careful")
        line = _drain_last_line()
    finally:
        _native.disable_native()

    assert "\x1b[33m" in line  # ANSI yellow
    assert "[WARN    ]" in line
