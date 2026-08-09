"""Invariant checks for release-bound Python systemd services."""

from __future__ import annotations

import ast
import re
import runpy
import shlex
import subprocess
from pathlib import Path

import pytest

from hermes_cli import gateway as gateway_cli


REPO_ROOT = Path(__file__).resolve().parents[2]
BYTECODE_ENVIRONMENT = 'Environment="PYTHONDONTWRITEBYTECODE=1"'


def _looks_like_python_or_hermes_exec_start(line: str) -> bool:
    stripped = line.strip()
    if not stripped.startswith("ExecStart="):
        return False
    try:
        tokens = shlex.split(stripped.removeprefix("ExecStart="), posix=True)
    except ValueError:
        return False
    if not tokens:
        return False

    executable = Path(tokens[0].lstrip("-+!:@")).name.lower()
    arguments = tokens[1:]
    if executable == "env":
        while arguments and (arguments[0].startswith("-") or "=" in arguments[0]):
            arguments = arguments[1:]
        if not arguments:
            return False
        executable = Path(arguments[0]).name.lower()
        arguments = arguments[1:]

    if executable == "hermes" or executable.endswith(".py"):
        return True
    if re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", executable):
        return True
    return executable == "uv" and len(arguments) >= 2 and arguments[0] == "run" and (
        arguments[1].endswith(".py") or "-m" in arguments[1:]
    )


def _tracked_paths(pathspec: str) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--", pathspec],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    )
    return [Path(raw.decode()) for raw in result.stdout.split(b"\0") if raw]


def _release_bound_service_stanzas() -> list[tuple[Path, str]]:
    """Discover tracked static service artifacts without a unit-name list."""
    discovered: list[tuple[Path, str]] = []
    for relative_path in _tracked_paths("*.service"):
        source = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
        for stanza in re.findall(
            r"(?ms)^\s*\[Service\]\s*$\n?(.*?)(?=^\s*\[[^]]+\]\s*$|\Z)", source
        ):
            exec_starts = [
                line
                for line in stanza.splitlines()
                if line.strip().startswith("ExecStart=")
            ]
            if any(_looks_like_python_or_hermes_exec_start(line) for line in exec_starts):
                discovered.append((relative_path, stanza))
    return discovered


def _legacy_systemd_generator_paths() -> list[Path]:
    """Structurally discover tracked scripts that define a unit renderer."""
    discovered = []
    for relative_path in _tracked_paths("scripts/*"):
        path = REPO_ROOT / relative_path
        if not path.is_file():
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        if any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "generate_systemd_unit"
            for node in tree.body
        ):
            discovered.append(relative_path)
    return discovered


@pytest.mark.parametrize("system", [False, True], ids=["user", "system"])
def test_generated_gateway_units_disable_bytecode_writes(system, tmp_path, monkeypatch):
    if system:
        target_home = tmp_path / "service"
        target_home.mkdir()
        monkeypatch.setattr(
            gateway_cli,
            "_system_service_identity",
            lambda _user: ("service", "service", str(target_home)),
        )
        monkeypatch.setattr(
            gateway_cli,
            "_hermes_home_for_target_user",
            lambda home: str(Path(home) / ".hermes"),
        )

    unit = gateway_cli.generate_systemd_unit(
        system=system, run_as_user="service" if system else None
    )

    assert unit.splitlines().count(BYTECODE_ENVIRONMENT) == 1


def _has_bytecode_environment(stanza: str) -> bool:
    return any(line.strip() == BYTECODE_ENVIRONMENT for line in stanza.splitlines())


@pytest.mark.parametrize(
    "line",
    [
        f"# {BYTECODE_ENVIRONMENT}",
        f"; {BYTECODE_ENVIRONMENT}",
        f"NotAn{BYTECODE_ENVIRONMENT}",
    ],
)
def test_bytecode_environment_requires_exact_stripped_line(line):
    assert not _has_bytecode_environment(line)


def test_direct_python_script_exec_start_is_discovered():
    assert _looks_like_python_or_hermes_exec_start("ExecStart=/opt/hermes/worker.py")


@pytest.mark.parametrize(
    "line",
    [
        "# ExecStart=/usr/bin/python -m package.worker",
        "; ExecStart=/usr/bin/python -m package.worker",
        "NotExecStart=/usr/bin/python -m package.worker",
        "Description=bogus ExecStart=/usr/bin/python -m package.worker",
    ],
)
def test_exec_start_requires_active_exact_directive(line):
    assert not _looks_like_python_or_hermes_exec_start(line)


def test_non_python_command_with_hermes_argument_is_not_discovered():
    assert not _looks_like_python_or_hermes_exec_start(
        "ExecStart=/usr/bin/not-python --label hermes"
    )


@pytest.mark.parametrize(
    "line",
    [
        "ExecStart=/usr/bin/env python3 -m package.worker",
        "ExecStart=/usr/bin/env hermes gateway run",
        "ExecStart=/usr/bin/env /opt/release/worker.py",
        "ExecStart=/usr/bin/uv run -m package.worker",
    ],
)
def test_supported_python_launch_forms_are_discovered(line):
    assert _looks_like_python_or_hermes_exec_start(line)


def test_static_inventory_ignores_untracked_workspace_services(tmp_path, monkeypatch):
    tracked = tmp_path / "tracked.service"
    tracked.write_text(
        "[Service]\nExecStart=/usr/bin/python -m package.worker\n"
        f"{BYTECODE_ENVIRONMENT}\n[Install]\n",
        encoding="utf-8",
    )
    untracked = tmp_path / "generated.service"
    untracked.write_text(
        "[Service]\nExecStart=/usr/bin/python -m package.worker\n[Install]\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", tracked.name], cwd=tmp_path, check=True)
    monkeypatch.setattr(
        "tests.hermes_cli.test_release_bound_systemd_units.REPO_ROOT", tmp_path
    )

    assert [path for path, _stanza in _release_bound_service_stanzas()] == [
        Path(tracked.name)
    ]


def test_discovered_legacy_systemd_generators_disable_bytecode_writes():
    generator_paths = _legacy_systemd_generator_paths()

    assert generator_paths, "expected to structurally discover a legacy systemd renderer"
    for relative_path in generator_paths:
        namespace = runpy.run_path(str(REPO_ROOT / relative_path))
        unit = namespace["generate_systemd_unit"]()
        assert unit.splitlines().count(BYTECODE_ENVIRONMENT) == 1, relative_path


def test_all_release_bound_python_systemd_services_disable_bytecode_writes():
    discovered = _release_bound_service_stanzas()

    assert discovered, "expected to discover at least one release-bound systemd service"
    missing = [str(path) for path, stanza in discovered if not _has_bytecode_environment(stanza)]
    assert not missing, "missing PYTHONDONTWRITEBYTECODE=1 in: " + ", ".join(missing)
