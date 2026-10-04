"""Tests for the update check mechanism in hermes_cli.banner."""

import json
import os
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest




def test_check_for_updates_uses_cache(tmp_path, monkeypatch):
    """When cache is fresh, check_for_updates returns cached value after the HEAD identity probe."""
    from hermes_cli.banner import check_for_updates
    from hermes_cli import __version__

    # Create a fake git repo and fresh cache
    repo_dir = tmp_path / "hermes-agent"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()

    cache_file = tmp_path / ".update_check"
    cache_file.write_text(
        json.dumps(
            {
                "ts": time.time(),
                "behind": 3,
                "ver": __version__,
                "repo": str(Path(__file__).parents[2]),
            }
        )
    )

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with patch("hermes_cli.banner.subprocess.run") as mock_run:
        result = check_for_updates()

    assert result == 3
    assert mock_run.call_count == 1


def test_check_for_updates_invalidates_on_version_change(tmp_path, monkeypatch):
    """A fresh cache from a different installed version must be re-checked, not reused.

    Regression for #34491: after `pip install --upgrade`, VERSION changes but the
    cache's 6h TTL hadn't expired and rev was unchanged (both None), so the stale
    'behind' count survived the upgrade. The version guard forces a recheck.
    """
    import hermes_cli.banner as banner

    # No local git checkout -> the PyPI path is exercised (pip-install class).
    fake_banner = tmp_path / "hermes_cli" / "banner.py"
    fake_banner.parent.mkdir(parents=True, exist_ok=True)
    fake_banner.touch()
    monkeypatch.setattr(banner, "__file__", str(fake_banner))

    # Fresh (within TTL) cache that says "behind", but stamped with an OLD version.
    cache_file = tmp_path / ".update_check"
    cache_file.write_text(
        json.dumps({"ts": time.time(), "behind": 1, "rev": None, "ver": "0.0.1-old"})
    )

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_REVISION", raising=False)
    with patch("hermes_cli.banner.subprocess.run") as mock_run, \
         patch("hermes_cli.banner.check_via_pypi", return_value=0) as mock_pypi:
        result = banner.check_for_updates()

    # Stale-version cache rejected -> fresh PyPI fallback check ran.
    assert result == 0
    mock_run.assert_not_called()
    mock_pypi.assert_called_once_with()

    # Cache rewritten with the current installed version.
    written = json.loads(cache_file.read_text())
    assert written["ver"] == banner.VERSION


def test_check_for_updates_expired_cache(tmp_path, monkeypatch):
    """An expired cache invokes the passive local-tip checker, never fetch."""
    import hermes_cli.banner as banner
    repo_dir = tmp_path / "hermes-agent"
    (repo_dir / ".git").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(banner, "__file__", str(repo_dir / "hermes_cli" / "banner.py"))
    (tmp_path / ".update_check").write_text(json.dumps({"ts": 0, "behind": 1}))
    with patch("hermes_cli.banner._check_via_local_git", return_value=5) as check:
        assert banner.check_for_updates() == 5
    check.assert_called_once_with(repo_dir)


def test_check_for_updates_official_ssh_origin_uses_https_probe(tmp_path):
    """Passive update checks must not trigger SSH auth for official installs."""
    import hermes_cli.banner as banner

    repo_dir = tmp_path / "hermes-agent"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd == ["git", "remote", "get-url", "origin"]:
            return MagicMock(returncode=0, stdout="git@github.com:NousResearch/hermes-agent.git\n")
        if cmd == ["git", "rev-parse", "HEAD"]:
            return MagicMock(returncode=0, stdout="local-sha\n")
        if cmd == [
            "git",
            "merge-base",
            "--is-ancestor",
            "upstream-sha",
            "HEAD",
        ]:
            return MagicMock(returncode=1, stdout="")
        if cmd == [
            "git",
            "ls-remote",
            "https://github.com/NousResearch/hermes-agent.git",
            "refs/heads/main",
        ]:
            return MagicMock(returncode=0, stdout="upstream-sha\trefs/heads/main\n")
        raise AssertionError(f"unexpected git command: {cmd!r}")

    with patch("hermes_cli.banner.subprocess.run", side_effect=fake_run):
        result = banner._check_via_local_git(repo_dir)

    assert result == banner.UPDATE_AVAILABLE_NO_COUNT
    assert ["git", "fetch", "origin", "--quiet"] not in calls


