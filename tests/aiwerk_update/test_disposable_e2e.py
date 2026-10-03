from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess

from scripts.aiwerk_update.local_e2e import DisposableSnapshotArtifactBuilder, LocalGitPublisher
from scripts.aiwerk_update.state import RunStore
from scripts.aiwerk_update.workflow import execute_update


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


def test_two_repository_disposable_e2e_reaches_handoff_without_network(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-q")
    _git(source, "checkout", "-q", "-b", "main")
    (source / ".ci/content-loss").mkdir(parents=True)
    (source / ".ci/content-loss/retirements.json").write_text(
        '{"approvals":[],"schema_version":1}\n'
    )
    (source / "pyproject.toml").write_text("[project]\nname='demo'\nversion='1'\n")
    (source / "runtime.py").write_text("VALUE = 1\n")
    _git(source, "add", ".")
    _git(source, "commit", "-q", "-m", "base")
    base = _git(source, "rev-parse", "HEAD")
    (source / "pyproject.toml").write_text("[project]\nname='demo'\nversion='2'\n")
    (source / "runtime.py").write_text("VALUE = 2\n")
    _git(source, "add", ".")
    _git(source, "commit", "-q", "-m", "candidate")
    candidate = _git(source, "rev-parse", "HEAD")

    fork = tmp_path / "fork.git"
    subprocess.run(["git", "init", "--bare", "-q", str(fork)], check=True)
    _git(source, "push", str(fork), f"{base}:refs/heads/main")
    store = RunStore.create(
        tmp_path / "runs",
        run_id="two-repo",
        request={"through": "local-handoff", "source_publication": True},
        authority={"base_commit": base, "target_commit": candidate},
    )
    store.record_preflight({"verdict": "PASS", "failures": []})

    handoff = execute_update(
        store=store,
        candidate_repo=source,
        base_commit=base,
        candidate_commit=candidate,
        upstream_commit=candidate,
        protected_controls={"pyproject.toml"},
        publisher=LocalGitPublisher(fork=fork, workspace=tmp_path / "merges"),
        artifact_builder=DisposableSnapshotArtifactBuilder(),
        qualifier=LocalQualifier(),
        approval_not_before=datetime(2026, 10, 3, tzinfo=timezone.utc),
        approval_expires_at=datetime(2026, 10, 10, tzinfo=timezone.utc),
    )

    assert handoff["status"] == "HANDOFF_READY"
    assert handoff["activation"].startswith("NOT_RUN")
    publication = json.loads((store.root / "publication.json").read_text())
    assert [publication[name]["pr_url"] for name in ("approval", "control", "product")] == [
        "local://approval/1",
        "local://control/2",
        "local://product/3",
    ]
    assert _git(source, "ls-remote", str(fork), "refs/heads/main").split()[0] == publication[
        "product"
    ]["merged_commit"]
    assert json.loads((store.root / "state.json").read_text())["phase"] == "HANDOFF_READY"
