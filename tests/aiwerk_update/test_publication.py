from __future__ import annotations

import json
from pathlib import Path
import pytest

from scripts.aiwerk_update.publication import (
    GitHubClient,
    GitHubPublisher,
    PublicationError,
    evaluate_check_runs,
    exact_merge_request,
    validate_workflow_inventory,
    validate_workflow_trigger,
)


REQUIRED = {
    "AIWerk content-loss guard",
    "All required checks pass",
    "AIWerk Honcho retained behavior",
    "AIWerk session-history retained behavior",
}


def _row(identifier: int, name: str, conclusion: str = "success", status: str = "completed") -> dict:
    return {
        "id": identifier,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "app": {"id": 1, "slug": "github-actions"},
        "details_url": f"https://example.invalid/{identifier}",
    }


def test_red_check_is_reported_even_before_aggregate_exists() -> None:
    rows = [
        _row(1, "AIWerk content-loss guard", "failure"),
        _row(2, "AIWerk Honcho retained behavior"),
        _row(3, "AIWerk session-history retained behavior"),
    ]

    verdict = evaluate_check_runs(rows, REQUIRED)

    assert verdict["state"] == "FAIL"
    assert verdict["red"][0]["name"] == "AIWerk content-loss guard"


def test_cancelled_visible_check_is_not_clean_green() -> None:
    rows = [_row(index, name) for index, name in enumerate(sorted(REQUIRED), 1)]
    rows.append(_row(99, "Detect affected areas", "cancelled"))

    verdict = evaluate_check_runs(rows, REQUIRED)

    assert verdict["state"] == "FAIL"
    assert verdict["red"][0]["conclusion"] == "cancelled"


def test_all_visible_latest_terminal_and_required_success_is_pass() -> None:
    rows = [_row(index, name) for index, name in enumerate(sorted(REQUIRED), 1)]
    rows.extend([_row(90, "optional-build", "skipped"), _row(91, "osv-scanner", "neutral")])

    verdict = evaluate_check_runs(rows, REQUIRED)

    assert verdict["state"] == "PASS"
    assert verdict["reason"] == "all visible latest checks acceptable"
    assert verdict["red"] == []
    assert [row["name"] for row in verdict["checks"]] == sorted(
        [*REQUIRED, "optional-build", "osv-scanner"]
    )


def test_workflow_inventory_requires_active_triggerable_ci() -> None:
    with pytest.raises(PublicationError, match="inactive"):
        validate_workflow_inventory(
            [{"name": "CI", "path": ".github/workflows/ci.yaml", "state": "disabled_manually"}],
            required_path=".github/workflows/ci.yaml",
        )
    validate_workflow_inventory(
        [{"name": "CI", "path": ".github/workflows/ci.yaml", "state": "active"}],
        required_path=".github/workflows/ci.yaml",
    )


def test_workflow_trigger_requires_ordinary_pull_request_events(tmp_path: Path) -> None:
    workflow = tmp_path / ".github/workflows/ci.yaml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("on:\n  workflow_dispatch:\n")
    with pytest.raises(PublicationError, match="pull_request"):
        validate_workflow_trigger(tmp_path, ".github/workflows/ci.yaml")
    workflow.write_text("on:\n  pull_request:\n")
    validate_workflow_trigger(tmp_path, ".github/workflows/ci.yaml")


def test_exact_merge_request_is_aiwerk_only_and_head_bound() -> None:
    request = exact_merge_request(
        repository="AIWerk/hermes-agent",
        pr_number=166,
        head_sha="a" * 40,
    )

    assert request == {
        "method": "PUT",
        "endpoint": "repos/AIWerk/hermes-agent/pulls/166/merge",
        "fields": {"sha": "a" * 40, "merge_method": "merge"},
    }
    with pytest.raises(PublicationError, match="AIWerk"):
        exact_merge_request(
            repository="NousResearch/hermes-agent", pr_number=166, head_sha="a" * 40
        )


