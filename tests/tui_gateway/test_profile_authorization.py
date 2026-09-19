"""Installed-handler authorization including direct callers and real pool workers."""
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock, MagicMock
from contextlib import nullcontext

import pytest

from tests.profile_authorization_support import EMPLOYEE, install_policy, seed_store

METHODS = [
    "session.list", "session.most_recent", "session.create", "session.resume", "session.history",
    "session.status", "session.title", "session.delete", "session.set_hidden", "session.close",
    "session.interrupt", "session.undo", "session.compress", "session.save", "session.branch",
    "session.side.start", "session.side.back", "session.cwd.set", "session.workspace.move",
    "session.active_list", "session.activate", "session.control", "session.control.read",
    "session.events.since", "session.events.stats", "session.usage", "session.context_breakdown",
    "prompt.submit", "prompt.background", "prompt.btw", "config.get", "config.set",
    "profiles.list", "profiles.describe", "profiles.configure", "profiles.create",
    "mcp.catalog", "mcp.servers.list", "mcp.servers.status", "mcp.servers.add",
    "mcp.servers.remove", "tools.configure", "reload.mcp", "model.options",
    "session.foreign.list", "session.foreign.preview", "session.foreign.import",
]


@pytest.fixture
def rpc(tmp_path, monkeypatch):
    env = install_policy(tmp_path, monkeypatch)
    from tui_gateway import server
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_db", None)
    return env, server


def invoke(server, method, params, actor=EMPLOYEE):
    responses = []
    done = threading.Event()
    transport = SimpleNamespace(write=lambda response: (responses.append(response), done.set()))
    result = server.dispatch({"jsonrpc": "2.0", "id": "r", "method": method, "params": params},
                             transport=transport, actor_context=actor)
    if result is not None:
        return result
    assert done.wait(10), "pool worker did not finish"
    return responses[0]


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("entry", ["dispatch", "installed"])
def test_registry_denies_before_scope_db_live_lookup_or_side_effect(rpc, monkeypatch, method, entry):
    env, server = rpc
    assert method in server._methods, f"Census method is not registered: {method}"
    spies = {}
    for name in ["_profile_home", "_profile_db", "_profile_session_db", "_get_db", "_sess", "_sess_nowait", "_pop_session_by_id", "_tts_stream_stop"]:
        if hasattr(server, name):
            db = MagicMock()
            db.list_sessions.return_value = []
            db.search_sessions.return_value = []
            db.get_session.return_value = None
            value = None
            if name in ("_sess", "_sess_nowait"):
                value = (None, server._err("r", 4001, "session not found"))
            elif name == "_profile_db":
                value = nullcontext(db)
            elif name == "_profile_session_db":
                value = (db, False)
            elif name == "_get_db":
                value = db
            spies[name] = Mock(return_value=value)
            monkeypatch.setattr(server, name, spies[name])
    params = {"profile": "other-home", "name": "other-home", "session_id": "live", "session_key": "owned", "title": "new", "message": "hello"}
    try:
        if entry == "dispatch":
            response = invoke(server, method, params)
        else:
            token = server.bind_cui_actor_context(EMPLOYEE)
            try:
                response = server._methods[method]("r", params)
            finally:
                server.reset_cui_actor_context(token)
    finally:
        assert not any(spy.called for spy in spies.values()), "profile resource reached before authorization"
    assert response.get("error", {}).get("message") == "profile access denied", response


@pytest.mark.parametrize("actor", [{}, {"untrusted": "field"}, None, {**EMPLOYEE, "actor_id": []}])
def test_explicit_invalid_actor_does_not_inherit_authority(rpc, monkeypatch, actor):
    _, server = rpc
    monkeypatch.setenv("AIWERK_CUI_ACTOR_CONTEXT", __import__("json").dumps(EMPLOYEE))
    token = server.bind_cui_actor_context(EMPLOYEE)
    try:
        response = invoke(server, "session.list", {}, actor)
    finally:
        server.reset_cui_actor_context(token)
    assert response.get("error", {}).get("message") == "profile access denied", response


