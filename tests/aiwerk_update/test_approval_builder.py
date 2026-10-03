from __future__ import annotations

from datetime import datetime, timezone

import pytest

from scripts.aiwerk_update.approval import ApprovalBuildError, append_content_approvals
from scripts.aiwerk_update.contract import TransitionChange


A = "a" * 40
B = "b" * 40
START = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
END = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)


def test_append_content_approvals_preserves_legacy_and_has_no_pr_binding() -> None:
    legacy = {
        "schema_version": 1,
        "approvals": [
            {
                "id": "LEGACY",
                "subject_path": "old.txt",
                "expected_old_blob": A,
                "allowed_target_state": "absent",
                "valid_for_pr": 12,
                "not_before": "2026-01-01T00:00:00Z",
                "expires_at": "2026-01-02T00:00:00Z",
                "reason": "legacy",
            }
        ],
    }
    changes = [
        TransitionChange("pyproject.toml", A, B),
        TransitionChange("retired-test.py", A, None),
    ]

    result = append_content_approvals(
        legacy,
        changes=changes,
        protected_controls={"pyproject.toml", ".ci/content-loss/retirements.json"},
        not_before=START,
        expires_at=END,
        reason="upstream refresh exact transitions",
    )

    assert result["schema_version"] == 2
    assert result["approvals"] == legacy["approvals"]
    assert [row["transition_class"] for row in result["transition_approvals"]] == [
        "control-transition",
        "path-retirement",
    ]
    assert all("valid_for_pr" not in row for row in result["transition_approvals"])
    assert len({row["id"] for row in result["transition_approvals"]}) == 2


def test_append_content_approvals_is_idempotent_for_same_bindings() -> None:
    base = {"schema_version": 1, "approvals": []}
    change = TransitionChange("pyproject.toml", A, B)
    first = append_content_approvals(
        base,
        changes=[change],
        protected_controls={"pyproject.toml", ".ci/content-loss/retirements.json"},
        not_before=START,
        expires_at=END,
        reason="exact transition",
    )
    second = append_content_approvals(
        first,
        changes=[change],
        protected_controls={"pyproject.toml", ".ci/content-loss/retirements.json"},
        not_before=START,
        expires_at=END,
        reason="exact transition",
    )

    assert second == first


def test_expired_content_approval_can_be_renewed_with_non_overlapping_window() -> None:
    change = TransitionChange("pyproject.toml", A, B)
    expired = append_content_approvals(
        {"schema_version": 1, "approvals": []},
        changes=[change],
        protected_controls={"pyproject.toml", ".ci/content-loss/retirements.json"},
        not_before=datetime(2026, 9, 1, tzinfo=timezone.utc),
        expires_at=datetime(2026, 9, 2, tzinfo=timezone.utc),
        reason="first window",
    )

    renewed = append_content_approvals(
        expired,
        changes=[change],
        protected_controls={"pyproject.toml", ".ci/content-loss/retirements.json"},
        not_before=START,
        expires_at=END,
        reason="renewed window",
    )

    assert len(renewed["transition_approvals"]) == 2
    assert renewed["transition_approvals"][0]["id"] != renewed["transition_approvals"][1]["id"]


def test_append_content_approvals_rejects_wrong_transition_shape() -> None:
    with pytest.raises(ApprovalBuildError, match="control.*blob"):
        append_content_approvals(
            {"schema_version": 1, "approvals": []},
            changes=[TransitionChange("pyproject.toml", A, None)],
            protected_controls={"pyproject.toml", ".ci/content-loss/retirements.json"},
            not_before=START,
            expires_at=END,
            reason="bad",
        )
    with pytest.raises(ApprovalBuildError, match="non-control.*retirement"):
        append_content_approvals(
            {"schema_version": 1, "approvals": []},
            changes=[TransitionChange("product.py", A, B)],
            protected_controls={"pyproject.toml", ".ci/content-loss/retirements.json"},
            not_before=START,
            expires_at=END,
            reason="bad",
        )
