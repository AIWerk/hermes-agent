from __future__ import annotations

import json
import tomllib
from collections import deque
from pathlib import Path

from packaging.markers import Marker
from packaging.utils import parse_wheel_filename


ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "immutable-release-build.json"
CANONICAL_EXTRAS = ["messaging", "honcho", "tts-premium", "mcp", "firecrawl"]
TARGET_MARKER_ENV = {
    "implementation_name": "cpython",
    "implementation_version": "3.12.0",
    "os_name": "posix",
    "platform_machine": "x86_64",
    "platform_python_implementation": "CPython",
    "platform_release": "",
    "platform_system": "Linux",
    "platform_version": "",
    "python_full_version": "3.12.0",
    "python_version": "3.12",
    "sys_platform": "linux",
}


def _packaged_roots(pyproject: dict) -> list[str]:
    includes = pyproject["tool"]["setuptools"]["packages"]["find"]["include"]
    return sorted({entry.split(".", 1)[0] for entry in includes})


def test_release_build_contract_covers_packaged_python_inventory() -> None:
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert contract["schema_version"] == 2
    assert contract["release_namespace"] == "releases-v2"
    assert contract["python"]["package_roots"] == _packaged_roots(pyproject)
    assert contract["python"]["module_files"] == [
        f"{name}.py" for name in pyproject["tool"]["setuptools"]["py-modules"]
    ]
    assert contract["python"]["include_venv_site_packages"] is True

    declared = set(contract["python"]["package_roots"]) | set(contract["python"]["module_files"])
    assert all((ROOT / item).exists() for item in declared)


def test_release_build_contract_requires_canonical_two_phase_python_build() -> None:
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    build = contract["build"]
    python_build = build["python"]

    assert contract["schema_version"] == 2
    assert python_build["selected_extras"] == CANONICAL_EXTRAS
    assert python_build["target"] == {
        "python": "3.12",
        "platform": "x86_64-linux-gnu",
    }
    assert python_build["dependency_sync"] == [
        "uv",
        "sync",
        "--locked",
        "--no-build",
        "--no-install-project",
        "--no-default-groups",
        "--extra",
        "messaging",
        "--extra",
        "honcho",
        "--extra",
        "tts-premium",
        "--extra",
        "mcp",
        "--extra",
        "firecrawl",
    ]
    assert python_build["root_build_requirements"] == [
        {
            "name": "setuptools",
            "version": "83.0.0",
            "source": "uv.lock",
            "wheel_only": True,
            "hash_required": True,
        }
    ]
    assert python_build["root_build_requirement_install"] == [
        "uv",
        "pip",
        "install",
        "--no-config",
        "--python",
        ".venv/bin/python",
        "--no-deps",
        "--only-binary",
        ":all:",
        "--require-hashes",
        "--requirements",
        ".aiwerk-root-build-requirements.txt",
    ]
    assert python_build["root_install"] == [
        "uv",
        "pip",
        "install",
        "--python",
        ".venv/bin/python",
        "--offline",
        "--no-deps",
        "--no-build-isolation",
        "--editable",
        ".",
    ]
    assert python_build["root_build_requirement_remove"] == [
        "uv",
        "pip",
        "uninstall",
        "--python",
        ".venv/bin/python",
        "setuptools",
    ]
    assert python_build["root_project"] == {
        "name": "hermes-agent",
        "version": "0.19.1",
        "source": ".",
        "source_kind": "exact-candidate-tree",
        "required_evidence": [
            "candidate_commit",
            "candidate_tree",
            "METADATA",
            "entry_points.txt",
            "direct_url.json",
            "RECORD",
            "installed_artifact_hashes",
        ],
    }

    encoded_python = json.dumps(python_build)
    assert "--all-extras" not in encoded_python
    assert "--all-groups" not in encoded_python
    assert "google" not in python_build["selected_extras"]
    assert "dingtalk" not in python_build["selected_extras"]
    assert "--no-build" not in python_build["root_install"]
    assert python_build["dependency_sync"].count("--no-build") == 1
    assert python_build["dependency_sync"].count("--no-install-project") == 1
    assert "--no-install-project" not in python_build["root_install"]
    assert "--offline" in python_build["root_install"]
    assert "--no-deps" in python_build["root_install"]
    assert "--editable" in python_build["root_install"]
    assert "--no-config" not in python_build["dependency_sync"]

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


