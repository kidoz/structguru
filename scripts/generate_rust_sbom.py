"""Generate a deterministic CycloneDX 1.5 SBOM from Cargo metadata."""

from __future__ import annotations

import argparse
import json
import subprocess
import tomllib
from pathlib import Path
from typing import Any
from urllib.parse import quote


def _package_ref(package: dict[str, Any]) -> str:
    """Return a unique CycloneDX reference for one Cargo package."""
    return f"cargo:{package['id']}"


def _component(package: dict[str, Any]) -> dict[str, Any]:
    """Convert one Cargo metadata package into a CycloneDX component."""
    name = str(package["name"])
    version = str(package["version"])
    component: dict[str, Any] = {
        "type": "library",
        "bom-ref": _package_ref(package),
        "name": name,
        "version": version,
        "purl": f"pkg:cargo/{quote(name, safe='')}@{quote(version, safe='')}",
    }
    if license_expression := package.get("license"):
        component["licenses"] = [{"expression": license_expression}]
    return component


def build_sbom(root: Path) -> dict[str, Any]:
    """Build a CycloneDX document for the Rust workspace at *root*."""
    completed = subprocess.run(
        ["cargo", "metadata", "--locked", "--format-version", "1"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    metadata: dict[str, Any] = json.loads(completed.stdout)
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject["project"]
    root_ref = f"pkg:pypi/{project['name']}@{project['version']}"

    packages = {package["id"]: package for package in metadata["packages"]}
    components = sorted(
        (_component(package) for package in packages.values()),
        key=lambda component: (component["name"], component["version"], component["bom-ref"]),
    )
    dependencies = [
        {
            "ref": _package_ref(packages[node["id"]]),
            "dependsOn": sorted(_package_ref(packages[dep["pkg"]]) for dep in node["deps"]),
        }
        for node in metadata["resolve"]["nodes"]
    ]
    dependencies.sort(key=lambda dependency: dependency["ref"])
    workspace_refs = sorted(
        _package_ref(packages[package_id]) for package_id in metadata["workspace_members"]
    )
    dependencies.insert(0, {"ref": root_ref, "dependsOn": workspace_refs})

    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {
            "component": {
                "type": "library",
                "bom-ref": root_ref,
                "name": project["name"],
                "version": project["version"],
                "purl": root_ref,
            }
        },
        "components": components,
        "dependencies": dependencies,
    }


def main() -> None:
    """Parse CLI arguments and write the generated SBOM."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    document = build_sbom(root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(f"{json.dumps(document, indent=2, sort_keys=True)}\n", encoding="utf-8")


if __name__ == "__main__":
    main()
