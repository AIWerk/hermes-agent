"""Regression coverage for side-session resume lineage isolation (#111)."""

from __future__ import annotations

import importlib.util
import io
import json
import threading
from pathlib import Path

import pytest

from hermes_state import SessionDB
from tui_gateway import server


_SMOKE_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "aiwerk" / "cui_smoke.py"
_SMOKE_SPEC = importlib.util.spec_from_file_location("aiwerk_cui_smoke_111", _SMOKE_SCRIPT)
assert _SMOKE_SPEC is not None and _SMOKE_SPEC.loader is not None
_smoke = importlib.util.module_from_spec(_SMOKE_SPEC)
_SMOKE_SPEC.loader.exec_module(_smoke)


def _messages(db: SessionDB, session_id: str) -> list[str]:
    return [
        str(message.get("content") or "")
        for message in db.get_messages_as_conversation(
            session_id, include_ancestors=True
        )
    ]


def test_side_child_never_hijacks_parent_resume_or_inherits_parent_history(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("parent", source="tui")
    db.append_message("parent", "user", "MAIN-A")
    db.create_session(
        "side",
        source="tui",
        parent_session_id="parent",
        model_config={"_side_from": "parent"},
    )
    db.append_message("side", "user", "SIDE-B")

    assert db.resolve_resume_session_id("parent") == "parent"
    assert _messages(db, "parent") == ["MAIN-A"]
    assert db.resolve_resume_session_id("side") == "side"
    assert _messages(db, "side") == ["SIDE-B"]


def _gateway_session(key: str = "parent") -> dict:
    class Agent:
        model = "test-model"
        max_iterations = 3
        reasoning_config = {"enabled": True}

        def commit_memory_session(self, _history):
            return None

        def _invalidate_system_prompt(self):
            return None

    ready = threading.Event()
    ready.set()
    return {
        "agent": Agent(),
        "agent_ready": ready,
        "attached_images": [],
        "edit_snapshots": {},
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "inflight_turn": None,
        "image_counter": 0,
        "pending_title": None,
        "running": False,
        "session_key": key,
        "show_reasoning": False,
        "slash_worker": None,
        "tool_progress_mode": "all",
        "tool_started_at": {},
    }


def test_gateway_side_flow_persists_marker_and_parent_resume_excludes_side(
    tmp_path, monkeypatch
):
    import hermes_state

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(server, "_db", db)
    monkeypatch.setattr(server, "_side_source", lambda: "tui-test")
    monkeypatch.setattr(
        server, "_reset_session_agent", lambda _sid, _session: {"model": "test-model"}
    )
    monkeypatch.setattr(server, "_notify_session_boundary", lambda *a, **k: None)
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)

    parent_id = "parent"
    db.create_session(parent_id, source="tui-test")
    db.append_message(parent_id, "user", "MAIN-A")
    sid = "live"
    session = _gateway_session(parent_id)
    server._sessions[sid] = session
    try:
        started = server._methods["session.side.start"]("r1", {"session_id": sid})
        assert "error" not in started, started
        side_id = started["result"]["side_session_id"]
        side_row = db.get_session(side_id)
        assert side_row is not None
        assert json.loads(side_row["model_config"])["_side_from"] == parent_id

        def persist_prompt(_rid, _sid, live_session, text, **_kwargs):
            db.append_message(live_session["session_key"], "user", text)
            live_session["history"].append({"role": "user", "content": text})
            live_session["running"] = False

        monkeypatch.setattr(server, "_run_prompt_submit", persist_prompt)
        submitted = server._methods["prompt.submit"](
            "prompt-side", {"session_id": sid, "text": "SIDE-B"}
        )
        assert "error" not in submitted, submitted
        run_thread = session.get("_run_thread")
        assert run_thread is not None
        run_thread.join(timeout=2)
        assert not run_thread.is_alive()
        assert _messages(db, side_id) == ["SIDE-B"]

        returned = server._methods["session.side.back"]("r2", {"session_id": sid})
        assert "error" not in returned, returned

        resumed_id = db.resolve_resume_session_id(parent_id)
        assert resumed_id == parent_id
        assert _messages(db, resumed_id) == ["MAIN-A"]

        resumed = server._methods["session.resume"](
            "r3", {"session_id": parent_id, "cols": 100}
        )
        assert "error" not in resumed, resumed
        serialized_messages = json.dumps(
            resumed["result"].get("messages", []), ensure_ascii=False
        )
        assert "MAIN-A" in serialized_messages
        assert "SIDE-B" not in serialized_messages
    finally:
        server._sessions.pop(sid, None)
        db.close()


