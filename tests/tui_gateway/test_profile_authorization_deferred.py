"""Admission and delayed worker revocation must precede resource work."""
import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.profile_authorization_support import EMPLOYEE, install_policy


class Socket:
    def __init__(self, before_frame=lambda: None):
        self.client = SimpleNamespace(host="127.0.0.1", port=1234)
        self.scope = {}
        self.query_params = {}
        self.accepted = False
        self.closed = None
        self.sent = []
        self.before_frame = before_frame
        self.received = False

    async def accept(self, **kwargs):
        self.accepted = True

    async def close(self, **kwargs):
        self.closed = kwargs

    async def send_text(self, text):
        self.sent.append(json.loads(text))

    async def receive_text(self):
        from starlette.websockets import WebSocketDisconnect
        if self.received:
            raise WebSocketDisconnect(1000)
        self.received = True
        self.before_frame()
        return json.dumps({"jsonrpc": "2.0", "id": "r", "method": "gateway.ping", "params": {}})


@pytest.fixture
def env(tmp_path, monkeypatch):
    return install_policy(tmp_path, monkeypatch)


@pytest.mark.parametrize("actor", [None, {}, {"user_id": "internal", "provider": "internal"}, {**EMPLOYEE, "actor_id": "unknown"}])
def test_ws_admission_denies_before_accept_skin_or_bootstrap(env, monkeypatch, actor):
    from tui_gateway import ws
    socket = Socket()
    skin = Mock(return_value={})
    monkeypatch.setattr(ws.server, "resolve_skin", skin)
    for name in ["_ensure_skin_watcher", "_start_backend_heartbeat_refresher", "_schedule_startup_orphan_sweep"]:
        monkeypatch.setattr(ws.server, name, Mock())
    asyncio.run(ws.handle_ws(socket, auth_identity=actor))
    assert not socket.accepted and not skin.called
    assert socket.closed["code"] == 4403


def test_open_ws_rechecks_policy_before_heartbeat_fast_path(env, monkeypatch):
    from tui_gateway import ws
    socket = Socket(lambda: env.revoke(action="profile.use"))
    monkeypatch.setattr(ws.server, "resolve_skin", lambda: {})
    for name in ["_ensure_skin_watcher", "_start_backend_heartbeat_refresher", "_schedule_startup_orphan_sweep", "register_live_transport", "unregister_live_transport"]:
        monkeypatch.setattr(ws.server, name, lambda *a: None)
    asyncio.run(ws.handle_ws(socket, auth_identity=EMPLOYEE))
    assert socket.accepted
    assert not any(frame.get("id") == "r" and "result" in frame for frame in socket.sent)
    assert socket.closed or any("error" in frame for frame in socket.sent)


@pytest.mark.parametrize("entry", ["registered", "direct"])
def test_active_gateway_and_direct_handler_share_policy_admission(env, monkeypatch, entry):
    from hermes_cli import web_server
    from hermes_cli.web_routers import chat_ws
    from tui_gateway import ws
    socket = Socket()
    socket._hermes_auth_identity = dict(EMPLOYEE)
    env.revoke(action="profile.use")
    async def allow(*args, **kwargs):
        return True
    monkeypatch.setattr(chat_ws, "_close_unless_sidecar_allowed", allow)
    monkeypatch.setattr(web_server, "_assistant_mode_enabled", lambda: False)
    monkeypatch.setattr(web_server, "_DASHBOARD_EMBEDDED_CHAT_ENABLED", True)
    monkeypatch.setattr(web_server, "_ws_auth_ok", lambda _ws: True)
    monkeypatch.setattr(web_server, "_ws_request_is_allowed", lambda _ws: True)
    monkeypatch.setattr(ws.server, "resolve_skin", Mock(return_value={}))
    handler = chat_ws.gateway_ws if entry == "direct" else next(
        route.endpoint for route in web_server.app.routes if getattr(route, "path", None) == "/api/ws")
    asyncio.run(handler(socket))
    assert not socket.accepted and not ws.server.resolve_skin.called


