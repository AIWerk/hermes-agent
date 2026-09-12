# AIWerk Weekly Upstream Refresh Runbook

For every weekly upstream refresh:

## 0. Publish upstream object authority first

1. Resolve the annotated Nous release tag and record both tag object and peeled commit.
2. Create `upstream/v<release>` at the peeled commit.
3. Before any dependent control or product PR, non-force push that immutable mirror, read the remote OID back, and prove the exact commit resolves under the real trusted-CI fetch shape.
4. A local object or a later refresh branch is not CI reachability evidence.

## 1. Isolate all candidate diagnostics

Merge-tree code is untrusted executable input until qualification. Every diagnostic, import probe, migration check, focused test, or ad-hoc Python command that can import the candidate must run through:

    scripts/ci/run_refresh_diagnostic.py -- <command> [args...]

The launcher replaces `HOME`, `HERMES_HOME`, and `TMPDIR` with one disposable external root, disables lazy installs and bytecode writes, and removes profile selection. Never instantiate `SessionDB`, import startup modules, or run candidate scripts directly against the operator or tenant home. A read-only-looking constructor may perform schema migration before the diagnostic returns.

The canonical test runner remains valid only when its own isolated-home contract is active and verified. External coding agents must receive the same isolated environment before launch; prompt text alone is not a state boundary.

## 2. Preserve release-contract cross-binding

1. Read the candidate version from committed `pyproject.toml`.
2. Update `build.python.root_project.version` in committed `immutable-release-build.json` to the same value in that PR.
3. Run `tests/ci/test_immutable_release_build.py`; a version mismatch is a blocking CI failure.
4. Run the canonical suite with its default two file retries; the three declared timing-sensitive files stay in the runner serial lane.

The refresh PR is incomplete until object reachability, diagnostic isolation, content-loss gates, version cross-binding, focused checks, and the complete suite pass on the exact candidate.
