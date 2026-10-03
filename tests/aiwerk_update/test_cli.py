from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

from scripts.aiwerk_update.cli import build_parser, main
from scripts.aiwerk_update.source import capture_git_authority
from scripts.aiwerk_update.state import RunStore


def _git(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Updater Test",
        "GIT_AUTHOR_EMAIL": "noreply@github.com",
        "GIT_COMMITTER_NAME": "Updater Test",
        "GIT_COMMITTER_EMAIL": "noreply@github.com",
    }
    result = subprocess.run(
        ["git", *args], cwd=repo, env=env, text=True, capture_output=True, check=True
    )
    return result.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "remote", "add", "origin", "https://github.com/AIWerk/hermes-agent.git")
    _git(repo, "checkout", "-q", "-b", "main")
    (repo / "base.txt").write_text("base\n")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "branch", "origin-main")
    _git(repo, "checkout", "-q", "-b", "upstream-main")
    (repo / "upstream.txt").write_text("upstream\n")
    _git(repo, "add", "upstream.txt")
    _git(repo, "commit", "-q", "-m", "upstream")
    return repo


def test_capture_git_authority_binds_commit_tree_and_merge_base(tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    authority = capture_git_authority(repo, "origin-main", "upstream-main")

    assert authority["base_commit"] == _git(repo, "rev-parse", "origin-main")
    assert authority["target_commit"] == _git(repo, "rev-parse", "upstream-main")
    assert authority["base_tree"] == _git(repo, "rev-parse", "origin-main^{tree}")
    assert authority["target_tree"] == _git(repo, "rev-parse", "upstream-main^{tree}")
    assert authority["merge_base"] == _git(repo, "merge-base", "origin-main", "upstream-main")


def test_cli_check_is_read_only_and_json(tmp_path: Path, capsys) -> None:
    repo = _repo(tmp_path)
    before = _git(repo, "status", "--porcelain=v1", "--untracked-files=all")

    rc = main(
        [
            "check",
            "--repo",
            str(repo),
            "--base-ref",
            "origin-main",
            "--target-ref",
            "upstream-main",
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["verdict"] == "OBSERVATION_ONLY"
    assert payload["ready_for_preflight"] is True
    assert payload["authority"]["repository"] == "AIWerk/hermes-agent"
    assert payload["authority"]["base_commit"] == _git(repo, "rev-parse", "origin-main")
    assert _git(repo, "status", "--porcelain=v1", "--untracked-files=all") == before


def test_cli_rejects_non_aiwerk_source_repository(tmp_path: Path, capsys) -> None:
    repo = _repo(tmp_path)
    _git(repo, "remote", "set-url", "origin", "https://github.com/NousResearch/hermes-agent.git")

    rc = main(
        [
            "check",
            "--repo",
            str(repo),
            "--base-ref",
            "origin-main",
            "--target-ref",
            "upstream-main",
            "--json",
        ]
    )

    assert rc == 2
    assert "AIWerk/hermes-agent" in capsys.readouterr().err


def test_cli_run_dry_preflight_creates_resumable_run_without_claim(tmp_path: Path, capsys) -> None:
    repo = _repo(tmp_path)
    refreshes = tmp_path / "refreshes"

    rc = main(
        [
            "run",
            "--repo",
            str(repo),
            "--base-ref",
            "origin-main",
            "--target-ref",
            "upstream-main",
            "--refresh-root",
            str(refreshes),
            "--through",
            "local-handoff",
            "--authorize",
            "source-publication",
            "--preflight-profile",
            "minimal",
            "--preflight-only",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    run_root = Path(output["run_root"])
    assert rc == 0
    assert output["state"] == "PREFLIGHT_PASS"
    assert (run_root / "preflight.json").is_file()
    assert not (run_root / "execution.claim").exists()
    status_rc = main(["status", "--run-root", str(run_root), "--json"])
    status = json.loads(capsys.readouterr().out)
    assert status_rc == 0
    assert status["phase"] == "PREFLIGHT_PASS"
    resume_rc = main(["resume", "--run-root", str(run_root)])
    assert resume_rc == 2
    assert "not execution-bound" in capsys.readouterr().err


def test_cli_run_materializes_upstream_first_overlay_before_preflight(
    tmp_path: Path, capsys
) -> None:
    repo = _repo(tmp_path)
    target = _git(repo, "rev-parse", "upstream-main")
    _git(repo, "checkout", "-q", "origin-main")
    _git(repo, "checkout", "-q", "-b", "aiwerk-overlay")
    (repo / "aiwerk.txt").write_text("capability\n")
    _git(repo, "add", "aiwerk.txt")
    _git(repo, "commit", "-q", "-m", "AIWerk capability")
    overlay = _git(repo, "rev-parse", "HEAD")
    manifest = tmp_path / "overlay.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "repository": "AIWerk/hermes-agent",
                "upstream_commit": target,
                "overlay_commits": [overlay],
            }
        )
    )

    rc = main(
        [
            "run",
            "--repo",
            str(repo),
            "--base-ref",
            "origin-main",
            "--target-ref",
            "upstream-main",
            "--refresh-root",
            str(tmp_path / "refreshes"),
            "--through",
            "local-handoff",
            "--authorize",
            "source-publication",
            "--overlay-manifest",
            str(manifest),
            "--preflight-profile",
            "minimal",
            "--preflight-only",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    run_root = Path(output["run_root"])
    assert rc == 0
    assert (run_root / "candidate" / "upstream.txt").read_text() == "upstream\n"
    assert (run_root / "candidate" / "aiwerk.txt").read_text() == "capability\n"
    assert json.loads((run_root / "candidate.json").read_text())["status"] == "CANDIDATE_READY"


def test_cli_exposes_resume_and_verifies_terminal_evidence(tmp_path: Path, capsys) -> None:
    store = RunStore.create(
        tmp_path / "refreshes",
        run_id="done-run",
        request={"through": "local-handoff", "source_publication": True},
        authority={"base_commit": "a" * 40, "target_commit": "b" * 40},
    )
    store.record_preflight({"verdict": "PASS", "failures": []})
    store.begin_execution()
    for stage in ("control", "product", "publication", "artifact"):
        store.complete_stage(stage)
    store.finish_handoff({"status": "HANDOFF_READY", "activation": "NOT_RUN"})

    rc = main(["evidence", "--run-root", str(store.root)])
    evidence = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert evidence["phase"] == "HANDOFF_READY"
    assert evidence["manifest_present"] is True
    assert "final.json" in evidence["verified_files"]
    parsed = build_parser().parse_args(["resume", "--run-root", str(store.root)])
    assert parsed.command == "resume"

    (store.root / "manifest.sha256").write_text("")
    failed = main(["evidence", "--run-root", str(store.root)])
    assert failed == 2
    assert "manifest" in capsys.readouterr().err
