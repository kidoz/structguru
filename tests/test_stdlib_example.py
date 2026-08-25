"""Smoke tests for the extras-free stdlib existing-loggers example.

This stays separate from ``test_examples.py`` because that module skips when
optional framework extras are unavailable.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from structguru import _runtime

pytestmark = pytest.mark.skipif(
    not _runtime.is_available(),
    reason="native extension not built",
)

_EXAMPLE = (
    Path(__file__).resolve().parent.parent / "examples" / "stdlib_existing_loggers" / "main.py"
)


def _run_example(
    *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_EXAMPLE), *args],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


def _assert_policy_output(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "CREATED_AFTER_RECORD" in result.stdout
    assert "EXISTING_LOGGER_RECORD" not in result.stdout


def test_stdlib_existing_loggers_example_with_code_config() -> None:
    _assert_policy_output(_run_example())


def test_stdlib_existing_loggers_example_with_environment() -> None:
    env = os.environ.copy()
    env["STRUCTGURU_STDLIB_LEVEL"] = "DEBUG"
    env["STRUCTGURU_STDLIB_DISABLE_EXISTING_LOGGERS"] = "true"
    _assert_policy_output(_run_example("--from-env", env=env))
