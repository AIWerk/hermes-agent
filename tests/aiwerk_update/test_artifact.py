from __future__ import annotations

import base64
import hashlib
import io
import json
from pathlib import Path
import tarfile
import os
import subprocess
import sys
import zipfile

import pytest

from scripts.aiwerk_update.artifact import (
    ArtifactBuildConfig,
    ArtifactError,
    ExternalRuntimeArtifactBuilder,
    _installed_updater_identities,
    verify_artifact_package,
)
from scripts.aiwerk_update.contract import canonical_bytes


def test_updater_entrypoint_has_payload_relocatable_shebang() -> None:
    entrypoint = Path(__file__).parents[2] / "scripts/aiwerk-runtime-integrate"

    assert entrypoint.read_bytes().splitlines()[0] == b"#!/usr/bin/env python3"


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _package(tmp_path: Path) -> tuple[Path, str, str]:
    commit = "a" * 40
    git_tree = "b" * 40
    inventory_sha = "c" * 64
    root = tmp_path / "package"
    runtime = root / "runtime"
    runtime.mkdir(parents=True)
    payload_file = runtime / "runtime.txt"
    payload_file.write_bytes(b"runtime\n")
    payload_file.chmod(0o444)
    runtime.chmod(0o555)
    entries = [
        {
            "path": "runtime.txt",
            "type": "file",
            "mode": 0o444,
            "size": 8,
            "sha256": _sha(b"runtime\n"),
        }
    ]
    payload = canonical_bytes({"schema": 1, "kind": "aiwerk-payload-manifest", "entries": entries})
    aux = canonical_bytes({"schema": 1, "kind": "aiwerk-aux-manifest", "entries": []})
    qualification = canonical_bytes(
        {
            "schema": 1,
            "kind": "aiwerk-qualification-receipt",
            "source_commit": commit,
            "source_tree": inventory_sha,
            "status": "PASS",
            "suite_sha256": "d" * 64,
        }
    )
    detectors = [
        canonical_bytes(
            {
                "schema": 1,
                "kind": "aiwerk-detector-receipt",
                "source_commit": commit,
                "source_tree": inventory_sha,
                "detector": name,
                "status": "PASS",
                "result_sha256": digit * 64,
            }
        )
        for name, digit in (("supply-chain", "e"), ("osv", "f"))
    ]
    updater_manifest = canonical_bytes(
        {
            "schema": 1,
            "kind": "aiwerk-updater-source-manifest",
            "entries": [],
        }
    )
    release = canonical_bytes(
        {
            "schema": 1,
            "kind": "aiwerk-release",
            "release_id": commit,
            "source_commit": commit,
            "source_tree": inventory_sha,
            "platform": "linux-x86_64-cpython-312",
            "minimum_updater": "2.24.0",
            "migration_class": "forward_only",
            "entry_module": "hermes_cli.main",
            "dashboard_version": "1.0",
            "payload_manifest_sha256": _sha(payload),
            "aux_manifest_sha256": _sha(aux),
            "inventory_sha256": _sha(canonical_bytes(entries)),
            "web_dist_sha256": "1" * 64,
            "updater_manifest_sha256": _sha(updater_manifest),
            "qualification_receipt_sha256": _sha(qualification),
            "detector_receipt_sha256": [_sha(value) for value in detectors],
            "rollback_authority_sha256": None,
        }
    )
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w", format=tarfile.PAX_FORMAT) as handle:
        info = tarfile.TarInfo("runtime.txt")
        info.mode = 0o444
        info.size = 8
        info.mtime = 1
        handle.addfile(info, io.BytesIO(b"runtime\n"))
    archive_raw = archive.getvalue()
    publication = canonical_bytes(
        {
            "schema": 1,
            "kind": "aiwerk-publication",
            "release_id": commit,
            "release_manifest_url": f"https://repo.example/releases/{commit}/release.json",
            "release_manifest_sha256": _sha(release),
            "archive_url": f"https://repo.example/releases/{commit}/artifact.tar",
            "archive_sha256": _sha(archive_raw),
            "archive_size": len(archive_raw),
            "payload_manifest_sha256": _sha(payload),
            "aux_manifest_sha256": _sha(aux),
            "updater_manifest_sha256": _sha(updater_manifest),
            "rollback_authority_sha256": None,
        }
    )
    pin = canonical_bytes(
        {
            "schema": 1,
            "kind": "aiwerk-approved-pin",
            "release_id": commit,
            "publication_url": f"https://repo.example/releases/{commit}/publication.json",
            "publication_sha256": _sha(publication),
            "updater_manifest_sha256": _sha(updater_manifest),
        }
    )
    files = {
        "artifact.tar": archive_raw,
        "payload-manifest.json": payload,
        "aux-manifest.json": aux,
        "release.json": release,
        "publication.json": publication,
        "approved-pin.json": pin,
        "updater-manifest.json": updater_manifest,
        "qualification-receipt.json": qualification,
        "detector-1.json": detectors[0],
        "detector-2.json": detectors[1],
    }
    for name, raw in files.items():
        (root / name).write_bytes(raw)
    return root, commit, git_tree


