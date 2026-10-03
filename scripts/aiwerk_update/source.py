"""Source transition planning for the maintained updater."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
from typing import Any, Iterable, Mapping

from .contract import ContractError, TransitionChange, canonical_sha256


LEDGER_PATH = ".ci/content-loss/retirements.json"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise ContractError(
            f"git command failed rc={result.returncode} argv={args!r}: {result.stderr[-2000:]}"
        )
    return result.stdout.strip()


def _repository_identity(repo: Path) -> tuple[str, str]:
    origin_url = _git(repo, "remote", "get-url", "origin")
    normalized = origin_url.removesuffix(".git")
    if normalized == "git@github.com:AIWerk/hermes-agent":
        repository = "AIWerk/hermes-agent"
    elif normalized == "https://github.com/AIWerk/hermes-agent":
        repository = "AIWerk/hermes-agent"
    else:
        raise ContractError(
            "source repository origin must be exactly AIWerk/hermes-agent over HTTPS or SSH"
        )
    return repository, origin_url


def capture_git_authority(repo: Path, base_ref: str, target_ref: str) -> dict[str, str]:
    if not repo.is_dir():
        raise ContractError("repository path is unavailable")
    repository, origin_url = _repository_identity(repo)
    base_commit = _git(repo, "rev-parse", "--verify", f"{base_ref}^{{commit}}")
    target_commit = _git(repo, "rev-parse", "--verify", f"{target_ref}^{{commit}}")
    return {
        "repository": repository,
        "origin_url": origin_url,
        "base_ref": base_ref,
        "target_ref": target_ref,
        "base_commit": base_commit,
        "target_commit": target_commit,
        "base_tree": _git(repo, "rev-parse", f"{base_commit}^{{tree}}"),
        "target_tree": _git(repo, "rev-parse", f"{target_commit}^{{tree}}"),
        "merge_base": _git(repo, "merge-base", base_commit, target_commit),
    }


@dataclass(frozen=True, slots=True)
class OverlayManifest:
    schema_version: int
    repository: str
    upstream_commit: str
    overlay_commits: tuple[str, ...]
    digest: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "OverlayManifest":
        required = {"schema_version", "repository", "upstream_commit", "overlay_commits"}
        actual = set(raw)
        if actual != required:
            raise ContractError(
                f"overlay manifest unknown fields or missing fields: "
                f"missing={sorted(required - actual)} unknown={sorted(actual - required)}"
            )
        if raw["schema_version"] != 1 or raw["repository"] != "AIWerk/hermes-agent":
            raise ContractError("unsupported overlay manifest identity")
        upstream = raw["upstream_commit"]
        commits = raw["overlay_commits"]
        if not isinstance(upstream, str) or len(upstream) != 40 or any(
            char not in "0123456789abcdef" for char in upstream
        ):
            raise ContractError("invalid upstream commit")
        if not isinstance(commits, list) or not commits:
            raise ContractError("overlay_commits must be a non-empty list")
        if len(commits) != len(set(commits)):
            raise ContractError("overlay commits must be unique")
        if any(
            not isinstance(commit, str)
            or len(commit) != 40
            or any(char not in "0123456789abcdef" for char in commit)
            for commit in commits
        ):
            raise ContractError("invalid overlay commit")
        authority = {
            "schema_version": 1,
            "repository": raw["repository"],
            "upstream_commit": upstream,
            "overlay_commits": commits,
        }
        return cls(1, raw["repository"], upstream, tuple(commits), canonical_sha256(authority))

    @classmethod
    def read(cls, path: Path) -> "OverlayManifest":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ContractError(f"invalid overlay manifest: {exc}") from exc
        if not isinstance(raw, dict):
            raise ContractError("overlay manifest must be an object")
        return cls.from_dict(raw)


def prepare_upstream_first_candidate(
    source_repo: Path,
    workspace: Path,
    manifest: OverlayManifest,
) -> dict[str, Any]:
    if workspace.exists():
        raise ContractError("candidate workspace already exists")
    workspace.parent.mkdir(parents=True, exist_ok=True)
    clone = subprocess.run(
        ["git", "clone", "--no-local", "--quiet", str(source_repo), str(workspace)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if clone.returncode != 0:
        raise ContractError(f"candidate clone failed: {clone.stderr[-2000:]}")
    _git(workspace, "config", "user.name", "AIWerk Maintenance Agent")
    _git(workspace, "config", "user.email", "noreply@github.com")
    _git(workspace, "checkout", "--quiet", "-B", "aiwerk/update-candidate", manifest.upstream_commit)
    applied: list[str] = []
    for commit in manifest.overlay_commits:
        result = subprocess.run(
            ["git", "cherry-pick", commit],
            cwd=workspace,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if result.returncode != 0:
            conflicts = [
                path
                for path in _git(workspace, "diff", "--name-only", "--diff-filter=U").splitlines()
                if path
            ]
            return {
                "status": "CONFLICT",
                "upstream_commit": manifest.upstream_commit,
                "overlay_commits": list(manifest.overlay_commits),
                "applied_overlay_commits": applied,
                "failed_overlay_commit": commit,
                "conflicting_paths": sorted(conflicts),
                "workspace": str(workspace),
                "manifest_digest": manifest.digest,
            }
        applied.append(commit)
    baseline_path = workspace / LEDGER_PATH.replace("retirements.json", "baseline.json")
    if baseline_path.is_file() and not baseline_path.is_symlink():
        try:
            baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ContractError(f"candidate content-loss baseline is invalid: {exc}") from exc
        if not isinstance(baseline, dict):
            raise ContractError("candidate content-loss baseline must be an object")
        previous = baseline.get("accepted_upstream_commit")
        if previous != manifest.upstream_commit:
            if not isinstance(previous, str) or len(previous) != 40:
                raise ContractError("candidate accepted upstream identity is invalid")
            baseline["previous_accepted_upstream_commit"] = previous
            baseline["accepted_upstream_commit"] = manifest.upstream_commit
            baseline_path.write_text(
                json.dumps(baseline, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            _git(workspace, "add", "--", baseline_path.relative_to(workspace).as_posix())
            _git(workspace, "commit", "-q", "-m", "Advance accepted upstream authority")
    head = _git(workspace, "rev-parse", "HEAD")
    return {
        "status": "CANDIDATE_READY",
        "upstream_commit": manifest.upstream_commit,
        "overlay_commits": list(manifest.overlay_commits),
        "applied_overlay_commits": applied,
        "candidate_commit": head,
        "candidate_tree": _git(workspace, "rev-parse", "HEAD^{tree}"),
        "workspace": str(workspace),
        "manifest_digest": manifest.digest,
    }


def materialize_transition_commit(
    source_repo: Path,
    workspace: Path,
    *,
    base_commit: str,
    candidate_commit: str,
    paths: Iterable[str],
    message: str,
    base_repo: Path | None = None,
) -> dict[str, Any]:
    selected = sorted(set(paths))
    if not selected or any(
        not path or Path(path).is_absolute() or ".." in Path(path).parts
        for path in selected
    ):
        raise ContractError("transition paths must be non-empty confined relative paths")
    if workspace.exists():
        raise ContractError("transition workspace already exists")
    workspace.parent.mkdir(parents=True, exist_ok=True)
    clone = subprocess.run(
        ["git", "clone", "--no-local", "--quiet", str(source_repo), str(workspace)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if clone.returncode != 0:
        raise ContractError(f"transition clone failed: {clone.stderr[-2000:]}")
    base_probe = subprocess.run(
        ["git", "cat-file", "-e", f"{base_commit}^{{commit}}"],
        cwd=workspace,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if base_probe.returncode != 0:
        if base_repo is None:
            raise ContractError("transition base commit is absent from source repository")
        _git(workspace, "fetch", "--quiet", str(base_repo), base_commit)
    _git(workspace, "config", "user.name", "AIWerk Maintenance Agent")
    _git(workspace, "config", "user.email", "noreply@github.com")
    _git(workspace, "checkout", "--quiet", "-B", "aiwerk/update-stage", base_commit)
    for path in selected:
        probe = subprocess.run(
            ["git", "cat-file", "-e", f"{candidate_commit}:{path}"],
            cwd=workspace,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if probe.returncode == 0:
            _git(workspace, "checkout", candidate_commit, "--", path)
        else:
            subprocess.run(
                ["git", "rm", "-q", "-f", "--ignore-unmatch", "--", path],
                cwd=workspace,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
    _git(workspace, "add", "--", *selected)
    changed = [
        path
        for path in _git(workspace, "diff", "--cached", "--name-only").splitlines()
        if path
    ]
    if changed != selected:
        raise ContractError(
            f"materialized transition differs from requested paths: requested={selected!r} changed={changed!r}"
        )
    _git(workspace, "commit", "-q", "-m", message)
    head = _git(workspace, "rev-parse", "HEAD")
    return {
        "commit": head,
        "tree": _git(workspace, "rev-parse", "HEAD^{tree}"),
        "parent": _git(workspace, "show", "-s", "--format=%P", "HEAD"),
        "changed_paths": changed,
        "workspace": str(workspace),
    }


def materialize_file_commit(
    source_repo: Path,
    workspace: Path,
    *,
    base_commit: str,
    path: str,
    content: bytes,
    message: str,
) -> dict[str, Any]:
    relative = Path(path)
    if not path or relative.is_absolute() or ".." in relative.parts:
        raise ContractError("materialized file path must be confined and relative")
    if workspace.exists():
        raise ContractError("materialized file workspace already exists")
    workspace.parent.mkdir(parents=True, exist_ok=True)
    clone = subprocess.run(
        ["git", "clone", "--no-local", "--quiet", str(source_repo), str(workspace)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if clone.returncode != 0:
        raise ContractError(f"file-transition clone failed: {clone.stderr[-2000:]}")
    _git(workspace, "config", "user.name", "AIWerk Maintenance Agent")
    _git(workspace, "config", "user.email", "noreply@github.com")
    _git(workspace, "checkout", "--quiet", "-B", "aiwerk/update-approval", base_commit)
    destination = workspace / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)
    _git(workspace, "add", "--", path)
    changed = [
        item
        for item in _git(workspace, "diff", "--cached", "--name-only").splitlines()
        if item
    ]
    if changed != [path]:
        raise ContractError(f"file transition must change exactly {path}: {changed!r}")
    _git(workspace, "commit", "-q", "-m", message)
    head = _git(workspace, "rev-parse", "HEAD")
    return {
        "commit": head,
        "tree": _git(workspace, "rev-parse", "HEAD^{tree}"),
        "parent": _git(workspace, "show", "-s", "--format=%P", "HEAD"),
        "changed_paths": changed,
        "workspace": str(workspace),
    }


def measure_transition_changes(
    repo: Path, base_ref: str, target_ref: str
) -> tuple[TransitionChange, ...]:
    def tree(ref: str) -> dict[str, tuple[str, str, str]]:
        result = subprocess.run(
            ["git", "ls-tree", "-r", "-z", ref],
            cwd=repo,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode != 0:
            raise ContractError(f"cannot enumerate tree {ref}: {result.stderr.decode('utf-8', 'replace')}")
        entries: dict[str, tuple[str, str, str]] = {}
        for record in result.stdout.split(b"\0"):
            if not record:
                continue
            metadata, raw_path = record.split(b"\t", 1)
            mode, kind, oid = metadata.decode("ascii").split()
            path = raw_path.decode("utf-8")
            entries[path] = (mode, kind, oid)
        return entries

    before = tree(base_ref)
    after = tree(target_ref)
    changes: list[TransitionChange] = []
    for path in sorted(before.keys() | after.keys()):
        old = before.get(path)
        new = after.get(path)
        if old == new:
            continue
        if old is not None and old[1] != "blob":
            raise ContractError(f"non-blob base transition is unsupported: {path}")
        if new is not None and new[1] != "blob":
            raise ContractError(f"non-blob target transition is unsupported: {path}")
        changes.append(
            TransitionChange(
                path=path,
                old_blob=None if old is None else old[2],
                new_blob=None if new is None else new[2],
            )
        )
    return tuple(changes)


@dataclass(frozen=True, slots=True)
class TransitionPlan:
    control: tuple[TransitionChange, ...]
    product: tuple[TransitionChange, ...]

    @property
    def product_has_protected_controls(self) -> bool:
        control_paths = {item.path for item in self.control}
        return any(item.path in control_paths for item in self.product)


def split_transition_changes(
    changes: Iterable[TransitionChange], protected_controls: set[str]
) -> TransitionPlan:
    ordered = sorted(changes, key=lambda item: item.path)
    paths = [item.path for item in ordered]
    if len(paths) != len(set(paths)):
        raise ContractError("transition paths must be unique")
    if LEDGER_PATH in paths and len(paths) != 1:
        raise ContractError("approval ledger publication must be ledger-only")
    control = tuple(item for item in ordered if item.path in protected_controls)
    product = tuple(item for item in ordered if item.path not in protected_controls)
    return TransitionPlan(control=control, product=product)