@pytest.mark.parametrize("field", ["profile", "name", "clone_from", "session_id", "session_key"])
@pytest.mark.parametrize("value", [True, 0, [], {}, " ", "../x"])
def test_typed_selectors_reject_before_resource_access(rpc, monkeypatch, field, value):
    _, server = rpc
    opened = Mock(return_value=nullcontext(MagicMock()))
    monkeypatch.setattr(server, "_profile_db", opened)
    method = "profiles.create" if field in ("name", "clone_from") else "session.list"
    response = invoke(server, method, {field: value})
    assert not opened.called, "malformed selector reached profile database"
    assert "error" in response and response["error"]["code"] in (-32602, 403), response


@pytest.mark.parametrize("value,expected", [(True, True), (False, False), ("false", False), ("true", True)])
def test_hidden_flag_uses_owning_lifecycle_normalization(rpc, value, expected):
    env, server = rpc
    db, _ = seed_store(env)
    try:
        response = invoke(server, "session.set_hidden", {"profile": "employee-home", "session_id": "owned", "hidden": value})
        assert "result" in response, response
        assert response["result"]["hidden"] is expected
        assert bool(db.get_session("owned")["hidden"]) is expected
        env.revoke(action="session.mutate")
        denied = invoke(server, "session.set_hidden", {"profile": "employee-home", "session_id": "owned", "hidden": not expected})
        assert denied.get("error", {}).get("message") == "profile access denied", denied
        assert bool(db.get_session("owned")["hidden"]) is expected
    finally:
        db.close()


def test_unknown_method_keeps_protocol_error_without_profile_work(rpc, monkeypatch):
    _, server = rpc
    opened = Mock()
    monkeypatch.setattr(server, "_profile_home", opened)
    response = invoke(server, "unknown.method", {"profile": "other-home"})
    assert response["error"]["code"] == -32601 and not opened.called
    response = invoke(server, "session.events.since", [])
    assert response["error"]["code"] == -32602


def test_real_sqlite_list_uses_policy_default_and_rechecks_root(rpc):
    env, server = rpc
    db, _ = seed_store(env)
    db.close()
    result = invoke(server, "session.list", {})
    assert "result" in result, result
    assert "owned" in str(result) and "foreign" not in str(result) and "ambiguous" not in str(result)
    env.revoke(action="session.list")
    assert invoke(server, "session.list", {})["error"]["message"] == "profile access denied"


def test_live_target_conflict_denies_before_pop_or_tts(rpc, monkeypatch):
    env, server = rpc
    server._sessions["live"] = {"session_key": "owned", "profile": "peer-home", "profile_home": str(env.root / "profiles/peer-home"), "cui_actor_context": dict(EMPLOYEE)}
    pop, tts = Mock(return_value=None), Mock()
    monkeypatch.setattr(server, "_pop_session_by_id", pop)
    monkeypatch.setattr(server, "_tts_stream_stop", tts)
    for method in ["session.close", "session.interrupt"]:
        response = invoke(server, method, {"session_id": "live", "profile": "employee-home"})
        assert response.get("error", {}).get("message") == "profile access denied", response
    assert not pop.called and not tts.called


def test_both_registrars_install_outer_guard(rpc):
    from tui_gateway.method_ctx import HandlerRegistry
    _, server = rpc
    seen = []
    registry = HandlerRegistry()
    registry.method("unmapped.profile.consumer")(lambda rid, params: (seen.append(True), server._ok(rid, {}))[1])
    registry.install(server)
    server.method("unmapped.facade.consumer")(lambda rid, params: (seen.append(True), server._ok(rid, {}))[1])
    try:
        for method in ["unmapped.profile.consumer", "unmapped.facade.consumer"]:
            response = invoke(server, method, {})
            assert response.get("error", {}).get("message") == "profile access denied"
        assert not seen
    finally:
        server._methods.pop("unmapped.profile.consumer", None)
        server._methods.pop("unmapped.facade.consumer", None)


def test_queued_worker_rechecks_policy_at_execution(rpc, monkeypatch):
    env, server = rpc
    queued = []
    monkeypatch.setattr(server, "_pool", SimpleNamespace(submit=queued.append))
    responses = []
    transport = SimpleNamespace(write=responses.append)
    server.dispatch({"jsonrpc": "2.0", "id": "r", "method": "session.resume", "params": {"session_id": "owned"}}, transport=transport, actor_context=EMPLOYEE)
    assert len(queued) == 1
    env.revoke(action="session.resume")
    thread = threading.Thread(target=queued[0])
    thread.start()
    thread.join(10)
    assert not thread.is_alive()
    assert responses[0]["error"]["message"] == "profile access denied"