def test_verify_artifact_package_checks_hash_graph_archive_and_source(tmp_path: Path) -> None:
    root, commit, git_tree = _package(tmp_path)

    receipt = verify_artifact_package(
        root,
        expected_commit=commit,
        expected_git_tree=git_tree,
    )

    assert receipt["verdict"] == "PASS"
    assert receipt["source_commit"] == commit
    assert receipt["source_git_tree"] == git_tree
    assert receipt["source_inventory_sha256"] == "c" * 64
    assert receipt["artifact_root"] == str(root.resolve())


def test_verify_artifact_package_rejects_archive_tamper(tmp_path: Path) -> None:
    root, commit, git_tree = _package(tmp_path)
    (root / "artifact.tar").write_bytes(b"tampered")

    with pytest.raises(ArtifactError, match="archive"):
        verify_artifact_package(root, expected_commit=commit, expected_git_tree=git_tree)


def test_verify_artifact_package_rejects_payload_aux_path_overlap(tmp_path: Path) -> None:
    root, commit, git_tree = _package(tmp_path)
    payload = json.loads((root / "payload-manifest.json").read_text())
    aux_raw = canonical_bytes(
        {"schema": 1, "kind": "aiwerk-aux-manifest", "entries": payload["entries"]}
    )
    (root / "aux-manifest.json").write_bytes(aux_raw)
    release = json.loads((root / "release.json").read_text())
    release["aux_manifest_sha256"] = _sha(aux_raw)
    release_raw = canonical_bytes(release)
    (root / "release.json").write_bytes(release_raw)
    publication = json.loads((root / "publication.json").read_text())
    publication["aux_manifest_sha256"] = _sha(aux_raw)
    publication["release_manifest_sha256"] = _sha(release_raw)
    publication_raw = canonical_bytes(publication)
    (root / "publication.json").write_bytes(publication_raw)
    pin = json.loads((root / "approved-pin.json").read_text())
    pin["publication_sha256"] = _sha(publication_raw)
    (root / "approved-pin.json").write_bytes(canonical_bytes(pin))

    with pytest.raises(ArtifactError, match="overlap"):
        verify_artifact_package(root, expected_commit=commit, expected_git_tree=git_tree)


