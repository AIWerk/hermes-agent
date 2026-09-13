#!/usr/bin/env python3
"""Fail fast when the canonical full-suite environment differs from CI."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import runpy
import shutil
import subprocess
import sys
import tomllib
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

CI_TEST_EXTRAS = tuple(
    runpy.run_path(str(Path(__file__).with_name("ci_test_profile.py")))["CI_TEST_EXTRAS"]
)


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _file_url_path(value: str | None) -> Path | None:
    if not value:
        return None
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme != "file":
        return None
    path = urllib.request.url2pathname(parsed.path)
    if parsed.netloc and parsed.netloc.lower() != "localhost":
        path = f"//{parsed.netloc}/{path.lstrip('/')}"
    return Path(path).resolve()


def compare_environment(
    *,
    expected: dict[str, str],
    actual: dict[str, str],
    repo_root: Path,
    environment_prefix: Path,
    root_project_name: str,
    root_direct_url: str | None,
    root_editable: bool,
    require_project_local: bool,
) -> dict[str, Any]:
    expected = {canonicalize_name(k): v for k, v in expected.items()}
    actual = {canonicalize_name(k): v for k, v in actual.items()}
    issues: list[dict[str, Any]] = []
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    mismatches = {
        name: {"expected": expected[name], "actual": actual[name]}
        for name in sorted(set(expected) & set(actual))
        if expected[name] != actual[name]
    }
    if missing:
        issues.append({"code": "missing", "packages": missing})
    if extra:
        issues.append({"code": "extra", "packages": extra})
    if mismatches:
        issues.append({"code": "version_mismatch", "packages": mismatches})
    root_name = canonicalize_name(root_project_name)
    if root_name in expected:
        source = _file_url_path(root_direct_url)
        if source != repo_root.resolve():
            issues.append(
                {
                    "code": "root_source",
                    "expected": repo_root.resolve().as_uri(),
                    "actual": root_direct_url,
                }
            )
        if root_editable is not True:
            issues.append({"code": "root_not_editable", "actual": root_editable})
    if require_project_local and not _inside(environment_prefix, repo_root):
        issues.append(
            {
                "code": "venv_geometry",
                "expected_parent": str(repo_root.resolve()),
                "actual": str(environment_prefix.resolve()),
            }
        )
    return {"passed": not issues, "issues": issues}


def parse_export(export_text: str, project: dict[str, Any]) -> dict[str, str]:
    environment = default_environment()
    expected: dict[str, str] = {}
    editable_root = False
    for line in export_text.splitlines():
        if not line or line.startswith((" ", "#")):
            continue
        if line == "-e .":
            editable_root = True
            continue
        requirement = Requirement(line)
        if requirement.marker and not requirement.marker.evaluate(environment):
            continue
        pins = list(requirement.specifier)
        if requirement.url or len(pins) != 1 or pins[0].operator != "==":
            raise RuntimeError(f"CI export contains a non-exact requirement: {line}")
        expected[canonicalize_name(requirement.name)] = pins[0].version
    if not editable_root:
        raise RuntimeError("CI export omitted editable root project")
    expected[canonicalize_name(project["name"])] = str(project["version"])
    return expected


def installed_inventory(root_project_name: str) -> tuple[dict[str, str], str | None, bool]:
    actual: dict[str, str] = {}
    duplicates: set[str] = set()
    root_url: str | None = None
    root_editable = False
    root_name = canonicalize_name(root_project_name)
    for distribution in importlib.metadata.distributions():
        raw_name = distribution.metadata.get("Name")
        if not raw_name:
            continue
        name = canonicalize_name(raw_name)
        if name in actual:
            duplicates.add(name)
        actual[name] = distribution.version
        if name == root_name:
            raw = distribution.read_text("direct_url.json")  # windows-footgun: ok — metadata API decodes UTF-8
            if raw:
                value = json.loads(raw)
                if isinstance(value, dict):
                    root_url = value.get("url")
                    directory = value.get("dir_info")
                    root_editable = isinstance(directory, dict) and directory.get("editable") is True
    if duplicates:
        raise RuntimeError(f"multiple installed versions: {', '.join(sorted(duplicates))}")
    return actual, root_url, root_editable


def _git_executable() -> str:
    git = shutil.which("git")
    if not git:
        raise RuntimeError("git is unavailable; cannot bind the candidate tree")
    return git


def _git(repo_root: Path, *args: str) -> str:
    result = subprocess.run(
        [_git_executable(), "-C", str(repo_root), *args],
        text=True,
        capture_output=True,
        timeout=30,
    )
    if result.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def check_ci_test_environment(repo_root: Path, python_executable: Path) -> dict[str, Any]:
    repo_root = repo_root.resolve()
    python_executable = python_executable.resolve()
    if python_executable != Path(sys.executable).resolve():
        raise RuntimeError(
            "selected interpreter does not match the interpreter running the preflight: "
            f"selected={python_executable} running={Path(sys.executable).resolve()}"
        )
    unstaged = subprocess.run(
        [_git_executable(), "-C", str(repo_root), "diff", "--quiet"],
        timeout=30,
    )
    if unstaged.returncode != 0:
        raise RuntimeError("candidate has unstaged tracked changes")
    untracked = _git(repo_root, "ls-files", "--others", "--exclude-standard")
    if untracked:
        raise RuntimeError(f"candidate has untracked files: {untracked.splitlines()}")
    project = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    lock_bytes = (repo_root / "uv.lock").read_bytes()
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("uv is unavailable; cannot validate the locked CI test profile")
    command = [
        uv,
        "export",
        "--locked",
        "--python",
        str(python_executable),
    ]
    for extra in CI_TEST_EXTRAS:
        command.extend(("--extra", extra))
    command.extend(("--no-hashes", "--no-header"))
    exported = subprocess.run(
        command,
        cwd=repo_root,
        text=True,
        capture_output=True,
        timeout=60,
    )
    if exported.returncode:
        raise RuntimeError(f"locked CI profile export failed: {exported.stderr.strip()}")
    expected = parse_export(exported.stdout, project)
    actual, root_url, root_editable = installed_inventory(str(project["name"]))
    report = compare_environment(
        expected=expected,
        actual=actual,
        repo_root=repo_root,
        environment_prefix=Path(sys.prefix),
        root_project_name=str(project["name"]),
        root_direct_url=root_url,
        root_editable=root_editable,
        require_project_local=True,
    )
    report.update(
        {
            "commit": _git(repo_root, "rev-parse", "HEAD"),
            "tree": _git(repo_root, "write-tree"),
            "lock_sha256": hashlib.sha256(lock_bytes).hexdigest(),
            "python": str(python_executable),
            "environment_prefix": str(Path(sys.prefix).resolve()),
            "expected_count": len(expected),
            "actual_count": len(actual),
            "extras": list(CI_TEST_EXTRAS),
        }
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--python", required=True, type=Path)
    args = parser.parse_args()
    try:
        report = check_ci_test_environment(args.repo_root, args.python)
    except Exception as exc:
        print(f"error: canonical test environment preflight failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if not report["passed"]:
        print("error: canonical test environment does not match the locked CI profile", file=sys.stderr)
        print(json.dumps(report, sort_keys=True), file=sys.stderr)
        return 2
    print(
        "▶ environment preflight: PASS "
        f"tree={report['tree']} lock={report['lock_sha256']} "
        f"packages={report['actual_count']} venv={report['environment_prefix']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
