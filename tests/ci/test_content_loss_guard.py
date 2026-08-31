"""Adversarial contract tests for the AIWerk content-loss guard."""

from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "content_loss_guard.py"
_SPEC = importlib.util.spec_from_file_location("content_loss_guard", _SCRIPT)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError("Failed to load content_loss_guard.py")
_guard = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_guard)

ContentLossError = _guard.ContentLossError
evaluate_range = _guard.evaluate_range
main = _guard.main
make_effective_merge_tree = _guard.make_effective_merge_tree
write_reports = _guard.write_reports

_NOW = datetime(2026, 8, 31, 10, 0, tzinfo=timezone.utc)


def _git(repo: Path, *args: str, input_bytes: bytes | None = None) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "R7A Test",
        "GIT_AUTHOR_EMAIL": "r7a@example.invalid",
        "GIT_COMMITTER_NAME": "R7A Test",
        "GIT_COMMITTER_EMAIL": "r7a@example.invalid",
    }
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        env=env,
        input=input_bytes,
        capture_output=True,
        check=True,
    )
    return result.stdout.decode("utf-8").strip()


def _write(repo: Path, rel: str, content: str | bytes) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _baseline(upstream: str) -> dict:
    return {
        "schema_version": 1,
        "baseline_id": "r7a-test",
        "bootstrap": {
            "source_commit": "0" * 40,
            "source_tree": "1" * 40,
            "path_count": 1,
        },
        "accepted_upstream_commit": upstream,
        "recovery_ledger": {
            "status": "incomplete-bootstrap",
            "canonical_recovery_complete": False,
            "source_packet_sha256": "0" * 64,
            "canonical_ledger_sha256": "37517e5f3dc66819f61f5a7bb8ace1921282415f10551d2defa5c3eb0985b570",
            "total": 0,
            "deleted": 0,
            "marker_zeroed": 0,
            "entries": [],
        },
        "markers": [
            {
                "id": "aiwerk-brand",
                "needle": "aiwerk",
                "normalization": "unicode-nfkc-casefold",
                "zero_transition": "block",
                "decrease": "report",
            }
        ],
        "protected_paths": [
            {
                "id": "immutable-release-contract",
                "path": "immutable-release-build.json",
                "required": True,
            }
        ],
        "selectors": [
            {
                "id": "release-preserve-repository",
                "type": "json_pointer_equals",
                "path": "immutable-release-build.json",
                "pointer": "/build/preserve_complete_repository",
                "value": True,
            },
            {
                "id": "release-dashboard-closing-gate",
                "type": "json_array_contains_object",
                "path": "immutable-release-build.json",
                "pointer": "/closing_gate/scenarios",
                "where": {"name": "dashboard-entry"},
                "required_keys": ["startup_modules", "late_modules"],
            },
        ],
        "protected_controls": [
            ".github/workflows/aiwerk-content-loss-guard.yml",
            ".ci/content-loss/baseline.json",
            ".ci/content-loss/retirements.json",
            "scripts/ci/content_loss_guard.py",
            "tests/ci/test_content_loss_guard.py",
        ],
    }


def _contract(*, preserve: bool = True) -> str:
    return json.dumps(
        {
            "build": {"preserve_complete_repository": preserve},
            "closing_gate": {
                "scenarios": [
                    {
                        "name": "dashboard-entry",
                        "startup_modules": ["hermes_cli.config"],
                        "late_modules": ["hermes_cli.web_server"],
                    }
                ]
            },
        },
        sort_keys=True,
    ) + "\n"


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str, str]:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "R7A Test")
    _git(root, "config", "user.email", "r7a@example.invalid")

    _write(root, "upstream.txt", "upstream\n")
    _write(root, "shared-modified.txt", "upstream bytes\n")
    upstream = _commit(root, "upstream")

    _write(root, "shared-modified.txt", "fork override without brand marker\n")
    _write(root, "fork-only.txt", "AIWerk private capability\n")
    _write(root, "marker.txt", "AIWerk integration\n")
    _write(root, "immutable-release-build.json", _contract())
    _write(root, ".ci/content-loss/baseline.json", json.dumps(_baseline(upstream), sort_keys=True) + "\n")
    _write(root, ".ci/content-loss/retirements.json", '{"approvals":[],"schema_version":1}\n')
    base = _commit(root, "base")
    return root, upstream, base


