import json
import time
from pathlib import Path
from unittest.mock import patch

import hermes_cli.banner as banner


def test_update_check_cache_without_head_is_ignored_for_git_checkout(tmp_path):
    cache_file = tmp_path / ".update_check"
    cache_file.write_text(
        json.dumps({"ts": time.time(), "behind": 169, "rev": None, "ver": banner.VERSION})
    )

    repo_dir = Path("/tmp/hermes-active-checkout")
    identity = {"rev": None, "repo": str(repo_dir), "head": "fresh-head"}

    with patch("hermes_cli.banner.get_hermes_home", return_value=tmp_path), \
         patch("hermes_cli.banner._resolve_repo_dir", return_value=repo_dir), \
         patch("hermes_cli.banner._update_check_cache_identity", return_value=identity), \
         patch("hermes_cli.banner._check_via_local_git", return_value=0) as check:
        assert banner.check_for_updates() == 0

    check.assert_called_once_with(repo_dir)
    saved = json.loads(cache_file.read_text())
    assert saved["behind"] == 0
    assert saved["repo"] == str(repo_dir)
    assert saved["head"] == "fresh-head"


def test_update_check_cache_reused_when_repo_and_head_match(tmp_path):
    repo_dir = Path("/tmp/hermes-active-checkout")
    identity = {"rev": None, "repo": str(repo_dir), "head": "same-head"}
    (tmp_path / ".update_check").write_text(
        json.dumps(
            {
                "ts": time.time(),
                "behind": 7,
                "rev": None,
                "repo": str(repo_dir),
                "head": "same-head",
                "ver": banner.VERSION,
            }
        )
    )

    with patch("hermes_cli.banner.get_hermes_home", return_value=tmp_path), \
         patch("hermes_cli.banner._resolve_repo_dir", return_value=repo_dir), \
         patch("hermes_cli.banner._update_check_cache_identity", return_value=identity), \
         patch("hermes_cli.banner._check_via_local_git") as check:
        assert banner.check_for_updates() == 7

    check.assert_not_called()
