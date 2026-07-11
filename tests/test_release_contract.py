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


def test_governance_dependency_and_gate_are_declared() -> None:
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    makefile = Path("Makefile").read_text(encoding="utf-8")

    assert '"jsonschema>=4.25,<5"' in pyproject
    assert "governance:" in makefile
    assert "python-check:" in makefile
    assert "rust-check:" in makefile
    assert "check: python-check rust-check" in makefile
    assert "validate_standards_package.py --root . --mode strict-governance" in makefile
