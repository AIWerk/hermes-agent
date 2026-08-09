from __future__ import annotations

import json
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "immutable-release-build.json"


def _packaged_roots(pyproject: dict) -> list[str]:
    includes = pyproject["tool"]["setuptools"]["packages"]["find"]["include"]
    return sorted({entry.split(".", 1)[0] for entry in includes})


def test_release_build_contract_covers_packaged_python_inventory() -> None:
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert contract["schema_version"] == 1
    assert contract["release_namespace"] == "releases-v2"
    assert contract["python"]["package_roots"] == _packaged_roots(pyproject)
    assert contract["python"]["module_files"] == [
        f"{name}.py" for name in pyproject["tool"]["setuptools"]["py-modules"]
    ]
    assert contract["python"]["include_venv_site_packages"] is True

    declared = set(contract["python"]["package_roots"]) | set(contract["python"]["module_files"])
    assert all((ROOT / item).exists() for item in declared)


def test_release_build_contract_requires_full_locked_build_without_pruning() -> None:
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    build = contract["build"]

    assert contract["build"]["python"] == [
        "uv",
        "sync",
        "--locked",
        "--no-build",
        "--no-install-project",
        "--all-groups",
        "--all-extras",
    ]
    assert build["node"] == ["npm", "ci", "--ignore-scripts"]
    assert "--workspace" not in build["node"]
    assert build["web"] == ["npm", "run", "build", "--workspace", "web"]
    assert build["web_artifact"] == "hermes_cli/web_dist/index.html"
    assert build["preserve_complete_repository"] is True
    assert build["preserve_complete_npm_workspace_graph"] is True
    assert build["python_environment"] == {
        "PYTHONDONTWRITEBYTECODE": "1",
        "UV_COMPILE_BYTECODE": "0",
        "UV_PYTHON_DOWNLOADS": "never",
    }
    assert build["node_environment"] == {
        "NPM_CONFIG_OMIT": "",
        "NPM_CONFIG_PRODUCTION": "false",
    }

    encoded = json.dumps(contract)
    assert "PYTHONPYCACHEPREFIX" not in encoded
    assert "apps/desktop" not in contract.get("excluded_paths", [])


def test_release_build_contract_declares_mandatory_actual_release_gate_scenarios() -> None:
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    scenarios = contract["closing_gate"]["scenarios"]

    assert contract["closing_gate"]["mandatory"] is True
    assert scenarios == [
        {
            "name": "cli-entry",
            "startup_modules": ["hermes_constants"],
            "late_modules": ["hermes_cli.main"],
        },
        {
            "name": "dashboard-entry",
            "startup_modules": ["hermes_cli.config"],
            "late_modules": ["hermes_cli.web_server"],
        },
        {
            "name": "gateway-entry",
            "startup_modules": ["gateway.status"],
            "late_modules": ["gateway.run"],
        },
    ]
    assert all(
        set(item["startup_modules"]).isdisjoint(item["late_modules"])
        for item in scenarios
    )