def test_github_client_opens_and_merges_only_exact_aiwerk_pr() -> None:
    calls: list[tuple[str, ...]] = []

    def runner(argv: tuple[str, ...]) -> str:
        calls.append(argv)
        if argv[:2] == ("pr", "create"):
            return "https://github.com/AIWerk/hermes-agent/pull/166\n"
        if any(item.endswith("pulls/166/merge") for item in argv):
            return '{"merged":true,"sha":"' + "b" * 40 + '"}'
        raise AssertionError(argv)

    client = GitHubClient(repository="AIWerk/hermes-agent", runner=runner)
    created = client.create_pr(
        base="main",
        head="aiwerk/update/test",
        title="update",
        body="exact update",
    )
    merged = client.merge_exact(pr_number=166, head_sha="a" * 40)

    assert created == {"number": 166, "url": "https://github.com/AIWerk/hermes-agent/pull/166"}
    assert merged == {"merged": True, "sha": "b" * 40}
    assert all("NousResearch" not in " ".join(call) for call in calls)


def test_github_client_reads_workflow_and_exact_head_checks() -> None:
    required = sorted(REQUIRED)

    def runner(argv: tuple[str, ...]) -> str:
        endpoint = argv[-1]
        if endpoint == "repos/AIWerk/hermes-agent/actions/workflows?per_page=100":
            return json.dumps(
                {
                    "workflows": [
                        {
                            "name": "CI",
                            "path": ".github/workflows/ci.yaml",
                            "state": "active",
                        }
                    ]
                }
            )
        if endpoint.endswith("/check-runs?per_page=100"):
            return json.dumps(
                {"check_runs": [_row(index, name) for index, name in enumerate(required, 1)]}
            )
        raise AssertionError(argv)

    client = GitHubClient(repository="AIWerk/hermes-agent", runner=runner)

    client.require_active_workflow(".github/workflows/ci.yaml")
    verdict = client.check_verdict(head_sha="a" * 40, required=set(required))

    assert verdict["state"] == "PASS"


def test_github_publisher_pushes_fork_branch_waits_and_reads_back_exact_merge(
    tmp_path,
) -> None:
    base = "a" * 40
    head = "b" * 40
    merged = "c" * 40
    tree = "d" * 40
    git_calls: list[tuple[str, ...]] = []
    main_updated = False
    workflow = tmp_path / ".github/workflows/ci.yaml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("on:\n  pull_request:\n")

    class Client:
        repository = "AIWerk/hermes-agent"

        def require_active_workflow(self, path: str) -> None:
            assert path == ".github/workflows/ci.yaml"

        def create_pr(self, **kwargs):
            assert kwargs["base"] == "main"
            return {"number": 17, "url": "https://github.com/AIWerk/hermes-agent/pull/17"}

        def find_pr(self, head_ref: str):
            return None

        def pr_identity(self, number: int):
            assert number == 17
            return {"head_sha": head, "base_sha": base, "base_ref": "main", "head_ref": "aiwerk/update/control-" + head[:12]}

        def wait_for_checks(self, **kwargs):
            assert kwargs["head_sha"] == head
            return {"state": "PASS"}

        def merge_exact(self, **kwargs):
            raise AssertionError("merge API must not be used because it cannot bind the base")

    def git_runner(_repo: Path, argv: tuple[str, ...]) -> str:
        nonlocal main_updated
        git_calls.append(argv)
        if argv[:2] == ("show", "-s"):
            return base if argv[-1] == head else f"{base} {head}"
        if argv[:2] == ("rev-parse", "HEAD"):
            return merged
        if argv[0] == "ls-remote":
            if argv[-1].startswith("refs/heads/aiwerk/update/"):
                return ""
            return f"{merged if main_updated else base}\trefs/heads/main"
        if argv[0] == "push" and argv[-1].endswith(":refs/heads/main"):
            main_updated = True
            return ""
        if argv[:2] == ("rev-parse", f"{merged}^{{tree}}"):
            return tree
        return ""

    publisher = GitHubPublisher(
        client=Client(),
        required_checks={"All required checks pass"},
        git_runner=git_runner,
        poll_seconds=0,
        timeout_seconds=30,
    )

    result = publisher.publish(
        stage="control",
        repo=tmp_path,
        base_commit=base,
        candidate_commit=head,
        changed_paths=("pyproject.toml",),
    )

    assert result.merged_commit == merged
    assert result.merged_tree == tree
    assert any(call[0] == "push" and "AIWerk/hermes-agent.git" in call[1] for call in git_calls)
    assert (
        "push",
        "https://github.com/AIWerk/hermes-agent.git",
        f"{merged}:refs/heads/main",
    ) in git_calls
    assert all("--force" not in item for call in git_calls for item in call)
    assert any(call[:2] == ("fetch", "--quiet") and call[-1] == merged for call in git_calls)
