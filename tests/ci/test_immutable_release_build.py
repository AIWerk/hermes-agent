"""CI contracts for immutable runtime build metadata."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_root_project_version_matches_pyproject_version() -> None:
    release_build = json.loads(
        (_REPO_ROOT / "immutable-release-build.json").read_text(encoding="utf-8")
    )
    pyproject = tomllib.loads(
        (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )

    assert (
        release_build["build"]["python"]["root_project"]["version"]
        == pyproject["project"]["version"]
    ), "immutable release root-project version must track pyproject.toml"
