"""Immutable release verification and Local handoff preparation."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
from typing import Any

from .artifact import _installed_identity_receipt_path
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


def installed_identity_probe_code() -> str:
    return (
        "import importlib.metadata,json,sys;"
        "from pathlib import Path;"
        "import aiwerk_runtime_updater as package;"
        "dist=importlib.metadata.distribution('aiwerk-runtime-updater');"
        "print(json.dumps({'prefix':sys.prefix,'executable':sys.executable,"
        "'package':str(Path(package.__file__).resolve().parent),"
        "'distribution':str(Path(dist.locate_file('')).resolve())},"
        "sort_keys=True,separators=(',',':')))"
    )


def _verified_proof_hashes(verification: dict[str, Any]) -> dict[str, str]:
    paths = verification.get("proof_receipts")
    kinds = {
        "installed_update_check": "AIWERK_INSTALLED_UPDATE_CHECK_RECEIPT",
        "extracted_target_preflight": "AIWERK_EXTRACTED_TARGET_PREFLIGHT_RECEIPT",
        "recovery": "AIWERK_RECOVERY_PROOF_RECEIPT",
    }
    if not isinstance(paths, dict) or set(paths) != set(kinds):
        raise ReleaseError("artifact verification incomplete: persisted proof receipts")
    installed_root = verification.get("installed_updater_root")
    identity_receipt_path = Path(
        str(verification.get("installed_updater_identity_receipt_path", ""))
    )
    identity_receipt_sha256 = verification.get(
        "installed_updater_identity_receipt_sha256"
    )
    expected_identity_receipt_path = _installed_identity_receipt_path(
        Path(str(verification.get("artifact_root", "")))
    )
    if (
        not isinstance(installed_root, str)
        or not Path(installed_root).is_absolute()
        or not identity_receipt_path.is_absolute()
        or identity_receipt_path != expected_identity_receipt_path
        or identity_receipt_path.is_symlink()
        or not identity_receipt_path.is_file()
        or not isinstance(identity_receipt_sha256, str)
        or len(identity_receipt_sha256) != 64
    ):
        raise ReleaseError("artifact verification incomplete: builder-bound identity receipt")
    identity_receipt_raw = identity_receipt_path.read_bytes()
    if hashlib.sha256(identity_receipt_raw).hexdigest() != identity_receipt_sha256:
        raise ReleaseError("builder-bound identity receipt hash mismatch")
    try:
        identity_receipt = json.loads(identity_receipt_raw)
    except json.JSONDecodeError as exc:
        raise ReleaseError("builder-bound identity receipt is invalid") from exc
    if (
        not isinstance(identity_receipt, dict)
        or identity_receipt_raw != canonical_bytes(identity_receipt)
        or identity_receipt.get("kind") != "AIWERK_INSTALLED_UPDATER_IDENTITY"
        or identity_receipt.get("artifact_root") != verification.get("artifact_root")
        or identity_receipt.get("installed_updater_root") != installed_root
        or identity_receipt.get("source_commit") != verification.get("source_commit")
        or identity_receipt.get("source_git_tree") != verification.get("source_git_tree")
        or identity_receipt.get("installed_updater_source_identity")
        != verification.get("installed_updater_source_identity")
        or identity_receipt.get("installed_updater_wheel_identity")
        != verification.get("installed_updater_wheel_identity")
    ):
        raise ReleaseError("builder-bound identity receipt mismatch")
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
        "target_config_path",
        "target_config_sha256",
        "predecessor_config_path",
        "predecessor_config_sha256",
        "predecessor_root",
        "publication_sha256",
        "identity_probe_argv",
        "identity_probe_exit_code",
        "identity_probe_output_path",
        "identity_probe_stdout_sha256",
    )
    for name, (_raw, value) in documents.items():
        if any(field not in value for field in execution_fields):
            raise ReleaseError(f"persisted execution proof incomplete: {name}")
        root = Path(str(value["installed_updater_root"]))
        python = Path(str(value["installed_updater_python"]))
        if (
            not root.is_absolute()
            or str(root) != installed_root
            or not python.is_absolute()
            or python.parent != root / "bin"
            or not isinstance(value["identity_probe_argv"], list)
            or not value["identity_probe_argv"]
            or not all(
                isinstance(item, str) and item for item in value["identity_probe_argv"]
            )
            or value["identity_probe_exit_code"] != 0
            or not isinstance(value["identity_probe_output_path"], str)
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
            tuple(json.dumps(value[field], sort_keys=True) for field in execution_fields)
            for _raw, value in documents.values()
        }
    ) != 1:
        raise ReleaseError("persisted execution proof identity mismatch")

    common_value = documents["installed_update_check"][1]
    root = Path(str(common_value["installed_updater_root"]))
    python = Path(str(common_value["installed_updater_python"]))
    target_config = Path(str(common_value["target_config_path"]))
    predecessor_config = Path(str(common_value["predecessor_config_path"]))
    predecessor_root = Path(str(common_value["predecessor_root"]))
    artifact_root = Path(str(verification.get("artifact_root", "")))
    for path, digest, label in (
        (target_config, common_value["target_config_sha256"], "target config"),
        (
            predecessor_config,
            common_value["predecessor_config_sha256"],
            "predecessor config",
        ),
        (
            artifact_root / "publication.json",
            common_value["publication_sha256"],
            "publication",
        ),
    ):
        if (
            not path.is_absolute()
            or path.is_symlink()
            or not path.is_file()
            or path.resolve(strict=True) != path
            or hashlib.sha256(path.read_bytes()).hexdigest() != digest
        ):
            raise ReleaseError(f"persisted execution proof {label} binding mismatch")
    if (
        not predecessor_root.is_absolute()
        or predecessor_root.is_symlink()
        or not predecessor_root.is_dir()
        or predecessor_root.resolve(strict=True) != predecessor_root
    ):
        raise ReleaseError("persisted execution proof predecessor root mismatch")
    expected_identity_argv = [
        str(python),
        "-I",
        "-B",
        "-c",
        installed_identity_probe_code(),
    ]
    if common_value["identity_probe_argv"] != expected_identity_argv:
        raise ReleaseError("persisted native argv mismatch: identity probe")

    receipt_roots = {Path(str(paths[name])).parent for name in kinds}
    if len(receipt_roots) != 1:
        raise ReleaseError("persisted proof receipts do not share one run root")
    run_root = next(iter(receipt_roots))
    required_outputs = {
        "installed-updater-identity-probe.stdout",
        "installed-update-check.stdout",
        "extracted-target-preflight.stdout",
        "target-systemd-bridge.stdout",
        "predecessor-systemd-bridge.stdout",
    }
    actual_outputs = {path.name for path in run_root.glob("*.stdout")}
    if actual_outputs != required_outputs:
        raise ReleaseError("retained native output set differs")

    def retained_output(
        value: dict[str, Any],
        *,
        path_field: str,
        hash_field: str,
        argv_field: str,
        expected_name: str,
    ) -> bytes:
        path = Path(str(value.get(path_field, "")))
        argv = value.get(argv_field)
        expected_hash = value.get(hash_field)
        if (
            not path.is_absolute()
            or path.parent != run_root
            or path.name != expected_name
            or path.is_symlink()
            or not path.is_file()
            or path.resolve(strict=True) != path
            or not isinstance(argv, list)
            or not argv
            or not all(isinstance(item, str) and item for item in argv)
        ):
            raise ReleaseError(f"retained native output malformed: {expected_name}")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected_hash:
            raise ReleaseError(f"retained native output hash mismatch: {expected_name}")
        return raw

    common_value = documents["installed_update_check"][1]
    identity_raw = retained_output(
        common_value,
        path_field="identity_probe_output_path",
        hash_field="identity_probe_stdout_sha256",
        argv_field="identity_probe_argv",
        expected_name="installed-updater-identity-probe.stdout",
    )
    try:
        identity_value = json.loads(identity_raw)
    except json.JSONDecodeError as exc:
        raise ReleaseError("retained native output identity probe is not JSON") from exc
    root = Path(str(common_value["installed_updater_root"]))
    python = Path(str(common_value["installed_updater_python"]))
    package = Path(str(identity_value.get("package", "")))
    distribution = Path(str(identity_value.get("distribution", "")))
    if (
        identity_value.get("prefix") != str(root)
        or identity_value.get("executable") != str(python)
        or package.parent != distribution
        or package.name != "aiwerk_runtime_updater"
    ):
        raise ReleaseError("retained native output identity probe mismatch")

    check = documents["installed_update_check"][1]
    expected_check_argv = [
        str(python),
        "-I",
        "-B",
        "-m",
        "aiwerk_runtime_updater.cli",
        "--lock-path",
        str(run_root / "native-check.lock"),
        "update",
        "--check",
        "--version",
        str(verification.get("release_id")),
        "--config",
        str(target_config),
        "--expect-updater-sha256",
        str(verification.get("installed_updater_source_identity")),
        "--expect-publication-sha256",
        str(common_value["publication_sha256"]),
        "--expect-archive-sha256",
        str(verification.get("archive_sha256")),
        "--expect-archive-size",
        str(verification.get("archive_size")),
        "--receipt-stdout",
        "--request-dir",
        str(run_root / "native-requests"),
    ]
    if check.get("argv") != expected_check_argv:
        raise ReleaseError("persisted native argv mismatch: update check")
    try:
        _required_digest(check.get("stdout_sha256"), "installed update-check stdout")
    except ReleaseError as exc:
        raise ReleaseError("persisted execution proof incomplete: update-check stdout") from exc
    check_raw = retained_output(
        check,
        path_field="output_path",
        hash_field="stdout_sha256",
        argv_field="argv",
        expected_name="installed-update-check.stdout",
    )
    try:
        check_output = json.loads(check_raw)
    except json.JSONDecodeError as exc:
        raise ReleaseError("retained native output update-check is not JSON") from exc
    if (
        check.get("status") != "AVAILABLE"
        or check.get("exit_code") != 0
        or check.get("release_id") != verification.get("release_id")
        or check.get("archive_sha256") != verification.get("archive_sha256")
        or check.get("archive_size") != verification.get("archive_size")
        or check_output
        != {
            "status": "AVAILABLE",
            "release_id": verification.get("release_id"),
            "archive_sha256": verification.get("archive_sha256"),
            "archive_size": verification.get("archive_size"),
            "migration_class": "forward_only",
        }
    ):
        raise ReleaseError("persisted update-check receipt is not an exact AVAILABLE result")
    target = documents["extracted_target_preflight"][1]
    expected_target_argv = [
        str(python),
        "-I",
        "-B",
        "-m",
        "aiwerk_runtime_updater.artifact_preflight",
        "--config",
        str(target_config),
        "--release-root",
        str(artifact_root / "runtime"),
    ]
    if target.get("argv") != expected_target_argv:
        raise ReleaseError("persisted native argv mismatch: extracted target")
    try:
        _required_digest(target.get("stdout_sha256"), "extracted-target stdout")
    except ReleaseError as exc:
        raise ReleaseError("persisted execution proof incomplete: extracted-target stdout") from exc
    target_raw = retained_output(
        target,
        path_field="output_path",
        hash_field="stdout_sha256",
        argv_field="argv",
        expected_name="extracted-target-preflight.stdout",
    )
    try:
        retained_version_output = target_raw.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise ReleaseError("retained native output target version is not UTF-8") from exc
    version_output = target.get("version_output")
    if (
        not isinstance(version_output, str)
        or retained_version_output != version_output
        or not version_output.startswith("Hermes Agent v")
        or target.get("status") != "PASS"
        or target.get("exit_code") != 0
        or target.get("release_id") != verification.get("release_id")
        or target.get("target_root") != str(Path(str(verification["artifact_root"])) / "runtime")
    ):
        raise ReleaseError("persisted extracted-target receipt mismatch")
    recovery = documents["recovery"][1]
    expected_target_bridge_argv = [
        str(python),
        "-I",
        "-B",
        "-m",
        "aiwerk_runtime_updater.systemd_bridge_render",
        "--config",
        str(target_config),
        "--verify-installed-root",
        str(artifact_root / "runtime"),
    ]
    expected_predecessor_bridge_argv = [
        str(python),
        "-I",
        "-B",
        "-m",
        "aiwerk_runtime_updater.systemd_bridge_render",
        "--config",
        str(predecessor_config),
        "--verify-installed-root",
        str(predecessor_root),
    ]
    if recovery.get("target_bridge_argv") != expected_target_bridge_argv:
        raise ReleaseError("persisted native argv mismatch: target bridge")
    if recovery.get("predecessor_bridge_argv") != expected_predecessor_bridge_argv:
        raise ReleaseError("persisted native argv mismatch: predecessor bridge")
    for field in (
        "target_bridge_stdout_sha256",
        "predecessor_bridge_stdout_sha256",
    ):
        try:
            _required_digest(recovery.get(field), field)
        except ReleaseError as exc:
            raise ReleaseError(f"persisted execution proof incomplete: {field}") from exc
    retained_output(
        recovery,
        path_field="target_bridge_output_path",
        hash_field="target_bridge_stdout_sha256",
        argv_field="target_bridge_argv",
        expected_name="target-systemd-bridge.stdout",
    )
    retained_output(
        recovery,
        path_field="predecessor_bridge_output_path",
        hash_field="predecessor_bridge_stdout_sha256",
        argv_field="predecessor_bridge_argv",
        expected_name="predecessor-systemd-bridge.stdout",
    )
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
    return {
        "installed_update_check_receipt_sha256": hashlib.sha256(
            documents["installed_update_check"][0]
        ).hexdigest(),
        "extracted_target_preflight_receipt_sha256": hashlib.sha256(
            documents["extracted_target_preflight"][0]
        ).hexdigest(),
        "recovery_receipt_sha256": hashlib.sha256(
            documents["recovery"][0]
        ).hexdigest(),
        "recovery_disposition": str(recovery["disposition"]),
        "post_start_policy": str(recovery["post_start_policy"]),
    }


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
    if (
        proof_hashes.get("recovery_disposition")
        == "FORWARD_ONLY_POSTFAILURE_PREDECESSOR_RECOVERY_UNPROVEN"
    ):
        return {
            "schema_version": 1,
            "kind": "AIWERK_LOCAL_ACTIVATION_HANDOFF",
            "status": "HANDOFF_BLOCKED_RECOVERY",
            "completion": False,
            "artifact_root": resolved,
            **{key: verification[key] for key in required},
            **proof_hashes,
            "activation": "NOT_RUN",
            "service_restart": "NOT_RUN",
            "tenant_operations": "NOT_RUN",
        }
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
