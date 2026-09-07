# AIWerk Weekly Upstream Refresh Runbook

For every weekly upstream refresh PR:

1. Read the candidate version from committed `pyproject.toml`.
2. Update `build.python.root_project.version` in committed `immutable-release-build.json` to the same value in that PR.
3. Run `tests/ci/test_immutable_release_build.py`; a version mismatch is a blocking CI failure.

The refresh PR is incomplete until this cross-binding gate passes.
