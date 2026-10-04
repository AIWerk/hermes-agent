"""Immutable release verification and Local handoff preparation."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
from typing import Any

from .contract import _atomic_write, canonical_bytes


MANIFEST = "artifact-manifest.json"


class ReleaseError(RuntimeError):
    pass


def _required_digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ReleaseError(f"artifact verification incomplete: {label}")
    return value


def _verified_proof_hashes(verification: dict[str, Any]) -> dict[str, str]:
    paths = verification.get("proof_receipts")
    kinds = {
        "installed_update_check": "AIWERK_INSTALLED_UPDATE_CHECK_RECEIPT",
        "extracted_target_preflight": "AIWERK_EXTRACTED_TARGET_PREFLIGHT_RECEIPT",
        "recovery": "AIWERK_RECOVERY_PROOF_RECEIPT",
    }
    if not isinstance(paths, dict) or set(paths) != set(kinds):
        raise ReleaseError("artifact verification incomplete: persisted proof receipts")
    documents: dict[str, tuple[bytes, dict[str, Any]]] = {}
    for name, kind in kinds.items():
        path = Path(str(paths[name]))
        if not path.is_absolute() or path.is_symlink() or not path.is_file():
            raise ReleaseError(f"persisted proof receipt unavailable: {name}")
        try:
            raw = path.read_bytes()
            value = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            raise ReleaseError(f"persisted proof receipt invalid: {name}: {exc}") from exc
        if not isinstance(value, dict) or raw != canonical_bytes(value):
            raise ReleaseError(f"persisted proof receipt is not canonical: {name}")
        if value.get("schema_version") != 1 or value.get("kind") != kind:
            raise ReleaseError(f"persisted proof receipt kind mismatch: {name}")
        for field in (
            "artifact_root",
            "source_commit",
            "source_git_tree",
            "installed_updater_source_identity",
            "installed_updater_wheel_identity",
        ):
            if value.get(field) != verification.get(field):
                raise ReleaseError(f"persisted proof receipt identity mismatch: {name}")
        documents[name] = raw, value
    execution_fields = (
        "installed_updater_root",
        "installed_updater_python",
        "identity_probe_stdout_sha256",
    )
    for name, (_raw, value) in documents.items():
        if any(field not in value for field in execution_fields):
            raise ReleaseError(f"persisted execution proof incomplete: {name}")
        root = Path(str(value["installed_updater_root"]))
        python = Path(str(value["installed_updater_python"]))
        if (
            not root.is_absolute()
            or not python.is_absolute()
            or python.parent != root / "bin"
            or not isinstance(value["identity_probe_stdout_sha256"], str)
            or len(value["identity_probe_stdout_sha256"]) != 64
            or any(
                character not in "0123456789abcdef"
                for character in value["identity_probe_stdout_sha256"]
            )
        ):
            raise ReleaseError(f"persisted execution proof malformed: {name}")
    if len(
        {
            (
                value["installed_updater_root"],
                value["installed_updater_python"],
                value["identity_probe_stdout_sha256"],
            )
            for _raw, value in documents.values()
        }
    ) != 1:
        raise ReleaseError("persisted execution proof identity mismatch")
    check = documents["installed_update_check"][1]
    try:
        _required_digest(check.get("stdout_sha256"), "installed update-check stdout")
    except ReleaseError as exc:
        raise ReleaseError("persisted execution proof incomplete: update-check stdout") from exc
    if (
        check.get("status") != "AVAILABLE"
        or check.get("exit_code") != 0
        or check.get("release_id") != verification.get("release_id")
        or check.get("archive_sha256") != verification.get("archive_sha256")
        or check.get("archive_size") != verification.get("archive_size")
    ):
        raise ReleaseError("persisted update-check receipt is not an exact AVAILABLE result")
    target = documents["extracted_target_preflight"][1]
    try:
        _required_digest(target.get("stdout_sha256"), "extracted-target stdout")
    except ReleaseError as exc:
        raise ReleaseError("persisted execution proof incomplete: extracted-target stdout") from exc
    version_output = target.get("version_output")
    if (
        not isinstance(version_output, str)
        or not version_output.startswith("Hermes Agent v")
        or target.get("status") != "PASS"
        or target.get("exit_code") != 0
        or target.get("release_id") != verification.get("release_id")
        or target.get("target_root") != str(Path(str(verification["artifact_root"])) / "runtime")
    ):
        raise ReleaseError("persisted extracted-target receipt mismatch")
    recovery = documents["recovery"][1]
    for field in (
        "target_bridge_stdout_sha256",
        "predecessor_bridge_stdout_sha256",
    ):
        try:
            _required_digest(recovery.get(field), field)
        except ReleaseError as exc:
            raise ReleaseError(f"persisted execution proof incomplete: {field}") from exc
    if (
        recovery.get("status") != "BLOCKED"
        or recovery.get("disposition")
        != "FORWARD_ONLY_POSTFAILURE_PREDECESSOR_RECOVERY_UNPROVEN"
        or recovery.get("migration_class") != "forward_only"
        or recovery.get("native_rollback_supported") is not False
        or recovery.get("post_start_policy") != "CONTAINMENT_ONLY"
        or recovery.get("target_bridge_verified") is not True
        or recovery.get("predecessor_bridge_verified") is not True
        or recovery.get("units_verified") is not True
        or recovery.get("release_id") != verification.get("release_id")
    ):
        raise ReleaseError("persisted forward-only recovery assessment is invalid")
    raise ReleaseError(
        "executable post-failure predecessor recovery remains unproven; HANDOFF_READY rejected"
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def inventory_release(root: Path) -> dict[str, Any]:
    if not root.is_dir() or root.is_symlink():
        raise ReleaseError("release root must be a physical directory")
    files: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if relative == MANIFEST:
            continue
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
            if stat.S_ISDIR(mode):
                continue
            raise ReleaseError(f"release contains non-regular entry: {relative}")
        files.append(
            {
                "path": relative,
                "mode": f"{stat.S_IMODE(mode):04o}",
                "size": path.stat().st_size,
                "sha256": _sha256(path),
            }
        )
    return {"files": files, "file_count": len(files), "regular_file_bytes": sum(row["size"] for row in files)}


def verify_release(
    root: Path,
    *,
    source_repo: Path,
    expected_commit: str,
    expected_tree: str,
) -> dict[str, Any]:
    commit_probe = subprocess.run(
        ["git", "cat-file", "-e", f"{expected_commit}^{{commit}}"],
        cwd=source_repo,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    tree_probe = subprocess.run(
        ["git", "rev-parse", f"{expected_commit}^{{tree}}"],
        cwd=source_repo,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="ascii",
        errors="replace",
        check=False,
    )
    if (
        commit_probe.returncode != 0
        or tree_probe.returncode != 0
        or tree_probe.stdout.strip() != expected_tree
    ):
        raise ReleaseError("source Git object identity is not proven")
    manifest_path = root / MANIFEST
    try:
        raw = manifest_path.read_bytes()
        manifest = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"artifact manifest unavailable: {exc}") from exc
    required = {"schema_version", "source_commit", "source_tree", "inventory"}
    if not isinstance(manifest, dict) or set(manifest) != required or manifest["schema_version"] != 1:
        raise ReleaseError("artifact manifest schema mismatch")
    if manifest["source_commit"] != expected_commit or manifest["source_tree"] != expected_tree:
        raise ReleaseError("artifact source identity mismatch")
    measured = inventory_release(root)
    if manifest["inventory"] != measured:
        raise ReleaseError("artifact inventory mismatch")
    return {
        "schema_version": 1,
        "kind": "AIWERK_IMMUTABLE_RELEASE_VERIFICATION",
        "verdict": "PASS",
        "release_root": str(root.resolve()),
        "source_commit": expected_commit,
        "source_tree": expected_tree,
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "inventory": measured,
    }


def write_artifact_handoff(
    output: Path,
    *,
    artifact_root: Path,
    verification: dict[str, Any],
) -> dict[str, Any]:
    if verification.get("verdict") != "PASS":
        raise ReleaseError("Local handoff requires passing artifact verification")
    if verification.get("kind") != "AIWERK_IMMUTABLE_ARTIFACT_VERIFICATION":
        raise ReleaseError("artifact verification kind mismatch")
    resolved = str(artifact_root.resolve())
    if verification.get("artifact_root") != resolved:
        raise ReleaseError("artifact handoff root differs from verified package root")
    required = (
        "source_commit",
        "source_git_tree",
        "source_inventory_sha256",
        "release_id",
        "release_manifest_sha256",
        "archive_sha256",
        "archive_size",
        "installed_updater_source_identity",
        "installed_updater_wheel_identity",
    )
    if any(key not in verification for key in required):
        raise ReleaseError("artifact verification is incomplete")
    for key in (
        "installed_updater_source_identity",
        "installed_updater_wheel_identity",
    ):
        _required_digest(verification[key], key)
    proof_hashes = _verified_proof_hashes(verification)
    handoff = {
        "schema_version": 1,
        "kind": "AIWERK_LOCAL_ACTIVATION_HANDOFF",
        "status": "HANDOFF_READY",
        "artifact_root": resolved,
        **{key: verification[key] for key in required},
        **proof_hashes,
        "activation": "NOT_RUN_REQUIRES_SEPARATE_ATTILA_GO_AND_JEROME",
        "service_restart": "NOT_RUN",
        "tenant_operations": "NOT_RUN",
    }
    _atomic_write(output, canonical_bytes(handoff))
    return handoff


def write_local_handoff(
    output: Path,
    *,
    release_root: Path,
    verification: dict[str, Any],
) -> dict[str, Any]:
    if verification.get("verdict") != "PASS":
        raise ReleaseError("Local fixture handoff requires a passing immutable release verification")
    if verification.get("kind") != "AIWERK_IMMUTABLE_RELEASE_VERIFICATION":
        raise ReleaseError("Local fixture handoff verification kind mismatch")
    verified_root = verification.get("release_root")
    if verified_root != str(release_root.resolve()):
        raise ReleaseError("Local fixture handoff release_root differs from verified release root")
    handoff = {
        "schema_version": 1,
        "kind": "AIWERK_LOCAL_HANDOFF_FIXTURE",
        "status": "FIXTURE_ONLY",
        "release_root": str(release_root.resolve()),
        "source_commit": verification["source_commit"],
        "source_tree": verification["source_tree"],
        "manifest_sha256": verification["manifest_sha256"],
        "activation": "NOT_RUN_FIXTURE_ONLY",
        "service_restart": "NOT_RUN",
        "tenant_operations": "NOT_RUN",
    }
    _atomic_write(output, canonical_bytes(handoff))
    return handoff