def _codes(report: dict) -> set[str]:
    return set(report["reason_codes"])


def test_fork_only_file_deletion_is_blocked(repo: tuple[Path, str, str]) -> None:
    root, upstream, base = repo
    (root / "fork-only.txt").unlink()
    target = _commit(root, "delete fork capability")

    report = evaluate_range(root, base, target, upstream, pr_number=17, now=_NOW)

    assert report["verdict"] == "FAIL"
    assert "FORK_OWNED_PATH_REMOVED" in _codes(report)
    assert report["measurements"]["missing_paths"][0]["path"] == "fork-only.txt"


def test_upstream_origin_deletion_is_reported_but_not_mislabeled_as_fork_loss(
    repo: tuple[Path, str, str],
) -> None:
    root, upstream, base = repo
    (root / "upstream.txt").unlink()
    target = _commit(root, "delete upstream path")

    report = evaluate_range(root, base, target, upstream, pr_number=17, now=_NOW)

    assert report["verdict"] == "PASS"
    assert "FORK_OWNED_PATH_REMOVED" not in _codes(report)
    assert report["measurements"]["missing_paths"][0]["classification"] == "upstream-origin"


def test_upstream_existing_but_fork_modified_file_deletion_is_blocked(
    repo: tuple[Path, str, str],
) -> None:
    root, upstream, base = repo
    (root / "shared-modified.txt").unlink()
    target = _commit(root, "delete fork-modified shared path")

    report = evaluate_range(root, base, target, upstream, pr_number=17, now=_NOW)

    assert report["verdict"] == "FAIL"
    assert "FORK_OWNED_PATH_REMOVED" in _codes(report)
    finding = next(
        item
        for item in report["measurements"]["missing_paths"]
        if item["path"] == "shared-modified.txt"
    )
    assert finding["classification"] == "fork-modified"


def test_fork_modified_path_mode_or_type_transition_is_blocked(
    repo: tuple[Path, str, str],
) -> None:
    root, upstream, base = repo
    link_blob = _git(root, "hash-object", "-w", "--stdin", input_bytes=b"elsewhere.txt\n")
    _git(root, "update-index", "--add", "--cacheinfo", f"120000,{link_blob},shared-modified.txt")
    _git(root, "commit", "-qm", "replace fork-modified blob with symlink")
    target = _git(root, "rev-parse", "HEAD")

    report = evaluate_range(root, base, target, upstream, pr_number=17, now=_NOW)

    assert report["verdict"] == "FAIL"
    assert "FORK_OWNED_MODE_OR_TYPE_CHANGED" in _codes(report)
    assert report["measurements"]["mode_type_transitions"] == [
        {
            "new_mode": "120000",
            "new_type": "blob",
            "old_mode": "100644",
            "old_type": "blob",
            "path": "shared-modified.txt",
        }
    ]


def test_malformed_protected_path_record_is_authority_error(
    repo: tuple[Path, str, str],
) -> None:
    root, upstream, _base = repo
    baseline = json.loads((root / ".ci/content-loss/baseline.json").read_text(encoding="utf-8"))
    baseline["protected_paths"] = [
        {"id": "critical", "path": "shared-modified.txt", "required": "true"}
    ]
    _write(
        root,
        ".ci/content-loss/baseline.json",
        json.dumps(baseline, sort_keys=True) + "\n",
    )
    malformed_base = _commit(root, "malformed protected path authority")

    with pytest.raises(ContentLossError, match="protected path"):
        evaluate_range(
            root,
            malformed_base,
            malformed_base,
            upstream,
            pr_number=17,
            now=_NOW,
        )


def test_aiwerk_marker_zero_transition_is_blocked(repo: tuple[Path, str, str]) -> None:
    root, upstream, base = repo
    _write(root, "marker.txt", "generic integration\n")
    target = _commit(root, "erase marker")

    report = evaluate_range(root, base, target, upstream, pr_number=17, now=_NOW)

    assert report["verdict"] == "FAIL"
    assert "FORK_MARKER_ZEROED" in _codes(report)
    assert report["measurements"]["marker_zero_transitions"] == [
        {"marker_id": "aiwerk-brand", "new_count": 0, "old_count": 1, "path": "marker.txt"}
    ]


