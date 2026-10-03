from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest

from scripts.aiwerk_update.contract import canonical_bytes
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


def _proof_receipts(tmp_path: Path, verification: dict) -> dict[str, str]:
    artifact_root = Path(verification["artifact_root"])
    (artifact_root / "runtime").mkdir(exist_ok=True)
    common = {
        "schema_version": 1,
        "artifact_root": str(artifact_root),
        "source_commit": verification["source_commit"],
        "source_git_tree": verification["source_git_tree"],
        "installed_updater_source_identity": verification["installed_updater_source_identity"],
        "installed_updater_wheel_identity": verification["installed_updater_wheel_identity"],
    }
    documents = {
        "installed_update_check": {
            **common,
            "kind": "AIWERK_INSTALLED_UPDATE_CHECK_RECEIPT",
            "status": "AVAILABLE",
            "exit_code": 0,
            "release_id": verification["release_id"],
            "archive_sha256": verification["archive_sha256"],
            "archive_size": verification["archive_size"],
        },
        "extracted_target_preflight": {
            **common,
            "kind": "AIWERK_EXTRACTED_TARGET_PREFLIGHT_RECEIPT",
            "status": "PASS",
            "exit_code": 0,
            "release_id": verification["release_id"],
            "target_root": str(artifact_root / "runtime"),
        },
        "recovery": {
            **common,
            "kind": "AIWERK_RECOVERY_PROOF_RECEIPT",
            "status": "PASS",
            "release_id": verification["release_id"],
            "disposition": "FORWARD_ONLY_PRESTART_RECOVERY_PROVED",
            "migration_class": "forward_only",
            "native_rollback_supported": False,
            "post_start_policy": "CONTAINMENT_ONLY",
            "target_bridge_verified": True,
            "predecessor_bridge_verified": True,
            "units_verified": True,
        },
    }
    paths = {}
    for name, document in documents.items():
        path = tmp_path / f"{name}.json"
        path.write_bytes(canonical_bytes(document))
        paths[name] = str(path.resolve())
    return paths


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
    receipt.update(
        {
            "installed_updater_source_identity": "1" * 64,
            "installed_updater_wheel_identity": "2" * 64,
            "installed_update_check_receipt_sha256": "3" * 64,
            "extracted_target_preflight_receipt_sha256": "4" * 64,
            "recovery_receipt_sha256": "5" * 64,
        }
    )
    output = tmp_path / "local-handoff.json"

    handoff = write_local_handoff(output, release_root=root, verification=receipt)

    assert handoff["status"] == "FIXTURE_ONLY"
    assert handoff["kind"] == "AIWERK_LOCAL_HANDOFF_FIXTURE"
    assert handoff["activation"] == "NOT_RUN_FIXTURE_ONLY"
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
        "installed_updater_source_identity": "1" * 64,
        "installed_updater_wheel_identity": "2" * 64,
        "installed_update_check_receipt_sha256": "3" * 64,
        "extracted_target_preflight_receipt_sha256": "4" * 64,
        "recovery_receipt_sha256": "5" * 64,
    }
    verification["proof_receipts"] = _proof_receipts(tmp_path, verification)

    handoff = write_artifact_handoff(
        tmp_path / "handoff.json",
        artifact_root=package,
        verification=verification,
    )

    assert handoff["status"] == "HANDOFF_READY"
    assert handoff["artifact_root"] == str(package.resolve())
    assert handoff["activation"].startswith("NOT_RUN")
    assert handoff["service_restart"] == "NOT_RUN"


def test_artifact_handoff_requires_identity_check_target_and_recovery_receipts(
    tmp_path: Path,
) -> None:
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
        "installed_updater_source_identity": "1" * 64,
        "installed_updater_wheel_identity": "2" * 64,
        "installed_update_check_receipt_sha256": "3" * 64,
        "extracted_target_preflight_receipt_sha256": "4" * 64,
        "recovery_receipt_sha256": "5" * 64,
    }
    identity_fields = (
        "installed_updater_source_identity",
        "installed_updater_wheel_identity",
    )

    for field in identity_fields:
        incomplete = dict(verification)
        incomplete.pop(field)
        with pytest.raises(ReleaseError, match="incomplete"):
            write_artifact_handoff(
                tmp_path / f"missing-{field}.json",
                artifact_root=package,
                verification=incomplete,
            )
    with pytest.raises(ReleaseError, match="persisted proof receipts"):
        write_artifact_handoff(
            tmp_path / "hashes-only.json",
            artifact_root=package,
            verification=verification,
        )

    verification["proof_receipts"] = _proof_receipts(tmp_path, verification)
    handoff = write_artifact_handoff(
        tmp_path / "identity-bound-handoff.json",
        artifact_root=package,
        verification=verification,
    )
    proof_hashes = {
        "installed_update_check_receipt_sha256": hashlib.sha256(
            Path(verification["proof_receipts"]["installed_update_check"]).read_bytes()
        ).hexdigest(),
        "extracted_target_preflight_receipt_sha256": hashlib.sha256(
            Path(verification["proof_receipts"]["extracted_target_preflight"]).read_bytes()
        ).hexdigest(),
        "recovery_receipt_sha256": hashlib.sha256(
            Path(verification["proof_receipts"]["recovery"]).read_bytes()
        ).hexdigest(),
    }
    assert {field: handoff[field] for field in identity_fields} == {
        field: verification[field] for field in identity_fields
    }
    assert {field: handoff[field] for field in proof_hashes} == proof_hashes

    check_path = Path(verification["proof_receipts"]["installed_update_check"])
    check = json.loads(check_path.read_bytes())
    check["status"] = "PASS"
    check_path.write_bytes(canonical_bytes(check))
    with pytest.raises(ReleaseError, match="exact AVAILABLE"):
        write_artifact_handoff(
            tmp_path / "caller-pass-only.json",
            artifact_root=package,
            verification=verification,
        )
