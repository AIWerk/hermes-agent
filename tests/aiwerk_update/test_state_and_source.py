from __future__ import annotations

import json
from pathlib import Path

import pytest

import scripts.aiwerk_update.state as state_module
from scripts.aiwerk_update.contract import ContractError, TransitionChange
from scripts.aiwerk_update.source import split_transition_changes
from scripts.aiwerk_update.state import RunStore, StateError


A = "a" * 40
B = "b" * 40


def test_split_transition_changes_puts_controls_before_product() -> None:
    changes = [
        TransitionChange("agent/runtime.py", A, B),
        TransitionChange("pyproject.toml", A, B),
        TransitionChange("uv.lock", A, B),
    ]

    plan = split_transition_changes(changes, {"pyproject.toml", "uv.lock"})

    assert [change.path for change in plan.control] == ["pyproject.toml", "uv.lock"]
    assert [change.path for change in plan.product] == ["agent/runtime.py"]
    assert plan.product_has_protected_controls is False


def test_split_transition_rejects_ledger_mixed_with_any_other_path() -> None:
    changes = [
        TransitionChange(".ci/content-loss/retirements.json", A, B),
        TransitionChange("agent/runtime.py", A, B),
    ]

    with pytest.raises(ContractError, match="ledger-only"):
        split_transition_changes(changes, {".ci/content-loss/retirements.json"})


def test_run_store_preflight_does_not_create_execution_claim(tmp_path: Path) -> None:
    store = RunStore.create(
        tmp_path,
        run_id="20261002T220000Z-base-target",
        request={"through": "local-handoff", "source_publication": True},
        authority={"base_commit": A, "target_commit": B},
    )

    store.record_preflight({"verdict": "PASS", "failures": []})

    assert not (store.root / "execution.claim").exists()
    assert json.loads((store.root / "state.json").read_text())["phase"] == "PREFLIGHT_PASS"
    assert store.next_stage() == "execute"


def test_run_store_claim_is_created_exactly_once_at_execution_boundary(tmp_path: Path) -> None:
    store = RunStore.create(
        tmp_path,
        run_id="20261002T220000Z-base-target",
        request={"through": "local-handoff", "source_publication": True},
        authority={"base_commit": A, "target_commit": B},
    )
    store.record_preflight({"verdict": "PASS", "failures": []})

    store.begin_execution()

    assert (store.root / "execution.claim").is_file()
    assert json.loads((store.root / "state.json").read_text())["phase"] == "EXECUTING"
    store.begin_execution()
    assert json.loads((store.root / "state.json").read_text())["phase"] == "EXECUTING"


def test_run_store_recovers_claim_created_before_state_transition(tmp_path: Path) -> None:
    store = RunStore.create(
        tmp_path,
        run_id="20261002T220000Z-base-target",
        request={"through": "local-handoff", "source_publication": True},
        authority={"base_commit": A, "target_commit": B},
    )
    store.record_preflight({"verdict": "PASS", "failures": []})
    claim = store.root / "execution.claim"
    claim.write_text(json.dumps(store.claim_payload(), sort_keys=True, separators=(",", ":")) + "\n")

    reopened = RunStore.open(store.root)
    reopened.begin_execution()

    assert json.loads((store.root / "state.json").read_text())["phase"] == "EXECUTING"
    assert reopened.next_stage() == "control"


