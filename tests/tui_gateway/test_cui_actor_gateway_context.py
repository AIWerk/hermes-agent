import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from tui_gateway import server


TRUSTED_ACTOR = {
    "tenant_id": "tenant-a",
    "actor_id": "actor-a",
    "role": "user",
    "display_name": "Actor A",
    "user_id": "user-a",
    "provider": "basic",
}


def _patch_session_create_dependencies(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_schedule_agent_build", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda: None)
    monkeypatch.setattr(server, "_completion_cwd", lambda params=None: str(tmp_path))
    monkeypatch.setattr(server, "_profile_home", lambda profile=None: None)
    monkeypatch.setattr(server, "_load_show_reasoning", lambda: False)
    monkeypatch.setattr(server, "_load_tool_progress_mode", lambda: "collapsed")


def test_dispatch_stores_only_server_actor_allowlist_and_resets(monkeypatch, tmp_path):
    _patch_session_create_dependencies(monkeypatch, tmp_path)
    server._sessions.clear()
    actor = {**TRUSTED_ACTOR, "minted_at": 123, "arbitrary": "drop"}
    try:
        response = server.dispatch(
            {
                "jsonrpc": "2.0",
                "id": "create",
                "method": "session.create",
                "params": {
                    "_cui_actor_role": "admin",
                    "actor_context": {"tenant_id": "evil", "role": "admin"},
                },
            },
            actor_context=actor,
        )
        sid = response["result"]["session_id"]
        assert server._sessions[sid]["cui_actor_context"] == TRUSTED_ACTOR
        assert server.current_cui_actor_context() == {}
    finally:
        server._sessions.clear()


def test_dispatch_resets_actor_context_after_handler_error(monkeypatch):
    def explode(_request):
        assert server.current_cui_actor_context() == TRUSTED_ACTOR
        raise RuntimeError("boom")

    monkeypatch.setattr(server, "handle_request", explode)
    with pytest.raises(RuntimeError, match="boom"):
        server.dispatch(
            {"jsonrpc": "2.0", "id": "x", "method": "ping", "params": {}},
            actor_context=TRUSTED_ACTOR,
        )
    assert server.current_cui_actor_context() == {}


def test_dispatch_explicit_invalid_actor_keeps_database_scoped(monkeypatch):
    raw_db = object()
    seen = {}
    monkeypatch.setattr(server, "_db", raw_db)

    def inspect(_request):
        db = server._get_db()
        seen["is_raw"] = db is raw_db
        seen["actor"] = getattr(db, "_actor", None)
        return {"id": "x", "result": True}

    monkeypatch.setattr(server, "handle_request", inspect)
    response = server.dispatch(
        {"jsonrpc": "2.0", "id": "x", "method": "ping", "params": {}},
        actor_context={"tenant_id": "tenant-a", "actor_id": "actor-a", "role": "unknown"},
    )

    assert response == {"id": "x", "result": True}
    assert seen == {"is_raw": False, "actor": {"_restricted": "1"}}
    assert server.current_cui_actor_context() == {}


def test_concurrent_dispatches_do_not_bleed_actor_context(monkeypatch):
    barrier = threading.Barrier(2)
    seen = {}

    def inspect(request):
        barrier.wait(timeout=5)
        seen[request["id"]] = server.current_cui_actor_context()
        return {"id": request["id"], "result": True}

    monkeypatch.setattr(server, "handle_request", inspect)
    actors = {
        "a": {"tenant_id": "tenant-a", "actor_id": "actor-a", "role": "user"},
        "b": {"tenant_id": "tenant-b", "actor_id": "actor-b", "role": "support"},
    }
    threads = [
        threading.Thread(
            target=server.dispatch,
            args=({"jsonrpc": "2.0", "id": name, "method": "ping", "params": {}},),
            kwargs={"actor_context": actor},
        )
        for name, actor in actors.items()
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert seen == actors
    assert server.current_cui_actor_context() == {}


def test_deferred_record_captures_allowlisted_dispatch_actor():
    token = server.bind_cui_actor_context({**TRUSTED_ACTOR, "ignored": "drop"})
    try:
        record = server._deferred_session_record(
            "stored-session",
            cols=80,
            cwd="/tmp",
            history=[],
            lease=None,
        )
    finally:
        server.reset_cui_actor_context(token)

    assert record["cui_actor_context"] == TRUSTED_ACTOR
    assert server.current_cui_actor_context() == {}


def test_deferred_record_preserves_trusted_no_actor_authority():
    token = server.bind_cui_actor_context(None)
    try:
        record = server._deferred_session_record(
            "trusted-session",
            cols=80,
            cwd="/tmp",
            history=[],
            lease=None,
        )
    finally:
        server.reset_cui_actor_context(token)

    assert record["cui_actor_context"] == {}
    assert not record["cui_actor_context"]
    rebound = server.bind_cui_actor_context(record["cui_actor_context"])
    try:
        assert server.current_cui_actor_context() == {}
        assert not server.current_cui_actor_context()
    finally:
        server.reset_cui_actor_context(rebound)


def test_session_actor_runner_rebinds_and_resets_on_success_and_error():
    observations = []

    def success():
        observations.append(server.current_cui_actor_context())
        return "ok"

    assert server._run_with_cui_actor_context(TRUSTED_ACTOR, success) == "ok"
    assert observations == [TRUSTED_ACTOR]
    assert server.current_cui_actor_context() == {}

    def failure():
        observations.append(server.current_cui_actor_context())
        raise ValueError("turn failed")

    with pytest.raises(ValueError, match="turn failed"):
        server._run_with_cui_actor_context(TRUSTED_ACTOR, failure)
    assert observations[-1] == TRUSTED_ACTOR
    assert server.current_cui_actor_context() == {}


class _GatewaySocket:
    def __init__(self, request):
        self.request = request
        self.sent = []
        self.client = SimpleNamespace(host="127.0.0.1", port=1234)
        self.scope = {}
        self._received = False

    async def accept(self, **_kwargs):
        return None

    async def send_text(self, text):
        self.sent.append(text)

    async def receive_text(self):
        if not self._received:
            self._received = True
            return json.dumps(self.request)
        from starlette.websockets import WebSocketDisconnect

        raise WebSocketDisconnect(code=1000)

    async def close(self, **_kwargs):
        return None


@pytest.mark.asyncio
async def test_handle_ws_passes_server_identity_not_client_rpc_actor(monkeypatch):
    from tui_gateway import ws as gateway_ws

    request = {
        "jsonrpc": "2.0",
        "id": "rpc",
        "method": "ping",
        "params": {
            "actor_context": {"tenant_id": "evil", "role": "admin"},
            "_cui_actor_role": "admin",
        },
    }
    socket = _GatewaySocket(request)
    captured = {}

    def fake_dispatch(req, transport, actor_context=None):
        captured["request"] = req
        captured["actor_context"] = actor_context
        return {"jsonrpc": "2.0", "id": req["id"], "result": True}

    monkeypatch.setattr(gateway_ws.server, "dispatch", fake_dispatch)
    monkeypatch.setattr(gateway_ws.server, "resolve_skin", lambda: {})
    monkeypatch.setattr(gateway_ws.server, "_ensure_skin_watcher", lambda: None)
    monkeypatch.setattr(gateway_ws.server, "register_live_transport", lambda _transport: None)
    monkeypatch.setattr(gateway_ws.server, "unregister_live_transport", lambda _transport: None)
    monkeypatch.setattr(gateway_ws.server, "_schedule_startup_orphan_sweep", lambda: None)
    monkeypatch.setattr(gateway_ws, "_disable_nagle", lambda _ws: None)

    await gateway_ws.handle_ws(socket, auth_identity=TRUSTED_ACTOR)

    assert captured["actor_context"] == TRUSTED_ACTOR
    assert captured["request"]["params"]["actor_context"]["tenant_id"] == "evil"


def _owned_row(session_id, actor, *, visibility_scope="customer"):
    return {
        "id": session_id,
        "source": "tui",
        "model_config": json.dumps(
            {
                "_cui_visibility_scope": visibility_scope,
                "_cui_actor_role": actor["role"],
                "_cui_actor_id": actor["actor_id"],
                "_cui_tenant_id": actor["tenant_id"],
            }
        ),
    }


def test_gateway_persisted_lookup_makes_foreign_and_missing_indistinguishable():
    other = {**TRUSTED_ACTOR, "actor_id": "actor-b"}
    rows = {
        "own": _owned_row("own", TRUSTED_ACTOR),
        "foreign": _owned_row("foreign", other),
        "same-id-admin": _owned_row(
            "same-id-admin",
            {**TRUSTED_ACTOR, "role": "admin"},
            visibility_scope="admin",
        ),
        "legacy": {"id": "legacy", "source": "tui", "model_config": None},
    }

    class DB:
        def get_session(self, sid):
            return rows.get(sid)

    db = DB()
    assert server._visible_persisted_session_row(db, "own", TRUSTED_ACTOR) == rows["own"]
    assert server._visible_persisted_session_row(db, "foreign", TRUSTED_ACTOR) is None
    assert server._visible_persisted_session_row(db, "same-id-admin", TRUSTED_ACTOR) is None
    assert server._visible_persisted_session_row(db, "legacy", TRUSTED_ACTOR) is None
    assert server._visible_persisted_session_row(db, "missing", TRUSTED_ACTOR) is None


def test_gateway_actor_scoped_db_filters_before_page_and_denies_foreign_mutation():
    other = {**TRUSTED_ACTOR, "actor_id": "actor-b"}
    rows = [_owned_row("foreign", other), _owned_row("own-1", TRUSTED_ACTOR), _owned_row("own-2", TRUSTED_ACTOR)]

    class DB:
        def __init__(self):
            self.reopened = []

        def list_sessions_rich(self, *, limit, offset=0, **_kwargs):
            return rows[offset : offset + limit]

        def get_session(self, sid):
            return next((row for row in rows if row["id"] == sid), None)

        def get_session_by_title(self, _title):
            return self.get_session("foreign")

        def reopen_session(self, sid):
            self.reopened.append(sid)

    raw = DB()
    db = server._CuiActorScopedSessionDB(raw, TRUSTED_ACTOR)
    assert [row["id"] for row in db.list_sessions_rich(limit=1)] == ["own-1"]
    assert db.get_session_by_title("guessed") is None
    with pytest.raises(server._CuiSessionNotFound):
        db.reopen_session("foreign")
    assert raw.reopened == []


def test_gateway_stamp_uses_only_dispatch_bound_actor():
    config = {}
    token = server.bind_cui_actor_context({**TRUSTED_ACTOR, "arbitrary": "drop"})
    try:
        server._stamp_cui_actor_context(config, server.current_cui_actor_context())
    finally:
        server.reset_cui_actor_context(token)
    assert config["_cui_actor_context"] == TRUSTED_ACTOR
    assert config["_cui_actor_id"] == "actor-a"
    assert config["_cui_tenant_id"] == "tenant-a"


def _live_record(actor, *, session_key="stored-key", pending_title=None):
    return {
        "agent": None,
        "attached_images": ["kept.png"],
        "created_at": 100.0,
        "cui_actor_context": actor,
        "cwd": "/tmp/original",
        "history": [],
        "history_lock": threading.RLock(),
        "last_active": 100.0,
        "pending_title": pending_title,
        "running": False,
        "session_key": session_key,
        "transport": SimpleNamespace(write=lambda _frame: True),
    }


def _bound_actor(actor):
    return server.bind_cui_actor_context(actor)


def _request_as(actor, request, *, transport=None):
    actor_token = _bound_actor(actor)
    transport_token = server.bind_transport(transport) if transport is not None else None
    try:
        return server.handle_request(request)
    finally:
        if transport_token is not None:
            server.reset_transport(transport_token)
        server.reset_cui_actor_context(actor_token)


def test_gateway_live_lookup_confines_runtime_id_and_stored_key_actor_matrix():
    own = _live_record(TRUSTED_ACTOR, session_key="own-key")
    server._sessions.clear()
    server._sessions["runtime-own"] = own
    try:
        token = _bound_actor(TRUSTED_ACTOR)
        try:
            assert server._sess_nowait({"session_id": "runtime-own"}, "own") == (own, None)
            assert server._find_live_session_by_key("own-key") == ("runtime-own", own)
        finally:
            server.reset_cui_actor_context(token)

        token = _bound_actor({**TRUSTED_ACTOR, "actor_id": "actor-b"})
        try:
            foreign, foreign_err = server._sess_nowait({"session_id": "runtime-own"}, "foreign")
            missing, missing_err = server._sess_nowait({"session_id": "missing"}, "missing")
            assert foreign is missing is None
            assert foreign_err["error"] == missing_err["error"]
            assert server._find_live_session_by_key("own-key") is None
        finally:
            server.reset_cui_actor_context(token)

        assert server._sess_nowait({"session_id": "runtime-own"}, "local") == (own, None)
        assert server._find_live_session_by_key("own-key") == ("runtime-own", own)
    finally:
        server._sessions.clear()


def test_gateway_active_list_and_activate_hide_foreign_live_session_before_payload_or_rebind(monkeypatch):
    foreign = _live_record({**TRUSTED_ACTOR, "actor_id": "actor-b"})
    before_transport = foreign["transport"]
    materialized = []
    monkeypatch.setattr(
        server,
        "_session_live_item",
        lambda sid, _record, _current="": materialized.append(sid) or {"id": sid},
    )
    server._sessions.clear()
    server._sessions["foreign-live"] = foreign
    try:
        listed = _request_as(
            TRUSTED_ACTOR,
            {"id": "list", "method": "session.active_list", "params": {}},
        )
        assert listed["result"]["sessions"] == []
        assert materialized == []

        activated = _request_as(
            TRUSTED_ACTOR,
            {
                "id": "activate",
                "method": "session.activate",
                "params": {"session_id": "foreign-live"},
            },
            transport=SimpleNamespace(write=lambda _frame: True),
        )
        assert activated["error"] == {"code": 4001, "message": "session not found"}
        assert foreign["last_active"] == 100.0
        assert foreign["transport"] is before_transport
        assert "viewers" not in foreign
    finally:
        server._sessions.clear()


@pytest.mark.parametrize(
    ("method_name", "extra_params"),
    [
        ("session.title", {"title": "stolen"}),
        ("session.cwd.set", {"cwd": "/tmp"}),
        ("image.detach", {"path": "kept.png"}),
    ],
)
def test_gateway_foreign_live_side_effect_handlers_stop_at_central_lookup(method_name, extra_params):
    foreign = _live_record({**TRUSTED_ACTOR, "actor_id": "actor-b"})
    before = {
        "attached_images": list(foreign["attached_images"]),
        "cwd": foreign["cwd"],
        "last_active": foreign["last_active"],
        "pending_title": foreign["pending_title"],
        "transport": foreign["transport"],
    }
    server._sessions.clear()
    server._sessions["foreign-live"] = foreign
    try:
        response = _request_as(
            TRUSTED_ACTOR,
            {
                "id": method_name,
                "method": method_name,
                "params": {"session_id": "foreign-live", **extra_params},
            },
        )
        assert response["error"] == {"code": 4001, "message": "session not found"}
        assert {
            "attached_images": foreign["attached_images"],
            "cwd": foreign["cwd"],
            "last_active": foreign["last_active"],
            "pending_title": foreign["pending_title"],
            "transport": foreign["transport"],
        } == before
    finally:
        server._sessions.clear()


def test_profile_scoped_resume_uses_scoped_adapter_and_denies_foreign_exact_title_and_adoption_before_side_effects(monkeypatch):
    foreign_actor = {**TRUSTED_ACTOR, "actor_id": "actor-b"}
    target_rows = {
        "foreign-id": _owned_row("foreign-id", foreign_actor),
        "foreign-title": _owned_row("foreign-title", foreign_actor),
    }
    effects = []

    class RawDB:
        def get_session(self, sid):
            return target_rows.get(sid)

        def get_session_by_title(self, title):
            return target_rows.get("foreign-title") if title == "guessed-title" else None

        def reopen_session(self, sid):
            effects.append(("reopen", sid))

        def get_resume_conversations(self, sid):
            effects.append(("history", sid))
            return [], []

        def adopt_session_lineage_from(self, _donor, sid):
            effects.append(("adopt", sid))
            return {"adopted": True}

        def close(self):
            effects.append(("close", None))

    raw = RawDB()
    donor_rows = {
        "missing-with-foreign-donor": _owned_row(
            "missing-with-foreign-donor", foreign_actor
        )
    }

    class DonorDB(RawDB):
        def get_session(self, sid):
            return donor_rows.get(sid)

    donor_raw = DonorDB()

    def scoped_profile_db(_profile):
        return server._CuiActorScopedSessionDB(raw, server.current_cui_actor_context()), True

    monkeypatch.setattr(server, "_profile_home", lambda _profile: Path("/tmp/profile-worker"))
    monkeypatch.setattr(server, "_db_for_profile", scoped_profile_db)
    monkeypatch.setattr("hermes_state.SessionDB", lambda **_kwargs: raw)
    monkeypatch.setattr(
        server,
        "_get_db",
        lambda: server._CuiActorScopedSessionDB(
            donor_raw, server.current_cui_actor_context()
        ),
    )

    for target in ("foreign-id", "guessed-title", "missing-with-foreign-donor"):
        response = _request_as(
            TRUSTED_ACTOR,
            {
                "id": target,
                "method": "session.resume",
                "params": {"session_id": target, "profile": "worker"},
            },
        )
        assert response["error"] == {"code": 4007, "message": "session not found"}
    assert not any(name in {"reopen", "history", "adopt"} for name, _ in effects)


def test_profile_scoped_resume_denies_foreign_live_unpersisted_and_reuse_before_transport_rebind_but_preserves_owned_and_no_actor_paths(monkeypatch):
    foreign = _live_record(
        {**TRUSTED_ACTOR, "actor_id": "actor-b"},
        session_key="unpersisted-key",
        pending_title="Foreign Draft",
    )
    before_transport = foreign["transport"]
    foreign["profile_home"] = "/tmp/profile-worker"

    class EmptyDB:
        def get_session(self, _sid):
            return None

        def get_session_by_title(self, _title):
            return None

        def close(self):
            pass

    monkeypatch.setattr(server, "_profile_home", lambda _profile: Path("/tmp/profile-worker"))
    monkeypatch.setattr(server, "_db_for_profile", lambda _profile: (EmptyDB(), True))
    monkeypatch.setattr("hermes_state.SessionDB", lambda **_kwargs: EmptyDB())
    monkeypatch.setattr(server, "_get_db", lambda: EmptyDB())
    server._sessions.clear()
    server._sessions["foreign-live"] = foreign
    try:
        response = _request_as(
            TRUSTED_ACTOR,
            {
                "id": "foreign-unpersisted",
                "method": "session.resume",
                "params": {"session_id": "unpersisted-key", "profile": "worker"},
            },
            transport=SimpleNamespace(write=lambda _frame: True),
        )
        assert response["error"] == {"code": 4007, "message": "session not found"}
        assert foreign["last_active"] == 100.0
        assert foreign["transport"] is before_transport
        assert "viewers" not in foreign

        token = _bound_actor({**TRUSTED_ACTOR, "actor_id": "actor-b"})
        try:
            assert server._find_live_session_by_key("unpersisted-key") == ("foreign-live", foreign)
        finally:
            server.reset_cui_actor_context(token)
        assert server._find_live_session_by_key("unpersisted-key") == ("foreign-live", foreign)
    finally:
        server._sessions.clear()


def test_optional_live_inheritance_confines_completion_create_and_oneshot(monkeypatch, tmp_path):
    owned_cwd = tmp_path / "owned"
    foreign_cwd = tmp_path / "foreign"
    fallback_cwd = tmp_path / "fallback"
    for path in (owned_cwd, foreign_cwd, fallback_cwd):
        path.mkdir()
    (foreign_cwd / "foreign-secret.txt").write_text("secret", encoding="utf-8")
    (fallback_cwd / "fallback-visible.txt").write_text("visible", encoding="utf-8")

    owned = _live_record(TRUSTED_ACTOR)
    owned["cwd"] = str(owned_cwd)
    owned["agent"] = SimpleNamespace(runtime="owned-runtime")
    foreign = _live_record({**TRUSTED_ACTOR, "actor_id": "actor-b"})
    foreign["cwd"] = str(foreign_cwd)
    foreign["agent"] = SimpleNamespace(runtime="foreign-runtime")

    monkeypatch.setattr(server, "_profile_configured_cwd", lambda _home: str(fallback_cwd))
    monkeypatch.setattr(server, "_launch_configured_cwd", lambda: None)
    monkeypatch.setattr(server, "_profile_home", lambda _profile=None: None)
    monkeypatch.setattr(server, "_schedule_agent_build", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda: None)
    monkeypatch.setattr(server, "_load_show_reasoning", lambda: False)
    monkeypatch.setattr(server, "_load_tool_progress_mode", lambda: "collapsed")
    monkeypatch.setattr(server, "_resolve_model", lambda: "fallback-model")
    monkeypatch.setattr(server, "_main_runtime_from_agent", lambda agent: agent.runtime)

    inherited_runtimes = []
    import agent.oneshot

    monkeypatch.setattr(
        agent.oneshot,
        "run_oneshot",
        lambda **kwargs: inherited_runtimes.append(kwargs["main_runtime"]) or "ok",
    )

    server._sessions.clear()
    server._sessions.update({"owned": owned, "foreign": foreign})
    try:
        token = _bound_actor(TRUSTED_ACTOR)
        try:
            assert server._completion_cwd({"session_id": "owned"}) == str(owned_cwd)
            assert server._completion_cwd({"session_id": "foreign"}) == str(fallback_cwd)
            assert server._completion_cwd({"session_id": "missing"}) == str(fallback_cwd)
        finally:
            server.reset_cui_actor_context(token)
        assert server._completion_cwd({"session_id": "foreign"}) == str(foreign_cwd)

        created = _request_as(
            TRUSTED_ACTOR,
            {
                "id": "create-foreign",
                "method": "session.create",
                "params": {"session_id": "foreign"},
            },
        )
        assert created["result"]["info"]["cwd"] == str(fallback_cwd)

        completed = _request_as(
            TRUSTED_ACTOR,
            {
                "id": "complete-foreign",
                "method": "complete.path",
                "params": {"session_id": "foreign", "word": "fallback"},
            },
        )
        displays = [item["display"] for item in completed["result"]["items"]]
        assert displays == ["fallback-visible.txt"]
        assert "foreign-secret.txt" not in displays

        for actor, sid in (
            (TRUSTED_ACTOR, "owned"),
            (TRUSTED_ACTOR, "foreign"),
            (TRUSTED_ACTOR, "missing"),
            (None, "foreign"),
        ):
            response = _request_as(
                actor,
                {
                    "id": f"oneshot-{sid}",
                    "method": "llm.oneshot",
                    "params": {"session_id": sid, "input": "hello"},
                },
            )
            assert response["result"]["text"] == "ok"
        assert inherited_runtimes == [
            "owned-runtime",
            None,
            None,
            "foreign-runtime",
        ]
    finally:
        server._sessions.clear()


def test_config_set_confines_all_session_consuming_branches_before_mutation(monkeypatch):
    foreign = _live_record({**TRUSTED_ACTOR, "actor_id": "actor-b"})
    foreign["running"] = True
    owned = _live_record(TRUSTED_ACTOR)
    writes = []
    switches = []
    parses = []

    import hermes_cli.model_switch

    def parse(value):
        parses.append(value)
        return SimpleNamespace(model_input=value, explicit_provider="")

    monkeypatch.setattr(hermes_cli.model_switch, "parse_model_switch_args", parse)
    monkeypatch.setattr(
        server,
        "_apply_model_switch",
        lambda *args, **kwargs: switches.append((args, kwargs))
        or {"value": args[2], "warning": "", "scope": "global"},
    )
    monkeypatch.setattr(server, "_write_config_key", lambda key, value: writes.append((key, value)))
    monkeypatch.setattr(server, "_load_tool_progress_mode", lambda: "all")
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "queue")
    monkeypatch.setattr(server, "_load_cfg", lambda: {})

    server._sessions.clear()
    server._sessions.update({"foreign": foreign, "owned": owned})
    try:
        for sid, key, value in (
            ("foreign", "model", "new-model"),
            ("missing", "model", "new-model"),
            ("foreign", "verbose", "off"),
            ("missing", "verbose", "off"),
        ):
            before = (dict(foreign), dict(owned), list(writes), list(switches), list(parses))
            response = _request_as(
                TRUSTED_ACTOR,
                {
                    "id": f"{sid}-{key}",
                    "method": "config.set",
                    "params": {"session_id": sid, "key": key, "value": value},
                },
            )
            assert response["error"] == {"code": 4001, "message": "session not found"}
            assert (dict(foreign), dict(owned), writes, switches, parses) == before

        owned_response = _request_as(
            TRUSTED_ACTOR,
            {
                "id": "owned-verbose",
                "method": "config.set",
                "params": {"session_id": "owned", "key": "verbose", "value": "off"},
            },
        )
        assert owned_response["result"]["value"] == "off"
        assert owned["tool_progress_mode"] == "off"

        trusted_response = _request_as(
            None,
            {
                "id": "trusted-model",
                "method": "config.set",
                "params": {"session_id": "foreign", "key": "model", "value": "trusted-model"},
            },
        )
        assert trusted_response["result"]["deferred"] is True
        assert foreign["pending_model_switch"]["display_model"] == "trusted-model"

        global_model = _request_as(
            TRUSTED_ACTOR,
            {
                "id": "global-model",
                "method": "config.set",
                "params": {"key": "model", "value": "global-model"},
            },
        )
        assert global_model["result"]["value"] == "global-model"

        global_focus_status = _request_as(
            TRUSTED_ACTOR,
            {
                "id": "global-focus-status",
                "method": "config.set",
                "params": {"session_id": "foreign", "key": "focus", "value": "status"},
            },
        )
        assert global_focus_status["result"] == {
            "key": "focus",
            "value": "off",
            "tool_progress": "all",
        }

        global_only = _request_as(
            TRUSTED_ACTOR,
            {
                "id": "global-busy",
                "method": "config.set",
                "params": {"session_id": "foreign", "key": "busy", "value": "steer"},
            },
        )
        assert global_only["result"] == {"key": "busy", "value": "steer"}
        assert ("display.busy_input_mode", "steer") in writes
    finally:
        server._sessions.clear()


def test_profile_resume_reauthorizes_resolved_continuation_tip_before_side_effects(monkeypatch):
    foreign_actor = {**TRUSTED_ACTOR, "actor_id": "actor-b"}
    rows = {
        "parent": _owned_row("parent", TRUSTED_ACTOR),
        "owned-tip": _owned_row("owned-tip", TRUSTED_ACTOR),
        "foreign-tip": _owned_row("foreign-tip", foreign_actor),
    }
    effects = []

    class RawDB:
        def __init__(self):
            self.tip = "foreign-tip"

        def get_session(self, sid):
            return rows.get(sid)

        def get_session_by_title(self, _title):
            return None

        def resolve_resume_session_id(self, _sid):
            return self.tip

        def assert_resume_safe(self, sid):
            effects.append(("safety", sid))

        def close(self):
            effects.append(("close", None))

    raw = RawDB()

    def actor_db(_profile):
        actor = server.current_cui_actor_context()
        return (server._CuiActorScopedSessionDB(raw, actor) if actor else raw), False

    monkeypatch.setattr(server, "_profile_home", lambda _profile=None: None)
    monkeypatch.setattr(server, "_db_for_profile", actor_db)
    monkeypatch.setattr(server, "_profile_configured_cwd", lambda _home: "/tmp")
    monkeypatch.setattr(server, "_stored_session_runtime_overrides", lambda _row: {})
    monkeypatch.setattr(
        server,
        "_deferred_session_record",
        lambda session_key, **_kwargs: {
            "created_at": 1.0,
            "session_key": session_key,
            "resume_message_count": 0,
            "resume_hydrating": True,
        },
    )
    monkeypatch.setattr(
        server,
        "_claim_or_reuse_live",
        lambda sid, target, record, lease: effects.append(("publish", target)) or None,
    )
    monkeypatch.setattr(
        server,
        "_schedule_resume_hydration",
        lambda sid, target, db, close_db=False: effects.append(("history", target)),
    )
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda: effects.append(("cap", None)))

    request = {
        "id": "resume",
        "method": "session.resume",
        "params": {"session_id": "parent", "defer_history": True},
    }
    denied = _request_as(TRUSTED_ACTOR, request)
    assert denied["error"] == {"code": 4007, "message": "session not found"}
    assert effects == []

    raw.tip = "owned-tip"
    owned = _request_as(TRUSTED_ACTOR, request)
    assert owned["result"]["resumed"] == "owned-tip"
    assert ("safety", "owned-tip") in effects
    assert ("publish", "owned-tip") in effects

    effects.clear()
    raw.tip = "foreign-tip"
    trusted = _request_as(None, request)
    assert trusted["result"]["resumed"] == "foreign-tip"
    assert ("safety", "foreign-tip") in effects
    assert ("publish", "foreign-tip") in effects
