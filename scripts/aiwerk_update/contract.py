"""Stable updater contracts.

The authority represented here is intentionally independent of GitHub PR numbers.
GitHub identities are observations written by publication receipts, not approval
inputs.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping


_SHA40 = frozenset("0123456789abcdef")


class ContractError(ValueError):
    """Raised when an updater authority object is not exact and canonical."""


class TransitionClass(str, Enum):
    APPROVAL_CONTROL_ONLY = "APPROVAL_CONTROL_ONLY"
    CONTROL_ONLY = "CONTROL_ONLY"
    PRODUCT_ONLY = "PRODUCT_ONLY"
    ARTIFACT_HANDOFF = "ARTIFACT_HANDOFF"


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode(
        "utf-8"
    )


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _is_oid(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 40 and set(value) <= _SHA40


def _require_exact_fields(raw: Mapping[str, Any], required: set[str], label: str) -> None:
    actual = set(raw)
    if actual != required:
        raise ContractError(
            f"{label} unknown fields or missing fields: missing={sorted(required - actual)} "
            f"unknown={sorted(actual - required)}"
        )


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


@dataclass(frozen=True, slots=True)
class TransitionChange:
    path: str
    old_blob: str | None
    new_blob: str | None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "TransitionChange":
        _require_exact_fields(raw, {"path", "old_blob", "new_blob"}, "change")
        path = raw["path"]
        if (
            not isinstance(path, str)
            or not path
            or Path(path).is_absolute()
            or ".." in Path(path).parts
        ):
            raise ContractError("change path must be a confined relative path")
        old_blob = raw["old_blob"]
        new_blob = raw["new_blob"]
        if old_blob is not None and not _is_oid(old_blob):
            raise ContractError(f"change {path} has invalid old_blob")
        if new_blob is not None and not _is_oid(new_blob):
            raise ContractError(f"change {path} has invalid new_blob")
        if old_blob is None and new_blob is None:
            raise ContractError(f"change {path} cannot be absent on both sides")
        if old_blob == new_blob:
            raise ContractError(f"change {path} does not change content")
        return cls(path=path, old_blob=old_blob, new_blob=new_blob)

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "old_blob": self.old_blob, "new_blob": self.new_blob}


@dataclass(frozen=True, slots=True)
class Approval:
    schema_version: int
    repository: str
    transition_class: TransitionClass
    base_tree: str
    target_tree: str
    changes: tuple[TransitionChange, ...]
    transition_digest: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Approval":
        required = {
            "schema_version",
            "repository",
            "transition_class",
            "base_tree",
            "target_tree",
            "changes",
        }
        supplied_digest = raw.get("transition_digest")
        authority_raw = {key: value for key, value in raw.items() if key != "transition_digest"}
        _require_exact_fields(authority_raw, required, "approval")
        if supplied_digest is not None and (
            not isinstance(supplied_digest, str) or len(supplied_digest) != 64
        ):
            raise ContractError("approval transition_digest is invalid")
        if raw["schema_version"] != 1:
            raise ContractError("unsupported approval schema_version")
        repository = raw["repository"]
        if repository != "AIWerk/hermes-agent":
            raise ContractError("approval repository must be AIWerk/hermes-agent")
        try:
            transition_class = TransitionClass(raw["transition_class"])
        except (TypeError, ValueError) as exc:
            raise ContractError("invalid transition_class") from exc
        if not _is_oid(raw["base_tree"]) or not _is_oid(raw["target_tree"]):
            raise ContractError("approval trees must be lowercase 40-hex object ids")
        if raw["base_tree"] == raw["target_tree"]:
            raise ContractError("approval target_tree must differ from base_tree")
        raw_changes = raw["changes"]
        if not isinstance(raw_changes, list) or not raw_changes:
            raise ContractError("approval changes must be a non-empty list")
        changes = tuple(TransitionChange.from_dict(item) for item in raw_changes)
        paths = [item.path for item in changes]
        if len(paths) != len(set(paths)):
            raise ContractError("approval change paths must be unique")
        if paths != sorted(paths):
            raise ContractError("approval change paths must be sorted")
        authority = {
            "schema_version": 1,
            "repository": repository,
            "transition_class": transition_class.value,
            "base_tree": raw["base_tree"],
            "target_tree": raw["target_tree"],
            "changes": [item.to_dict() for item in changes],
        }
        transition_digest = canonical_sha256(authority)
        if supplied_digest is not None and supplied_digest != transition_digest:
            raise ContractError("approval transition digest mismatch")
        return cls(
            schema_version=1,
            repository=repository,
            transition_class=transition_class,
            base_tree=raw["base_tree"],
            target_tree=raw["target_tree"],
            changes=changes,
            transition_digest=transition_digest,
        )

    def authority_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "repository": self.repository,
            "transition_class": self.transition_class.value,
            "base_tree": self.base_tree,
            "target_tree": self.target_tree,
            "changes": [item.to_dict() for item in self.changes],
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.authority_dict(), "transition_digest": self.transition_digest}

    def write(self, path: Path) -> None:
        _atomic_write(path, canonical_bytes(self.to_dict()))