def test_check_via_local_git_shallow_clone_behind_reports_no_count(tmp_path):
    """Differing passive tips degrade to presence-only when compare cannot count."""
    import hermes_cli.banner as banner
    repo_dir = tmp_path / "repo"
    with patch.object(banner, "_git_stdout", side_effect=["https://github.com/NousResearch/hermes-agent.git", "local-sha"]), \
         patch.object(banner, "_github_branch_tip", return_value="remote-sha"), \
         patch.object(banner, "_git_ok", return_value=False), \
         patch.object(banner, "_github_compare_behind", return_value=None):
        assert banner._check_via_local_git(repo_dir) == banner.UPDATE_AVAILABLE_NO_COUNT


def test_check_via_local_git_shallow_clone_up_to_date(tmp_path):
    """Matching passive tips report up to date without any fetch."""
    import hermes_cli.banner as banner
    repo_dir = tmp_path / "repo"
    with patch.object(banner, "_git_stdout", side_effect=["https://github.com/NousResearch/hermes-agent.git", "same-sha"]), \
         patch.object(banner, "_github_branch_tip", return_value="same-sha"):
        assert banner._check_via_local_git(repo_dir) == 0


def test_check_via_local_git_full_clone_keeps_exact_count(tmp_path):
    """The passive compare API preserves an exact count when available."""
    import hermes_cli.banner as banner
    repo_dir = tmp_path / "repo"
    with patch.object(banner, "_git_stdout", side_effect=["https://github.com/NousResearch/hermes-agent.git", "local-sha"]), \
         patch.object(banner, "_github_branch_tip", return_value="remote-sha"), \
         patch.object(banner, "_git_ok", return_value=False), \
         patch.object(banner, "_github_compare_behind", return_value=7):
        assert banner._check_via_local_git(repo_dir) == 7


def test_check_for_updates_no_git_dir(tmp_path, monkeypatch):
    """Falls back to PyPI when no source-tree .git directory exists."""
    import hermes_cli.banner as banner

    # Create a fake banner.py so the fallback path also has no .git
    fake_banner = tmp_path / "hermes_cli" / "banner.py"
    fake_banner.parent.mkdir(parents=True, exist_ok=True)
    fake_banner.touch()

    monkeypatch.setattr(banner, "__file__", str(fake_banner))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with patch("hermes_cli.banner.subprocess.run") as mock_run, \
         patch("hermes_cli.banner.check_via_pypi", return_value=0) as mock_pypi:
        result = banner.check_for_updates()
    assert result == 0
    mock_run.assert_not_called()
    mock_pypi.assert_called_once_with()


def test_check_for_updates_fallback_to_project_root(tmp_path, monkeypatch):
    """Dev install: falls back to Path(__file__).parent.parent when HERMES_HOME has no git repo."""
    import hermes_cli.banner as banner

    project_root = Path(banner.__file__).parent.parent.resolve()
    if not (project_root / ".git").exists():
        pytest.skip("Not running from a git checkout")

    # Point HERMES_HOME at a temp dir with no hermes-agent/.git
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with patch("hermes_cli.banner.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="0\n")
        result = banner.check_for_updates()
    # Should have fallen back to project root and run git commands
    assert mock_run.call_count >= 1


def test_check_for_updates_docker_returns_none(tmp_path, monkeypatch):
    """Inside the Docker image, check_for_updates() must short-circuit to None.

    Regression: the published image excludes .git (.dockerignore) and sets no
    HERMES_REVISION (nix-only), so without a docker guard check_for_updates()
    would fall through and try to probe a non-existent git checkout. The guard
    must return None (so the > 0 render guards stay false) AND not reach the
    git probe or write a cache entry.
    """
    import hermes_cli.banner as banner

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cache_file = tmp_path / ".update_check"

    with patch("hermes_cli.config.detect_install_method", return_value="docker"), \
         patch("hermes_cli.banner.subprocess.run") as mock_run:
        result = banner.check_for_updates()

    assert result is None
    # The git probe should not have run.
    mock_run.assert_not_called()
    # And no phantom "behind" count should be cached for the next 6h.
    assert not cache_file.exists()


def test_prefetch_non_blocking(monkeypatch):
    """prefetch_update_check() should return immediately without blocking."""
    import hermes_cli.banner as banner
    monkeypatch.setattr(banner, "_skip_background_prefetch", lambda: False)

    # Reset module state
    banner._update_result = None
    banner._update_check_done = threading.Event()

    with patch.object(banner, "check_for_updates", return_value=5):
        start = time.monotonic()
        banner.prefetch_update_check()
        elapsed = time.monotonic() - start

        # Should return almost immediately (well under 1 second)
        assert elapsed < 1.0

        # Wait for the background thread to finish
        banner._update_check_done.wait(timeout=5)
        assert banner._update_result == 5