def test_marker_absent_preserves_local_registered_rpc(rpc, monkeypatch):
    env, server = rpc
    env.marker.unlink()
    response = invoke(server, "session.events.stats", {}, actor=None)
    assert "result" in response, response
    import contextvars
    from tests.tui_gateway.test_cui_actor_gateway_context import _patch_session_create_dependencies
    _patch_session_create_dependencies(monkeypatch, env.home)
    monkeypatch.setenv("AIWERK_CUI_ACTOR_CONTEXT", json.dumps(EMPLOYEE))
    def create():
        response = server._methods["session.create"]("legacy", {})
        assert "result" in response, response
        return server._sessions[response["result"]["session_id"]]["cui_actor_context"]
    assert contextvars.Context().run(create) == EMPLOYEE
    def invalid():
        token = server.bind_cui_actor_context({})
        try:
            return create()
        finally:
            server.reset_cui_actor_context(token)
    assert contextvars.Context().run(invalid).get("_restricted")
    env.marker.write_bytes(b"required-v1\n")
    env.marker.chmod(0o600)
    denied = contextvars.Context().run(server._methods["session.create"], "required", {})
    assert denied.get("error", {}).get("message") == "profile access denied", denied


@pytest.mark.parametrize("method", ["session.resume", "session.create", "prompt.submit", "config.get", "session.close"])
def test_admin_read_grant_never_enters_ordinary_rpc_runtime(rpc, monkeypatch, method):
    from tests.profile_authorization_support import ADMIN
    _, server = rpc
    opened = Mock(return_value=None)
    monkeypatch.setattr(server, "_profile_home", opened)
    response = invoke(server, method, {"profile": "employee-home", "session_id": "owned"}, actor=ADMIN)
    assert response.get("error", {}).get("message") == "profile access denied", response
    assert not opened.called


@pytest.fixture
def own_runtime(rpc, monkeypatch):
    from tests.regression.aiwerk.test_111_side_session_resume_isolation import _gateway_session
    env, server = rpc
    db, home = seed_store(env)
    session = _gateway_session("owned")
    session.update(profile="employee-home", profile_home=str(home), cui_actor_context=dict(EMPLOYEE),
                   cwd=str(env.home), active_session_lease=object())
    session["history"] = [{"role": "user", "content": "synthetic hello"}]
    monkeypatch.setattr(server, "_db", db)
    server._sessions["live"] = session
    monkeypatch.setattr(server, "_reset_session_agent", lambda *a: {"model": "test-model"})
    monkeypatch.setattr(server, "_notify_session_boundary", lambda *a, **k: None)
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(server, "_schedule_agent_build", lambda *a, **k: None)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda *a, **k: None)
    monkeypatch.setattr(server, "_maybe_schedule_auto_continue", lambda *a, **k: None)
    yield env, server, db, session
    db.close()


def test_own_real_resume_history_and_title_lifecycle(own_runtime):
    env, server, db, session = own_runtime
    response = invoke(server, "session.resume", {"profile": "employee-home", "session_id": "owned", "lazy": True})
    assert "result" in response, response
    sid = response["result"]["session_id"]
    assert "synthetic hello" in str(response)
    # A live own profile can have read membership while the root default does not.
    env.document["actors"][0]["default_profile"] = "default"
    env.document["memberships"].append(dict(tenant_id="tenant-a", actor_id="employee",
                                             profile_id="default", actions=["profile.discover"]))
    env.save()
    history = invoke(server, "session.history", {"session_id": sid})
    assert "result" in history and "synthetic hello" in str(history), history
    renamed = invoke(server, "session.title", {"session_id": sid, "title": "own title"})
    assert "result" in renamed, renamed
    assert db.get_session("owned")["title"] == "own title"
    server._sessions[sid]["cui_actor_context"] = {**EMPLOYEE, "actor_id": "unmapped"}
    denied = invoke(server, "session.history", {"session_id": sid})
    assert denied.get("error", {}).get("message") == "profile access denied", denied


