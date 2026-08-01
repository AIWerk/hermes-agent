# Phase 6 full verification and exact merge evidence — 2026-08-01

## Scope

Isolated validation of the AIWerk/upstream merge worktree only. No live release, gateway, protected wrapper/symlink, real `~/.hermes/state.db`, rollback source, push, or remote ref was modified.

## Exact merge object

- Merge commit: `4bbd8270bcef77908ad1d02b416d9b36e5450c94`
- Merge tree: `9b88e0623f87d031a79b99a80020fe785b6ca4e2`
- First parent (AIWerk): `3699d6bae552acc4490ee2f417c52d53ed94d36c`
- Second parent (NousResearch upstream): `470cf66b039c73bdd2c21d43094ce41a4db74eae`
- Subject: `merge: integrate NousResearch main into AIWerk main`
- Approved/frozen upstream cutoff: `470cf66b039c73bdd2c21d43094ce41a4db74eae`. Later upstream movement is outside this merge's fixed scope and requires a separate future integration.
- Both parents are ancestors of the merge.
- Parent divergence preserved: 241 AIWerk-only commits and 820 upstream-only commits.
- Merge versus first parent: 3,129 files; 142,638 insertions; 411,218 deletions.
- Merge versus second parent: 322 files; 73,190 insertions; 7,858 deletions.

## Blocking findings resolved after the merge object

1. Removed the accidentally retained legacy nested `gateway/run.py::run_sync` implementation and restored the upstream `TurnRunner.run_sync` binding.
2. Ported the verified AIWerk-only run-sync delta—voice-reply platform-stream suppression—into `TurnRunner`, adding `event_message_type` to `TurnContext`.
3. Restored 8 AIWerk state regression modules as explicitly named companion regression files; 70 restored tests pass.
4. Routed `hermes_cli/web_server.py::_run_json_command()` through the canonical subprocess environment builder while preserving local credential/HOME semantics and `BW_NOINTERACTION=true`.
5. Fixed container-aware doctor diagnostics so explicitly selected remote/cloud terminal backends remain diagnosable.
6. Fixed Windows managed-checkout EOL normalization: real edits are detected with `--numstat --ignore-cr-at-eol`, and NUL pathspec input is NUL-terminated.
7. Forced literal Git pathspec semantics for the EOL restore so tracked filenames such as `:(glob)*.py` or `:(literal)…` cannot expand into and destroy genuine edits in other files.
8. Updated tests for upstream read-only config loading, lazy cron DB path selection, durable compression-failure cooldowns, deterministic MCP mtime advancement, and isolation of the MCP no-change gate from first-open SessionDB schema writes.
9. Repaired the malformed queue-consumption test header produced by conflict resolution.

## Focused verification

- Gateway regression files after `TurnRunner` repair: 243 passed.
- Restored AIWerk state regression modules plus affected gateway tests: 73 passed.
- `tests/hermes_cli/test_update_eol_churn.py`: 11 passed, including adversarial literal-pathspec filenames.
- MCP mtime-gate tests: 2 passed.
- Config-readonly/run-agent tests: 3 passed.
- Compression fence/lock test: 1 passed.
- Subprocess environment guard: 2 passed.
- Targeted Ruff, `py_compile`, staged `git diff --check`, and unstaged `git diff --check`: passed.

## Full isolated suite

Environment:

- temporary `HERMES_HOME`
- temporary `HERMES_DB` under that home
- `UV_PYTHON_DOWNLOADS=never`
- `LD_LIBRARY_PATH=/home/agbergsmann/.local/sqlite-3.53.4/lib`
- canonical `scripts/run_tests.sh`
- 16 workers

Result:

```text
=== Summary: 2568 files, 25643 tests passed, 0 failed (100% complete) in 918.7s (16 workers) ===
FULL_VERIFY_EXIT=0
```

Raw run log at verification time:

- Path: `/tmp/hermes-full-verify-20260801-1932.log`
- SHA-256: `437efd53863657ed9cc623c314e2967bcf7c54a42be77141429d6d6e302af985`

The temporary Hermes home/database was removed by the test-run trap after exit.

## Gate state

The merge object itself is followed by a staged post-merge repair delta. Its final commit and tree identifiers must be measured after independent review and commit. Push remains blocked until Attila explicitly approves the exact final commit/tree and diff evidence.