def test_deferred_real_build_thread_rechecks_before_scope_and_db(env, monkeypatch):
    from tui_gateway import server
    ready = threading.Event()
    session = dict(session_key="owned", profile="employee-home", profile_home=str(env.root / "profiles/employee-home"),
                   cui_actor_context=dict(EMPLOYEE), agent_ready=ready)
    monkeypatch.setattr(server, "_sessions", {"live": session})
    monkeypatch.setattr(server, "_await_resume_history", lambda *args: True)
    scope, db, build = Mock(return_value=[]), Mock(), Mock()
    monkeypatch.setattr(server, "_bind_build_profile_scopes", scope)
    monkeypatch.setattr(server, "_open_profile_session_db", db)
    monkeypatch.setattr(server, "_make_agent", build)
    monkeypatch.setattr(server, "_finish_agent_build", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "_emit", lambda *args: None)
    env.revoke(action="profile.use")
    server._start_agent_build("live", session)
    assert ready.wait(10)
    assert not scope.called and not db.called and not build.called
    assert "profile access denied" in session.get("agent_error", "")


def test_deferred_hydration_rechecks_before_reopen_or_history(env, monkeypatch):
    from tui_gateway import server
    ready = threading.Event()
    session = dict(session_key="owned", profile="employee-home", profile_home=str(env.root / "profiles/employee-home"),
                   cui_actor_context=dict(EMPLOYEE), resume_history_ready=ready, agent_ready=threading.Event())
    monkeypatch.setattr(server, "_sessions", {"live": session})
    monkeypatch.setattr(server, "_emit", lambda *args: None)
    db = Mock()
    env.revoke(action="session.resume")
    server._schedule_resume_hydration("live", "owned", db)
    assert ready.wait(10)
    assert not db.reopen_session.called
    assert not db.get_resume_conversations.called


def test_compute_host_frame_carries_server_identity_not_client_fields(env):
    from tui_gateway import server
    session = dict(session_key="owned", profile="employee-home", profile_home=str(env.root / "profiles/employee-home"),
                   cui_actor_context=dict(EMPLOYEE), history_lock=threading.Lock(), history=[], cwd=str(env.home))
    frame = server._compute_host_turn_frame("r", "live", session, "hello")
    assert frame.get("cui_actor_context") == EMPLOYEE
    assert frame.get("profile") == "employee-home"


@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize("fault", ["revoked", "missing-actor", "conflicting-profile"])
def test_compute_child_rechecks_before_build_or_cached_rebind(env, monkeypatch, cached, fault):
    import io
    from tui_gateway import server
    from tui_gateway.compute_host import ComputeHost
    original = {"profile": "employee-home", "profile_home": str(env.root / "profiles/employee-home"),
                "cui_actor_context": dict(EMPLOYEE), "transport": object()}
    monkeypatch.setattr(server, "_sessions", {"live": original.copy()} if cached else {})
    host = ComputeHost(stdout=io.StringIO(), heartbeat_secs=0)
    built = Mock(return_value={})
    monkeypatch.setattr(host, "_build_server_session", built)
    frame = {"sid": "live", "session_key": "owned", "profile": "employee-home",
             "profile_home": original["profile_home"], "cui_actor_context": dict(EMPLOYEE)}
    if fault == "revoked":
        env.revoke(action="profile.use")
    elif fault == "missing-actor":
        frame.pop("cui_actor_context")
    else:
        frame["profile"] = "other-home"
        frame["profile_home"] = str(env.root / "profiles/other-home")
    denied = None
    try:
        try:
            host._ensure_server_session(server, frame)
        except Exception as exc:
            denied = exc
        assert not built.called, "unauthorized child reached agent build"
        if cached:
            assert server._sessions["live"] == original, "unauthorized child rebound cached runtime"
        assert denied is not None and str(denied) == "profile access denied"
    finally:
        host.close()


