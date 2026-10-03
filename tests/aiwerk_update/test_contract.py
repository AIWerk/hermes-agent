from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.aiwerk_update.contract import (
    Approval,
    ContractError,
    TransitionChange,
    TransitionClass,
    canonical_sha256,
)


OID_A = "a" * 40
OID_B = "b" * 40
TREE_A = "c" * 40
TREE_B = "d" * 40


def _approval_dict() -> dict:
    return {
        "schema_version": 1,
        "repository": "AIWerk/hermes-agent",
        "transition_class": "CONTROL_ONLY",
        "base_tree": TREE_A,
        "target_tree": TREE_B,
        "changes": [
            {"path": "pyproject.toml", "old_blob": OID_A, "new_blob": OID_B}
        ],
    }


def test_approval_binds_transition_class_and_exact_tree_or_blobs() -> None:
    approval = Approval.from_dict(_approval_dict())

    assert approval.transition_class is TransitionClass.CONTROL_ONLY
    assert approval.base_tree == TREE_A
    assert approval.target_tree == TREE_B
    assert approval.changes == (
        TransitionChange(path="pyproject.toml", old_blob=OID_A, new_blob=OID_B),
    )
    assert approval.transition_digest == canonical_sha256(approval.authority_dict())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("transition_class", "PRODUCT_ONLY"),
        ("base_tree", "e" * 40),
        ("target_tree", "f" * 40),
    ],
)
def test_approval_identity_changes_digest(field: str, value: str) -> None:
    original = Approval.from_dict(_approval_dict())
    changed = _approval_dict()
    changed[field] = value

    assert Approval.from_dict(changed).transition_digest != original.transition_digest


def test_approval_schema_rejects_pr_number_and_unknown_fields() -> None:
    raw = _approval_dict()
    raw["pr_number"] = 165

    with pytest.raises(ContractError, match="unknown fields"):
        Approval.from_dict(raw)


def test_transition_digest_is_canonical_and_byte_stable() -> None:
    first = _approval_dict()
    second = json.loads(json.dumps(first, sort_keys=False))
    second["changes"] = [dict(reversed(tuple(second["changes"][0].items())))]

    assert Approval.from_dict(first).transition_digest == Approval.from_dict(second).transition_digest


def test_approval_rejects_duplicate_or_unsorted_paths() -> None:
    raw = _approval_dict()
    raw["changes"] = [
        {"path": "z.txt", "old_blob": OID_A, "new_blob": OID_B},
        {"path": "a.txt", "old_blob": OID_A, "new_blob": OID_B},
    ]

    with pytest.raises(ContractError, match="sorted"):
        Approval.from_dict(raw)

    raw["changes"] = [
        {"path": "a.txt", "old_blob": OID_A, "new_blob": OID_B},
        {"path": "a.txt", "old_blob": OID_A, "new_blob": OID_B},
    ]
    with pytest.raises(ContractError, match="unique"):
        Approval.from_dict(raw)


def test_approval_round_trip_writes_no_pr_identity(tmp_path: Path) -> None:
    approval = Approval.from_dict(_approval_dict())
    target = tmp_path / "approval.json"
    approval.write(target)
    saved = json.loads(target.read_text(encoding="utf-8"))

    assert saved["transition_digest"] == approval.transition_digest
    assert "pr_number" not in saved
    assert saved["changes"] == _approval_dict()["changes"]
    assert Approval.from_dict(saved) == approval

    saved["transition_digest"] = "0" * 64
    with pytest.raises(ContractError, match="digest mismatch"):
        Approval.from_dict(saved)