def test_candidate_retirement_cannot_authorize_its_own_deletion(
    repo: tuple[Path, str, str],
) -> None:
    root, upstream, base = repo
    old_blob = _git(root, "rev-parse", f"{base}:fork-only.txt")
    candidate_approval = {
        "schema_version": 1,
        "approvals": [
            {
                "id": "RET-SELF",
                "subject_path": "fork-only.txt",
                "expected_old_blob": old_blob,
                "allowed_target_state": "absent",
                "valid_for_pr": 17,
                "not_before": "2026-08-31T00:00:00Z",
                "expires_at": "2026-09-01T00:00:00Z",
                "reason": "self-authorized",
            }
        ],
    }
    _write(root, ".ci/content-loss/retirements.json", json.dumps(candidate_approval) + "\n")
    (root / "fork-only.txt").unlink()
    target = _commit(root, "self authorize deletion")

    report = evaluate_range(root, base, target, upstream, pr_number=17, now=_NOW)

    assert report["verdict"] == "FAIL"
    assert report["retirements_applied"] == []
    assert "FORK_OWNED_PATH_REMOVED" in _codes(report)


def test_prior_exact_retirement_allows_only_bound_path_blob_and_pr(
    repo: tuple[Path, str, str],
) -> None:
    root, upstream, base0 = repo
    old_blob = _git(root, "rev-parse", f"{base0}:fork-only.txt")
    approval = {
        "schema_version": 1,
        "approvals": [
            {
                "id": "RET-17",
                "subject_path": "fork-only.txt",
                "expected_old_blob": old_blob,
                "allowed_target_state": "absent",
                "valid_for_pr": 17,
                "not_before": "2026-08-31T00:00:00Z",
                "expires_at": "2026-09-01T00:00:00Z",
                "reason": "explicit test retirement",
            }
        ],
    }
    _write(root, ".ci/content-loss/retirements.json", json.dumps(approval, sort_keys=True) + "\n")
    base = _commit(root, "approve retirement")
    (root / "fork-only.txt").unlink()
    target = _commit(root, "apply retirement")

    report = evaluate_range(root, base, target, upstream, pr_number=17, now=_NOW)

    assert report["verdict"] == "PASS"
    assert report["retirements_applied"] == ["RET-17"]
    assert "FORK_OWNED_PATH_REMOVED" not in _codes(report)


@pytest.mark.parametrize("preserve", [False])
def test_immutable_release_semantic_selector_blocks_hollow_contract(
    repo: tuple[Path, str, str], preserve: bool
) -> None:
    root, upstream, base = repo
    _write(root, "immutable-release-build.json", _contract(preserve=preserve))
    target = _commit(root, "hollow contract")

    report = evaluate_range(root, base, target, upstream, pr_number=17, now=_NOW)

    assert report["verdict"] == "FAIL"
    assert "CAPABILITY_SELECTOR_FAILED" in _codes(report)
    assert report["selectors"]["release-preserve-repository"] == "failed"


def test_protected_regular_file_replaced_by_symlink_fails_closed(
    repo: tuple[Path, str, str]
) -> None:
    root, upstream, base = repo
    (root / "immutable-release-build.json").unlink()
    (root / "immutable-release-build.json").symlink_to("marker.txt")
    target = _commit(root, "replace contract with symlink")

    report = evaluate_range(root, base, target, upstream, pr_number=17, now=_NOW)

    assert report["verdict"] == "FAIL"
    assert "PROTECTED_PATH_TYPE_CHANGED" in _codes(report)


def test_mixed_control_plane_and_product_change_is_blocked(
    repo: tuple[Path, str, str]
) -> None:
    root, upstream, base = repo
    _write(root, ".ci/content-loss/baseline.json", json.dumps(_baseline(upstream)) + "\n\n")
    _write(root, "new-product.py", "print('product')\n")
    target = _commit(root, "mix controls and product")

    report = evaluate_range(root, base, target, upstream, pr_number=17, now=_NOW)

    assert report["verdict"] == "FAIL"
    assert "MIXED_CONTROL_AND_PRODUCT_CHANGE" in _codes(report)