def test_own_real_side_prompt_and_return_lifecycle(own_runtime, monkeypatch):
    env, server, db, session = own_runtime
    started = invoke(server, "session.side.start", {"session_id": "live"})
    assert "result" in started, started
    side = started["result"]["side_session_id"]
    assert db.get_session(side) is not None
    admitted = threading.Event()
    def run(_rid, _sid, record, text, **kwargs):
        db.append_message(record["session_key"], "user", text)
        record["history"].append({"role": "user", "content": text})
        record["running"] = False
        admitted.set()
    # Only the model-turn boundary is synthetic; admission and side handlers are real.
    monkeypatch.setattr(server, "_run_prompt_submit", run)
    response = invoke(server, "prompt.submit", {"session_id": "live", "text": "side hello"})
    assert "result" in response, response
    assert admitted.wait(10)
    from tests.profile_authorization_support import owner
    original = db.get_session("owned")["model_config"]
    for column, value in (("model_config", json.dumps(owner({**EMPLOYEE, "actor_id": "unmapped"}))),
                          ("profile_name", "peer-home")):
        db._conn.execute(f"UPDATE sessions SET {column}=? WHERE id='owned'", (value,))
        db._conn.commit()
        before = list(db._conn.execute("SELECT * FROM session_stack"))
        boundary = Mock()
        with monkeypatch.context() as scoped:
            scoped.setattr(server, "_commit_gateway_session_boundary", boundary)
            denied = invoke(server, "session.side.back", {"session_id": "live"})
        assert denied.get("error", {}).get("message") == "profile access denied", denied
        assert not boundary.called
        assert list(db._conn.execute("SELECT * FROM session_stack")) == before
        assert session["session_key"] == side
        assert db.get_session("owned")["ended_at"] is not None
        db._conn.execute(f"UPDATE sessions SET {column}=? WHERE id='owned'",
                         (original if column == "model_config" else "employee-home",))
        db._conn.commit()
    returned = invoke(server, "session.side.back", {"session_id": "live"})
    assert "result" in returned, returned
    assert session["session_key"] == "owned"
    assert "side hello" not in str(db.get_messages_as_conversation("owned"))
    assert "side hello" in str(db.get_messages_as_conversation(side))


@pytest.mark.parametrize("selector", ["foreign", "foreign-title"])
def test_rpc_resolved_alias_owner_before_hidden_mutation(rpc, selector):
    env, server = rpc
    db, _ = seed_store(env)
    try:
        db.set_session_title("foreign", "foreign-title")
        response = invoke(server, "session.set_hidden", {"profile": "employee-home", "session_id": selector, "hidden": True})
        assert response.get("error", {}).get("message") == "profile access denied", response
        assert not db.get_session("foreign")["hidden"]
    finally:
        db.close()


@pytest.mark.parametrize("omit", [False, True, "false", "true"])
def test_resume_omit_messages_normalization_at_owner(own_runtime, omit):
    _, server, _, _ = own_runtime
    response = invoke(server, "session.resume", {"profile": "employee-home", "session_id": "owned", "lazy": True, "omit_messages": omit})
    assert "result" in response, response
    assert bool(response["result"]["messages"]) is (omit in (False, "false"))


@pytest.mark.parametrize("selector", ["owned", "owned-title"])
def test_rpc_compression_continuation_rechecks_owner_before_reopen(rpc, monkeypatch, selector):
    from hermes_state import SessionDB
    env, server = rpc
    db, _ = seed_store(env)
    db.set_session_title("owned", "owned-title")
    db._conn.execute("UPDATE sessions SET parent_session_id='owned' WHERE id='foreign'")
    db._conn.execute("UPDATE sessions SET end_reason='compression' WHERE id='owned'")
    db._conn.commit()
    reopen = Mock(side_effect=RuntimeError("unauthorized reopen"))
    monkeypatch.setattr(SessionDB, "reopen_session", reopen)
    try:
        response = invoke(server, "session.resume", {"profile": "employee-home", "session_id": selector})
        assert "error" in response and response["error"]["message"] in ("profile access denied", "session not found"), response
        assert not reopen.called
        assert not server._sessions
        hidden = Mock(wraps=db.set_session_hidden)
        with monkeypatch.context() as scoped:
            scoped.setattr(SessionDB, "set_session_hidden", hidden)
            response = invoke(server, "session.set_hidden", {
                "profile": "employee-home", "session_id": selector, "hidden": True})
        assert response.get("error", {}).get("message") == "profile access denied", response
        assert not hidden.called
        assert not db.get_session("owned")["hidden"]
        assert not db.get_session("foreign")["hidden"]
    finally:
        db.close()