def test_compute_child_control_revocation_precedes_mutator(env, monkeypatch):
    import io
    from tui_gateway import server
    from tui_gateway.compute_host import ComputeHost
    session = {"profile": "employee-home", "cui_actor_context": dict(EMPLOYEE)}
    monkeypatch.setattr(server, "_sessions", {"live": session})
    output = io.StringIO()
    host = ComputeHost(stdout=output, heartbeat_secs=0)
    mutate = Mock(return_value={})
    monkeypatch.setattr(host, "_control_ack", mutate)
    env.revoke(action="session.mutate")
    try:
        host._handle_control({"sid": "live", "request_id": "r", "route_name": "session.save",
                              "profile": "employee-home", "cui_actor_context": dict(EMPLOYEE)})
        assert not mutate.called
        assert "profile access denied" in output.getvalue()
    finally:
        host.close()


def test_marker_absent_keeps_legacy_websocket_admission(env, monkeypatch):
    from tui_gateway import ws
    env.marker.unlink()
    socket = Socket()
    monkeypatch.setattr(ws.server, "resolve_skin", lambda: {})
    for name in ["_ensure_skin_watcher", "_start_backend_heartbeat_refresher", "_schedule_startup_orphan_sweep", "register_live_transport", "unregister_live_transport"]:
        monkeypatch.setattr(ws.server, name, lambda *a: None)
    asyncio.run(ws.handle_ws(socket, auth_identity=None))
    assert socket.accepted
    assert any(frame.get("id") == "r" and "result" in frame for frame in socket.sent)


@pytest.mark.parametrize("path", ["/api/console", "/api/pty", "/api/pub", "/api/events", "/api/audio/speak-stream"])
def test_non_gateway_ws_required_policy_denies_before_accept_or_resource(env, monkeypatch, path):
    from hermes_cli import web_server
    from hermes_cli.web_routers import chat_ws, audio
    socket = Socket()
    socket._hermes_auth_identity = dict(EMPLOYEE)
    socket.scope["auth_identity"] = dict(EMPLOYEE)
    socket.query_params = {"profile": "employee-home", "channel": "synthetic"}
    socket.app = web_server.app
    # Keep the real pre-accept gates; only transport verification is synthetic.
    monkeypatch.setattr(web_server, "_assistant_mode_enabled", lambda: False)
    monkeypatch.setattr(web_server, "_DASHBOARD_EMBEDDED_CHAT_ENABLED", True)
    monkeypatch.setattr(chat_ws, "_ws_auth_reason", lambda ws: (None, "verified"))
    monkeypatch.setattr(chat_ws, "_ws_auth_mode", lambda: "test")
    monkeypatch.setattr(chat_ws, "_ws_host_origin_reason", lambda ws: None)
    monkeypatch.setattr(chat_ws, "_ws_client_reason", lambda ws: None)
    monkeypatch.setattr(web_server, "_ws_auth_ok", lambda ws: True)
    monkeypatch.setattr(web_server, "_ws_request_is_allowed", lambda ws: True)
    monkeypatch.setattr(audio, "_ws_auth_ok", lambda ws: True)
    monkeypatch.setattr(audio, "_ws_request_is_allowed", lambda ws: True)
    env.revoke(action="profile.use")
    sinks = []
    for target, name in [(web_server, "_resolve_profile_dir"), (audio, "_config_profile_scope")]:
        spy = Mock(side_effect=RuntimeError("resource reached"))
        monkeypatch.setattr(target, name, spy)
        sinks.append(spy)
    # Stop an unguarded route immediately upon acceptance, before any spawn/config.
    async def stop_on_accept(**kwargs):
        socket.accepted = True
        raise AssertionError("accepted unsupported policy-required socket")
    socket.accept = stop_on_accept
    handler = next(route.endpoint for route in web_server.app.routes if getattr(route, "path", None) == path)
    asyncio.run(handler(socket))
    assert not socket.accepted and not any(s.called for s in sinks)
    assert socket.closed and socket.closed["code"] == 4403


