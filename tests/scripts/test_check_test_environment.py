import subprocess
import urllib.request
from pathlib import Path

import pytest

from scripts import check_test_environment as environment
from scripts.check_test_environment import compare_environment


@pytest.mark.parametrize(
    "expected, actual, root_url, prefix, expected_code",
    [
        ({"pytest": "9.1.1", "modal": "1.3.4"}, {"pytest": "9.1.1"}, None, "repo/.venv", "missing"),
        ({"nemo-relay": "0.8.3"}, {"nemo-relay": "0.7.2"}, None, "repo/.venv", "version_mismatch"),
        ({"pytest": "9.1.1"}, {"pytest": "9.1.1", "stale-sdk": "1.0"}, None, "repo/.venv", "extra"),
        ({"hermes-agent": "0.21.1"}, {"hermes-agent": "0.21.1"}, "file:///other/worktree", "repo/.venv", "root_source"),
        ({"pytest": "9.1.1"}, {"pytest": "9.1.1"}, None, "/external/venv", "venv_geometry"),
    ],
)
def test_compare_environment_detects_every_observed_mismatch_class(
    tmp_path, expected, actual, root_url, prefix, expected_code
):
    repo = tmp_path / "repo"
    repo.mkdir()
    resolved_prefix = repo / ".venv" if prefix == "repo/.venv" else Path(prefix)

    report = compare_environment(
        expected=expected,
        actual=actual,
        repo_root=repo,
        environment_prefix=resolved_prefix,
        root_project_name="hermes-agent",
        root_direct_url=root_url,
        root_editable=True,
        require_project_local=True,
    )

    assert expected_code in {issue["code"] for issue in report["issues"]}
    assert report["passed"] is False

    inventory = {"hermes-agent": "0.21.1", "pytest": "9.1.1", "nemo-relay": "0.8.3"}
    exact = compare_environment(
        expected=inventory,
        actual=inventory,
        repo_root=repo,
        environment_prefix=repo / ".venv",
        root_project_name="hermes-agent",
        root_direct_url=repo.as_uri(),
        root_editable=True,
        require_project_local=True,
    )
    assert exact == {"passed": True, "issues": []}


def test_full_suite_environment_preflight_runs_before_discovery(monkeypatch):
    import scripts.run_tests_parallel as runner

    events = []
    monkeypatch.setattr(
        runner,
        "_run_ci_environment_preflight",
        lambda _repo_root: events.append("preflight"),
        raising=False,
    )
    monkeypatch.setattr(
        runner,
        "_discover_files",
        lambda _roots: events.append("discovery") or [],
    )
    monkeypatch.setattr("sys.argv", ["run_tests_parallel.py", "--paths", "tests"])

    assert runner.main() == 1
    assert events == ["preflight", "discovery"]


def test_review_blockers_are_closed_by_portable_shared_contract(monkeypatch, tmp_path):
    from scripts.ci_test_profile import CI_TEST_EXTRAS
    from scripts.run_tests_parallel import _requires_ci_environment_preflight

    calls = []

    def fake_run(argv, **_kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr(environment.shutil, "which", lambda name: "/portable/git" if name == "git" else None)
    monkeypatch.setattr(environment.subprocess, "run", fake_run)
    assert environment._git(tmp_path, "rev-parse", "HEAD") == "ok"
    assert calls[0][0] == "/portable/git"

    converted = tmp_path / "converted-drive"
    monkeypatch.setattr(
        urllib.request,
        "url2pathname",
        lambda path: str(converted) if path.startswith("/C:/") else path,
    )
    assert environment._file_url_path("file:///C:/worktree") == converted.resolve()
    unc = environment._file_url_path("file://server/share/worktree")
    assert "server" in unc.parts and "share" in unc.parts

    report = compare_environment(
        expected={"hermes-agent": "0.21.1"},
        actual={"hermes-agent": "0.21.1"},
        repo_root=tmp_path,
        environment_prefix=tmp_path / ".venv",
        root_project_name="hermes-agent",
        root_direct_url=tmp_path.as_uri(),
        root_editable=False,
        require_project_local=True,
    )
    assert "root_not_editable" in {issue["code"] for issue in report["issues"]}

    assert _requires_ci_environment_preflight([tmp_path / "tests"], None, tmp_path) is True
    assert _requires_ci_environment_preflight([], [tmp_path / "tests/a.py"], tmp_path) is False
    assert _requires_ci_environment_preflight(
        [], [tmp_path / "tests/a.py"], tmp_path, explicitly_required=True
    ) is True
    assert environment.CI_TEST_EXTRAS == CI_TEST_EXTRAS