def test_run_store_never_publishes_a_partial_execution_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RunStore.create(
        tmp_path,
        run_id="20261002T220000Z-base-target",
        request={"through": "local-handoff", "source_publication": True},
        authority={"base_commit": A, "target_commit": B},
    )
    store.record_preflight({"verdict": "PASS", "failures": []})
    real_write = state_module.os.write
    calls = 0

    def interrupted_write(fd: int, data: bytes) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_write(fd, data[: max(1, len(data) // 2)])
        raise OSError("injected claim-write interruption")

    with monkeypatch.context() as scoped:
        scoped.setattr(state_module.os, "write", interrupted_write)
        with pytest.raises(OSError, match="injected claim-write interruption"):
            store.begin_execution()

    assert not (store.root / "execution.claim").exists()
    assert list(store.root.glob(".execution.claim.*")) == []
    store.begin_execution()
    assert json.loads((store.root / "execution.claim").read_text()) == store.claim_payload()


def test_run_store_resume_uses_same_run_and_first_incomplete_stage(tmp_path: Path) -> None:
    store = RunStore.create(
        tmp_path,
        run_id="20261002T220000Z-base-target",
        request={"through": "local-handoff", "source_publication": True},
        authority={"base_commit": A, "target_commit": B},
    )
    store.record_preflight({"verdict": "PASS", "failures": []})
    store.begin_execution()
    store.complete_stage("control")
    reopened = RunStore.open(store.root)

    assert reopened.run_id == store.run_id
    assert reopened.next_stage() == "product"
    assert len((store.root / "events.jsonl").read_text().splitlines()) == 4


def test_finish_handoff_rejects_prestart_containment_only_recovery(tmp_path: Path) -> None:
    store = RunStore.create(
        tmp_path,
        run_id="20261002T220000Z-base-target",
        request={"through": "local-handoff", "source_publication": True},
        authority={"base_commit": A, "target_commit": B},
    )
    store.record_preflight({"verdict": "PASS", "failures": []})
    store.begin_execution()
    for stage in ("control", "product", "publication", "artifact"):
        store.complete_stage(stage)

    with pytest.raises(StateError, match="recovery|handoff"):
        store.finish_handoff(
            {
                "status": "HANDOFF_READY",
                "activation": "NOT_RUN_REQUIRES_SEPARATE_ATTILA_GO_AND_JEROME",
                "installed_updater_source_identity": "1" * 64,
                "installed_updater_wheel_identity": "2" * 64,
                "installed_update_check_receipt_sha256": "3" * 64,
                "extracted_target_preflight_receipt_sha256": "4" * 64,
                "recovery_receipt_sha256": "5" * 64,
                "recovery_disposition": "FORWARD_ONLY_PRESTART_RECOVERY_PROVED",
                "post_start_policy": "CONTAINMENT_ONLY",
            }
        )

    state = json.loads((store.root / "state.json").read_text())
    assert state["phase"] == "EXECUTING"
    assert not (store.root / "local-handoff.json").exists()
    assert not (store.root / "final.json").exists()
    assert not (store.root / "manifest.sha256").exists()


def test_finish_handoff_persists_blocked_recovery_without_handoff_ready(
    tmp_path: Path,
) -> None:
    store = RunStore.create(
        tmp_path,
        run_id="blocked-recovery",
        request={"through": "local-handoff", "source_publication": True},
        authority={"base_commit": A, "target_commit": B},
    )
    store.record_preflight({"verdict": "PASS", "failures": []})
    store.begin_execution()
    for stage in ("control", "product", "publication", "artifact"):
        store.complete_stage(stage)

    store.finish_handoff(
        {
            "schema_version": 1,
            "kind": "AIWERK_LOCAL_ACTIVATION_HANDOFF",
            "status": "HANDOFF_BLOCKED_RECOVERY",
            "completion": False,
            "activation": "NOT_RUN",
            "recovery_disposition": (
                "FORWARD_ONLY_POSTFAILURE_PREDECESSOR_RECOVERY_UNPROVEN"
            ),
        }
    )

    final = json.loads((store.root / "final.json").read_text())
    state = json.loads((store.root / "state.json").read_text())
    events = [json.loads(line) for line in (store.root / "events.jsonl").read_text().splitlines()]
    assert final["kind"] == "AIWERK_UPDATE_FINAL"
    assert final["status"] == "HANDOFF_BLOCKED_RECOVERY"
    assert final["completion"] is False
    assert final["activation"] == "NOT_RUN"
    assert state["phase"] == "HANDOFF_BLOCKED_RECOVERY"
    assert (store.root / "manifest.sha256").is_file()
    assert not (store.root / "local-handoff.json").exists()
    assert all(event["kind"] != "handoff-ready" for event in events)
    assert events[-1]["kind"] == "handoff-blocked-recovery"


def test_finish_handoff_rejects_activation_blocked_or_unproven_identity(
    tmp_path: Path,
) -> None:
    def executing_store(name: str) -> RunStore:
        store = RunStore.create(
            tmp_path,
            run_id=name,
            request={"through": "local-handoff", "source_publication": True},
            authority={"base_commit": A, "target_commit": B},
        )
        store.record_preflight({"verdict": "PASS", "failures": []})
        store.begin_execution()
        for stage in ("control", "product", "publication", "artifact"):
            store.complete_stage(stage)
        return store

    proof = {
        "installed_updater_source_identity": "1" * 64,
        "installed_updater_wheel_identity": "2" * 64,
        "installed_update_check_receipt_sha256": "3" * 64,
        "extracted_target_preflight_receipt_sha256": "4" * 64,
        "recovery_receipt_sha256": "5" * 64,
    }
    with pytest.raises(StateError, match="post-failure recovery"):
        executing_store("missing-proof").finish_handoff(
            {"status": "HANDOFF_READY", "activation": "NOT_RUN"}
        )
    with pytest.raises(StateError, match="post-failure recovery"):
        executing_store("activation-blocked").finish_handoff(
            {
                "status": "HANDOFF_READY",
                "activation": "NOT_RUN_ACTIVATION_BLOCKED",
                **proof,
            }
        )

    complete = executing_store("complete-proof")
    with pytest.raises(StateError, match="post-failure recovery"):
        complete.finish_handoff(
            {
                "status": "HANDOFF_READY",
                "activation": "NOT_RUN_REQUIRES_SEPARATE_ATTILA_GO_AND_JEROME",
                **proof,
            }
        )
    assert json.loads((complete.root / "state.json").read_text())["phase"] == "EXECUTING"


def test_run_store_open_rejects_bound_authority_candidate_or_config_tamper(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "candidate"
    repo.mkdir()
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "noreply@github.com"], cwd=repo, check=True)
    (repo / "value.txt").write_text("one\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "candidate"], cwd=repo, check=True)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    tree = subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=repo, text=True).strip()
    store = RunStore.create(
        tmp_path,
        run_id="bound-run",
        request={"through": "local-handoff"},
        authority={"base_commit": A, "target_commit": B},
    )
    repo.rename(store.root / "candidate")
    candidate = {"status": "CANDIDATE_READY", "candidate_commit": commit, "candidate_tree": tree}
    config = {"schema_version": 1, "repository": "AIWerk/hermes-agent"}
    (store.root / "candidate.json").write_text(json.dumps(candidate, sort_keys=True, separators=(",", ":")) + "\n")
    (store.root / "execution-config.json").write_text(json.dumps(config, sort_keys=True, separators=(",", ":")) + "\n")
    store.record_preflight({"verdict": "PASS", "failures": []})
    store.bind_execution_inputs(
        candidate=candidate,
        execution_config=config,
        qualified_commit=commit,
        qualified_tree=tree,
    )
    RunStore.open(store.root)

    (store.root / "execution-config.json").write_text('{"schema_version":2}\n')
    with pytest.raises(StateError, match="execution configuration"):
        RunStore.open(store.root)
