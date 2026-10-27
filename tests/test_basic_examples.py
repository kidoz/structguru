"""Smoke tests for runnable examples that need no optional dependencies."""

from __future__ import annotations

import json
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

_EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def _run_example(name: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, str(_EXAMPLES / name / "main.py")],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        check=False,
    )


def test_context_propagation_example() -> None:
    result = _run_example("context_propagation")

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    records = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(records) == 4

    work_records = [record for record in records if record["message"] != "Request finished"]
    assert {record["item"] for record in work_records} == {"first", "second", "third"}
    assert all(record["request_id"] == "req-42" for record in work_records)
    assert all(record["service"] == "context-example" for record in records)

    final = records[-1]
    assert final["message"] == "Request finished"
    assert "request_id" not in final


def test_sinks_and_testing_example() -> None:
    result = _run_example("sinks_and_testing")

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert result.stdout == ""
    assert result.stderr == ""