def _control_approval(
    root: Path,
    base: str,
    *,
    approval_id: str,
    subject_path: str,
    target_blob: str,
    valid_for_pr: int,
) -> dict:
    return {
        "id": approval_id,
        "subject_path": subject_path,
        "expected_old_blob": _git(root, "rev-parse", f"{base}:{subject_path}"),
        "allowed_target_state": f"blob:{target_blob}",
        "valid_for_pr": valid_for_pr,
        "not_before": "2026-08-31T00:00:00Z",
        "expires_at": "2026-09-01T00:00:00Z",
        "reason": "separate exact control transition",
    }


def test_unapproved_control_plane_only_change_is_blocked(
    repo: tuple[Path, str, str]
) -> None:
    root, upstream, base = repo
    _write(root, ".ci/content-loss/baseline.json", json.dumps(_baseline(upstream)) + "\n\n")
    target = _commit(root, "unapproved control only")

    report = evaluate_range(root, base, target, upstream, pr_number=17, now=_NOW)

    assert report["verdict"] == "FAIL"
    assert "UNAPPROVED_CONTROL_PLANE_CHANGE" in _codes(report)
    assert report["control_plane_changes"] == [".ci/content-loss/baseline.json"]
    assert report["authority_ref"] == base


def test_append_only_inert_control_approval_is_allowed_as_separate_stage(
    repo: tuple[Path, str, str]
) -> None:
    root, upstream, base = repo
    future_blob = _git(root, "hash-object", "--stdin", input_bytes=b"future baseline bytes\n")
    approval = _control_approval(
        root,
        base,
        approval_id="CTRL-23",
        subject_path=".ci/content-loss/baseline.json",
        target_blob=future_blob,
        valid_for_pr=23,
    )
    _write(
        root,
        ".ci/content-loss/retirements.json",
        json.dumps({"approvals": [approval], "schema_version": 1}, sort_keys=True) + "\n",
    )
    target = _commit(root, "approve future control transition")

    report = evaluate_range(root, base, target, upstream, pr_number=17, now=_NOW)

    assert report["verdict"] == "PASS"
    assert report["control_transition_approvals_added"] == ["CTRL-23"]
    assert report["retirements_applied"] == []


def test_append_only_inert_path_retirement_is_allowed_as_separate_stage(
    repo: tuple[Path, str, str]
) -> None:
    root, upstream, base = repo
    approval = {
        "id": "RET-23",
        "subject_path": "fork-only.txt",
        "expected_old_blob": _git(root, "rev-parse", f"{base}:fork-only.txt"),
        "allowed_target_state": "absent",
        "valid_for_pr": 23,
        "not_before": "2026-08-31T00:00:00Z",
        "expires_at": "2026-09-01T00:00:00Z",
        "reason": "separate exact path retirement",
    }
    _write(
        root,
        ".ci/content-loss/retirements.json",
        json.dumps({"approvals": [approval], "schema_version": 1}, sort_keys=True) + "\n",
    )
    target = _commit(root, "approve future path retirement")

    report = evaluate_range(root, base, target, upstream, pr_number=17, now=_NOW)

    assert report["verdict"] == "PASS"
    assert report["path_retirement_approvals_added"] == ["RET-23"]
    assert report["control_transition_approvals_added"] == []


def test_append_approval_requires_timezone_bound_timestamps(
    repo: tuple[Path, str, str]
) -> None:
    root, upstream, base = repo
    approval = {
        "id": "RET-23",
        "subject_path": "fork-only.txt",
        "expected_old_blob": _git(root, "rev-parse", f"{base}:fork-only.txt"),
        "allowed_target_state": "absent",
        "valid_for_pr": 23,
        "not_before": "2026-08-31T00:00:00",
        "expires_at": "2026-09-01T00:00:00Z",
        "reason": "malformed timestamp",
    }
    _write(
        root,
        ".ci/content-loss/retirements.json",
        json.dumps({"approvals": [approval], "schema_version": 1}, sort_keys=True) + "\n",
    )
    target = _commit(root, "attempt naive approval")

    with pytest.raises(ContentLossError, match="timezone"):
        evaluate_range(root, base, target, upstream, pr_number=17, now=_NOW)


