"""Repeatable collect-all updater preflight."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import subprocess
from typing import Callable, Sequence

from .contract import _atomic_write, canonical_bytes


@dataclass(frozen=True, slots=True)
class Probe:
    name: str
    argv: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.name or not self.argv or not all(isinstance(item, str) and item for item in self.argv):
            raise ValueError("probe requires a name and non-empty argv")


Runner = Callable[[tuple[str, ...], Path], subprocess.CompletedProcess[str]]


def repository_preflight_probes(
    *,
    base_commit: str,
    candidate_commit: str,
    upstream_commit: str,
    output_dir: Path,
    include_canonical_suite: bool,
    include_js: bool,
    include_content_loss: bool = True,
) -> tuple[Probe, ...]:
    output_dir.mkdir(parents=True, exist_ok=True)
    probes = [
        Probe("diff-check", ("git", "diff", "--check", f"{base_commit}...{candidate_commit}")),
        Probe("contributor-attribution", ("python3", "scripts/audit_pr_attribution.py")),
        Probe("windows-footguns", ("python3", "scripts/check-windows-footguns.py", "--all")),
        Probe("compat-pointers", ("python3", "scripts/check_compat_pointers.py")),
    ]
    if include_content_loss:
        probes.append(
            Probe(
                "content-loss",
                (
                    "python3",
                    "scripts/ci/content_loss_guard.py",
                    "check-transition",
                    "--repo",
                    ".",
                    "--active",
                    base_commit,
                    "--target",
                    candidate_commit,
                    "--upstream",
                    upstream_commit,
                    "--json-out",
                    str(output_dir / "content-loss-report.json"),
                    "--markdown-out",
                    str(output_dir / "content-loss-report.md"),
                ),
            )
        )
    if include_canonical_suite:
        probes.append(Probe("canonical-python-suite", ("scripts/run_tests.sh",)))
    if include_js:
        probes.append(
            Probe(
                "js-workspace-checks",
                ("node", ".github/scripts/run-workspace-checks.mjs", "--concurrency", "2"),
            )
        )
    return tuple(probes)


def _default_runner(argv: tuple[str, ...], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def collect_preflight(
    *,
    probes: Sequence[Probe],
    cwd: Path,
    output: Path,
    runner: Runner = _default_runner,
) -> dict:
    if not cwd.is_dir():
        raise ValueError("preflight cwd must be an existing directory")
    results: list[dict] = []
    failures: list[dict] = []
    seen: set[str] = set()
    for probe in probes:
        if probe.name in seen:
            raise ValueError(f"duplicate preflight probe: {probe.name}")
        seen.add(probe.name)
        try:
            completed = runner(probe.argv, cwd)
        except OSError as exc:
            result = {
                "name": probe.name,
                "argv": list(probe.argv),
                "returncode": None,
                "stdout_sha256": _digest(""),
                "stderr_sha256": _digest(str(exc)),
                "error_type": type(exc).__name__,
            }
            results.append(result)
            failures.append(result)
            continue
        result = {
            "name": probe.name,
            "argv": list(probe.argv),
            "returncode": completed.returncode,
            "stdout_sha256": _digest(completed.stdout),
            "stderr_sha256": _digest(completed.stderr),
        }
        results.append(result)
        if completed.returncode != 0:
            failures.append(result)
    receipt = {
        "schema_version": 1,
        "kind": "AIWERK_UPDATE_PREFLIGHT",
        "verdict": "PASS" if not failures else "FAIL",
        "results": results,
        "failures": failures,
        "mutation_claim_created": False,
    }
    _atomic_write(output, canonical_bytes(receipt))
    return receipt


class RepositoryQualifier:
    """Run the expensive exact transition gates after control authority lands."""

    def qualify(
        self,
        *,
        repo: Path,
        base_commit: str,
        candidate_commit: str,
        upstream_commit: str,
        output_dir: Path,
    ) -> dict:
        tree = subprocess.run(
            ["git", "rev-parse", f"{candidate_commit}^{{tree}}"],
            cwd=repo,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if tree.returncode != 0:
            raise ValueError("qualification candidate tree is unavailable")
        receipt = collect_preflight(
            probes=repository_preflight_probes(
                base_commit=base_commit,
                candidate_commit=candidate_commit,
                upstream_commit=upstream_commit,
                output_dir=output_dir,
                include_canonical_suite=True,
                include_js=True,
                include_content_loss=True,
            ),
            cwd=repo,
            output=output_dir / "qualification-probes.json",
        )
        return {
            **receipt,
            "kind": "AIWERK_UPDATE_QUALIFICATION",
            "qualified_base_commit": base_commit,
            "qualified_commit": candidate_commit,
            "qualified_tree": tree.stdout.strip(),
            "upstream_commit": upstream_commit,
        }
