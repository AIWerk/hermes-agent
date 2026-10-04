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
    _verified_proof_hashes,
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
    outputs = {
        "installed-updater-identity-probe.stdout": canonical_bytes(
            {
                "prefix": str(tmp_path / "installed-updater"),
                "executable": str(tmp_path / "installed-updater/bin/python3.12"),
                "package": str(
                    tmp_path
                    / "installed-updater/lib/python3.12/site-packages/aiwerk_runtime_updater"
                ),
                "distribution": str(tmp_path / "installed-updater/lib/python3.12/site-packages"),
            }
        ),
        "installed-update-check.stdout": canonical_bytes(
            {
                "status": "AVAILABLE",
                "release_id": verification["release_id"],
                "archive_sha256": verification["archive_sha256"],
                "archive_size": verification["archive_size"],
                "migration_class": "forward_only",
            }
        ),
        "extracted-target-preflight.stdout": b"Hermes Agent v0.21.1\n",
        "target-systemd-bridge.stdout": b"target bridge\n",
        "predecessor-systemd-bridge.stdout": b"predecessor bridge\n",
    }
    for name, raw in outputs.items():
        (tmp_path / name).write_bytes(raw)
    identity_output = tmp_path / "installed-updater-identity-probe.stdout"
    common = {
        "schema_version": 1,
        "artifact_root": str(artifact_root),
        "source_commit": verification["source_commit"],
        "source_git_tree": verification["source_git_tree"],
        "installed_updater_source_identity": verification["installed_updater_source_identity"],
        "installed_updater_wheel_identity": verification["installed_updater_wheel_identity"],
        "installed_updater_root": str(tmp_path / "installed-updater"),
        "installed_updater_python": str(tmp_path / "installed-updater/bin/python3.12"),
        "identity_probe_argv": [
            str(tmp_path / "installed-updater/bin/python3.12"),
            "-I",
            "-B",
            "-c",
            "identity-probe",
        ],
        "identity_probe_exit_code": 0,
        "identity_probe_output_path": str(identity_output),
        "identity_probe_stdout_sha256": hashlib.sha256(identity_output.read_bytes()).hexdigest(),
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
            "argv": ["python", "-m", "aiwerk_runtime_updater.cli", "update", "--check"],
            "output_path": str(tmp_path / "installed-update-check.stdout"),
            "stdout_sha256": hashlib.sha256(
                (tmp_path / "installed-update-check.stdout").read_bytes()
            ).hexdigest(),
        },
        "extracted_target_preflight": {
            **common,
            "kind": "AIWERK_EXTRACTED_TARGET_PREFLIGHT_RECEIPT",
            "status": "PASS",
            "exit_code": 0,
            "release_id": verification["release_id"],
            "target_root": str(artifact_root / "runtime"),
            "version_output": "Hermes Agent v0.21.1",
            "argv": ["python", "-m", "aiwerk_runtime_updater.artifact_preflight"],
            "output_path": str(tmp_path / "extracted-target-preflight.stdout"),
            "stdout_sha256": hashlib.sha256(
                (tmp_path / "extracted-target-preflight.stdout").read_bytes()
            ).hexdigest(),
        },
        "recovery": {
            **common,
            "kind": "AIWERK_RECOVERY_PROOF_RECEIPT",
            "status": "BLOCKED",
            "release_id": verification["release_id"],
            "disposition": "FORWARD_ONLY_POSTFAILURE_PREDECESSOR_RECOVERY_UNPROVEN",
            "migration_class": "forward_only",
            "native_rollback_supported": False,
            "post_start_policy": "CONTAINMENT_ONLY",
            "target_bridge_verified": True,
            "predecessor_bridge_verified": True,
            "units_verified": True,
            "target_bridge_argv": [
                "python",
                "-m",
                "aiwerk_runtime_updater.systemd_bridge_render",
                "--config",
                "target",
            ],
            "target_bridge_output_path": str(tmp_path / "target-systemd-bridge.stdout"),
            "target_bridge_stdout_sha256": hashlib.sha256(
                (tmp_path / "target-systemd-bridge.stdout").read_bytes()
            ).hexdigest(),
            "predecessor_bridge_argv": [
                "python",
                "-m",
                "aiwerk_runtime_updater.systemd_bridge_render",
                "--config",
                "predecessor",
            ],
            "predecessor_bridge_output_path": str(
                tmp_path / "predecessor-systemd-bridge.stdout"
            ),
            "predecessor_bridge_stdout_sha256": hashlib.sha256(
                (tmp_path / "predecessor-systemd-bridge.stdout").read_bytes()
            ).hexdigest(),
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


def test_artifact_handoff_rejects_synthetic_execution_receipts(tmp_path: Path) -> None:
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

    with pytest.raises(ReleaseError, match="execution proof|recovery|identity receipt"):
        write_artifact_handoff(
            tmp_path / "handoff.json",
            artifact_root=package,
            verification=verification,
        )
    assert not (tmp_path / "handoff.json").exists()


def test_artifact_handoff_rejects_missing_or_tampered_retained_native_output(
    tmp_path: Path,
) -> None:
    package = tmp_path / "package"
    package.mkdir()
    verification = {
        "artifact_root": str(package.resolve()),
        "source_commit": "a" * 40,
        "source_git_tree": "b" * 40,
        "release_id": "a" * 40,
        "archive_sha256": "e" * 64,
        "archive_size": 123,
        "installed_updater_source_identity": "1" * 64,
        "installed_updater_wheel_identity": "2" * 64,
    }
    installed_root = tmp_path / "installed-updater"
    installed_root.mkdir()
    identity_receipt = canonical_bytes(
        {
            "schema_version": 1,
            "kind": "AIWERK_INSTALLED_UPDATER_IDENTITY",
            "artifact_root": verification["artifact_root"],
            "installed_updater_root": str(installed_root),
            "source_commit": verification["source_commit"],
            "source_git_tree": verification["source_git_tree"],
            "installed_updater_source_identity": verification[
                "installed_updater_source_identity"
            ],
            "installed_updater_wheel_identity": verification[
                "installed_updater_wheel_identity"
            ],
        }
    )
    identity_receipt_path = tmp_path / "installed-updater-identity.json"
    identity_receipt_path.write_bytes(identity_receipt)
    verification.update(
        installed_updater_root=str(installed_root),
        installed_updater_identity_receipt_path=str(identity_receipt_path),
        installed_updater_identity_receipt_sha256=hashlib.sha256(
            identity_receipt
        ).hexdigest(),
    )
    verification["proof_receipts"] = _proof_receipts(tmp_path, verification)
    (tmp_path / "installed-update-check.stdout").write_bytes(b"tampered\n")

    with pytest.raises(ReleaseError, match="retained native output"):
        _verified_proof_hashes(verification)

    (tmp_path / "installed-update-check.stdout").unlink()
    with pytest.raises(ReleaseError, match="retained native output"):
        _verified_proof_hashes(verification)


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
    with pytest.raises(ReleaseError, match="execution proof|recovery|identity receipt"):
        write_artifact_handoff(
            tmp_path / "identity-bound-handoff.json",
            artifact_root=package,
            verification=verification,
        )
    assert not (tmp_path / "identity-bound-handoff.json").exists()