def test_external_builder_reuse_rejects_wheel_only_identity_drift(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.email", "noreply@github.com"], cwd=source, check=True)
    (source / "pyproject.toml").write_text(
        '[project]\nname="hermes-agent"\nversion="1.2.3"\n'
    )
    (source / "uv.lock").write_text("lock\n")
    (source / "immutable-release-build.json").write_text("{}\n")
    (source / "runtime.py").write_text("VALUE = 1\n")
    subprocess.run(["git", "add", "."], cwd=source, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "candidate"], cwd=source, check=True)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    tree = subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=source, text=True).strip()

    tools = tmp_path / "tools"
    tools.mkdir()
    payload_tool = tools / "payload"
    runtime_tool = tools / "runtime"
    npm_tool = tools / "npm"
    for tool in (payload_tool, runtime_tool, npm_tool):
        tool.write_text("tool\n")
        tool.chmod(0o755)
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    dependency = wheelhouse / "demo-1-py3-none-any.whl"
    dependency.write_bytes(b"dependency-wheel")
    old_payload = wheelhouse / "hermes_agent-0.9.0-py3-none-any.whl"
    old_payload.write_bytes(b"old")
    lock = tmp_path / "wheel-lock.json"
    lock.write_bytes(
        canonical_bytes(
            {
                "schema": 1,
                "kind": "aiwerk-wheel-lock",
                "wheels": [
                    {"name": dependency.name, "sha256": _sha(dependency.read_bytes())},
                    {"name": old_payload.name, "sha256": _sha(old_payload.read_bytes())},
                ],
            }
        )
    )
    input_hashes = {
        name: _sha((source / name).read_bytes())
        for name in ("pyproject.toml", "uv.lock", "immutable-release-build.json")
    }
    installed_updater = tmp_path / "installed-updater"
    package = installed_updater / "aiwerk_runtime_updater"
    package.mkdir(parents=True)
    (package / "cli.py").write_bytes(b"VERSION = '2.24.0'\n")
    dist_info = installed_updater / "aiwerk_runtime_updater-2.24.0.dist-info"
    dist_info.mkdir()
    record = dist_info / "RECORD"
    direct_url = dist_info / "direct_url.json"
    direct_url.write_bytes(b'{"url":"file:///original"}\n')
    cli_raw = (package / "cli.py").read_bytes()
    cli_digest = base64.urlsafe_b64encode(hashlib.sha256(cli_raw).digest()).rstrip(b"=").decode()
    direct_url_digest = base64.urlsafe_b64encode(
        hashlib.sha256(direct_url.read_bytes()).digest()
    ).rstrip(b"=").decode()
    record.write_text(
        f"aiwerk_runtime_updater/cli.py,sha256={cli_digest},{len(cli_raw)}\n"
        f"aiwerk_runtime_updater-2.24.0.dist-info/direct_url.json,sha256={direct_url_digest},{direct_url.stat().st_size}\n"
        "aiwerk_runtime_updater-2.24.0.dist-info/RECORD,,\n"
    )
    original_installed_identities = _installed_updater_identities(installed_updater)
    commands: list[tuple[str, ...]] = []
    inventory_sha = "9" * 64
    built_identity: dict[str, str] = {}

    def runner(argv: tuple[str, ...], cwd: Path) -> None:
        commands.append(argv)
        if argv[0] == str(npm_tool) and argv[1:3] == ("run", "build"):
            web = cwd / "hermes_cli/web_dist"
            web.mkdir(parents=True)
            (web / "index.html").write_text("ok\n")
        elif argv[0] == str(payload_tool):
            output = Path(argv[argv.index("--output") + 1])
            output.parent.mkdir(parents=True, exist_ok=True)
            provenance = {
                "source_commit": commit,
                "source_tree": tree,
                "tracked_git_object_inventory_sha256": inventory_sha,
            }
            with zipfile.ZipFile(output, "w") as archive:
                archive.writestr(
                    "hermes_agent-1.2.3.dist-info/aiwerk-payload-provenance.json",
                    canonical_bytes(provenance),
                )
        elif argv[0] == str(runtime_tool):
            Path(argv[argv.index("--output") + 1]).mkdir()

    def verifier(
        root: Path,
        *,
        expected_commit: str,
        expected_git_tree: str,
        expected_installed_updater_source_identity: str,
        expected_installed_updater_wheel_identity: str,
    ):
        assert root.is_dir()
        measured = {
            "installed_updater_source_identity": expected_installed_updater_source_identity,
            "installed_updater_wheel_identity": expected_installed_updater_wheel_identity,
        }
        if not built_identity:
            built_identity.update(measured)
        return {
            "schema_version": 1,
            "kind": "AIWERK_IMMUTABLE_ARTIFACT_VERIFICATION",
            "verdict": "PASS",
            "artifact_root": str(root.resolve()),
            "source_commit": expected_commit,
            "source_git_tree": expected_git_tree,
            "source_inventory_sha256": inventory_sha,
            "release_id": expected_commit,
            "release_manifest_sha256": "a" * 64,
            "archive_sha256": "b" * 64,
            "archive_size": 1,
            **measured,
        }

    config = ArtifactBuildConfig(
        payload_builder=payload_tool,
        payload_builder_sha256=_sha(payload_tool.read_bytes()),
        runtime_builder=runtime_tool,
        runtime_builder_sha256=_sha(runtime_tool.read_bytes()),
        npm=npm_tool,
        npm_sha256=_sha(npm_tool.read_bytes()),
        wheelhouse=wheelhouse,
        wheel_lock=lock,
        python_prefix=Path(os.path.dirname(os.path.dirname(os.path.realpath(sys.executable)))),
        python_sha256=_sha(Path(os.path.realpath(sys.executable)).read_bytes()),
        epoch=1,
        source_input_sha256=input_hashes,
        installed_updater_root=installed_updater,
    )
    builder = ExternalRuntimeArtifactBuilder(config, runner=runner, verifier=verifier)

    receipt = builder.build_verified(
        source_repo=source,
        source_commit=commit,
        source_tree=tree,
        output=tmp_path / "artifact",
        evidence={
            "qualification_sha256": "6" * 64,
            "detector_sha256": {"supply-chain": "7" * 64, "osv": "8" * 64},
        },
    )

    assert receipt["verdict"] == "PASS"
    runtime_argv = next(argv for argv in commands if argv[0] == str(runtime_tool))
    assert old_payload.name not in " ".join(runtime_argv)
    assert "hermes_agent-1.2.3-py3-none-any.whl" in " ".join(runtime_argv)
    assert len(receipt["installed_updater_source_identity"]) == 64
    assert len(receipt["installed_updater_wheel_identity"]) == 64
    assert receipt["installed_updater_source_identity"] != receipt["installed_updater_wheel_identity"]
    assert builder.build_verified(
        source_repo=source,
        source_commit=commit,
        source_tree=tree,
        output=tmp_path / "artifact",
        evidence={
            "qualification_sha256": "6" * 64,
            "detector_sha256": {"supply-chain": "7" * 64, "osv": "8" * 64},
        },
    ) == receipt
    direct_url.write_bytes(b'{"url":"file:///wheel-only-drift"}\n')
    drifted_direct_url_digest = base64.urlsafe_b64encode(
        hashlib.sha256(direct_url.read_bytes()).digest()
    ).rstrip(b"=").decode()
    record.write_text(
        f"aiwerk_runtime_updater/cli.py,sha256={cli_digest},{len(cli_raw)}\n"
        f"aiwerk_runtime_updater-2.24.0.dist-info/direct_url.json,sha256={drifted_direct_url_digest},{direct_url.stat().st_size}\n"
        "aiwerk_runtime_updater-2.24.0.dist-info/RECORD,,\n"
    )
    drifted_installed_identities = _installed_updater_identities(installed_updater)
    assert drifted_installed_identities[0] == original_installed_identities[0]
    assert drifted_installed_identities[1] != original_installed_identities[1]
    with pytest.raises(ArtifactError, match="installed updater wheel identity mismatch"):
        builder.build_verified(
            source_repo=source,
            source_commit=commit,
            source_tree=tree,
            output=tmp_path / "artifact",
            evidence={
                "qualification_sha256": "6" * 64,
                "detector_sha256": {"supply-chain": "7" * 64, "osv": "8" * 64},
            },
        )
    assert not any("systemctl" in item or "activate" in item for argv in commands for item in argv)