def test_prior_exact_control_approval_allows_only_bound_blob_and_pr(
    repo: tuple[Path, str, str]
) -> None:
    root, upstream, base0 = repo
    candidate_bytes = (json.dumps(_baseline(upstream), sort_keys=True) + "\n\n").encode()
    candidate_blob = _git(root, "hash-object", "--stdin", input_bytes=candidate_bytes)
    approval = _control_approval(
        root,
        base0,
        approval_id="CTRL-17",
        subject_path=".ci/content-loss/baseline.json",
        target_blob=candidate_blob,
        valid_for_pr=17,
    )
    _write(
        root,
        ".ci/content-loss/retirements.json",
        json.dumps({"approvals": [approval], "schema_version": 1}, sort_keys=True) + "\n",
    )
    base = _commit(root, "approved exact future control blob")
    _write(root, ".ci/content-loss/baseline.json", candidate_bytes)
    target = _commit(root, "apply exact control transition")

    report = evaluate_range(root, base, target, upstream, pr_number=17, now=_NOW)

    assert report["verdict"] == "PASS"
    assert report["control_transitions_applied"] == ["CTRL-17"]


def test_control_approval_with_wrong_target_blob_fails_closed(
    repo: tuple[Path, str, str]
) -> None:
    root, upstream, base0 = repo
    approved_blob = _git(root, "hash-object", "--stdin", input_bytes=b"approved bytes\n")
    approval = _control_approval(
        root,
        base0,
        approval_id="CTRL-17",
        subject_path=".ci/content-loss/baseline.json",
        target_blob=approved_blob,
        valid_for_pr=17,
    )
    _write(
        root,
        ".ci/content-loss/retirements.json",
        json.dumps({"approvals": [approval], "schema_version": 1}, sort_keys=True) + "\n",
    )
    base = _commit(root, "approve different control blob")
    _write(root, ".ci/content-loss/baseline.json", json.dumps(_baseline(upstream)) + "\n\n")
    target = _commit(root, "attempt wrong control blob")

    report = evaluate_range(root, base, target, upstream, pr_number=17, now=_NOW)

    assert report["verdict"] == "FAIL"
    assert "UNAPPROVED_CONTROL_PLANE_CHANGE" in _codes(report)
    assert report["control_transitions_applied"] == []


def test_duplicate_json_keys_in_base_authority_are_an_error(
    repo: tuple[Path, str, str]
) -> None:
    root, upstream, _base = repo
    _write(root, ".ci/content-loss/baseline.json", '{"schema_version":1,"schema_version":1}\n')
    malformed_base = _commit(root, "malformed authority")
    _write(root, "marker.txt", "still AIWerk\n")
    target = _commit(root, "candidate")

    with pytest.raises(ContentLossError, match="duplicate JSON key"):
        evaluate_range(root, malformed_base, target, upstream, pr_number=17, now=_NOW)


def test_non_utf8_replacement_of_marker_bearing_text_is_an_error(
    repo: tuple[Path, str, str]
) -> None:
    root, upstream, base = repo
    _write(root, "marker.txt", b"\xff\xfe")
    target = _commit(root, "binary replacement")

    with pytest.raises(ContentLossError, match="marker-bearing text became non-UTF-8"):
        evaluate_range(root, base, target, upstream, pr_number=17, now=_NOW)


def test_effective_merge_tree_contains_base_and_head_without_checkout(tmp_path: Path) -> None:
    root = tmp_path / "merge-repo"
    root.mkdir()
    _git(root, "init", "-q")
    _write(root, "base.txt", "base\n")
    base = _commit(root, "base")
    _git(root, "checkout", "-q", "-b", "feature")
    _write(root, "head.txt", "head\n")
    head = _commit(root, "head")

    tree = make_effective_merge_tree(root, base, head)
    paths = set(_git(root, "ls-tree", "-r", "--name-only", tree).splitlines())

    assert paths == {"base.txt", "head.txt"}


