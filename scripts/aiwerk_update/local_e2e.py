"""Network-free executable adapters used only by disposable E2E qualification."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Any

from .release import MANIFEST, inventory_release, verify_release
from .workflow import PublicationResult


class LocalE2EError(RuntimeError):
    pass


def _git(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "AIWerk Updater E2E",
        "GIT_AUTHOR_EMAIL": "noreply@github.com",
        "GIT_COMMITTER_NAME": "AIWerk Updater E2E",
        "GIT_COMMITTER_EMAIL": "noreply@github.com",
    }
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise LocalE2EError(
            f"local E2E git failed rc={result.returncode} argv={args!r}: {result.stderr[-2000:]}"
        )
    return result.stdout.strip()


class LocalGitPublisher:
    def __init__(self, *, fork: Path, workspace: Path) -> None:
        self.fork = fork.resolve(strict=True)
        self.workspace = workspace.absolute()
        self.workspace.mkdir(parents=True, exist_ok=False)
        self.count = 0

    def publish(
        self,
        *,
        stage: str,
        repo: Path,
        base_commit: str,
        candidate_commit: str,
        changed_paths: tuple[str, ...],
    ) -> PublicationResult:
        if stage not in {"approval", "control", "product"} or not changed_paths:
            raise LocalE2EError("invalid local E2E publication")
        if _git(repo, "show", "-s", "--format=%P", candidate_commit) != base_commit:
            raise LocalE2EError("local candidate parent mismatch")
        self.count += 1
        branch = f"aiwerk/update/{stage}-{self.count}"
        _git(repo, "push", str(self.fork), f"{candidate_commit}:refs/heads/{branch}")
        merge_root = self.workspace / f"{self.count}-{stage}"
        clone = subprocess.run(
            ["git", "clone", "--no-local", "--quiet", "--no-checkout", str(self.fork), str(merge_root)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if clone.returncode != 0:
            raise LocalE2EError(f"local fork clone failed: {clone.stderr[-2000:]}")
        _git(merge_root, "checkout", "-q", "-b", "main", "origin/main")
        _git(
            merge_root,
            "merge",
            "--no-ff",
            "-m",
            f"Merge local updater {stage} #{self.count}",
            f"origin/{branch}",
        )
        merged = _git(merge_root, "rev-parse", "HEAD")
        parents = _git(merge_root, "show", "-s", "--format=%P", merged)
        if parents != f"{base_commit} {candidate_commit}":
            raise LocalE2EError("local merge parent order mismatch")
        tree = _git(merge_root, "rev-parse", "HEAD^{tree}")
        _git(merge_root, "push", str(self.fork), "HEAD:refs/heads/main")
        _git(repo, "fetch", "-q", str(self.fork), merged)
        _git(repo, "checkout", "--detach", "-q", merged)
        return PublicationResult(
            stage=stage,
            pr_number=self.count,
            pr_url=f"local://{stage}/{self.count}",
            head_commit=candidate_commit,
            merged_commit=merged,
            merged_tree=tree,
            check_evidence={
                "supply-chain": hashlib.sha256(
                    f"local-supply-chain:{merged}".encode()
                ).hexdigest(),
                "osv": hashlib.sha256(f"local-osv:{merged}".encode()).hexdigest(),
            },
        )


class DisposableSnapshotArtifactBuilder:
    """Fixture artifact builder; production code uses ExternalRuntimeArtifactBuilder."""

    def build_verified(
        self,
        *,
        source_repo: Path,
        source_commit: str,
        source_tree: str,
        output: Path,
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        if set(evidence.get("detector_sha256", {})) != {"supply-chain", "osv"}:
            raise LocalE2EError("local artifact evidence incomplete")
        output.mkdir()
        runtime = subprocess.run(
            ["git", "show", f"{source_commit}:runtime.py"],
            cwd=source_repo,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if runtime.returncode != 0:
            raise LocalE2EError("local artifact source unavailable")
        (output / "runtime.py").write_bytes(runtime.stdout)
        inventory = inventory_release(output)
        manifest = {
            "schema_version": 1,
            "source_commit": source_commit,
            "source_tree": source_tree,
            "inventory": inventory,
        }
        (output / MANIFEST).write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
        )
        return verify_release(
            output,
            source_repo=source_repo,
            expected_commit=source_commit,
            expected_tree=source_tree,
        )
