from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import venv

import pytest

from scripts.aiwerk_update.artifact import (
    _installed_identity_receipt_path,
    _installed_updater_identities,
)
from scripts.aiwerk_update import cli as cli_module
from scripts.aiwerk_update.cli import build_parser, main
from scripts.aiwerk_update.contract import canonical_bytes
from scripts.aiwerk_update.source import capture_git_authority
from scripts.aiwerk_update.state import RunStore, StateError


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
    with pytest.raises(StateError, match="post-failure recovery"):
        store.finish_handoff(
            {
                "status": "HANDOFF_READY",
                "activation": "NOT_RUN_REQUIRES_SEPARATE_ATTILA_GO_AND_JEROME",
                "installed_updater_source_identity": "1" * 64,
                "installed_updater_wheel_identity": "2" * 64,
                "installed_update_check_receipt_sha256": "3" * 64,
                "extracted_target_preflight_receipt_sha256": "4" * 64,
                "recovery_receipt_sha256": "5" * 64,
            }
        )

    rc = main(["evidence", "--run-root", str(store.root)])
    evidence = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert evidence["phase"] == "EXECUTING"
    assert evidence["manifest_present"] is False
    assert "final.json" not in evidence["verified_files"]
    parsed = build_parser().parse_args(["resume", "--run-root", str(store.root)])
    assert parsed.command == "resume"

    (store.root / "manifest.sha256").write_text("")
    failed = main(["evidence", "--run-root", str(store.root)])
    assert failed == 2
    assert "manifest" in capsys.readouterr().err


def _producer_fixture(tmp_path: Path, *, available: bool = True) -> list[str]:
    run_root = tmp_path / "run"
    RunStore.create(
        tmp_path,
        run_id="run",
        request={"through": "local-handoff", "source_publication": True},
        authority={"base_commit": "a" * 40, "target_commit": "b" * 40},
    )
    artifact_root = tmp_path / "artifact"
    runtime = artifact_root / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "artifact-manifest.json").write_text("{}\n")
    publication = canonical_bytes({"schema": 1, "kind": "aiwerk-publication"})
    (artifact_root / "publication.json").write_bytes(publication)
    verification = {
        "schema_version": 1,
        "kind": "AIWERK_IMMUTABLE_ARTIFACT_VERIFICATION",
        "verdict": "PASS",
        "artifact_root": str(artifact_root.resolve()),
        "source_commit": "a" * 40,
        "source_git_tree": "b" * 40,
        "source_inventory_sha256": "c" * 64,
        "release_id": "a" * 40,
        "release_manifest_sha256": "d" * 64,
        "archive_sha256": "e" * 64,
        "archive_size": 123,
        "installed_updater_source_identity": "1" * 64,
        "installed_updater_wheel_identity": "2" * 64,
    }
    (run_root / "artifact.json").write_bytes(canonical_bytes(verification))
    target_config = tmp_path / "target.json"
    predecessor_config = tmp_path / "predecessor.json"
    target_config.write_bytes(canonical_bytes({"migration_class": "forward_only"}))
    predecessor_config.write_bytes(canonical_bytes({"migration_class": "forward_only"}))
    predecessor_root = tmp_path / "predecessor"
    predecessor_root.mkdir()
    native = tmp_path / "native-python"
    status = "AVAILABLE" if available else "PASS"
    native.write_text(
        f"#!{sys.executable}\n"
        "import json,sys\n"
        "args=sys.argv\n"
        "if 'aiwerk_runtime_updater.cli' in args:\n"
        f" print(json.dumps({{'status':'{status}','release_id':'{'a' * 40}','archive_sha256':'{'e' * 64}','archive_size':123,'migration_class':'forward_only'}},sort_keys=True,separators=(',',':')))\n"
        "elif 'aiwerk_runtime_updater.artifact_preflight' in args:\n"
        " print('Hermes Agent v0.21.1')\n"
        "elif 'aiwerk_runtime_updater.systemd_bridge_render' in args:\n"
        " pass\n"
        "else:\n"
        " raise SystemExit(9)\n"
    )
    native.chmod(0o755)
    return [
        "prove-handoff",
        "--run-root",
        str(run_root),
        "--updater-python",
        str(native),
        "--config",
        str(target_config),
        "--artifact-root",
        str(artifact_root),
        "--predecessor-config",
        str(predecessor_config),
        "--predecessor-root",
        str(predecessor_root),
    ]


