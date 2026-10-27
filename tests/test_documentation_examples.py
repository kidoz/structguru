"""Structural checks for code examples shipped in the documentation."""

from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from structguru import __version__

_ROOT = Path(__file__).resolve().parent.parent
_MARKDOWN_FILES = [_ROOT / "README.md", *sorted((_ROOT / "docs").rglob("*.md"))]
_FENCE_RE = re.compile(
    r"^```(?P<language>[^\n]*)\n(?P<body>.*?)^```\s*$",
    re.MULTILINE | re.DOTALL,
)


def _path_id(path: Path) -> str:
    return path.relative_to(_ROOT).as_posix()


def _fences(path: Path, language: str) -> list[str]:
    return [
        match.group("body")
        for match in _FENCE_RE.finditer(path.read_text())
        if match.group("language").strip() == language
    ]


@pytest.mark.parametrize("path", _MARKDOWN_FILES, ids=_path_id)
def test_python_fences_have_valid_syntax(path: Path) -> None:
    for index, source in enumerate(_fences(path, "python")):
        try:
            ast.parse(source)
        except SyntaxError as exc:
            pytest.fail(f"Python fence {index} in {_path_id(path)} is invalid: {exc}")


@pytest.mark.parametrize("path", _MARKDOWN_FILES, ids=_path_id)
def test_complete_json_fences_are_parseable(path: Path) -> None:
    for index, source in enumerate(_fences(path, "json")):
        if "…" in source or not source.lstrip().startswith(("{", "[")):
            continue
        try:
            json.loads(source)
        except json.JSONDecodeError:
            try:
                for line in source.splitlines():
                    if line.strip():
                        json.loads(line)
            except json.JSONDecodeError as exc:
                pytest.fail(f"JSON fence {index} in {_path_id(path)} is invalid: {exc}")


@pytest.mark.parametrize("path", _MARKDOWN_FILES, ids=_path_id)
@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is unavailable")
def test_bash_fences_have_valid_syntax(path: Path) -> None:
    for index, source in enumerate(_fences(path, "bash")):
        result = subprocess.run(
            ["bash", "-n"],
            input=source,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        assert result.returncode == 0, (
            f"Bash fence {index} in {_path_id(path)} is invalid: {result.stderr}"
        )


def test_getting_started_displays_the_current_version() -> None:
    text = (_ROOT / "docs" / "tutorials" / "getting-started.md").read_text()
    match = re.search(r"You should see the version number:\s*```text\n([^`\n]+)", text)
    assert match is not None
    assert match.group(1) == __version__