def test_reports_are_deterministic_and_end_with_newline(
    repo: tuple[Path, str, str], tmp_path: Path
) -> None:
    root, upstream, base = repo
    report = evaluate_range(root, base, base, upstream, pr_number=17, now=_NOW)
    json_out = tmp_path / "report.json"
    markdown_out = tmp_path / "report.md"

    write_reports(report, json_out=json_out, markdown_out=markdown_out)

    raw = json_out.read_bytes()
    assert raw.endswith(b"\n")
    assert raw == (json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
    assert markdown_out.read_text(encoding="utf-8").startswith("# AIWerk content-loss guard: PASS\n")


def test_workflow_fetch_preserves_candidate_ancestry_from_trusted_pr_ref() -> None:
    workflow = (
        Path(__file__).resolve().parents[2]
        / ".github/workflows/aiwerk-content-loss-guard.yml"
    ).read_text(encoding="utf-8")

    assert "refs/pull/${PR_NUMBER}/head" in workflow
    assert "git fetch --no-tags --force origin" in workflow
    assert "--depth=1" not in workflow
    assert "HEAD_REPO_URL" not in workflow
    assert 'test "$RESOLVED_HEAD" = "$HEAD_SHA"' in workflow


def test_success_report_binds_all_authority_objects_and_completeness(
    repo: tuple[Path, str, str]
) -> None:
    root, upstream, base = repo

    report = evaluate_range(root, base, base, upstream, pr_number=17, now=_NOW)

    assert report["guard_version"] == "r7a-v1"
    assert report["program_status"] == {
        "canonical_recovery_complete": False,
        "phase": "incomplete-bootstrap",
        "tracked_incident_paths": 0,
    }
    assert report["input_refs"] == {
        "active": base,
        "target": base,
        "upstream": upstream,
    }
    assert report["authority_blobs"] == {
        "baseline": _git(root, "rev-parse", f"{base}:.ci/content-loss/baseline.json"),
        "retirements": _git(root, "rev-parse", f"{base}:.ci/content-loss/retirements.json"),
    }
    assert report["completeness"] == {
        "all_selectors_evaluated": True,
        "authority_loaded": True,
        "git_objects_complete": True,
        "reports_complete": True,
        "upstream_classification_complete": True,
    }


def test_check_pr_report_binds_head_parents_and_effective_tree(
    repo: tuple[Path, str, str], tmp_path: Path
) -> None:
    root, upstream, base = repo
    _write(root, "new.py", "print('safe addition')\n")
    head = _commit(root, "head")
    expected_tree = make_effective_merge_tree(root, base, head)
    json_out = tmp_path / "pr.json"
    markdown_out = tmp_path / "pr.md"

    rc = main(
        [
            "check-pr",
            "--repo",
            str(root),
            "--base",
            base,
            "--head",
            head,
            "--upstream",
            upstream,
            "--pr-number",
            "17",
            "--json-out",
            str(json_out),
            "--markdown-out",
            str(markdown_out),
        ]
    )
    report = json.loads(json_out.read_text(encoding="utf-8"))

    assert rc == 0
    assert report["mode"] == "pull-request"
    assert report["candidate_head_sha"] == head
    assert report["candidate_head_parents"] == [base]
    assert report["effective_target"] == {"commit": None, "tree": expected_tree}


def test_cli_error_still_writes_canonical_error_envelope(
    repo: tuple[Path, str, str], tmp_path: Path
) -> None:
    root, upstream, base = repo
    json_out = tmp_path / "error.json"
    markdown_out = tmp_path / "error.md"

    rc = main(
        [
            "check-range",
            "--repo",
            str(root),
            "--active",
            base,
            "--target",
            "f" * 40,
            "--upstream",
            upstream,
            "--json-out",
            str(json_out),
            "--markdown-out",
            str(markdown_out),
        ]
    )
    report = json.loads(json_out.read_text(encoding="utf-8"))

    assert rc == 3
    assert report["verdict"] == "ERROR"
    assert report["guard_version"] == "r7a-v1"
    assert report["reason_codes"] == ["EVIDENCE_ERROR"]
    assert report["completeness"] == {
        "all_selectors_evaluated": False,
        "authority_loaded": False,
        "git_objects_complete": False,
        "reports_complete": True,
        "upstream_classification_complete": False,
    }
    assert report["input_refs"] == {
        "active": base,
        "target": "f" * 40,
        "upstream": upstream,
    }
    assert json_out.read_bytes().endswith(b"\n")
    assert markdown_out.read_text(encoding="utf-8").startswith(
        "# AIWerk content-loss guard: ERROR\n"
    )


def test_committed_baseline_binds_incomplete_135_path_recovery_ledger() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    baseline = json.loads(
        (repo_root / ".ci/content-loss/baseline.json").read_text(encoding="utf-8")
    )
    ledger = baseline["recovery_ledger"]
    entries = ledger["entries"]
    canonical = json.dumps(
        entries,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n"

    assert ledger["status"] == "incomplete-bootstrap"
    assert ledger["canonical_recovery_complete"] is False
    assert ledger["source_packet_sha256"] == (
        "8262d0e5fac12fc0b6f856f0e7e7091fe9e089e78310aec7f16dc2d120a089ec"
    )
    assert ledger["canonical_ledger_sha256"] == hashlib.sha256(canonical.encode()).hexdigest()
    assert ledger["canonical_ledger_sha256"] == (
        "b99ea555b9ee0eeac3e60ce702502b217f5aa130e89ec2c8cecb0d24b8dba894"
    )
    assert ledger["total"] == len(entries) == len({item["path"] for item in entries}) == 135
    assert ledger["deleted"] == sum(item["incident_kind"] == "deleted" for item in entries) == 100
    assert ledger["marker_zeroed"] == sum(
        item["incident_kind"] == "aiwerk_content_removed" for item in entries
    ) == 35
    assert all(set(item) == {"incident_kind", "path"} for item in entries)


def test_exact_historical_8bd_to_7b_fixture_blocks_known_losses() -> None:
    assert _guard._HISTORICAL_ACTIVE == "8bd56009c186b6e0fddc8aa96898201a42a1efb4"
    assert _guard._HISTORICAL_TARGET == "7b0cf741a009bc3c61b44f9cefef7815aab88da3"
    assert _guard._HISTORICAL_UPSTREAM == "8f2f4caff42cfce29d6fc0992b9f4157409f8d03"
    repo_root = Path(__file__).resolve().parents[2]
    required = {
        "8bd56009c186b6e0fddc8aa96898201a42a1efb4",
        "7b0cf741a009bc3c61b44f9cefef7815aab88da3",
        "8f2f4caff42cfce29d6fc0992b9f4157409f8d03",
    }
    if any(
        subprocess.run(
            ["git", "cat-file", "-e", f"{ref}^{{commit}}"],
            cwd=repo_root,
            capture_output=True,
        ).returncode
        for ref in required
    ):
        pytest.skip("historical commits unavailable in this checkout; trusted workflow fetches full history")

    report = evaluate_range(
        repo_root,
        "8bd56009c186b6e0fddc8aa96898201a42a1efb4",
        "7b0cf741a009bc3c61b44f9cefef7815aab88da3",
        "8f2f4caff42cfce29d6fc0992b9f4157409f8d03",
        pr_number=None,
        now=_NOW,
    )
    missing = {item["path"] for item in report["measurements"]["missing_paths"]}
    zeroed = {
        item["path"] for item in report["measurements"]["marker_zero_transitions"]
    }
    committed_baseline = json.loads(
        (repo_root / ".ci/content-loss/baseline.json").read_text(encoding="utf-8")
    )
    ledger_entries = committed_baseline["recovery_ledger"]["entries"]
    ledger_deleted = {
        item["path"] for item in ledger_entries if item["incident_kind"] == "deleted"
    }
    ledger_zeroed = {
        item["path"]
        for item in ledger_entries
        if item["incident_kind"] == "aiwerk_content_removed"
    }

    validator = getattr(_guard, "validate_historical_recovery_bijection", None)
    assert validator is not None
    validator(report, committed_baseline["recovery_ledger"])

    assert report["verdict"] == "FAIL"
    assert len(report["measurements"]["missing_paths"]) == 102
    assert len(report["measurements"]["marker_zero_transitions"]) == 35
    assert len(ledger_deleted) == 100
    assert len(ledger_zeroed) == 35
    assert ledger_deleted <= missing
    assert ledger_zeroed == zeroed
    assert len(missing - ledger_deleted) == 2
    assert report["input_refs"] == {
        "active": "8bd56009c186b6e0fddc8aa96898201a42a1efb4",
        "target": "7b0cf741a009bc3c61b44f9cefef7815aab88da3",
        "upstream": "8f2f4caff42cfce29d6fc0992b9f4157409f8d03",
    }
    assert {
        "immutable-release-build.json",
        "web/src/pages/AiwerkAssistantPage.tsx",
        "web/src/lib/cui-slash.ts",
    } <= missing
    assert "FORK_MARKER_ZEROED" in _codes(report)