def _bound_producer_fixture(tmp_path: Path) -> list[str]:
    tmp_path.mkdir()
    argv = _producer_fixture(tmp_path)
    updater_root = tmp_path / "installed-updater"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(updater_root)
    site_packages = next(updater_root.glob("lib/python*/site-packages"))
    package = site_packages / "aiwerk_runtime_updater"
    package.mkdir()
    (package / "__init__.py").write_text("\n")
    (package / "source.py").write_text(
        "def source_identity(): return ('" + "1" * 64 + "', b'source')\n"
        "def installed_wheel_identity(): return ('" + "2" * 64 + "', b'wheel')\n"
    )
    (package / "cli.py").write_text(
        "import json\n"
        "if __name__ == '__main__':\n"
        " print(json.dumps({'status':'AVAILABLE','release_id':'"
        + "a" * 40
        + "','archive_sha256':'"
        + "e" * 64
        + "','archive_size':123,'migration_class':'forward_only'},sort_keys=True,separators=(',',':')))\n"
    )
    (package / "artifact_preflight.py").write_text(
        "if __name__ == '__main__': print('Hermes Agent v0.21.1')\n"
    )
    (package / "systemd_bridge_render.py").write_text("\n")
    dist_info = site_packages / "aiwerk_runtime_updater-2.24.0.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: aiwerk-runtime-updater\nVersion: 2.24.0\n"
    )
    (dist_info / "RECORD").write_text("aiwerk_runtime_updater-2.24.0.dist-info/RECORD,,\n")
    updater_index = argv.index("--updater-python")
    del argv[updater_index : updater_index + 2]
    argv.extend(("--installed-updater-root", str(updater_root)))
    return argv


def _builder_bound_producer_fixture(tmp_path: Path) -> list[str]:
    argv = _bound_producer_fixture(tmp_path)
    updater_root = Path(argv[argv.index("--installed-updater-root") + 1])
    site_packages = next(updater_root.glob("lib/python*/site-packages"))
    package = site_packages / "aiwerk_runtime_updater"
    dist_info = site_packages / "aiwerk_runtime_updater-2.24.0.dist-info"
    (package / "source.py").write_text("# identity is measured by trusted artifact code\n")
    members = [
        path
        for path in sorted([*package.glob("*.py"), dist_info / "METADATA"])
        if path.is_file()
    ]
    rows = []
    for path in members:
        raw = path.read_bytes()
        digest = base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).rstrip(b"=").decode()
        rows.append(
            f"{path.relative_to(site_packages).as_posix()},sha256={digest},{len(raw)}"
        )
    rows.append("aiwerk_runtime_updater-2.24.0.dist-info/RECORD,,")
    (dist_info / "RECORD").write_text("\n".join(rows) + "\n")
    source_identity, wheel_identity = _installed_updater_identities(updater_root)
    run_root = Path(argv[argv.index("--run-root") + 1])
    artifact_path = run_root / "artifact.json"
    verification = json.loads(artifact_path.read_text())
    identity_receipt_path = _installed_identity_receipt_path(
        Path(verification["artifact_root"])
    )
    identity_receipt = {
        "schema_version": 1,
        "kind": "AIWERK_INSTALLED_UPDATER_IDENTITY",
        "artifact_root": verification["artifact_root"],
        "installed_updater_root": str(updater_root.resolve()),
        "source_commit": verification["source_commit"],
        "source_git_tree": verification["source_git_tree"],
        "installed_updater_source_identity": source_identity,
        "installed_updater_wheel_identity": wheel_identity,
    }
    identity_raw = canonical_bytes(identity_receipt)
    identity_receipt_path.write_bytes(identity_raw)
    verification.update(
        installed_updater_source_identity=source_identity,
        installed_updater_wheel_identity=wheel_identity,
        installed_updater_root=str(updater_root.resolve()),
        installed_updater_identity_receipt_path=str(identity_receipt_path.resolve()),
        installed_updater_identity_receipt_sha256=hashlib.sha256(identity_raw).hexdigest(),
    )
    artifact_path.write_bytes(canonical_bytes(verification))
    store = RunStore.open(run_root)
    store.record_preflight({"verdict": "PASS", "failures": []})
    store.begin_execution()
    for stage in ("control", "product", "publication"):
        store.complete_stage(stage)
    store.bind_artifact_verification(verification)
    store.complete_stage("artifact")
    return argv


@pytest.mark.live_system_guard_bypass
def test_prove_handoff_rejects_self_attested_fake_installed_package(
    tmp_path: Path, capsys
) -> None:
    argv = _bound_producer_fixture(tmp_path / "fake")

    rc = main(argv)

    assert rc == 2
    assert "builder-bound" in capsys.readouterr().err


@pytest.mark.live_system_guard_bypass
def test_prove_handoff_accepts_builder_bound_independently_measured_root(
    tmp_path: Path, capsys
) -> None:
    argv = _builder_bound_producer_fixture(tmp_path / "bound-root")

    rc = main(argv)
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    output = json.loads(captured.out)
    run_root = Path(argv[argv.index("--run-root") + 1])

    assert rc == 0
    assert output["status"] == "HANDOFF_BLOCKED"
    for name in (
        "installed-updater-identity-probe.stdout",
        "installed-update-check.stdout",
        "extracted-target-preflight.stdout",
        "target-systemd-bridge.stdout",
        "predecessor-systemd-bridge.stdout",
    ):
        path = run_root / name
        assert path.is_file()
        assert not path.is_symlink()


