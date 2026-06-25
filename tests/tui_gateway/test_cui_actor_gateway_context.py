import json
import os

from tui_gateway import server


def test_dispatch_session_create_stores_authenticated_cui_actor(monkeypatch, tmp_path):
    server._sessions.clear()
    monkeypatch.setattr(server, "_start_agent_build", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "_completion_cwd", lambda params=None: str(tmp_path))
    monkeypatch.setattr(server, "_load_show_reasoning", lambda: False)
    monkeypatch.setattr(server, "_load_tool_progress_mode", lambda: "collapsed")

    actor = {
        "tenant_id": "meerwohnen",
        "actor_id": "aiwerk:attila:admin",
        "role": "admin",
        "display_name": "Attila",
        "provider": "dashboard_auth",
        "ignored": "must-not-persist",
    }
    try:
        resp = server.dispatch(
            {"jsonrpc": "2.0", "id": "r1", "method": "session.create", "params": {"cols": 80}},
            actor_context=actor,
        )
        sid = resp["result"]["session_id"]
        stored = server._sessions[sid]["cui_actor_context"]
        assert stored == {
            "tenant_id": "meerwohnen",
            "actor_id": "aiwerk:attila:admin",
            "role": "admin",
            "display_name": "Attila",
            "provider": "dashboard_auth",
        }
        assert server.current_cui_actor_context() is None
    finally:
        for sess in list(server._sessions.values()):
            server._teardown_session(sess)
        server._sessions.clear()


def test_apply_cui_actor_env_is_scoped_and_restored(monkeypatch):
    monkeypatch.setenv("AIWERK_CUI_ACTOR_ROLE", "old-role")
    actor = {
        "tenant_id": "meerwohnen",
        "actor_id": "aiwerk:attila:admin",
        "role": "admin",
        "display_name": "Attila",
        "ignored": "must-not-leak",
    }

    tokens = server._apply_cui_actor_env(actor)
    try:
        raw = os.environ["AIWERK_CUI_ACTOR_CONTEXT"]
        data = json.loads(raw)
        assert data["actor_id"] == "aiwerk:attila:admin"
        assert data["role"] == "admin"
        assert data["display_name"] == "Attila"
        assert "ignored" not in data
        assert os.environ["AIWERK_CUI_TENANT_ID"] == "meerwohnen"
        assert os.environ["AIWERK_CUI_ACTOR_ID"] == "aiwerk:attila:admin"
        assert os.environ["AIWERK_CUI_ACTOR_ROLE"] == "admin"
        assert os.environ["AIWERK_CUI_MANAGED_AUTONOMY"] == "1"
    finally:
        server._clear_cui_actor_env(tokens)

    assert "AIWERK_CUI_ACTOR_CONTEXT" not in os.environ
    assert os.environ["AIWERK_CUI_ACTOR_ROLE"] == "old-role"
    assert "AIWERK_CUI_MANAGED_AUTONOMY" not in os.environ
