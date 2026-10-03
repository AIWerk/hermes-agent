from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest

from scripts.aiwerk_update.release import (
    ReleaseError,
    inventory_release,
    verify_release,
    write_artifact_handoff,
    write_local_handoff,
)


def _git(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Release Test",
        "GIT_AUTHOR_EMAIL": "noreply@github.com",
        "GIT_COMMITTER_NAME": "Release Test",
        "GIT_COMMITTER_EMAIL": "noreply@github.com",
    }
    result = subprocess.run(
        ["git", *args], cwd=repo, env=env, text=True, capture_output=True, check=True
    )
    return result.stdout.strip()


def _source(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "source.py").write_text("VALUE = 1\n")
    _git(repo, "add", "source.py")
    _git(repo, "commit", "-q", "-m", "source")
    return repo, _git(repo, "rev-parse", "HEAD"), _git(repo, "rev-parse", "HEAD^{tree}")


def _artifact(tmp_path: Path) -> tuple[Path, Path, str, str]:
    source_repo, commit, tree = _source(tmp_path)
    root = tmp_path / "release"
    root.mkdir()
    (root / "runtime.txt").write_text("runtime\n")
    (root / "runtime.txt").chmod(0o644)
    inventory = inventory_release(root)
    manifest = {
        "schema_version": 1,
        "source_commit": commit,
        "source_tree": tree,
        "inventory": inventory,
    }
    (root / "artifact-manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
    )
    return root, source_repo, commit, tree


def test_inventory_and_verify_bind_exact_files_commit_and_tree(tmp_path: Path) -> None:
    root, source_repo, commit, tree = _artifact(tmp_path)

    receipt = verify_release(
        root, source_repo=source_repo, expected_commit=commit, expected_tree=tree
    )

    assert receipt["verdict"] == "PASS"
    assert receipt["source_commit"] == commit
    assert receipt["source_tree"] == tree
    assert receipt["inventory"]["files"] == [
        {
            "path": "runtime.txt",
            "mode": "0644",
            "size": 8,
            "sha256": hashlib.sha256(b"runtime\n").hexdigest(),
        }
    ]


def test_verify_rejects_tampered_release(tmp_path: Path) -> None:
    root, source_repo, commit, tree = _artifact(tmp_path)
    (root / "runtime.txt").write_text("tampered\n")

    with pytest.raises(ReleaseError, match="inventory mismatch"):
        verify_release(
            root, source_repo=source_repo, expected_commit=commit, expected_tree=tree
        )


def test_verify_rejects_unproven_git_identity(tmp_path: Path) -> None:
    root, source_repo, commit, tree = _artifact(tmp_path)

    with pytest.raises(ReleaseError, match="Git object identity"):
        verify_release(
            root,
            source_repo=source_repo,
            expected_commit="f" * 40,
            expected_tree=tree,
        )


def test_local_handoff_never_authorizes_activation_or_switches_root(tmp_path: Path) -> None:
    root, source_repo, commit, tree = _artifact(tmp_path)
    receipt = verify_release(
        root, source_repo=source_repo, expected_commit=commit, expected_tree=tree
    )
    output = tmp_path / "local-handoff.json"

    handoff = write_local_handoff(output, release_root=root, verification=receipt)

    assert handoff["status"] == "HANDOFF_READY"
    assert handoff["activation"] == "NOT_RUN_REQUIRES_SEPARATE_ATTILA_GO_AND_JEROME"
    assert json.loads(output.read_text()) == handoff
    other = tmp_path / "other-release"
    other.mkdir()
    with pytest.raises(ReleaseError, match="verified release root"):
        write_local_handoff(output, release_root=other, verification=receipt)


def test_artifact_package_handoff_is_nonactivating_and_hash_bound(tmp_path: Path) -> None:
    package = tmp_path / "package"
    package.mkdir()
    verification = {
        "schema_version": 1,
        "kind": "AIWERK_IMMUTABLE_ARTIFACT_VERIFICATION",
        "verdict": "PASS",
        "artifact_root": str(package.resolve()),
        "source_commit": "a" * 40,
        "source_git_tree": "b" * 40,
        "source_inventory_sha256": "c" * 64,
        "release_id": "a" * 40,
        "release_manifest_sha256": "d" * 64,
        "archive_sha256": "e" * 64,
        "archive_size": 123,
    }

    handoff = write_artifact_handoff(
        tmp_path / "handoff.json",
        artifact_root=package,
        verification=verification,
    )

    assert handoff["status"] == "HANDOFF_READY"
    assert handoff["artifact_root"] == str(package.resolve())
    assert handoff["activation"].startswith("NOT_RUN")
    assert handoff["service_restart"] == "NOT_RUN"