@pytest.mark.live_system_guard_bypass
def test_prove_handoff_rejects_caller_forged_builder_identity_receipt(
    tmp_path: Path, capsys
) -> None:
    argv = _builder_bound_producer_fixture(tmp_path / "forged-binding")
    run_root = Path(argv[argv.index("--run-root") + 1])
    artifact_path = run_root / "artifact.json"
    verification = json.loads(artifact_path.read_text())
    original = Path(verification["installed_updater_identity_receipt_path"])
    forged = tmp_path / "caller-created-identity.json"
    forged.write_bytes(original.read_bytes())
    verification["installed_updater_identity_receipt_path"] = str(forged.resolve())
    verification["installed_updater_identity_receipt_sha256"] = hashlib.sha256(
        forged.read_bytes()
    ).hexdigest()
    artifact_path.write_bytes(canonical_bytes(verification))

    rc = main(argv)

    assert rc == 2
    assert "artifact verification hash mismatch" in capsys.readouterr().err


def test_cli_resume_reports_blocked_recovery(tmp_path: Path, capsys, monkeypatch) -> None:
    class BoundStore:
        root = tmp_path
        run_id = "resume-blocked"

        @staticmethod
        def _read_state() -> dict[str, str]:
            return {
                "candidate_sha256": "1" * 64,
                "execution_config_sha256": "2" * 64,
                "qualified_commit": "3" * 40,
                "qualified_tree": "4" * 40,
                "phase": "HANDOFF_BLOCKED_RECOVERY",
            }

    monkeypatch.setattr(cli_module.RunStore, "open", lambda _root: BoundStore())
    monkeypatch.setattr(cli_module, "_read_object", lambda _path: {})
    monkeypatch.setattr(cli_module, "_execution_config", lambda _value: {})
    monkeypatch.setattr(
        cli_module,
        "_continue",
        lambda _store, _config: {
            "status": "HANDOFF_BLOCKED_RECOVERY",
            "completion": False,
            "activation": "NOT_RUN",
        },
    )

    rc = main(["resume", "--run-root", str(tmp_path)])
    output = json.loads(capsys.readouterr().out)

    assert rc == 1
    assert output["verdict"] == "BLOCKED"
    assert output["state"] == "HANDOFF_BLOCKED_RECOVERY"


@pytest.mark.live_system_guard_bypass
def test_prove_handoff_requires_exact_installed_updater_interpreter_and_package(
    tmp_path: Path, capsys
) -> None:
    argv = _producer_fixture(tmp_path)

    rc = main(argv)
    captured = capsys.readouterr()
    run_root = Path(argv[argv.index("--run-root") + 1])

    assert rc == 2
    assert "installed updater root" in captured.err
    assert not (run_root / "installed-update-check.json").exists()
    assert not (run_root / "extracted-target-preflight.json").exists()
    assert not (run_root / "recovery-proof.json").exists()

    bound_argv = _builder_bound_producer_fixture(tmp_path / "bound")
    bound_rc = main(bound_argv)
    bound_output = json.loads(capsys.readouterr().out)
    bound_run_root = Path(bound_argv[bound_argv.index("--run-root") + 1])
    assert bound_rc == 0
    assert bound_output["status"] == "HANDOFF_BLOCKED"
    assert bound_output["reason"] == "EXECUTABLE_POSTFAILURE_PREDECESSOR_RECOVERY_UNPROVEN"
    check = json.loads((bound_run_root / "installed-update-check.json").read_bytes())
    target = json.loads((bound_run_root / "extracted-target-preflight.json").read_bytes())
    recovery = json.loads((bound_run_root / "recovery-proof.json").read_bytes())
    assert check["installed_updater_root"].endswith("/installed-updater")
    assert len(check["identity_probe_stdout_sha256"]) == 64
    assert len(check["stdout_sha256"]) == 64
    assert target["version_output"] == "Hermes Agent v0.21.1"
    assert len(target["stdout_sha256"]) == 64
    assert recovery["status"] == "BLOCKED"
    assert recovery["disposition"] == "FORWARD_ONLY_POSTFAILURE_PREDECESSOR_RECOVERY_UNPROVEN"


@pytest.mark.live_system_guard_bypass
def test_cli_rejects_synthetic_or_mismatched_native_receipts(
    tmp_path: Path, capsys
) -> None:
    argv = _producer_fixture(tmp_path, available=False)

    rc = main(argv)
    run_root = Path(argv[argv.index("--run-root") + 1])

    assert rc == 2
    assert "installed updater root" in capsys.readouterr().err
    assert not (run_root / "installed-update-check.json").exists()
    assert not (run_root / "extracted-target-preflight.json").exists()
    assert not (run_root / "recovery-proof.json").exists()