def test_compressed_side_resume_keeps_side_lineage_but_stops_before_main(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("main", source="tui")
    db.append_message("main", "user", "MAIN-A")
    db.create_session(
        "side",
        source="tui",
        parent_session_id="main",
        model_config={"_side_from": "main", "max_iterations": 3},
    )
    db.append_message("side", "user", "SIDE-B")
    db.publish_compression_child(
        parent_session_id="side",
        child_session_id="side-cont",
        source="tui",
        messages=[{"role": "assistant", "content": "SIDE-C"}],
        model_config={"_side_from": "main", "max_iterations": 3},
        require_compression_lease=False,
    )

    assert db.resolve_resume_session_id("main") == "main"
    assert db.resolve_resume_session_id("side") == "side-cont"
    assert _messages(db, "side-cont") == ["SIDE-B", "SIDE-C"]
    child = db.get_session("side-cont")
    assert child is not None
    assert "_side_from" not in json.loads(child["model_config"])


def test_startup_backfills_unmarked_legacy_side_rows_idempotently(tmp_path):
    path = tmp_path / "state.db"
    db = SessionDB(path)
    db.create_session("parent", source="tui")
    db.create_session(
        "side",
        source="tui",
        parent_session_id="parent",
        model_config={"max_iterations": 3},
    )
    db.push_side_session("tui", "parent", "side")
    db.close()

    for _ in range(2):
        reopened = SessionDB(path)
        row = reopened.get_session("side")
        assert row is not None
        config = json.loads(row["model_config"])
        assert config == {"max_iterations": 3, "_side_from": "parent"}
        reopened.close()


def test_startup_backfill_replaces_malformed_side_config_to_preserve_boundary(tmp_path):
    path = tmp_path / "state.db"
    db = SessionDB(path)
    db.create_session("parent", source="tui")
    db.append_message("parent", "user", "MAIN")
    db.create_session("side", source="tui", parent_session_id="parent")
    db.append_message("side", "user", "SIDE")
    db.push_side_session("tui", "parent", "side")
    conn = db._conn
    assert conn is not None
    conn.execute(
        "UPDATE sessions SET model_config = ? WHERE id = ?", ("not-json", "side")
    )
    conn.commit()
    db.close()

    reopened = SessionDB(path)
    row = reopened.get_session("side")
    assert row is not None
    assert json.loads(row["model_config"]) == {"_side_from": "parent"}
    assert _messages(reopened, "side") == ["SIDE"]
    reopened.close()


def test_startup_backfill_replaces_nonobject_side_config_to_preserve_boundary(tmp_path):
    path = tmp_path / "state.db"
    db = SessionDB(path)
    db.create_session("parent", source="tui")
    db.append_message("parent", "user", "MAIN")
    db.create_session("side", source="tui", parent_session_id="parent")
    db.append_message("side", "user", "SIDE")
    db.push_side_session("tui", "parent", "side")
    conn = db._conn
    assert conn is not None
    conn.execute("UPDATE sessions SET model_config = '[]' WHERE id = 'side'")
    conn.commit()
    db.close()

    reopened = SessionDB(path)
    row = reopened.get_session("side")
    assert row is not None
    assert json.loads(row["model_config"]) == {"_side_from": "parent"}
    assert reopened.resolve_resume_session_id("parent") == "parent"
    assert _messages(reopened, "side") == ["SIDE"]
    reopened.close()


def test_startup_backfill_skips_inconsistent_links_and_reports_dry_run(tmp_path):
    path = tmp_path / "state.db"
    db = SessionDB(path)
    db.create_session("parent", source="tui")
    db.create_session("other", source="tui")
    db.create_session("side", source="tui", parent_session_id="parent")
    db.push_side_session("tui", "other", "side")
    conn = db._conn
    assert conn is not None

    report = db._backfill_side_session_markers(conn.cursor(), dry_run=True)
    assert report == {
        "candidates": 1,
        "marked": 0,
        "already_marked": 0,
        "skipped_inconsistent": 1,
    }
    row = db.get_session("side")
    assert row is not None
    assert "_side_from" not in json.loads(row["model_config"] or "{}")
    db.close()

    reopened = SessionDB(path)
    row = reopened.get_session("side")
    assert row is not None
    assert "_side_from" not in json.loads(row["model_config"] or "{}")
    reopened.close()


def test_inherited_side_marker_continuation_stops_at_side_root(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("main", source="tui")
    db.append_message("main", "user", "MAIN")
    db.create_session(
        "side",
        source="tui",
        parent_session_id="main",
        model_config={"_side_from": "main"},
    )
    db.append_message("side", "user", "SIDE")
    db.end_session("side", "compression")
    db.create_session(
        "side-cont",
        source="tui",
        parent_session_id="side",
        model_config={"_side_from": "main"},
    )
    db.append_message("side-cont", "assistant", "CONT")

    assert db.get_conversation_root("side-cont") == "side"
    assert _messages(db, "side-cont") == ["SIDE", "CONT"]
    assert db.get_compression_tip("side") == "side-cont"
    assert db.resolve_resume_session_id("side") == "side-cont"
    assert db.get_compression_lineage("side-cont") == ["side", "side-cont"]
    db.close()


def test_backfill_uses_consistent_link_even_when_newer_duplicate_is_invalid(tmp_path):
    path = tmp_path / "state.db"
    db = SessionDB(path)
    db.create_session("parent", source="tui")
    db.create_session("other", source="tui")
    db.create_session("side", source="tui", parent_session_id="parent")
    db.push_side_session("tui", "parent", "side")
    db.push_side_session("tui", "other", "side")
    conn = db._conn
    assert conn is not None

    report = db._backfill_side_session_markers(conn.cursor(), dry_run=True)
    assert report == {
        "candidates": 2,
        "marked": 1,
        "already_marked": 0,
        "skipped_inconsistent": 1,
    }
    db.close()

    reopened = SessionDB(path)
    row = reopened.get_session("side")
    assert row is not None
    assert json.loads(row["model_config"])["_side_from"] == "parent"
    reopened.close()


def test_inconsistent_legacy_side_candidate_remains_isolated(tmp_path):
    path = tmp_path / "state.db"
    db = SessionDB(path)
    db.create_session("parent", source="tui")
    db.append_message("parent", "user", "PARENT-SECRET")
    db.create_session("other", source="tui")
    db.create_session("side", source="tui", parent_session_id="parent")
    db.append_message("side", "user", "SIDE")
    db.push_side_session("tui", "other", "side")
    db.end_session("parent", "compression")
    db.close()

    reopened = SessionDB(path)
    assert reopened.get_compression_lineage("side") == ["side"]
    assert _messages(reopened, "side") == ["SIDE"]
    assert reopened.set_session_archived("parent", True) is True
    side = reopened.get_session("side")
    assert side is not None and side["archived"] == 0
    reopened.close()


def test_missing_legacy_side_evidence_falls_back_to_parent_end_reason(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("parent", source="tui")
    db.append_message("parent", "user", "PARENT-SECRET")
    db.end_session("parent", "side_session")
    db.create_session("side", source="tui", parent_session_id="parent")
    db.append_message("side", "user", "SIDE-ONLY")

    assert db.resolve_resume_session_id("parent") == "parent"
    assert db.get_conversation_root("side") == "side"
    assert _messages(db, "side") == ["SIDE-ONLY"]
    db.close()


def _adoption_pair(db: SessionDB, suffix: str, *, model_config=None):
    donor = f"donor-{suffix}"
    orphan = f"orphan-{suffix}"
    db.create_session(donor, source="tui", session_key=f"agent:tui:{suffix}")
    db.append_message(donor, "user", "DONOR")
    db.create_session(
        orphan,
        source="tui",
        parent_session_id=donor,
        model_config=model_config,
    )
    db.append_message(orphan, "user", "ORPHAN")
    return donor, orphan


def test_parentless_markered_rows_are_never_orphan_adoption_candidates(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("donor", source="tui", session_key="agent:tui:donor")
    db.append_message("donor", "user", "DONOR")
    for marker in ("_branched_from", "_side_from", "_delegate_from"):
        orphan = marker.removeprefix("_")
        db.create_session(
            orphan,
            source="tui",
            model_config={marker: "missing-parent"},
        )
        db.append_message(orphan, "user", "ORPHAN")

    records = db.find_orphaned_gateway_sessions()
    assert {record["orphan_id"] for record in records}.isdisjoint(
        {"branched_from", "side_from", "delegate_from"}
    )
    db.close()


def test_adoption_transaction_revalidates_all_side_isolation_evidence(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    marked_donor, marked_side = _adoption_pair(
        db, "marked", model_config={"_side_from": "donor-marked"}
    )
    stack_donor, stack_side = _adoption_pair(db, "stack")
    db.push_side_session("tui", stack_donor, stack_side)

    assert db.adopt_orphaned_gateway_session(marked_side, marked_donor) is False
    assert db.adopt_orphaned_gateway_session(stack_side, stack_donor) is False
    for donor, side in ((marked_donor, marked_side), (stack_donor, stack_side)):
        donor_row = db.get_session(donor)
        side_row = db.get_session(side)
        assert donor_row is not None and donor_row["end_reason"] is None
        assert side_row is not None and side_row["session_key"] is None
    db.close()


def test_parent_side_session_end_reason_excludes_legacy_orphan_candidate(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    donor, side = _adoption_pair(db, "legacy-side")
    db.end_session(donor, "side_session")

    records = db.find_orphaned_gateway_sessions()
    assert side not in {record["orphan_id"] for record in records}
    assert db.adopt_orphaned_gateway_session(side, donor) is False
    donor_row = db.get_session(donor)
    side_row = db.get_session(side)
    assert donor_row is not None and donor_row["end_reason"] == "side_session"
    assert side_row is not None and side_row["session_key"] is None
    db.close()


def test_compression_lineage_state_mutations_do_not_touch_side_sibling(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("parent", source="tui")
    db.create_session(
        "side",
        source="tui",
        parent_session_id="parent",
        model_config={"_side_from": "parent"},
    )
    db.end_session("parent", "compression")
    db.create_session("continuation", source="tui", parent_session_id="parent")

    assert db.get_compression_lineage("side") == ["side"]
    assert db.set_session_archived("parent", True) is True
    assert db.set_session_pinned("parent", True) is True
    assert db.set_session_hidden("parent", True) is True
    assert db.set_session_read("parent", True) is True

    parent = db.get_session("parent")
    continuation = db.get_session("continuation")
    side = db.get_session("side")
    assert parent is not None and continuation is not None and side is not None
    for row in (parent, continuation):
        assert row["archived"] == 1
        assert row["pinned"] == 1
        assert row["hidden"] == 1
        assert row["last_read_at"] is not None
    assert side["archived"] == 0
    assert side["pinned"] == 0
    assert side["hidden"] == 0
    assert side["last_read_at"] is None
    db.close()


def _ws_frame(direction: str, payload: dict, socket_id: str = "ws-main") -> dict:
    return {
        "method": f"Network.webSocketFrame{direction}",
        "params": {
            "requestId": socket_id,
            "response": {"payloadData": json.dumps(payload)},
        },
    }


def test_cui_smoke_correlates_side_rpc_result_without_accepting_unrelated_frames():
    events = [
        _ws_frame("Sent", {"id": 40, "method": "session.status"}),
        _ws_frame("Sent", {"id": 41, "method": "session.side.start"}),
        _ws_frame("Received", {"id": 40, "result": {"wrong": True}}),
        _ws_frame(
            "Received",
            {
                "id": 41,
                "result": {
                    "side_session_id": "side-41",
                    "parent_session_id": "parent",
                },
            },
        ),
    ]

    assert _smoke.websocket_rpc_result(events, "session.side.start") == {
        "side_session_id": "side-41",
        "parent_session_id": "parent",
    }
    assert _smoke.websocket_rpc_result(events, "session.side.back") is None


def test_cui_smoke_rpc_correlation_binds_request_id_and_websocket():
    events = [
        _ws_frame(
            "Sent",
            {"id": 7, "method": "session.side.start", "params": {}},
            "ws-A",
        ),
        _ws_frame(
            "Sent", {"method": "session.side.start", "params": {}}, "ws-A"
        ),
        _ws_frame(
            "Received", {"id": 7, "result": {"side_session_id": "wrong-B"}}, "ws-B"
        ),
        _ws_frame(
            "Received",
            {"result": {"side_session_id": "wrong-no-id"}},
            "ws-A",
        ),
        _ws_frame(
            "Received", {"id": 7, "result": {"side_session_id": "side"}}, "ws-A"
        ),
    ]

    assert _smoke.websocket_rpc_result(events, "session.side.start") == {
        "side_session_id": "side"
    }


def test_cui_smoke_rpc_correlation_rejects_same_socket_id_reuse():
    events = [
        _ws_frame(
            "Sent",
            {"id": 7, "method": "session.side.start", "params": {}},
            "ws-A",
        ),
        _ws_frame(
            "Sent", {"id": 7, "method": "other.method", "params": {}}, "ws-A"
        ),
        _ws_frame(
            "Received",
            {"id": 7, "result": {"side_session_id": "forged-or-unrelated"}},
            "ws-A",
        ),
    ]

    assert _smoke.websocket_rpc_result(events, "session.side.start") is None


def test_cui_smoke_rpc_correlation_rejects_mixed_envelopes():
    valid_request = _ws_frame(
        "Sent", {"id": 7, "method": "session.side.start", "params": {}}, "ws-A"
    )
    valid_response = _ws_frame(
        "Received", {"id": 7, "result": {"side_session_id": "side"}}, "ws-A"
    )
    assert _smoke.websocket_rpc_result(
        [valid_request, valid_response], "session.side.start"
    ) == {"side_session_id": "side"}

    mixed_sent = _ws_frame(
        "Sent",
        {
            "id": 7,
            "method": "session.side.start",
            "result": {"side_session_id": "forged"},
        },
        "ws-A",
    )
    response = _ws_frame(
        "Received", {"id": 7, "result": {"side_session_id": "forged"}}, "ws-A"
    )
    assert _smoke.websocket_rpc_result([mixed_sent, response], "session.side.start") is None

    mixed_received = _ws_frame(
        "Received",
        {
            "id": 7,
            "method": "server.request",
            "result": {"side_session_id": "forged"},
        },
        "ws-A",
    )
    assert _smoke.websocket_rpc_result(
        [valid_request, mixed_received], "session.side.start"
    ) is None
    assert _smoke.websocket_rpc_result(
        [valid_request, mixed_received, valid_response], "session.side.start"
    ) is None

    duplicate_request = _ws_frame(
        "Sent", {"id": 7, "method": "session.side.start", "params": {}}, "ws-A"
    )
    assert _smoke.websocket_rpc_result(
        [valid_request, duplicate_request, valid_response], "session.side.start"
    ) is None


def test_cui_smoke_waits_for_correlated_rpc_response_from_cdp_events():
    class FakeCDP:
        def __init__(self):
            self.events = [
                _ws_frame("Sent", {"id": 41, "method": "session.side.start"})
            ]
            self.drains = 0

        def drain(self, _seconds):
            self.drains += 1
            self.events.append(
                _ws_frame(
                    "Received",
                    {"id": 41, "result": {"side_session_id": "side-41"}},
                )
            )

    cdp = FakeCDP()
    assert _smoke.wait_for_websocket_rpc(
        cdp, "session.side.start", start_index=0, timeout=1
    ) == {"side_session_id": "side-41"}
    assert cdp.drains == 1


def test_cui_smoke_requires_successful_message_complete_for_the_exact_session():
    submitted = _ws_frame(
        "Sent",
        {
            "id": "w7",
            "method": "prompt.submit",
            "params": {"session_id": "side", "text": "marker"},
        },
        "ws-A",
    )
    unrelated = _ws_frame(
        "Received",
        {
            "method": "event",
            "params": {
                "type": "message.complete",
                "session_id": "opaque-runtime-id",
                "payload": {"status": "complete"},
            },
        },
        "ws-B",
    )
    success = _ws_frame(
        "Received",
        {
            "method": "event",
            "params": {
                "type": "message.complete",
                "session_id": "opaque-runtime-id",
                "payload": {"status": "complete", "text": "done"},
            },
        },
        "ws-A",
    )

    assert _smoke.message_complete_result([submitted, unrelated, success], "marker") == {
        "status": "complete",
        "text": "done",
    }

    failed = _ws_frame(
        "Received",
        {
            "method": "event",
            "params": {
                "type": "message.complete",
                "session_id": "opaque-runtime-id",
                "payload": {"status": "error"},
            },
        },
        "ws-A",
    )
    with pytest.raises(RuntimeError, match="reported an error"):
        _smoke.message_complete_result([submitted, failed], "marker")


def test_cui_smoke_side_checks_fail_closed_for_leak_or_session_switch():
    assert _smoke.side_isolation_checks(
        "parent", "parent", "MAIN-A", "SIDE-B", "parent"
    ) == {
        "side_message_absent_after_reload": True,
        "side_parent_session_preserved": True,
        "side_back_returned_parent": True,
    }
    missing_panel = _smoke.side_isolation_checks(
        "parent", "parent", None, "SIDE-B", "parent"
    )
    assert missing_panel["side_message_absent_after_reload"] is False
    empty_panel = _smoke.side_isolation_checks(
        "parent", "parent", "", "SIDE-B", "parent"
    )
    assert empty_panel["side_message_absent_after_reload"] is False
    assert _smoke.side_isolation_checks(
        "parent", "side", "MAIN-A SIDE-B", "SIDE-B", "side"
    ) == {
        "side_message_absent_after_reload": False,
        "side_parent_session_preserved": False,
        "side_back_returned_parent": False,
    }


def test_cui_smoke_step_log_records_elapsed_time_in_json_and_stderr(monkeypatch):
    clock = iter((10.0, 10.125))
    monkeypatch.setattr(_smoke.time, "monotonic", lambda: next(clock))
    stderr = io.StringIO()
    steps = []
    log = _smoke.StepLog(steps, stderr=stderr)

    assert log.run("wait main turn complete", lambda: "done") == "done"
    assert steps == [
        {
            "name": "wait main turn complete",
            "elapsed_seconds": 0.125,
            "status": "PASS",
        }
    ]
    assert "wait main turn complete" in stderr.getvalue()
    assert "PASS" in stderr.getvalue()


def test_cui_smoke_step_log_names_the_failed_timeout(monkeypatch):
    clock = iter((20.0, 20.5))
    monkeypatch.setattr(_smoke.time, "monotonic", lambda: next(clock))
    stderr = io.StringIO()
    steps = []
    log = _smoke.StepLog(steps, stderr=stderr)

    with pytest.raises(TimeoutError, match="wait side turn complete"):
        log.run(
            "wait side turn complete",
            lambda: (_ for _ in ()).throw(TimeoutError("old unscoped timeout")),
        )

    assert steps == [
        {
            "name": "wait side turn complete",
            "elapsed_seconds": 0.5,
            "status": "FAIL",
        }
    ]
    assert "wait side turn complete" in stderr.getvalue()
    assert "FAIL" in stderr.getvalue()
    assert _smoke.public_error(
        TimeoutError("sensitive detail"), "wait side turn complete"
    ) == {
        "type": "TimeoutError",
        "message": "smoke execution failed",
        "step": "wait side turn complete",
    }


def test_cui_smoke_turn_completion_requires_new_answer_and_no_running_indicator():
    expression = _smoke.turn_completion_expression(
        'aside[aria-label="Nebenunterhaltung"][data-open="true"]', 3
    )

    assert "Diese Antwort vorlesen" in expression
    assert "Der Assistent arbeitet an der Antwort" in expression
    assert "> 3" in expression
    assert 'aside[aria-label=\\"Nebenunterhaltung\\"][data-open=\\"true\\"]' in expression
