"""Executable updater workflow from qualified candidate to Local handoff."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any, Protocol

from .approval import LEDGER_PATH, append_content_approvals
from .contract import _atomic_write, canonical_bytes
from .release import write_artifact_handoff, write_local_handoff
from .source import (
    materialize_transition_commit,
    materialize_file_commit,
    measure_transition_changes,
    split_transition_changes,
)
from .state import RunStore, StateError


@dataclass(frozen=True)
class PublicationResult:
    stage: str
    pr_number: int | None
    pr_url: str | None
    head_commit: str
    merged_commit: str
    merged_tree: str
    check_evidence: dict[str, str] = field(default_factory=dict)

    def to_dict(self, *, workspace: Path, changed_paths: tuple[str, ...]) -> dict[str, Any]:
        return {
            **asdict(self),
            "workspace": str(workspace.resolve()),
            "changed_paths": list(changed_paths),
        }


class Publisher(Protocol):
    def publish(
        self,
        *,
        stage: str,
        repo: Path,
        base_commit: str,
        candidate_commit: str,
        changed_paths: tuple[str, ...],
    ) -> PublicationResult: ...


class ArtifactBuilder(Protocol):
    def build_verified(
        self,
        *,
        source_repo: Path,
        source_commit: str,
        source_tree: str,
        output: Path,
        evidence: dict[str, Any],
    ) -> dict[str, Any]: ...


class Qualifier(Protocol):
    def qualify(
        self,
        *,
        repo: Path,
        base_commit: str,
        candidate_commit: str,
        upstream_commit: str,
        output_dir: Path,
    ) -> dict[str, Any]: ...


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        raise StateError(f"workflow receipt unavailable: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise StateError(f"workflow receipt must be an object: {path}")
    return value


def _write_publication(store: RunStore, value: dict[str, Any]) -> None:
    _atomic_write(store.root / "publication.json", canonical_bytes(value))


def _prepare_operation(
    store: RunStore,
    *,
    stage: str,
    base_commit: str,
    candidate_commit: str,
    changed_paths: tuple[str, ...],
) -> None:
    operation = {
        "schema_version": 1,
        "kind": "AIWERK_UPDATE_OPERATION",
        "stage": stage,
        "base_commit": base_commit,
        "candidate_commit": candidate_commit,
        "changed_paths": list(changed_paths),
    }
    path = store.root / f"operation-{stage}.json"
    if path.is_file():
        if _read_json(path) != operation:
            raise StateError(f"persisted {stage} operation identity mismatch")
        return
    _atomic_write(path, canonical_bytes(operation))


def _operation_candidate(
    store: RunStore, *, stage: str, base_commit: str, changed_paths: tuple[str, ...]
) -> str | None:
    path = store.root / f"operation-{stage}.json"
    if not path.is_file():
        return None
    operation = _read_json(path)
    if (
        operation.get("stage") != stage
        or operation.get("base_commit") != base_commit
        or operation.get("changed_paths") != list(changed_paths)
        or not isinstance(operation.get("candidate_commit"), str)
    ):
        raise StateError(f"persisted {stage} operation identity mismatch")
    return operation["candidate_commit"]


def _materialized_or_existing(
    *,
    source_repo: Path,
    workspace: Path,
    base_commit: str,
    candidate_commit: str,
    paths: tuple[str, ...],
    message: str,
    base_repo: Path | None = None,
    existing_commit: str | None = None,
) -> dict[str, Any]:
    if not workspace.exists():
        return materialize_transition_commit(
            source_repo,
            workspace,
            base_commit=base_commit,
            candidate_commit=candidate_commit,
            paths=paths,
            message=message,
            base_repo=base_repo,
        )
    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", *args], cwd=workspace, text=True, capture_output=True, check=False
        )
        if result.returncode != 0:
            raise StateError(f"existing {workspace.name} cannot be reconciled")
        return result.stdout.strip()
    head = existing_commit or git("rev-parse", "HEAD")
    parent = git("show", "-s", "--format=%P", head)
    changed = tuple(sorted(git("diff", "--name-only", f"{base_commit}..{head}").splitlines()))
    if parent != base_commit or changed != paths:
        raise StateError(f"existing {workspace.name} identity mismatch")
    return {
        "commit": head,
        "tree": git("rev-parse", "HEAD^{tree}"),
        "parent": parent,
        "changed_paths": list(changed),
        "workspace": str(workspace),
    }


def _checkpoint(store: RunStore, stage: str, stop_after: str | None) -> dict[str, Any] | None:
    if stop_after != stage:
        return None
    return {
        "status": "PAUSED_AFTER_STAGE",
        "stage": stage,
        "run_id": store.run_id,
        "next_stage": store.next_stage(),
    }


def execute_update(
    *,
    store: RunStore,
    candidate_repo: Path,
    base_commit: str,
    candidate_commit: str,
    upstream_commit: str,
    protected_controls: set[str],
    publisher: Publisher,
    artifact_builder: ArtifactBuilder,
    qualifier: Qualifier,
    approval_not_before: datetime | None = None,
    approval_expires_at: datetime | None = None,
    stop_after: str | None = None,
) -> dict[str, Any]:
    """Continue one preflight-qualified run without repeating completed stages."""
    if stop_after not in {None, "control", "product", "publication", "artifact"}:
        raise StateError("invalid stop_after stage")
    plan = split_transition_changes(
        measure_transition_changes(candidate_repo, base_commit, candidate_commit),
        protected_controls,
    )
    approval_changes = tuple(
        change
        for change in (*plan.control, *plan.product)
        if change.path in protected_controls or change.new_blob is None
    )
    if approval_changes and (
        approval_not_before is None or approval_expires_at is None
    ):
        raise StateError("content-bound approval window is required")
    if store.next_stage() == "execute":
        store.begin_execution()

    publication_path = store.root / "publication.json"
    publication: dict[str, Any] = (
        _read_json(publication_path) if publication_path.is_file() else {"schema_version": 1}
    )

    if store.next_stage() == "control":
        current_base = base_commit
        source_repo = candidate_repo
        if approval_changes:
            assert approval_not_before is not None and approval_expires_at is not None
            ledger_probe = subprocess.run(
                ["git", "show", f"{base_commit}:{LEDGER_PATH}"],
                cwd=candidate_repo,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            if ledger_probe.returncode != 0:
                raise StateError("base approval ledger is unavailable")
            try:
                ledger = json.loads(ledger_probe.stdout)
            except json.JSONDecodeError as exc:
                raise StateError("base approval ledger is invalid JSON") from exc
            approved_ledger = append_content_approvals(
                ledger,
                changes=approval_changes,
                protected_controls=protected_controls,
                not_before=approval_not_before,
                expires_at=approval_expires_at,
                reason="AIWerk updater content-bound transition",
            )
            approval_workspace = store.root / "approval-candidate"
            prior_approval_commit = _operation_candidate(
                store,
                stage="approval",
                base_commit=base_commit,
                changed_paths=(LEDGER_PATH,),
            )
            if approval_workspace.exists():
                head = prior_approval_commit or subprocess.run(
                    ["git", "rev-parse", "HEAD"], cwd=approval_workspace,
                    text=True, capture_output=True, check=True,
                ).stdout.strip()
                parent = subprocess.run(
                    ["git", "show", "-s", "--format=%P", head], cwd=approval_workspace,
                    text=True, capture_output=True, check=True,
                ).stdout.strip()
                changed = subprocess.run(
                    ["git", "diff", "--name-only", f"{base_commit}..{head}"], cwd=approval_workspace,
                    text=True, capture_output=True, check=True,
                ).stdout.splitlines()
                ledger_bytes = subprocess.run(
                    ["git", "show", f"{head}:{LEDGER_PATH}"], cwd=approval_workspace,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
                ).stdout
                if parent != base_commit or changed != [LEDGER_PATH] or ledger_bytes != canonical_bytes(approved_ledger):
                    raise StateError("existing approval candidate identity mismatch")
                approval_candidate = {
                    "commit": head,
                    "tree": subprocess.run(
                        ["git", "rev-parse", "HEAD^{tree}"], cwd=approval_workspace,
                        text=True, capture_output=True, check=True,
                    ).stdout.strip(),
                }
            else:
                approval_candidate = materialize_file_commit(
                    candidate_repo,
                    approval_workspace,
                    base_commit=base_commit,
                    path=LEDGER_PATH,
                    content=canonical_bytes(approved_ledger),
                    message="Authorize AIWerk updater transition",
                )
            _prepare_operation(
                store,
                stage="approval",
                base_commit=base_commit,
                candidate_commit=approval_candidate["commit"],
                changed_paths=(LEDGER_PATH,),
            )
            approval_result = publisher.publish(
                stage="approval",
                repo=approval_workspace,
                base_commit=base_commit,
                candidate_commit=approval_candidate["commit"],
                changed_paths=(LEDGER_PATH,),
            )
            publication["approval"] = approval_result.to_dict(
                workspace=approval_workspace, changed_paths=(LEDGER_PATH,)
            )
            current_base = approval_result.merged_commit
            source_repo = approval_workspace
        else:
            publication["approval"] = {
                "stage": "approval",
                "status": "SKIPPED_NO_APPROVALS",
                "workspace": str(candidate_repo.resolve()),
                "changed_paths": [],
                "head_commit": base_commit,
                "merged_commit": base_commit,
                "merged_tree": None,
                "pr_number": None,
                "pr_url": None,
            }

        control_paths = tuple(change.path for change in plan.control)
        if control_paths:
            workspace = store.root / "control-candidate"
            candidate = _materialized_or_existing(
                source_repo=source_repo,
                workspace=workspace,
                base_commit=current_base,
                candidate_commit=candidate_commit,
                paths=control_paths,
                message="AIWerk updater control transition",
                existing_commit=_operation_candidate(
                    store,
                    stage="control",
                    base_commit=current_base,
                    changed_paths=control_paths,
                ),
            )
            _prepare_operation(
                store,
                stage="control",
                base_commit=current_base,
                candidate_commit=candidate["commit"],
                changed_paths=control_paths,
            )
            result = publisher.publish(
                stage="control",
                repo=workspace,
                base_commit=current_base,
                candidate_commit=candidate["commit"],
                changed_paths=control_paths,
            )
            publication["control"] = result.to_dict(
                workspace=workspace, changed_paths=control_paths
            )
        else:
            publication["control"] = {
                "stage": "control",
                "status": "SKIPPED_NO_CHANGES",
                "workspace": str(source_repo.resolve()),
                "changed_paths": [],
                "head_commit": current_base,
                "merged_commit": current_base,
                "merged_tree": None,
                "pr_number": None,
                "pr_url": None,
            }
        _write_publication(store, publication)
        store.complete_stage("control")
        paused = _checkpoint(store, "control", stop_after)
        if paused:
            return paused

    if store.next_stage() == "product":
        control = publication.get("control") or _read_json(publication_path).get("control")
        if not isinstance(control, dict):
            raise StateError("control publication receipt is missing")
        current_base = str(control["merged_commit"])
        source_repo = candidate_repo
        base_repo = Path(str(control["workspace"]))
        product_paths = tuple(change.path for change in plan.product)
        if product_paths:
            workspace = store.root / "product-candidate"
            product = _materialized_or_existing(
                source_repo=source_repo,
                workspace=workspace,
                base_commit=current_base,
                candidate_commit=candidate_commit,
                paths=product_paths,
                message="AIWerk updater product transition",
                base_repo=base_repo,
                existing_commit=_operation_candidate(
                    store,
                    stage="product",
                    base_commit=current_base,
                    changed_paths=product_paths,
                ),
            )
        else:
            workspace = base_repo
            product = {
                "commit": current_base,
                "tree": control.get("merged_tree"),
                "parent": current_base,
                "changed_paths": [],
                "workspace": str(base_repo),
            }
        product_receipt = {
            "schema_version": 1,
            **product,
            "workspace": str(workspace.resolve()),
            "changed_paths": list(product_paths),
        }
        _atomic_write(store.root / "product-candidate.json", canonical_bytes(product_receipt))
        qualification_path = store.root / "qualification.json"
        if qualification_path.is_file():
            qualification = _read_json(qualification_path)
        else:
            qualification = qualifier.qualify(
                repo=workspace,
                base_commit=current_base,
                candidate_commit=str(product["commit"]),
                upstream_commit=upstream_commit,
                output_dir=store.root,
            )
            _atomic_write(qualification_path, canonical_bytes(qualification))
        if (
            qualification.get("verdict") != "PASS"
            or qualification.get("qualified_base_commit") != current_base
            or qualification.get("qualified_commit") != product["commit"]
            or qualification.get("qualified_tree") != product["tree"]
            or qualification.get("upstream_commit") != upstream_commit
        ):
            raise StateError("post-authority candidate qualification failed or mismatched")
        store.complete_stage("product")
        paused = _checkpoint(store, "product", stop_after)
        if paused:
            return paused

    if store.next_stage() == "publication":
        control = _read_json(publication_path)["control"]
        product = _read_json(store.root / "product-candidate.json")
        product_paths = tuple(str(path) for path in product["changed_paths"])
        if product_paths:
            _prepare_operation(
                store,
                stage="product",
                base_commit=str(control["merged_commit"]),
                candidate_commit=str(product["commit"]),
                changed_paths=product_paths,
            )
            result = publisher.publish(
                stage="product",
                repo=Path(product["workspace"]),
                base_commit=str(control["merged_commit"]),
                candidate_commit=str(product["commit"]),
                changed_paths=product_paths,
            )
            publication = _read_json(publication_path)
            publication["product"] = result.to_dict(
                workspace=Path(product["workspace"]), changed_paths=product_paths
            )
        else:
            publication = _read_json(publication_path)
            publication["product"] = {
                "stage": "product",
                "status": "SKIPPED_NO_CHANGES",
                "workspace": product["workspace"],
                "changed_paths": [],
                "head_commit": product["commit"],
                "merged_commit": product["commit"],
                "merged_tree": product["tree"],
                "pr_number": None,
                "pr_url": None,
            }
        _write_publication(store, publication)
        store.complete_stage("publication")
        paused = _checkpoint(store, "publication", stop_after)
        if paused:
            return paused

    if store.next_stage() == "artifact":
        product = _read_json(publication_path)["product"]
        qualification = _read_json(store.root / "qualification.json")
        if product.get("merged_tree") != qualification.get("qualified_tree"):
            raise StateError("published product tree differs from qualified tree")
        source_repo = Path(str(product["workspace"]))
        source_commit = str(product["merged_commit"])
        source_tree = str(product["merged_tree"])
        verification = artifact_builder.build_verified(
            source_repo=source_repo,
            source_commit=source_commit,
            source_tree=source_tree,
            output=store.root / "release",
            evidence={
                "qualification_sha256": hashlib.sha256(
                    (store.root / "qualification.json").read_bytes()
                ).hexdigest(),
                "detector_sha256": product.get("check_evidence", {}),
            },
        )
        _atomic_write(store.root / "artifact.json", canonical_bytes(verification))
        store.complete_stage("artifact")
        paused = _checkpoint(store, "artifact", stop_after)
        if paused:
            return paused

    if store.next_stage() == "handoff":
        verification = _read_json(store.root / "artifact.json")
        if verification.get("kind") == "AIWERK_IMMUTABLE_ARTIFACT_VERIFICATION":
            verification = dict(verification)
            verification["proof_receipts"] = {
                "installed_update_check": str(
                    (store.root / "installed-update-check.json").resolve()
                ),
                "extracted_target_preflight": str(
                    (store.root / "extracted-target-preflight.json").resolve()
                ),
                "recovery": str((store.root / "recovery-proof.json").resolve()),
            }
            handoff = write_artifact_handoff(
                store.root / "local-handoff.json",
                artifact_root=Path(str(verification["artifact_root"])),
                verification=verification,
            )
        else:
            handoff = write_local_handoff(
                store.root / "local-handoff.json",
                release_root=Path(str(verification["release_root"])),
                verification=verification,
            )
            return handoff
        store.finish_handoff(handoff)
        return handoff

    final = store.root / "local-handoff.json"
    if final.is_file():
        return _read_json(final)
    raise StateError(f"workflow cannot continue from stage {store.next_stage()!r}")