@pytest.mark.parametrize("flag", ["lazy", "eager_build", "defer_history", "omit_messages"])
@pytest.mark.parametrize("value", [False, True, "false", "true"])
def test_resume_flags_never_bypass_revoked_owner_action(rpc, monkeypatch, flag, value):
    env, server = rpc
    env.revoke(action="session.resume")
    opened = Mock(side_effect=RuntimeError("must deny before db"))
    monkeypatch.setattr(server, "_profile_session_db", opened)
    response = invoke(server, "session.resume", {"profile": "employee-home", "session_id": "owned", flag: value})
    assert response.get("error", {}).get("message") == "profile access denied", response
    assert not opened.called


@pytest.mark.parametrize("flag", ["hidden", "follow_profile_config", "room_plumbing"])
@pytest.mark.parametrize("value", [False, True])
def test_create_flags_do_not_grant_employee_external_launch(rpc, monkeypatch, flag, value):
    env, server = rpc
    # Flags cannot restore a revoked create grant. Own-session creation uses
    # profile.use + session.mutate, not the external profile.launch action.
    env.revoke(action="session.mutate")
    home = Mock(side_effect=RuntimeError("must deny before home"))
    monkeypatch.setattr(server, "_profile_home", home)
    response = invoke(server, "session.create", {"profile": "employee-home", flag: value})
    assert response.get("error", {}).get("message") == "profile access denied", response
    assert not home.called


@pytest.mark.parametrize("include", [False, True])
def test_own_rpc_discovery_include_sessions_never_widens_roster(rpc, include, monkeypatch):
    from tests.profile_authorization_support import owner
    env, server = rpc
    db, _ = seed_store(env)
    db.set_session_title("owned", "Bot Chat")
    db.create_session("worker", source="kanban", model_config=owner(EMPLOYEE), profile_name="employee-home")
    db.close()
    home = env.root / "profiles" / "employee-home"
    (home / "config.yaml").write_text("model:\n  default: synthetic-model\n  provider: synthetic-provider\n")
    (home / "profile.yaml").write_text("description: Own description\ndisplay_name: Own name\n")
    resurrection = Mock(side_effect=AssertionError("roster must not resurrect"))
    monkeypatch.setattr(server, "_resurrect_recoverable_canonical", resurrection)
    response = invoke(server, "profiles.list", {"include_sessions": include})
    assert "result" in response, response
    assert "employee-home" in str(response)
    assert "peer-home" not in str(response) and "other-home" not in str(response)
    row, = response["result"]["profiles"]
    assert row["model"] == "synthetic-model"
    assert row["provider"] == "synthetic-provider"
    assert row["description"] == "Own description" and row["display_name"] == "Own name"
    assert isinstance(row["skill_count"], int)
    for key in ("last_session", "worker_session", "canonical_session"):
        assert (key in row) is include
    if include:
        assert row["last_session"]["id"] == "owned"
        assert "synthetic hello" in row["last_session"]["preview"]
        assert row["worker_session"]["id"] == "worker"
        assert row["canonical_session"]["id"] == "owned"
        assert row["canonical_session"]["resolved_id"] == "owned"
        assert row["canonical_session"]["preview"] == row["last_session"]["preview"]
    assert "unmapped hidden" not in str(response) and "ambiguous hidden" not in str(response)
    assert not resurrection.called


@pytest.mark.parametrize("include", [False, True, "false", "true"])
def test_own_list_include_hidden_normalizes_without_widening_ownership(rpc, include):
    env, server = rpc
    db, _ = seed_store(env)
    try:
        db.set_session_hidden("owned", True)
        response = invoke(server, "session.list", {"profile": "employee-home", "include_hidden": include})
        assert "result" in response, response
        ids = {row["id"] for row in response["result"]["sessions"]}
        assert ids == ({"owned"} if include in (True, "true") else set())
    finally:
        db.close()
