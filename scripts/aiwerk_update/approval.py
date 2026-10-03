"""Build append-only content-bound guard approvals."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Iterable

from .contract import TransitionChange, canonical_bytes


LEDGER_PATH = ".ci/content-loss/retirements.json"


class ApprovalBuildError(ValueError):
    pass


def _utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ApprovalBuildError("approval timestamps must include timezone")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _binding_id(value: dict[str, Any]) -> str:
    digest = hashlib.sha256(canonical_bytes(value)).hexdigest()
    return f"CONTENT-{digest[:24].upper()}"


def append_content_approvals(
    ledger: dict[str, Any],
    *,
    changes: Iterable[TransitionChange],
    protected_controls: set[str],
    not_before: datetime,
    expires_at: datetime,
    reason: str,
) -> dict[str, Any]:
    if not isinstance(reason, str) or not reason.strip():
        raise ApprovalBuildError("approval reason is required")
    start = _utc(not_before)
    end = _utc(expires_at)
    if not_before.astimezone(timezone.utc) >= expires_at.astimezone(timezone.utc):
        raise ApprovalBuildError("approval validity interval must be positive")
    schema = ledger.get("schema_version")
    if schema == 1 and set(ledger) == {"schema_version", "approvals"}:
        legacy = ledger["approvals"]
        existing: list[dict[str, Any]] = []
    elif schema == 2 and set(ledger) == {
        "schema_version",
        "approvals",
        "transition_approvals",
    }:
        legacy = ledger["approvals"]
        existing = ledger["transition_approvals"]
    else:
        raise ApprovalBuildError("unsupported approval ledger schema")
    if not isinstance(legacy, list) or not isinstance(existing, list):
        raise ApprovalBuildError("approval ledger collections must be lists")
    result_transitions = [dict(item) for item in existing]
    for change in sorted(changes, key=lambda item: item.path):
        if change.path == LEDGER_PATH:
            raise ApprovalBuildError("approval ledger change must be published separately")
        if change.path in protected_controls:
            if change.old_blob is None or change.new_blob is None:
                raise ApprovalBuildError("control transition requires exact old and new blob")
            transition_class = "control-transition"
            target_state = f"blob:{change.new_blob}"
        else:
            if change.old_blob is None or change.new_blob is not None:
                raise ApprovalBuildError("non-control approval must be an exact path retirement")
            transition_class = "path-retirement"
            target_state = "absent"
        binding = (transition_class, change.path, change.old_blob, target_state)
        matches = [
            item
            for item in result_transitions
            if (
                item.get("transition_class"),
                item.get("subject_path"),
                item.get("expected_old_blob"),
                item.get("allowed_target_state"),
            )
            == binding
        ]
        exact_window = [
            item
            for item in matches
            if item.get("not_before") == start and item.get("expires_at") == end
        ]
        if exact_window:
            continue
        for item in matches:
            previous_start = datetime.fromisoformat(str(item["not_before"]).replace("Z", "+00:00"))
            previous_end = datetime.fromisoformat(str(item["expires_at"]).replace("Z", "+00:00"))
            if max(previous_start, not_before.astimezone(timezone.utc)) < min(
                previous_end, expires_at.astimezone(timezone.utc)
            ):
                raise ApprovalBuildError("renewed approval window overlaps an existing binding")
        authority = {
            "transition_class": transition_class,
            "subject_path": change.path,
            "expected_old_blob": change.old_blob,
            "allowed_target_state": target_state,
            "not_before": start,
            "expires_at": end,
        }
        result_transitions.append(
            {
                "id": _binding_id(authority),
                "transition_class": transition_class,
                "subject_path": change.path,
                "expected_old_blob": change.old_blob,
                "allowed_target_state": target_state,
                "not_before": start,
                "expires_at": end,
                "reason": reason.strip(),
            }
        )
    return {
        "schema_version": 2,
        "approvals": json.loads(json.dumps(legacy)),
        "transition_approvals": result_transitions,
    }
