"""Exercise actor propagation through the real prompt worker, not an inline thread."""
import json
import os
import threading
from types import SimpleNamespace

import pytest

from agent.cui_actor_context import current_bound_cui_actor_context
from tui_gateway import server


@pytest.mark.parametrize("failure", [False, True], ids=["success", "failure"])
def test_concurrent_turn_actor_authority_and_cleanup(monkeypatch, tmp_path, failure):
    actors = {
        "user": {"tenant_id": "synthetic-a", "actor_id": "alice", "role": "user"},
        "admin": {"tenant_id": "synthetic-b", "actor_id": "operator", "role": "admin"},
        "restricted": {"_restricted": "1"},
        "local": server.sanitize_cui_actor_context(None),
    }
    spoof = {"tenant_id": "forged", "actor_id": "forged", "role": "admin"}
    monkeypatch.setenv("AIWERK_CUI_ACTOR_CONTEXT", json.dumps(spoof))
    env_before = {k: v for k, v in os.environ.items() if k.startswith("AIWERK_CUI_")}
    raw_db = object()
    monkeypatch.setattr(server, "_db", raw_db)
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_CRASH_LOG", str(tmp_path / "crash.log"))
    barrier = threading.Barrier(len(actors))
    seen, cleaned, errors = {}, [], []
    main_thread = threading.get_ident()

    class ObservedThread(threading.Thread):
        def run(self):
            try:
                super().run()
            except BaseException as exc:
                errors.append(exc)
            finally:
                cleaned.append(current_bound_cui_actor_context())

    # Real OS threads and unmodified production actor helper; observe after target returns.
    monkeypatch.setattr(server.threading, "Thread", ObservedThread)
    for name in (
        "_emit", "_wire_callbacks", "_apply_pending_model_switch",
        "_sync_agent_model_with_config", "_sync_agent_compression_with_config",
        "_sync_bot_capabilities", "_register_session_cwd", "_sync_session_key_after_compress",
        "_after_complete_turn", "_publish_session_control_snapshot",
        "_emit_settled_session_info", "_run_post_turn_followups",
    ):
        monkeypatch.setattr(server, name, lambda *a, **kw: None)
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *a: None)
    monkeypatch.setattr(server, "_session_cwd", lambda s: str(tmp_path))
    monkeypatch.setattr(server, "_session_home", lambda s: tmp_path)
    monkeypatch.setattr(server, "_start_turn_voice", lambda: (None, False))
    monkeypatch.setattr(server, "_pending_reaction_notes", lambda s: "")
    monkeypatch.setattr(server, "_hud_surface_note", lambda s: "")
    monkeypatch.setattr(server, "_load_interim_assistant_messages", lambda: False)
    monkeypatch.setattr(server, "_get_usage", lambda a: {})
    monkeypatch.setattr(server, "_goal_followup_after_turn", lambda *a: None)
    monkeypatch.setattr(server, "_start_usage_ticker", lambda *a: (
        threading.Event(), SimpleNamespace(join=lambda: None)))

    def conversation(name):
        def run(*args, **kwargs):
            barrier.wait(timeout=10)
            db = server._get_db()
            seen[name] = {
                "actor": current_bound_cui_actor_context(),
                "compat_actor": server.current_cui_actor_context(),
                "database_actor": getattr(db, "_actor", None),
                "raw": db is raw_db,
                "admin": server._cui_actor_is_admin({"_cui_actor_role": "admin"}),
                "thread": threading.get_ident(),
            }
            if failure:
                raise RuntimeError("synthetic turn failure")
            return {"final_response": "synthetic complete"}
        return run

    for name, actor in actors.items():
        token = server.bind_cui_actor_context(actor)
        try:
            session = server._deferred_session_record(
                name, cols=80, cwd=str(tmp_path), history=[], lease=None)
        finally:
            server.reset_cui_actor_context(token)
        session["agent"] = SimpleNamespace(run_conversation=conversation(name), session_id=name)
        session["running"] = True
        server._sessions[name] = session
        # Caller identity and client-like metadata cannot override the live session authority.
        token = server.bind_cui_actor_context(spoof)
        try:
            assert server._run_prompt_submit(
                "rid", name, session, "synthetic prompt",
                display_metadata={"actor_context": spoof, "_cui_actor_role": "admin"})
            assert current_bound_cui_actor_context() == spoof
        finally:
            server.reset_cui_actor_context(token)

    for session in server._sessions.values():
        session["_run_thread"].join(timeout=15)
        assert not session["_run_thread"].is_alive()
        assert not session["running"]
    assert not errors
    assert set(seen) == set(actors), "real run_conversation was not reached"
    for name, actor in actors.items():
        observed = seen[name]
        assert observed.pop("thread") != main_thread
        assert observed == {
            "actor": actor, "compat_actor": actor,
            "database_actor": actor if actor else None,
            "raw": not bool(actor), "admin": name == "admin",
        }, f"prompt worker lost or replaced {name} authority"
    assert cleaned == [{}] * len(actors)
    assert current_bound_cui_actor_context() == {}
    assert {k: v for k, v in os.environ.items() if k.startswith("AIWERK_CUI_")} == env_before
