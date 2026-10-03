from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest

from scripts.aiwerk_update.contract import ContractError
from scripts.aiwerk_update.source import (
    OverlayManifest,
    materialize_transition_commit,
    measure_transition_changes,
    prepare_upstream_first_candidate,
)


def _git(repo: Path, *args: str, check: bool = True) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Updater Test",
        "GIT_AUTHOR_EMAIL": "noreply@github.com",
        "GIT_COMMITTER_NAME": "Updater Test",
        "GIT_COMMITTER_EMAIL": "noreply@github.com",
    }
    result = subprocess.run(
        ["git", *args], cwd=repo, env=env, text=True, capture_output=True, check=False
    )
    if check and result.returncode != 0:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


def _history(
    tmp_path: Path, *, conflict: bool = False, baseline: bool = False
) -> tuple[Path, str, str, str]:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-q")
    _git(source, "checkout", "-q", "-b", "main")
    (source / "common.txt").write_text("base\n")
    if baseline:
        (source / ".ci/content-loss").mkdir(parents=True)
        (source / ".ci/content-loss/baseline.json").write_text(
            json.dumps(
                {
                    "accepted_upstream_commit": "1" * 40,
                    "previous_accepted_upstream_commit": "0" * 40,
                }
            )
            + "\n"
        )
    _git(source, "add", "-A")
    _git(source, "commit", "-q", "-m", "common")
    common = _git(source, "rev-parse", "HEAD")
    _git(source, "checkout", "-q", "-b", "aiwerk")
    if conflict:
        (source / "common.txt").write_text("aiwerk\n")
    else:
        (source / "aiwerk.txt").write_text("capability\n")
    _git(source, "add", "-A")
    _git(source, "commit", "-q", "-m", "AIWerk capability")
    overlay = _git(source, "rev-parse", "HEAD")
    _git(source, "checkout", "-q", "-b", "upstream", common)
    if conflict:
        (source / "common.txt").write_text("upstream\n")
    else:
        (source / "upstream.txt").write_text("release\n")
    _git(source, "add", "-A")
    _git(source, "commit", "-q", "-m", "upstream release")
    target = _git(source, "rev-parse", "HEAD")
    return source, common, overlay, target


def test_overlay_manifest_is_strict_and_content_bound() -> None:
    raw = {
        "schema_version": 1,
        "repository": "AIWerk/hermes-agent",
        "upstream_commit": "a" * 40,
        "overlay_commits": ["b" * 40],
    }

    manifest = OverlayManifest.from_dict(raw)

    assert manifest.upstream_commit == "a" * 40
    assert manifest.overlay_commits == ("b" * 40,)
    assert len(manifest.digest) == 64
    with pytest.raises(ContractError, match="unknown fields"):
        OverlayManifest.from_dict({**raw, "pr_number": 166})


def test_prepare_candidate_starts_from_upstream_and_replays_overlay(tmp_path: Path) -> None:
    source, _common, overlay, target = _history(tmp_path)
    workspace = tmp_path / "candidate"
    manifest = OverlayManifest.from_dict(
        {
            "schema_version": 1,
            "repository": "AIWerk/hermes-agent",
            "upstream_commit": target,
            "overlay_commits": [overlay],
        }
    )

    result = prepare_upstream_first_candidate(source, workspace, manifest)

    assert result["status"] == "CANDIDATE_READY"
    assert result["upstream_commit"] == target
    assert result["overlay_commits"] == [overlay]
    assert _git(workspace, "show", "HEAD:upstream.txt") == "release"
    assert _git(workspace, "show", "HEAD:aiwerk.txt") == "capability"
    assert _git(workspace, "rev-list", "--parents", "-n", "1", "HEAD").split()[1] == target
    assert _git(source, "status", "--porcelain=v1", "--untracked-files=all") == ""


def test_prepare_candidate_preserves_conflict_for_updater_resolution(tmp_path: Path) -> None:
    source, _common, overlay, target = _history(tmp_path, conflict=True)
    workspace = tmp_path / "candidate"
    manifest = OverlayManifest.from_dict(
        {
            "schema_version": 1,
            "repository": "AIWerk/hermes-agent",
            "upstream_commit": target,
            "overlay_commits": [overlay],
        }
    )

    result = prepare_upstream_first_candidate(source, workspace, manifest)

    assert result["status"] == "CONFLICT"
    assert result["conflicting_paths"] == ["common.txt"]
    assert (workspace / ".git" / "CHERRY_PICK_HEAD").is_file()


def test_prepare_candidate_advances_accepted_upstream_control(tmp_path: Path) -> None:
    source, _common, overlay, target = _history(tmp_path, baseline=True)
    workspace = tmp_path / "candidate"
    manifest = OverlayManifest.from_dict(
        {
            "schema_version": 1,
            "repository": "AIWerk/hermes-agent",
            "upstream_commit": target,
            "overlay_commits": [overlay],
        }
    )

    result = prepare_upstream_first_candidate(source, workspace, manifest)
    baseline = json.loads(
        _git(workspace, "show", f"{result['candidate_commit']}:.ci/content-loss/baseline.json")
    )

    assert baseline["accepted_upstream_commit"] == target
    assert baseline["previous_accepted_upstream_commit"] == "1" * 40


def test_measure_transition_changes_returns_exact_old_and_new_blobs(tmp_path: Path) -> None:
    source, common, overlay, target = _history(tmp_path)
    workspace = tmp_path / "candidate"
    manifest = OverlayManifest.from_dict(
        {
            "schema_version": 1,
            "repository": "AIWerk/hermes-agent",
            "upstream_commit": target,
            "overlay_commits": [overlay],
        }
    )
    result = prepare_upstream_first_candidate(source, workspace, manifest)

    changes = measure_transition_changes(workspace, common, result["candidate_commit"])

    assert [change.path for change in changes] == ["aiwerk.txt", "upstream.txt"]
    assert all(change.old_blob is None and change.new_blob for change in changes)


def test_materialize_transition_commit_applies_only_named_paths(tmp_path: Path) -> None:
    source, common, overlay, target = _history(tmp_path)
    full = tmp_path / "full"
    manifest = OverlayManifest.from_dict(
        {
            "schema_version": 1,
            "repository": "AIWerk/hermes-agent",
            "upstream_commit": target,
            "overlay_commits": [overlay],
        }
    )
    candidate = prepare_upstream_first_candidate(source, full, manifest)
    stage = tmp_path / "stage"

    result = materialize_transition_commit(
        full,
        stage,
        base_commit=common,
        candidate_commit=candidate["candidate_commit"],
        paths=["aiwerk.txt"],
        message="control stage",
    )

    assert result["changed_paths"] == ["aiwerk.txt"]
    assert _git(stage, "show", "HEAD:aiwerk.txt") == "capability"
    assert _git(stage, "cat-file", "-e", "HEAD:upstream.txt", check=False) == ""
    assert _git(stage, "show", "-s", "--format=%P", "HEAD") == common
