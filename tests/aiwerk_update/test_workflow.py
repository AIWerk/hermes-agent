from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from datetime import datetime, timezone

import pytest

from scripts.aiwerk_update.release import MANIFEST, inventory_release, verify_release
from scripts.aiwerk_update.state import RunStore, StateError
from scripts.aiwerk_update.workflow import PublicationResult, execute_update


def _git(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Updater E2E",
        "GIT_AUTHOR_EMAIL": "noreply@github.com",
        "GIT_COMMITTER_NAME": "Updater E2E",
        "GIT_COMMITTER_EMAIL": "noreply@github.com",
    }
    result = subprocess.run(
        ["git", *args], cwd=repo, env=env, text=True, capture_output=True, check=True
    )
    return result.stdout.strip()


def _candidate(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "candidate"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "checkout", "-q", "-b", "main")
    (repo / "pyproject.toml").write_text("[project]\nname='demo'\nversion='1'\n")
    (repo / "runtime.py").write_text("VALUE = 1\n")
    (repo / ".ci/content-loss").mkdir(parents=True)
    (repo / ".ci/content-loss/retirements.json").write_text(
        '{"approvals":[],"schema_version":1}\n'
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "pyproject.toml").write_text("[project]\nname='demo'\nversion='2'\n")
    (repo / "runtime.py").write_text("VALUE = 2\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "full upstream-first candidate")
    return repo, base, _git(repo, "rev-parse", "HEAD")


class LocalPublisher:
    def __init__(self) -> None:
        self.stages: list[tuple[str, tuple[str, ...]]] = []

    def publish(
        self,
        *,
        stage: str,
        repo: Path,
        base_commit: str,
        candidate_commit: str,
        changed_paths: tuple[str, ...],
    ) -> PublicationResult:
        assert _git(repo, "show", "-s", "--format=%P", candidate_commit) == base_commit
        self.stages.append((stage, changed_paths))
        return PublicationResult(
            stage=stage,
            pr_number=len(self.stages),
            pr_url=f"local://pull/{len(self.stages)}",
            head_commit=candidate_commit,
            merged_commit=candidate_commit,
            merged_tree=_git(repo, "rev-parse", f"{candidate_commit}^{{tree}}"),
            check_evidence={"supply-chain": "7" * 64, "osv": "8" * 64},
        )


class SourceArtifactBuilder:
    def build_verified(
        self,
        *,
        source_repo: Path,
        source_commit: str,
        source_tree: str,
        output: Path,
        evidence: dict,
    ) -> dict:
        assert set(evidence["detector_sha256"]) == {"supply-chain", "osv"}
        output.mkdir()
        payload = subprocess.run(
            ["git", "show", f"{source_commit}:runtime.py"],
            cwd=source_repo,
            check=True,
            stdout=subprocess.PIPE,
        ).stdout
        (output / "runtime.py").write_bytes(payload)
        manifest = {
            "schema_version": 1,
            "source_commit": source_commit,
            "source_tree": source_tree,
            "inventory": inventory_release(output),
        }
        (output / MANIFEST).write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
        )
        receipt = verify_release(
            output,
            source_repo=source_repo,
            expected_commit=source_commit,
            expected_tree=source_tree,
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
        return receipt


class LocalQualifier:
    def qualify(
        self,
        *,
        repo: Path,
        base_commit: str,
        candidate_commit: str,
        upstream_commit: str,
        output_dir: Path,
    ) -> dict:
        return {
            "schema_version": 1,
            "kind": "AIWERK_UPDATE_QUALIFICATION",
            "verdict": "PASS",
            "results": [],
            "failures": [],
            "qualified_base_commit": base_commit,
            "qualified_commit": candidate_commit,
            "qualified_tree": _git(repo, "rev-parse", f"{candidate_commit}^{{tree}}"),
            "upstream_commit": upstream_commit,
        }


def test_execute_update_runs_control_then_product_and_stops_at_handoff(tmp_path: Path) -> None:
    repo, base, candidate = _candidate(tmp_path)
    store = RunStore.create(
        tmp_path / "runs",
        run_id="e2e",
        request={"through": "local-handoff", "source_publication": True},
        authority={"base_commit": base, "target_commit": candidate},
    )
    store.record_preflight({"verdict": "PASS", "failures": []})
    publisher = LocalPublisher()

    result = execute_update(
        store=store,
        candidate_repo=repo,
        base_commit=base,
        candidate_commit=candidate,
        upstream_commit=candidate,
        protected_controls={"pyproject.toml"},
        publisher=publisher,
        artifact_builder=SourceArtifactBuilder(),
        qualifier=LocalQualifier(),
        approval_not_before=datetime(2026, 10, 2, tzinfo=timezone.utc),
        approval_expires_at=datetime(2026, 10, 9, tzinfo=timezone.utc),
    )

    assert publisher.stages == [
        ("approval", (".ci/content-loss/retirements.json",)),
        ("control", ("pyproject.toml",)),
        ("product", ("runtime.py",)),
    ]
    assert result["status"] == "FIXTURE_ONLY"
    assert result["kind"] == "AIWERK_LOCAL_HANDOFF_FIXTURE"
    assert result["activation"] == "NOT_RUN_FIXTURE_ONLY"
    assert json.loads((store.root / "state.json").read_text())["phase"] == "EXECUTING"
    assert store.next_stage() == "handoff"
    assert json.loads((store.root / "publication.json").read_text())["product"]["pr_url"] == "local://pull/3"
    assert json.loads((store.root / "artifact.json").read_text())["verdict"] == "PASS"
    assert not (store.root / "manifest.sha256").exists()
    assert not (store.root / "candidate" / ".git").exists()


def test_artifact_stage_binds_verification_before_handoff(tmp_path: Path) -> None:
    repo, base, candidate = _candidate(tmp_path)
    store = RunStore.create(
        tmp_path / "runs",
        run_id="artifact-binding",
        request={"through": "local-handoff", "source_publication": True},
        authority={"base_commit": base, "target_commit": candidate},
    )
    store.record_preflight({"verdict": "PASS", "failures": []})
    execute_update(
        store=store,
        candidate_repo=repo,
        base_commit=base,
        candidate_commit=candidate,
        upstream_commit=candidate,
        protected_controls={"pyproject.toml"},
        publisher=LocalPublisher(),
        artifact_builder=SourceArtifactBuilder(),
        qualifier=LocalQualifier(),
        approval_not_before=datetime(2026, 10, 2, tzinfo=timezone.utc),
        approval_expires_at=datetime(2026, 10, 9, tzinfo=timezone.utc),
        stop_after="artifact",
    )
    artifact_path = store.root / "artifact.json"
    state = json.loads((store.root / "state.json").read_text())
    assert state["artifact_verification_sha256"] == hashlib.sha256(
        artifact_path.read_bytes()
    ).hexdigest()
    artifact = json.loads(artifact_path.read_text())
    artifact["installed_updater_source_identity"] = "9" * 64
    artifact_path.write_bytes(
        (json.dumps(artifact, sort_keys=True, separators=(",", ":")) + "\n").encode()
    )
    with pytest.raises(StateError, match="artifact verification hash mismatch"):
        RunStore.open(store.root)


def test_execute_update_resumes_after_product_publication_without_republishing(
    tmp_path: Path,
) -> None:
    repo, base, candidate = _candidate(tmp_path)
    store = RunStore.create(
        tmp_path / "runs",
        run_id="resume-e2e",
        request={"through": "local-handoff", "source_publication": True},
        authority={"base_commit": base, "target_commit": candidate},
    )
    store.record_preflight({"verdict": "PASS", "failures": []})
    publisher = LocalPublisher()
    execute_update(
        store=store,
        candidate_repo=repo,
        base_commit=base,
        candidate_commit=candidate,
        upstream_commit=candidate,
        protected_controls={"pyproject.toml"},
        publisher=publisher,
        artifact_builder=SourceArtifactBuilder(),
        qualifier=LocalQualifier(),
        approval_not_before=datetime(2026, 10, 2, tzinfo=timezone.utc),
        approval_expires_at=datetime(2026, 10, 9, tzinfo=timezone.utc),
        stop_after="publication",
    )
    assert store.next_stage() == "artifact"

    result = execute_update(
        store=RunStore.open(store.root),
        candidate_repo=repo,
        base_commit=base,
        candidate_commit=candidate,
        upstream_commit=candidate,
        protected_controls={"pyproject.toml"},
        publisher=publisher,
        artifact_builder=SourceArtifactBuilder(),
        qualifier=LocalQualifier(),
        approval_not_before=datetime(2026, 10, 2, tzinfo=timezone.utc),
        approval_expires_at=datetime(2026, 10, 9, tzinfo=timezone.utc),
    )

    assert len(publisher.stages) == 3
    assert result["status"] == "FIXTURE_ONLY"
    assert result["kind"] == "AIWERK_LOCAL_HANDOFF_FIXTURE"
    assert RunStore.open(store.root).next_stage() == "handoff"
