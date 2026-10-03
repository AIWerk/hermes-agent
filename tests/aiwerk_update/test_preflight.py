from __future__ import annotations

import json
from pathlib import Path
import subprocess

from scripts.aiwerk_update.preflight import Probe, collect_preflight, repository_preflight_probes


def test_preflight_collects_all_failures_in_one_run(tmp_path: Path) -> None:
    calls: list[tuple[str, ...]] = []

    def runner(argv: tuple[str, ...], cwd: Path) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        rc = 1 if argv[0] in {"missing-tool", "bad-gate"} else 0
        return subprocess.CompletedProcess(argv, rc, stdout=f"out:{argv[0]}", stderr=f"err:{argv[0]}")

    receipt = collect_preflight(
        probes=[
            Probe("tool", ("missing-tool", "--version")),
            Probe("identity", ("git", "rev-parse", "HEAD")),
            Probe("owner-gate", ("bad-gate",)),
        ],
        cwd=tmp_path,
        output=tmp_path / "preflight.json",
        runner=runner,
    )

    assert calls == [
        ("missing-tool", "--version"),
        ("git", "rev-parse", "HEAD"),
        ("bad-gate",),
    ]
    assert receipt["verdict"] == "FAIL"
    assert [failure["name"] for failure in receipt["failures"]] == ["tool", "owner-gate"]
    assert not (tmp_path / "execution.claim").exists()


def test_preflight_is_repeatable_and_receipt_is_canonical(tmp_path: Path) -> None:
    def runner(argv: tuple[str, ...], cwd: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, stdout="ok\n", stderr="")

    output = tmp_path / "preflight.json"
    first = collect_preflight(
        probes=[Probe("identity", ("git", "rev-parse", "HEAD"))],
        cwd=tmp_path,
        output=output,
        runner=runner,
    )
    first_bytes = output.read_bytes()
    second = collect_preflight(
        probes=[Probe("identity", ("git", "rev-parse", "HEAD"))],
        cwd=tmp_path,
        output=output,
        runner=runner,
    )

    assert first == second
    assert output.read_bytes() == first_bytes
    assert json.loads(first_bytes)["verdict"] == "PASS"


def test_preflight_records_missing_executable_and_continues(tmp_path: Path) -> None:
    calls: list[str] = []

    def runner(argv: tuple[str, ...], cwd: Path) -> subprocess.CompletedProcess[str]:
        calls.append(argv[0])
        if argv[0] == "missing":
            raise FileNotFoundError("missing")
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    receipt = collect_preflight(
        probes=[Probe("missing-tool", ("missing",)), Probe("later", ("later",))],
        cwd=tmp_path,
        output=tmp_path / "preflight.json",
        runner=runner,
    )

    assert calls == ["missing", "later"]
    assert receipt["verdict"] == "FAIL"
    assert receipt["failures"][0]["error_type"] == "FileNotFoundError"


def test_repository_preflight_includes_ci_failure_classes_seen_before_publication(
    tmp_path: Path,
) -> None:
    probes = repository_preflight_probes(
        base_commit="a" * 40,
        candidate_commit="b" * 40,
        upstream_commit="c" * 40,
        output_dir=tmp_path,
        include_canonical_suite=True,
        include_js=True,
    )

    by_name = {probe.name: probe.argv for probe in probes}
    assert {
        "diff-check",
        "contributor-attribution",
        "windows-footguns",
        "compat-pointers",
        "content-loss",
        "canonical-python-suite",
        "js-workspace-checks",
    } <= set(by_name)
    assert by_name["content-loss"][:3] == (
        "python3",
        "scripts/ci/content_loss_guard.py",
        "check-transition",
    )
    assert by_name["canonical-python-suite"] == ("scripts/run_tests.sh",)