@pytest.mark.parametrize("concurrent", [False, True])
def test_compute_child_verified_actor_bound_during_real_turn_and_cleaned(env, monkeypatch, concurrent):
    import io
    from concurrent.futures import ThreadPoolExecutor
    from agent.cui_actor_context import current_cui_actor_context
    from tests.tui_gateway.test_compute_host_turn_protocol import _session, _agent
    from tui_gateway import server
    from tui_gateway.compute_host import ComputeHost
    identities = [EMPLOYEE, {**EMPLOYEE, "actor_id": "peer"}] if concurrent else [EMPLOYEE]
    sessions = {}
    frames = []
    for actor in identities:
        sid = actor["actor_id"]
        profile = "employee-home" if sid == "employee" else "peer-home"
        record = _session(_agent(["ok"]))
        record.update(session_key=sid, profile=profile, profile_home=str(env.root / "profiles" / profile),
                      cui_actor_context=dict(actor))
        sessions[sid] = record
        frames.append(dict(sid=sid, request_id=sid, profile=profile, profile_home=record["profile_home"],
                           cui_actor_context=dict(actor), text="hello"))
    monkeypatch.setattr(server, "_sessions", sessions)
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda *a: None)
    monkeypatch.setattr(server, "_persist_branch_seed", lambda *a: None)
    monkeypatch.setattr(server, "_session_info", lambda *a: {})
    seen = {}
    barrier = threading.Barrier(len(identities))
    def model_boundary(rid, sid, session, text, **kwargs):
        barrier.wait(10)
        seen[sid] = current_cui_actor_context()
        session["running"] = False
    monkeypatch.setattr(server, "_run_prompt_submit", model_boundary)
    out = io.StringIO()
    host = ComputeHost(stdout=out, heartbeat_secs=0)
    def run(frame):
        before = current_cui_actor_context()
        host._run_real_turn(frame)
        assert current_cui_actor_context() == before
    try:
        with ThreadPoolExecutor(max_workers=len(identities)) as executor:
            list(executor.map(run, frames))
        assert seen == {a["actor_id"]: a for a in identities}
        emitted = [json.loads(line) for line in out.getvalue().splitlines()]
        assert {f["sid"] for f in emitted if f["type"] == "turn.end"} == set(sessions)
        assert not any(f["type"] == "turn.error" for f in emitted)
    finally:
        host.close()



def test_compute_child_new_build_receives_verified_actor_and_cleans_scope(env, monkeypatch):
    import io
    from agent.cui_actor_context import current_cui_actor_context
    from hermes_constants import get_hermes_home
    from tests.profile_authorization_support import seed_store
    from tests.tui_gateway.test_compute_host_turn_protocol import _agent
    from tui_gateway import server
    from tui_gateway.compute_host import ComputeHost
    db, home = seed_store(env)
    db.close()
    monkeypatch.setattr(server, "_sessions", {})
    seen = []
    def make_agent(*args, **kwargs):
        seen.append((current_cui_actor_context(), get_hermes_home()))
        return _agent(["ok"])
    monkeypatch.setattr(server, "_make_agent", make_agent)
    from tests.tui_gateway.test_compute_host_turn_protocol import _session
    def init_record(sid, key, agent, history, **kwargs):
        server._sessions[sid] = {**_session(agent), "session_key": key, "history": history}
    # Initialization's external workers are outside the child build/auth boundary.
    monkeypatch.setattr(server, "_init_session", init_record)
    monkeypatch.setattr(server, "_load_show_reasoning", lambda: False)
    monkeypatch.setattr(server, "_load_tool_progress_mode", lambda: "all")
    host = ComputeHost(stdout=io.StringIO(), heartbeat_secs=0)
    before_actor, before_home = current_cui_actor_context(), get_hermes_home()
    try:
        record = host._ensure_server_session(server, dict(sid="new", session_key="owned", profile="employee-home",
                      profile_home=str(home), cui_actor_context=dict(EMPLOYEE)))
        assert seen == [(EMPLOYEE, home)]
        assert record["cui_actor_context"] == EMPLOYEE
        assert record["profile"] == "employee-home"
        assert current_cui_actor_context() == before_actor
        assert get_hermes_home() == before_home
    finally:
        host.close()
