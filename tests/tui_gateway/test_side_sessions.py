import threading

from tui_gateway import server


class FakeAgent:
    model = "test-model"
    max_iterations = 3
    reasoning_config = {"enabled": True, "effort": "medium"}

    def __init__(self):
        self.committed = []

    def commit_memory_session(self, history):
        self.committed.append(list(history))

    def _invalidate_system_prompt(self):
        pass


def _session(key="main-session"):
    return {
        "agent": FakeAgent(),
        "agent_ready": threading.Event(),
        "attached_images": [],
        "edit_snapshots": {},
        "history": [
            {"role": "user", "content": "Main topic"},
            {"role": "assistant", "content": "Main answer"},
        ],
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


def test_gateway_side_start_parks_main_and_switches_live_session(tmp_path, monkeypatch):
    import hermes_state

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    db = hermes_state.SessionDB()
    monkeypatch.setattr(server, "_db", db)
    monkeypatch.setattr(server, "_side_source", lambda: "tui-test")
    monkeypatch.setattr(server, "_reset_session_agent", lambda sid, session: {"model": "test-model"})
    monkeypatch.setattr(server, "_notify_session_boundary", lambda *a, **k: None)
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)

    sid = "sid1"
    sess = _session()
    sess["agent_ready"].set()
    server._sessions[sid] = sess
    try:
        resp = server._methods["session.side.start"]("r1", {"session_id": sid, "title": "Quick side"})
    finally:
        server._sessions.pop(sid, None)

    assert "error" not in resp
    result = resp["result"]
    assert result["mode"] == "side"
    assert result["parent_session_id"] == "main-session"
    assert sess["session_key"] == result["side_session_id"]
    assert sess["history"] == []
    active = db.get_active_side_session(source="tui-test")
    assert active["parent_session_id"] == "main-session"
    assert active["side_session_id"] == result["side_session_id"]


def test_gateway_side_back_restores_parked_main_session(tmp_path, monkeypatch):
    import hermes_state

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    db = hermes_state.SessionDB()
    monkeypatch.setattr(server, "_db", db)
    monkeypatch.setattr(server, "_side_source", lambda: "tui-test")
    monkeypatch.setattr(server, "_reset_session_agent", lambda sid, session: {"model": "test-model"})
    monkeypatch.setattr(server, "_notify_session_boundary", lambda *a, **k: None)
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)

    parent_id = "main-session"
    side_id = "side-session"
    db.create_session(parent_id, "tui-test")
    db.create_session(side_id, "tui-test", parent_session_id=parent_id)
    db.append_message(parent_id, "user", "Main topic")
    db.append_message(parent_id, "assistant", "Main answer")
    db.push_side_session("tui-test", parent_id, side_id)

    sid = "sid1"
    sess = _session(side_id)
    sess["agent_ready"].set()
    sess["history"] = [{"role": "user", "content": "Side question"}]
    server._sessions[sid] = sess
    try:
        resp = server._methods["session.side.back"]("r1", {"session_id": sid})
    finally:
        server._sessions.pop(sid, None)

    assert "error" not in resp
    result = resp["result"]
    assert result["mode"] == "main"
    assert result["returned"] is True
    assert sess["session_key"] == parent_id
    assert sess["history"] == [
        {"role": "user", "content": "Main topic"},
        {"role": "assistant", "content": "Main answer"},
    ]
    assert db.get_active_side_session(source="tui-test") is None
