"""Repository-native publication safety primitives."""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import time
from typing import Any, Callable, Iterable
from urllib.parse import quote

from .contract import canonical_sha256
from .workflow import PublicationResult


_OID = re.compile(r"^[0-9a-f]{40}$")
_ALLOWED = {"success", "skipped", "neutral"}


class PublicationError(RuntimeError):
    pass


GhRunner = Callable[[tuple[str, ...]], str]


def _default_gh_runner(argv: tuple[str, ...]) -> str:
    result = subprocess.run(
        ["gh", *argv],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise PublicationError(
            f"gh failed rc={result.returncode} argv={argv!r}: {result.stderr[-2000:]}"
        )
    return result.stdout


class GitHubClient:
    def __init__(
        self,
        *,
        repository: str,
        runner: GhRunner = _default_gh_runner,
    ) -> None:
        if repository != "AIWerk/hermes-agent":
            raise PublicationError("GitHub client repository must be AIWerk/hermes-agent")
        self.repository = repository
        self._run = runner

    def create_pr(self, *, base: str, head: str, title: str, body: str) -> dict[str, Any]:
        if not base or not head or not title or not body:
            raise PublicationError("PR creation fields must be non-empty")
        output = self._run(
            (
                "pr",
                "create",
                "-R",
                self.repository,
                "--base",
                base,
                "--head",
                head,
                "--title",
                title,
                "--body",
                body,
            )
        ).strip()
        match = re.fullmatch(
            r"https://github\.com/AIWerk/hermes-agent/pull/([1-9][0-9]*)",
            output,
        )
        if match is None:
            raise PublicationError(f"unexpected PR creation response: {output!r}")
        return {"number": int(match.group(1)), "url": output}

    def _api_get(self, endpoint: str) -> Any:
        output = self._run(("api", "--method", "GET", endpoint))
        try:
            return json.loads(output)
        except json.JSONDecodeError as exc:
            raise PublicationError(f"GitHub API response is not JSON: {endpoint}") from exc

    def require_active_workflow(self, path: str) -> None:
        payload = self._api_get(
            f"repos/{self.repository}/actions/workflows?per_page=100"
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("workflows"), list):
            raise PublicationError("workflow inventory response is malformed")
        validate_workflow_inventory(payload["workflows"], required_path=path)

    def check_verdict(self, *, head_sha: str, required: set[str]) -> dict[str, Any]:
        if not _OID.fullmatch(head_sha):
            raise PublicationError("invalid check head SHA")
        payload = self._api_get(
            f"repos/{self.repository}/commits/{head_sha}/check-runs?per_page=100"
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("check_runs"), list):
            raise PublicationError("check-runs response is malformed")
        return evaluate_check_runs(payload["check_runs"], required)

    def pr_identity(self, number: int) -> dict[str, Any]:
        if not isinstance(number, int) or number <= 0:
            raise PublicationError("invalid PR number")
        payload = self._api_get(f"repos/{self.repository}/pulls/{number}")
        try:
            head_sha = payload["head"]["sha"]
            head_ref = payload["head"]["ref"]
            base_sha = payload["base"]["sha"]
            base_ref = payload["base"]["ref"]
        except (KeyError, TypeError) as exc:
            raise PublicationError("PR identity response is malformed") from exc
        if (
            not _OID.fullmatch(str(head_sha))
            or not _OID.fullmatch(str(base_sha))
            or base_ref != "main"
        ):
            raise PublicationError("PR identity response is invalid")
        return {
            "head_sha": head_sha,
            "head_ref": head_ref,
            "base_sha": base_sha,
            "base_ref": base_ref,
        }

    def find_pr(self, head_ref: str) -> dict[str, Any] | None:
        if not head_ref or "/" not in head_ref:
            raise PublicationError("invalid publication head ref")
        endpoint = (
            f"repos/{self.repository}/pulls?state=all&head="
            f"{quote('AIWerk:' + head_ref, safe='')}&per_page=100"
        )
        payload = self._api_get(endpoint)
        if not isinstance(payload, list):
            raise PublicationError("PR lookup response is malformed")
        matches = [
            row
            for row in payload
            if isinstance(row, dict)
            and (row.get("head") or {}).get("ref") == head_ref
            and (row.get("base") or {}).get("ref") == "main"
        ]
        if len(matches) > 1:
            raise PublicationError("publication PR lookup is ambiguous")
        if not matches:
            return None
        row = matches[0]
        if not isinstance(row.get("number"), int) or not isinstance(row.get("html_url"), str):
            raise PublicationError("publication PR lookup identity is malformed")
        return {"number": row["number"], "url": row["html_url"]}

    def wait_for_checks(
        self,
        *,
        head_sha: str,
        required: set[str],
        poll_seconds: float,
        timeout_seconds: float,
        sleeper: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> dict[str, Any]:
        if poll_seconds < 0 or timeout_seconds <= 0:
            raise PublicationError("invalid check wait bounds")
        deadline = monotonic() + timeout_seconds
        while True:
            verdict = self.check_verdict(head_sha=head_sha, required=required)
            if verdict["state"] == "PASS":
                return verdict
            if verdict["state"] == "FAIL":
                raise PublicationError(verdict["reason"])
            if monotonic() >= deadline:
                raise PublicationError("required checks timed out")
            sleeper(poll_seconds)

    def merge_exact(self, *, pr_number: int, head_sha: str) -> dict[str, Any]:
        request = exact_merge_request(
            repository=self.repository,
            pr_number=pr_number,
            head_sha=head_sha,
        )
        output = self._run(
            (
                "api",
                "--method",
                request["method"],
                request["endpoint"],
                "-f",
                f"sha={head_sha}",
                "-f",
                "merge_method=merge",
            )
        )
        try:
            payload = json.loads(output)
        except json.JSONDecodeError as exc:
            raise PublicationError("merge response is not JSON") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("merged") is not True
            or not _OID.fullmatch(str(payload.get("sha", "")))
        ):
            raise PublicationError(f"exact merge did not succeed: {payload!r}")
        return {"merged": True, "sha": payload["sha"]}


def _latest(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    latest: dict[tuple[Any, str], dict[str, Any]] = {}
    for row in rows:
        app = row.get("app") or {}
        key = (app.get("id", app.get("slug")), str(row.get("name", "")))
        if not key[1] or not isinstance(row.get("id"), int):
            raise PublicationError("malformed check run")
        if key not in latest or row["id"] > latest[key]["id"]:
            latest[key] = row
    return sorted(latest.values(), key=lambda item: (item["name"], item["id"]))


def evaluate_check_runs(rows: Iterable[dict[str, Any]], required: set[str]) -> dict[str, Any]:
    latest = _latest(rows)
    checks = [
        {
            "id": row["id"],
            "name": row["name"],
            "status": row.get("status"),
            "conclusion": row.get("conclusion"),
            "details_url": row.get("details_url"),
            "app_id": (row.get("app") or {}).get("id"),
        }
        for row in latest
    ]
    red = [
        {
            "id": row["id"],
            "name": row["name"],
            "conclusion": row.get("conclusion"),
            "details_url": row.get("details_url"),
        }
        for row in latest
        if row.get("status") == "completed" and row.get("conclusion") not in _ALLOWED
    ]
    if red:
        return {"state": "FAIL", "reason": "one or more visible latest checks are red", "red": red}
    if any(row.get("status") != "completed" for row in latest):
        return {"state": "WAIT", "reason": "visible checks are still running", "red": []}
    by_name = {row["name"]: row for row in latest}
    missing = sorted(required - set(by_name))
    if missing:
        return {"state": "WAIT", "reason": f"required checks missing: {missing}", "red": []}
    unsuccessful = sorted(name for name in required if by_name[name].get("conclusion") != "success")
    if unsuccessful:
        return {
            "state": "FAIL",
            "reason": f"required checks not successful: {unsuccessful}",
            "red": [],
        }
    return {
        "state": "PASS",
        "reason": "all visible latest checks acceptable",
        "red": [],
        "checks": checks,
    }


def validate_workflow_inventory(
    workflows: Iterable[dict[str, Any]], *, required_path: str
) -> None:
    matches = [row for row in workflows if row.get("path") == required_path]
    if len(matches) != 1:
        raise PublicationError(f"required workflow is missing or ambiguous: {required_path}")
    if matches[0].get("state") != "active":
        raise PublicationError(f"required workflow is inactive: {required_path}")


def validate_workflow_trigger(repo: Path, required_path: str) -> None:
    path = repo / required_path
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise PublicationError(f"required workflow bytes unavailable: {required_path}") from exc
    match = re.search(r"(?m)^\s{0,4}pull_request\s*:\s*(?:#.*)?$", text)
    if match is None:
        raise PublicationError("required workflow does not trigger on pull_request")
    window = text[match.end() : match.end() + 500]
    if re.search(r"(?m)^\s+types\s*:", window) and not re.search(
        r"\b(?:opened|synchronize|reopened)\b", window
    ):
        raise PublicationError("pull_request trigger omits candidate publication events")


def exact_merge_request(*, repository: str, pr_number: int, head_sha: str) -> dict[str, Any]:
    if repository != "AIWerk/hermes-agent":
        raise PublicationError("merge repository must be AIWerk/hermes-agent")
    if not isinstance(pr_number, int) or pr_number <= 0:
        raise PublicationError("invalid PR number")
    if not _OID.fullmatch(head_sha):
        raise PublicationError("invalid exact head SHA")
    return {
        "method": "PUT",
        "endpoint": f"repos/{repository}/pulls/{pr_number}/merge",
        "fields": {"sha": head_sha, "merge_method": "merge"},
    }


GitRunner = Callable[[Path, tuple[str, ...]], str]


def _default_git_runner(repo: Path, argv: tuple[str, ...]) -> str:
    result = subprocess.run(
        ["git", *argv],
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
        raise PublicationError(
            f"git failed rc={result.returncode} argv={argv!r}: {result.stderr[-2000:]}"
        )
    return result.stdout.strip()


class GitHubPublisher:
    """Ordinary fork branch→PR→CI→exact-head merge publisher."""

    _FORK_URL = "https://github.com/AIWerk/hermes-agent.git"

    def __init__(
        self,
        *,
        client: Any,
        required_checks: set[str],
        workflow_path: str = ".github/workflows/ci.yaml",
        git_runner: GitRunner = _default_git_runner,
        poll_seconds: float = 120,
        timeout_seconds: float = 21600,
    ) -> None:
        if getattr(client, "repository", None) != "AIWerk/hermes-agent":
            raise PublicationError("publisher client repository must be AIWerk/hermes-agent")
        if not required_checks:
            raise PublicationError("publisher requires at least one protected check")
        self.client = client
        self.required_checks = set(required_checks)
        self.workflow_path = workflow_path
        self.git = git_runner
        self.poll_seconds = poll_seconds
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def _check_evidence(check_verdict: dict[str, Any]) -> dict[str, str]:
        evidence: dict[str, str] = {}
        for detector, predicate in (
            ("supply-chain", lambda name: "supply" in name and "chain" in name),
            ("osv", lambda name: "osv" in name),
        ):
            matches = [
                row
                for row in check_verdict.get("checks", [])
                if predicate(str(row.get("name", "")).casefold())
            ]
            if len(matches) == 1:
                evidence[detector] = canonical_sha256(matches[0])
        return evidence

    def publish(
        self,
        *,
        stage: str,
        repo: Path,
        base_commit: str,
        candidate_commit: str,
        changed_paths: tuple[str, ...],
    ) -> PublicationResult:
        if stage not in {"approval", "control", "product"}:
            raise PublicationError("invalid publication stage")
        if not _OID.fullmatch(base_commit) or not _OID.fullmatch(candidate_commit):
            raise PublicationError("invalid publication object identity")
        if not changed_paths or tuple(sorted(set(changed_paths))) != changed_paths:
            raise PublicationError("publication paths must be sorted and unique")
        parent = self.git(repo, ("show", "-s", "--format=%P", candidate_commit))
        if parent != base_commit:
            raise PublicationError("candidate is not a one-parent transition from exact base")
        branch = f"aiwerk/update/{stage}-{candidate_commit[:12]}"
        remote_branch = self.git(
            repo, ("ls-remote", self._FORK_URL, f"refs/heads/{branch}")
        )
        if remote_branch:
            fields = remote_branch.split()
            if not fields or fields[0] != candidate_commit:
                raise PublicationError("publication branch exists with wrong head")
        validate_workflow_trigger(repo, self.workflow_path)
        self.client.require_active_workflow(self.workflow_path)
        if not remote_branch:
            self.git(
                repo,
                ("push", self._FORK_URL, f"{candidate_commit}:refs/heads/{branch}"),
            )
        created = self.client.find_pr(branch)
        if created is None:
            created = self.client.create_pr(
                base="main",
                head=branch,
                title=f"AIWerk updater {stage} transition",
                body=(
                    f"Maintained updater stage: {stage}\n\n"
                    f"Exact head: `{candidate_commit}`\n"
                    f"Changed paths: {', '.join(changed_paths)}"
                ),
            )
        identity = self.client.pr_identity(created["number"])
        expected_identity = {
            "head_sha": candidate_commit,
            "head_ref": branch,
            "base_sha": base_commit,
            "base_ref": "main",
        }
        if identity != expected_identity:
            raise PublicationError(f"PR identity mismatch: {identity!r}")
        check_verdict = self.client.wait_for_checks(
            head_sha=candidate_commit,
            required=self.required_checks,
            poll_seconds=self.poll_seconds,
            timeout_seconds=self.timeout_seconds,
        )
        main_before = self.git(repo, ("ls-remote", self._FORK_URL, "refs/heads/main"))
        main_fields = main_before.split()
        if not main_fields:
            raise PublicationError("fork main is absent")
        if main_fields[0] != base_commit:
            existing_merge = main_fields[0]
            self.git(repo, ("fetch", "--quiet", self._FORK_URL, existing_merge))
            existing_parents = self.git(
                repo, ("show", "-s", "--format=%P", existing_merge)
            )
            if existing_parents != f"{base_commit} {candidate_commit}":
                raise PublicationError("fork main advanced outside this exact operation")
            self.git(repo, ("checkout", "--detach", "--quiet", existing_merge))
            return PublicationResult(
                stage=stage,
                pr_number=created["number"],
                pr_url=created["url"],
                head_commit=candidate_commit,
                merged_commit=existing_merge,
                merged_tree=self.git(repo, ("rev-parse", f"{existing_merge}^{{tree}}")),
                check_evidence=self._check_evidence(check_verdict),
            )
        self.git(repo, ("checkout", "--detach", "--quiet", base_commit))
        self.git(
            repo,
            (
                "merge",
                "--no-ff",
                "-m",
                f"Merge pull request #{created['number']} from AIWerk/{branch}",
                candidate_commit,
            ),
        )
        merge_commit = self.git(repo, ("rev-parse", "HEAD"))
        if not _OID.fullmatch(merge_commit):
            raise PublicationError("local exact merge commit identity is invalid")
        parents = self.git(repo, ("show", "-s", "--format=%P", merge_commit))
        if parents != f"{base_commit} {candidate_commit}":
            raise PublicationError("merged commit parent order mismatch")
        self.git(
            repo,
            ("push", self._FORK_URL, f"{merge_commit}:refs/heads/main"),
        )
        self.git(repo, ("fetch", "--quiet", self._FORK_URL, merge_commit))
        main_row = self.git(repo, ("ls-remote", self._FORK_URL, "refs/heads/main"))
        if main_row.split()[:1] != [merge_commit]:
            raise PublicationError("fork main does not name merged commit")
        merge_tree = self.git(repo, ("rev-parse", f"{merge_commit}^{{tree}}"))
        check_evidence = self._check_evidence(check_verdict)
        return PublicationResult(
            stage=stage,
            pr_number=created["number"],
            pr_url=created["url"],
            head_commit=candidate_commit,
            merged_commit=merge_commit,
            merged_tree=merge_tree,
            check_evidence=check_evidence,
        )
