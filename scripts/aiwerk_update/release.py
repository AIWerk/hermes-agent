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
    )
    if any(key not in verification for key in required):
        raise ReleaseError("artifact verification is incomplete")
    handoff = {
        "schema_version": 1,
        "kind": "AIWERK_LOCAL_ACTIVATION_HANDOFF",
        "status": "HANDOFF_READY",
        "artifact_root": resolved,
        **{key: verification[key] for key in required},
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
        raise ReleaseError("Local handoff requires a passing immutable release verification")
    verified_root = verification.get("release_root")
    if verified_root != str(release_root.resolve()):
        raise ReleaseError("Local handoff release_root differs from verified release root")
    handoff = {
        "schema_version": 1,
        "kind": "AIWERK_LOCAL_ACTIVATION_HANDOFF",
        "status": "HANDOFF_READY",
        "release_root": str(release_root.resolve()),
        "source_commit": verification["source_commit"],
        "source_tree": verification["source_tree"],
        "manifest_sha256": verification["manifest_sha256"],
        "activation": "NOT_RUN_REQUIRES_SEPARATE_ATTILA_GO_AND_JEROME",
        "service_restart": "NOT_RUN",
        "tenant_operations": "NOT_RUN",
    }
    _atomic_write(output, canonical_bytes(handoff))
    return handoff
