"""Regression tests for release gating and dependency inventories."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_rust_sbom_contains_workspace_and_dependency_graph(tmp_path: Path) -> None:
    output = tmp_path / "rust.cdx.json"

    subprocess.run(
        [sys.executable, "scripts/generate_rust_sbom.py", "--output", str(output)],
        check=True,
    )
    sbom = json.loads(output.read_text(encoding="utf-8"))

    assert sbom["bomFormat"] == "CycloneDX"
    assert sbom["specVersion"] == "1.5"
    assert sbom["metadata"]["component"]["name"] == "structguru"
    component_names = {component["name"] for component in sbom["components"]}
    assert {"pyo3", "structguru-core", "structguru-py"} <= component_names
    assert sbom["dependencies"]


def test_publish_job_depends_on_release_and_supply_chain_gates() -> None:
    workflow = Path(".github/workflows/wheels.yml").read_text(encoding="utf-8")

    assert "release-python" in workflow
    assert "release-free-threaded" in workflow
    assert "release-rust" in workflow
    assert "supply-chain" in workflow
    assert (
        "needs: [release-python, release-free-threaded, release-rust, build, sdist, supply-chain]"
        in workflow
    )


def test_quality_gate_targets_are_declared() -> None:
    makefile = Path("Makefile").read_text(encoding="utf-8")

    assert "python-check:" in makefile
    assert "rust-check:" in makefile
    assert "check: python-check rust-check" in makefile


def test_ci_audits_dependencies_on_every_change() -> None:
    # A vulnerable dependency must surface at pull-request time, not at publish
    # time — wheels.yml only runs the audit on the release path.
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "supply-chain:" in workflow
    assert "uv audit --locked" in workflow
    assert "cargo deny --locked check advisories" in workflow
    assert Path("deny.toml").is_file()
