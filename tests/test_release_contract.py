"""Regression tests for release gating and dependency inventories."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path


def _workflow_job(workflow: str, name: str) -> str:
    match = re.search(rf"(?m)^  {re.escape(name)}:\n", workflow)
    assert match is not None, f"missing workflow job {name}"
    remainder = workflow[match.end() :]
    next_job = re.search(r"(?m)^  [a-z][a-z0-9-]*:\n", remainder)
    end = match.end() + next_job.start() if next_job else len(workflow)
    return workflow[match.start() : end]


def _job_header(workflow: str, name: str) -> str:
    return _workflow_job(workflow, name).split("\n    steps:", 1)[0]


def test_rust_sbom_contains_workspace_and_dependency_graph(tmp_path: Path) -> None:
    output = tmp_path / "rust.cdx.json"

    subprocess.run(
        [sys.executable, "scripts/generate_rust_sbom.py", "--output", str(output)],
        check=True,
    )
    sbom = json.loads(output.read_text(encoding="utf-8"))

    assert sbom["bomFormat"] == "CycloneDX"
    assert sbom["specVersion"] == "1.5"
    root = sbom["metadata"]["component"]
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert root["name"] == "structguru"
    assert root["bom-ref"] == root["purl"] == f"pkg:pypi/structguru@{project['version']}"
    component_names = {component["name"] for component in sbom["components"]}
    assert {"pyo3", "structguru-core", "structguru-py"} <= component_names
    known_refs = {root["bom-ref"]}
    component_refs = set()
    for component in sbom["components"]:
        assert component["bom-ref"] not in component_refs
        component_refs.add(component["bom-ref"])
        assert "file://" not in component["bom-ref"]
        if component["name"] in {"structguru-core", "structguru-py"}:
            assert "purl" not in component
            assert component["bom-ref"].startswith("cargo:workspace/")
        else:
            assert component["bom-ref"] == component["purl"]
            assert component["bom-ref"].startswith("pkg:cargo/")
        known_refs.add(component["bom-ref"])
    assert sbom["dependencies"]
    for dependency in sbom["dependencies"]:
        assert dependency["ref"] in known_refs
        assert set(dependency["dependsOn"]) <= known_refs


def test_rust_sbom_refs_are_source_stable_and_private_purls_are_omitted() -> None:
    import runpy

    _package_ref = runpy.run_path("scripts/generate_rust_sbom.py")["_package_ref"]

    private = {
        "id": "path+file:///private/checkout/structguru-core#1.5.0",
        "name": "structguru-core",
        "version": "1.5.0",
        "source": None,
    }
    moved = {**private, "id": "path+file:///another/checkout/structguru-core#1.5.0"}
    registry = {
        "id": "registry+https://github.com/rust-lang/crates.io-index#pyo3@0.27.0",
        "name": "pyo3",
        "version": "0.27.0",
        "source": "registry+https://github.com/rust-lang/crates.io-index",
    }

    assert _package_ref(private) == _package_ref(moved) == "cargo:workspace/structguru-core@1.5.0"
    assert _package_ref(registry) == "pkg:cargo/pyo3@0.27.0"


def test_github_release_requires_successful_publication() -> None:
    # The release job used to need only the SBOM upload, so a failed test,
    # wheel build, or PyPI publish still created the GitHub Release. `if`
    # replaces the implicit success() check, so the result has to be named.
    workflow = Path(".github/workflows/wheels.yml").read_text(encoding="utf-8")
    header = _job_header(workflow, "github-release")

    assert "needs: [publish, supply-chain]" in header
    assert "needs.publish.result == 'success'" in header
    assert "needs.supply-chain.result == 'success'" in header
    assert "startsWith(github.ref, 'refs/tags/v')" in header


def test_publish_job_depends_on_release_and_supply_chain_gates() -> None:
    workflow = Path(".github/workflows/wheels.yml").read_text(encoding="utf-8")
    header = _job_header(workflow, "publish")

    for gate in (
        "release-python",
        "release-lowest",
        "release-free-threaded",
        "release-rust",
        "release-msrv",
        "build",
        "audit-wheels",
        "runtime-smoke",
        "build-free-threaded",
        "sdist",
        "supply-chain",
    ):
        assert gate in header


def test_free_threaded_wheels_are_built_and_smoke_tested() -> None:
    # pip never selects the abi3 wheel on free-threaded CPython, so 3.14t and
    # 3.15t need their own version-specific wheels or every install compiles
    # from the sdist.
    workflow = Path(".github/workflows/wheels.yml").read_text(encoding="utf-8")

    assert "build-free-threaded:" in workflow
    assert 'ft-python: "3.14t"' in workflow
    assert 'ft-python: "3.15t"' in workflow
    assert "/opt/python/cp314-cp314t/bin/python" in workflow
    assert "/opt/python/cp315-cp315t/bin/python" in workflow
    assert "assert not sys._is_gil_enabled()" in workflow
    assert "name: wheels-free-threaded-" in workflow


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
    assert "cargo deny --locked check advisories bans licenses sources" in workflow
    assert Path("deny.toml").is_file()


def test_cargo_deny_checks_sources_in_local_and_release_audits() -> None:
    command = "cargo deny --locked check advisories bans licenses sources"

    assert command in Path("Makefile").read_text(encoding="utf-8")
    assert command in Path(".github/workflows/wheels.yml").read_text(encoding="utf-8")


def test_wheel_audit_is_locked_and_isolated_from_builds() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    workflow = Path(".github/workflows/wheels.yml").read_text(encoding="utf-8")
    job = _workflow_job(workflow, "audit-wheels")

    assert pyproject["dependency-groups"]["wheel-audit"] == ["abi3audit==0.0.26"]
    assert "needs: build" in _job_header(workflow, "audit-wheels")
    assert "uv sync --locked --no-default-groups --group wheel-audit --no-install-project" in job
    assert "uv run --no-sync abi3audit --strict --report dist/*.whl" in job
    assert "pip install abi3audit" not in workflow
    assert "abi3audit" not in _workflow_job(workflow, "build")


def test_runtime_smoke_covers_native_and_pinned_musl_platforms() -> None:
    workflow = Path(".github/workflows/wheels.yml").read_text(encoding="utf-8")
    job = _workflow_job(workflow, "runtime-smoke")

    assert "needs: build" in _job_header(workflow, "runtime-smoke")
    assert "ubuntu-24.04-arm" in job
    assert "macos-15-intel" in job
    assert "docker/setup-qemu-action@99012661954931238ded8c8b007157a8430204e1" in job
    assert re.search(r"alpine:3\.22@sha256:[0-9a-f]{64}", job)
    assert 'cd "$RUNNER_TEMP"' in job
    assert "apk add --no-cache python3 py3-pip" in job
    assert "pip install --no-index /dist/*.whl" in job


def test_sdist_is_installed_and_imported_outside_the_checkout() -> None:
    workflow = Path(".github/workflows/wheels.yml").read_text(encoding="utf-8")
    job = _workflow_job(workflow, "sdist")

    assert 'python -m venv "$RUNNER_TEMP/sdist-smoke"' in job
    assert "-m pip install dist/*.tar.gz" in job
    assert 'cd "$RUNNER_TEMP"' in job
    assert "assert structguru.is_available()" in job


def test_lowest_direct_dependency_lanes_do_not_restore_locked_versions() -> None:
    for path, job_name in (
        (".github/workflows/ci.yml", "lowest"),
        (".github/workflows/wheels.yml", "release-lowest"),
    ):
        workflow = Path(path).read_text(encoding="utf-8")
        job = _workflow_job(workflow, job_name)

        assert 'UV_NO_SYNC: "1"' in job
        assert "uv sync --resolution lowest-direct --all-extras" in job
        assert "uv run --no-sync python - <<'PY'" in job
        assert "importlib.metadata.version(name)" in job
        assert "assert Version(installed) >= Version(minimum)" in job
        assert "run: make lint test" in job
    assert "needs: metadata" in _job_header(workflow, "release-lowest")


def test_release_enforces_the_rust_msrv() -> None:
    workflow = Path(".github/workflows/wheels.yml").read_text(encoding="utf-8")
    job = _workflow_job(workflow, "release-msrv")

    assert "needs: metadata" in _job_header(workflow, "release-msrv")
    assert 'toolchain: "1.89.0"' in job
    assert "run: cargo check --workspace" in job


def test_license_is_included_in_the_sdist_only() -> None:
    # PyPI rejects an sdist that lacks the declared License-File, but the wheel
    # already bundles it under dist-info/licenses/; an unscoped include also
    # dropped a stray top-level LICENSE into site-packages on install.
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    assert pyproject["tool"]["maturin"]["include"] == [{"path": "LICENSE", "format": "sdist"}]