def test_root_and_build_requirements_are_bound_to_project_and_lock() -> None:
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    python_build = contract["build"]["python"]

    project = pyproject["project"]
    root_contract = python_build["root_project"]
    assert (root_contract["name"], root_contract["version"]) == (
        project["name"],
        project["version"],
    )

    root_lock = [
        package
        for package in lock["package"]
        if package["name"] == project["name"]
        and package["version"] == project["version"]
        and package.get("source") == {"editable": "."}
    ]
    assert len(root_lock) == 1

    build_requirements = python_build["root_build_requirements"]
    assert len(build_requirements) == 1
    build_requirement = build_requirements[0]
    authority_requirement = (
        f"{build_requirement['name']}=={build_requirement['version']}"
    )
    assert pyproject["build-system"]["requires"] == [authority_requirement]
    assert build_requirement["source"] == "uv.lock"
    assert build_requirement["wheel_only"] is True
    assert build_requirement["hash_required"] is True

    locked_build_packages = [
        package
        for package in lock["package"]
        if package["name"] == build_requirement["name"]
        and package["version"] == build_requirement["version"]
    ]
    assert len(locked_build_packages) == 1
    wheels = locked_build_packages[0].get("wheels", [])
    assert wheels
    for wheel in wheels:
        assert wheel["url"].endswith(".whl")
        algorithm, digest = wheel["hash"].split(":", 1)
        assert algorithm == "sha256"
        assert len(digest) == 64
        assert all(character in "0123456789abcdef" for character in digest)


def _marker_applies(dependency: dict) -> bool:
    marker = dependency.get("marker")
    return marker is None or Marker(marker).evaluate(TARGET_MARKER_ENV)


def _canonical_lock_closure() -> list[tuple[str, str]]:
    """Derive the selected Linux/Python 3.12 closure from uv.lock only."""
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    packages = lock["package"]
    by_name: dict[str, list[dict]] = {}
    for package in packages:
        by_name.setdefault(package["name"], []).append(package)

    root = next(package for package in by_name["hermes-agent"] if package["source"] == {"editable": "."})
    pending = deque((dep["name"], tuple(dep.get("extra", ()))) for dep in root["dependencies"] if _marker_applies(dep))
    for extra in CANONICAL_EXTRAS:
        pending.extend(
            (dep["name"], tuple(dep.get("extra", ())))
            for dep in root["optional-dependencies"][extra]
            if _marker_applies(dep)
        )

    selected: dict[str, str] = {root["name"]: root["version"]}
    expanded: set[tuple[str, tuple[str, ...]]] = set()
    while pending:
        name, extras = pending.popleft()
        key = (name, extras)
        if key in expanded:
            continue
        expanded.add(key)
        candidates = [package for package in by_name[name] if _marker_applies(package)]
        assert len(candidates) == 1, f"expected one lock candidate for {name}, got {candidates}"
        package = candidates[0]
        previous = selected.setdefault(name, package["version"])
        assert previous == package["version"], f"multiple versions selected for {name}"
        pending.extend(
            (dep["name"], tuple(dep.get("extra", ())))
            for dep in package.get("dependencies", ())
            if _marker_applies(dep)
        )
        for extra in extras:
            pending.extend(
                (dep["name"], tuple(dep.get("extra", ())))
                for dep in package.get("optional-dependencies", {}).get(extra, ())
                if _marker_applies(dep)
            )
    return sorted(selected.items())


def _has_linux_python312_wheel(package: dict) -> bool:
    for wheel in package.get("wheels", ()):
        filename = wheel["url"].rsplit("/", 1)[-1]
        _, _, _, tags = parse_wheel_filename(filename)
        for tag in tags:
            python_ok = tag.interpreter in {"py3", "py2.py3", "cp312"} or tag.interpreter.startswith("py3")
            if tag.abi == "abi3" and tag.interpreter.startswith("cp3"):
                python_ok = int(tag.interpreter[2:]) <= 312
            abi_ok = tag.abi in {"none", "abi3", "cp312"}
            platform_ok = tag.platform == "any" or (
                "x86_64" in tag.platform
                and (tag.platform.startswith("manylinux") or tag.platform.startswith("linux"))
            )
            if python_ok and abi_ok and platform_ok:
                return True
    return False


def test_canonical_lock_closure_is_wheel_only_and_omits_unselected_google_stack() -> None:
    closure = _canonical_lock_closure()
    names = {name for name, _ in closure}
    assert "hermes-agent" in names
    assert {"mcp", "httpx-sse", "sse-starlette", "jsonschema", "jsonschema-specifications", "referencing", "rpds-py", "pydantic-settings"} <= names
    assert {"firecrawl-py", "nest-asyncio"} <= names
    assert {"google-auth", "pyasn1", "pyasn1-modules"}.isdisjoint(names)

    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    by_identity = {(package["name"], package["version"]): package for package in lock["package"]}
    missing_wheels = [
        f"{name}=={version}"
        for name, version in closure
        if name != "hermes-agent" and not _has_linux_python312_wheel(by_identity[(name, version)])
    ]
    assert not missing_wheels, f"external dependencies lack compatible wheels: {missing_wheels}"


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
