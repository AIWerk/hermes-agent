"""Actor authority must survive the real deferred-build worker boundary."""
import json
import os
import pytest
from agent.cui_actor_context import current_bound_cui_actor_context
import sys
import threading
from types import SimpleNamespace

from tui_gateway import server


ACTOR = {
    "tenant_id": "synthetic-tenant",
    "actor_id": "synthetic-customer",
    "role": "user",
}


def test_deferred_agent_build_preserves_actor_scoped_database(monkeypatch, tmp_path):
    # Stub effectful dependencies, NOT the production thread or actor/DB helpers.
    monkeypatch.setitem(
        sys.modules, "tui_gateway.entry",
        SimpleNamespace(ensure_mcp_discovery_started=lambda: None),
    )
    monkeypatch.setattr(server, "_load_show_reasoning", lambda: False)
    monkeypatch.setattr(server, "_load_tool_progress_mode", lambda: "collapsed")
    monkeypatch.setattr(server, "_config_model_target", lambda: None)
    monkeypatch.setattr(server, "_wire_session_agent", lambda *_args: False)
    monkeypatch.setattr(server, "_announce_built_agent", lambda *_args: None)
    monkeypatch.setattr(server, "_emit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "_sessions", {})
    raw_db = object()
    monkeypatch.setattr(server, "_db", raw_db)
    observations = []
    caller_thread = threading.get_ident()
    agent = SimpleNamespace()

    def make_agent(*_args, **_kwargs):
        db = server._get_db()
        observations.append({
            "actor": server.current_cui_actor_context(),
            "raw_database_exposed": db is raw_db,
            "database_actor": getattr(db, "_actor", None),
            "worker_thread": threading.get_ident(),
        })
        return agent

    monkeypatch.setattr(server, "_make_agent", make_agent)
    token = server.bind_cui_actor_context(ACTOR)
    try:
        record = server._deferred_session_record(
            "synthetic-session", cols=80, cwd=str(tmp_path), history=[], lease=None,
        )
        # Positive control: synchronous authority and DB scoping work on baseline.
        assert record["cui_actor_context"] == ACTOR
        assert server.current_cui_actor_context() == ACTOR
        assert server._get_db()._actor == ACTOR
        server._sessions["synthetic-runtime"] = record
        server._start_agent_build("synthetic-runtime", record)
    finally:
        server.reset_cui_actor_context(token)

    worker = record["_agent_build_thread"]
    worker.join(timeout=10)
    assert not worker.is_alive(), "deferred build did not settle"
    assert record["agent_ready"].is_set()
    assert record["agent_error"] is None
    assert record["agent"] is agent
    assert server.current_cui_actor_context() == {}
    assert len(observations) == 1
    observed = observations[0]
    assert observed.pop("worker_thread") != caller_thread
    assert observed == {
        "actor": ACTOR,
        "raw_database_exposed": False,
        "database_actor": ACTOR,
    }, "deferred build lost customer actor authority and exposed the raw session DB"


@pytest.mark.parametrize("failure", [False, True], ids=["success", "failure"])
def test_concurrent_build_actor_authority_and_cleanup(monkeypatch, tmp_path, failure):
    actors = {
        "user": ACTOR,
        "admin": {"tenant_id": "synthetic-b", "actor_id": "operator", "role": "admin"},
        "restricted": {"_restricted": "1"},
        "local": server.sanitize_cui_actor_context(None),
    }
    spoof = {"tenant_id": "forged", "actor_id": "forged", "role": "admin"}
    monkeypatch.setenv("AIWERK_CUI_ACTOR_CONTEXT", json.dumps(spoof))
    env_before = {k: v for k, v in os.environ.items() if k.startswith("AIWERK_CUI_")}
    monkeypatch.setitem(sys.modules, "tui_gateway.entry", SimpleNamespace(
        ensure_mcp_discovery_started=lambda: None))
    monkeypatch.setattr(server, "_load_show_reasoning", lambda: False)
    monkeypatch.setattr(server, "_load_tool_progress_mode", lambda: "collapsed")
    monkeypatch.setattr(server, "_config_model_target", lambda: None)
    monkeypatch.setattr(server, "_wire_session_agent", lambda *a: False)
    monkeypatch.setattr(server, "_announce_built_agent", lambda *a: None)
    monkeypatch.setattr(server, "_emit", lambda *a, **kw: None)
    monkeypatch.setattr(server, "_sessions", {})
    raw_db = object()
    monkeypatch.setattr(server, "_db", raw_db)
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

    monkeypatch.setattr(server.threading, "Thread", ObservedThread)

    def make_agent(sid, key, **kwargs):
        barrier.wait(timeout=10)
        db = server._get_db()
        seen[sid] = {
            "actor": current_bound_cui_actor_context(),
            "compat_actor": server.current_cui_actor_context(),
            "database_actor": getattr(db, "_actor", None),
            "raw": db is raw_db,
            "admin": server._cui_actor_is_admin({"_cui_actor_role": "admin"}),
            "thread": threading.get_ident(),
        }
        if failure:
            raise RuntimeError("synthetic build failure")
        return SimpleNamespace()

    monkeypatch.setattr(server, "_make_agent", make_agent)
    for name, actor in actors.items():
        token = server.bind_cui_actor_context(actor)
        try:
            record = server._deferred_session_record(
                name, cols=80, cwd=str(tmp_path), history=[], lease=None)
        finally:
            server.reset_cui_actor_context(token)
        server._sessions[name] = record
        token = server.bind_cui_actor_context(spoof)
        try:
            server._start_agent_build(name, record)
            assert current_bound_cui_actor_context() == spoof
        finally:
            server.reset_cui_actor_context(token)

    for record in server._sessions.values():
        record["_agent_build_thread"].join(timeout=15)
        assert not record["_agent_build_thread"].is_alive()
        assert record["agent_ready"].is_set()
        assert record["agent_error"] == ("synthetic build failure" if failure else None)
    assert not errors
    assert set(seen) == set(actors)
    for name, actor in actors.items():
        observed = seen[name]
        assert observed.pop("thread") != main_thread
        assert observed == {
            "actor": actor, "compat_actor": actor,
            "database_actor": actor if actor else None,
            "raw": not bool(actor), "admin": name == "admin",
        }, f"build worker lost or replaced {name} authority"
    assert cleaned == [{}] * len(actors)
    assert current_bound_cui_actor_context() == {}
    assert {k: v for k, v in os.environ.items() if k.startswith("AIWERK_CUI_")} == env_before
