"""Mounted recents -> detail/history contracts for current and legacy owners."""
from types import SimpleNamespace

import pytest


ACTOR = {"tenant_id": "tenant-a", "actor_id": "actor-a", "role": "admin"}
ROLES = ("admin", "operator", "aiwerk_admin", "owner", "user", "customer",
         "tenant_user", "member", "tenant_admin", "support")


def _config(actor):
    from agent.agent_init import _stamp_authenticated_cui_session_owner
    from agent.cui_actor_context import bind_cui_actor_context, reset_cui_actor_context

    token = bind_cui_actor_context(actor)
    try:
        config = {}
        _stamp_authenticated_cui_session_owner(config)
        return config
    finally:
        reset_cui_actor_context(token)


@pytest.fixture
def history_api(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    import hermes_state
    from hermes_cli import web_server
    from hermes_cli.dashboard_auth import middleware

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-test"))
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "hermes-test" / "state.db")
    identity = dict(ACTOR)

    async def authenticated(request, call_next):
        request.state.session = SimpleNamespace(**identity)
        return await call_next(request)

    monkeypatch.setattr(middleware, "gated_auth_middleware", authenticated)
    monkeypatch.setattr(web_server.app.state, "auth_required", True, raising=False)
    client = TestClient(web_server.app)
    client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
    db = hermes_state.SessionDB()

    def seed(sid, config, **kwargs):
        db.create_session(sid, source="web", model_config=config, **kwargs)
        db.append_message(sid, "user", "history " + sid)

    try:
        yield client, db, identity, seed
    finally:
        client.close()
        db.close()


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("shape", ("current", "legacy-flat", "legacy-nested", "legacy-both"))
def test_exact_owner_recents_page_into_readable_history(history_api, role, shape):
    client, db, identity, seed = history_api
    identity["role"] = role
    config = _config(identity)
    if shape != "current":
        config.pop("_cui_visibility_scope")
    if shape == "legacy-flat":
        config.pop("_cui_actor_context")
    if shape == "legacy-nested":
        for key in ("_cui_actor_role", "_cui_actor_id", "_cui_tenant_id"):
            config.pop(key)
    seed("own-old", config)
    seed("foreign", _config({**identity, "actor_id": "someone-else"}))
    seed("own-new", config)
    before = db.get_session("own-old")["model_config"]
    seen = []
    for offset in range(3):
        response = client.get("/api/sessions", params={
            "limit": 1, "offset": offset, "order": "created",
            "exclude_sources": "cron", "hide_automated": 1,
            "full": offset % 2,
        })
        assert response.status_code == 200, response.text
        page = response.json()
        assert page["total"] == 2
        assert len(page["sessions"]) == (1 if offset < 2 else 0)
        for row in page["sessions"]:
            sid = row["id"]
            seen.append(sid)
            assert "model_config" not in row
            detail = client.get(f"/api/sessions/{sid}")
            assert detail.status_code == 200, detail.text
            assert detail.json()["id"] == sid
            assert "model_config" not in detail.json()
            history = client.get(f"/api/sessions/{sid}/messages")
            assert history.status_code == 200, history.text
            assert history.json()["session_id"] == sid
            assert [m["content"] for m in history.json()["messages"]] == ["history " + sid]
    assert seen == ["own-new", "own-old"]
    assert db.get_session("own-old")["model_config"] == before  # no backfill


@pytest.mark.parametrize("role", ("admin", "user"))
@pytest.mark.parametrize("fault", (
    "foreign-actor", "foreign-tenant", "foreign-role", "unowned", "missing-actor",
    "missing-tenant", "missing-role", "unknown-row-role", "conflicting-scope",
    "null-scope", "internal-scope", "conflicting-nested-actor", "conflicting-nested-tenant",
    "conflicting-nested-role", "partial-nested", "partial-flat", "invalid-nested",
    "unknown-request-role", "missing-request-actor", "missing-request-tenant",
))
def test_recents_and_history_deny_unproven_or_conflicting_ownership(history_api, role, fault):
    client, db, identity, seed = history_api
    identity["role"] = role
    config = _config(identity)
    if fault.startswith("foreign-"):
        field = {"foreign-actor": "actor_id", "foreign-tenant": "tenant_id", "foreign-role": "role"}[fault]
        value = ("user" if role == "admin" else "admin") if field == "role" else "foreign"
        config = _config({**identity, field: value})
    elif fault == "unowned":
        config = {"display_name": "actor-a", "source": "web"}
    elif fault in ("missing-actor", "missing-tenant", "missing-role", "unknown-row-role"):
        field = {"missing-actor": "_cui_actor_id", "missing-tenant": "_cui_tenant_id",
                 "missing-role": "_cui_actor_role", "unknown-row-role": "_cui_actor_role"}[fault]
        config.pop("_cui_actor_context")
        config[field] = "unknown" if fault == "unknown-row-role" else ""
    elif fault.endswith("scope"):
        config["_cui_visibility_scope"] = {
            "conflicting-scope": "customer" if role == "admin" else "admin",
            "null-scope": None, "internal-scope": "internal",
        }[fault]
    elif fault.startswith("conflicting-nested-"):
        field = {"actor": "actor_id", "tenant": "tenant_id", "role": "role"}[fault.rsplit("-", 1)[1]]
        config["_cui_actor_context"][field] = "user" if field == "role" and role == "admin" else "admin" if field == "role" else "foreign"
    elif fault == "partial-nested":
        config["_cui_actor_context"].pop("tenant_id")
    elif fault == "partial-flat":
        config.pop("_cui_tenant_id")
    elif fault == "invalid-nested":
        config["_cui_actor_context"] = "not an identity"
    else:
        field = {"unknown-request-role": "role", "missing-request-actor": "actor_id",
                 "missing-request-tenant": "tenant_id"}[fault]
        identity[field] = "unknown" if field == "role" else ""
    seed("hidden", config)
    response = client.get("/api/sessions?limit=10&offset=0")
    restricted = "request" in fault
    if restricted:
        assert response.status_code == 403
    else:
        assert response.status_code == 200
        assert response.json()["sessions"] == []
        assert response.json()["total"] == 0
    for suffix in ("", "/messages"):
        response = client.get("/api/sessions/hidden" + suffix)
        assert response.status_code == (403 if restricted else 404), response.text


@pytest.fixture
def resume_rpc(history_api, monkeypatch):
    from queue import Queue
    from tui_gateway import server

    _, db, identity, _ = history_api
    monkeypatch.setattr(server, "_db", db)
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_profile_home", lambda profile=None: None)
    # Only background work is disabled: lookup, authorization, cold restore,
    # record construction and RPC response/history serialization remain real.
    monkeypatch.setattr(server, "_schedule_agent_build", lambda *a, **kw: None)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda: None)
    monkeypatch.setattr(server, "_maybe_schedule_auto_continue", lambda *a: None)
    responses = Queue()
    transport = SimpleNamespace(write=responses.put)

    def resume(sid):
        response = server.dispatch(
            {"jsonrpc": "2.0", "id": "resume", "method": "session.resume",
             "params": {"session_id": sid, "cols": 100}},
            transport=transport, actor_context=dict(identity),
        )
        return response if response is not None else responses.get(timeout=15)

    return resume


@pytest.mark.parametrize("stored_role,request_role", [
    *((role, role) for role in ROLES),
    ("user", "customer"), ("customer", "member"),
    ("admin", "aiwerk_admin"), ("aiwerk_admin", "operator"),
])
@pytest.mark.parametrize("shape", ("current", "legacy-flat", "legacy-nested", "legacy-both"))
def test_http_recent_id_resumes_through_authenticated_rpc_with_history(
    history_api, resume_rpc, stored_role, request_role, shape,
):
    client, db, identity, seed = history_api
    identity["role"] = request_role
    config = _config({**identity, "role": stored_role})
    if shape != "current":
        config.pop("_cui_visibility_scope")
    if shape == "legacy-flat":
        config.pop("_cui_actor_context")
    if shape == "legacy-nested":
        for key in ("_cui_actor_role", "_cui_actor_id", "_cui_tenant_id"):
            config.pop(key)
    seed("recent", config)
    before = db.get_session("recent")["model_config"]
    listing = client.get("/api/sessions?limit=10&offset=0")
    assert listing.status_code == 200, listing.text
    assert listing.json()["total"] == 1
    sid = listing.json()["sessions"][0]["id"]
    response = resume_rpc(sid)
    assert "error" not in response, response
    result = response["result"]
    assert result["resumed"] == sid
    assert result["message_count"] == 1
    assert [message["text"] for message in result["messages"]] == ["history " + sid]
    assert db.get_session(sid)["model_config"] == before


@pytest.mark.parametrize("request_role,stored_role", [
    ("user", "user"), ("customer", "user"), ("aiwerk_admin", "admin"),
])
@pytest.mark.parametrize("shape", ("current-alias", "legacy-flat", "legacy-nested", "legacy-both"))
@pytest.mark.parametrize("fault", ("tenant", "actor", "role", "incomplete", "conflict"))
def test_new_legacy_resume_acceptance_does_not_admit_invalid_owners(
    history_api, resume_rpc, request_role, stored_role, shape, fault,
):
    client, _, identity, seed = history_api
    identity["role"] = request_role
    if shape == "current-alias" and request_role == stored_role:
        stored_role = "customer"
    config = _config({**identity, "role": stored_role})
    if shape != "current-alias":
        config.pop("_cui_visibility_scope")
    if shape == "legacy-flat":
        config.pop("_cui_actor_context")
    if shape == "legacy-nested":
        for key in ("_cui_actor_role", "_cui_actor_id", "_cui_tenant_id"):
            config.pop(key)
    if fault == "conflict":
        config["_cui_actor_context"] = {**identity, "actor_id": "foreign"}
        config["_cui_actor_id"] = identity["actor_id"]
    else:
        field = {"tenant": "tenant_id", "actor": "actor_id", "role": "role", "incomplete": "tenant_id"}[fault]
        flat = {"tenant_id": "_cui_tenant_id", "actor_id": "_cui_actor_id", "role": "_cui_actor_role"}[field]
        value = "" if fault == "incomplete" else "support" if fault == "role" else "foreign"
        if flat in config:
            config[flat] = value
        if "_cui_actor_context" in config:
            config["_cui_actor_context"][field] = value
    seed("hidden-rpc", config)
    listing = client.get("/api/sessions?limit=10&offset=0")
    assert listing.status_code == 200
    assert listing.json()["sessions"] == []
    assert resume_rpc("hidden-rpc")["error"] == {"code": 4007, "message": "session not found"}


@pytest.mark.parametrize("foreign_end", ("root", "tip"))
def test_admin_history_cannot_launder_foreign_compression_lineage(history_api, foreign_end):
    client, db, identity, seed = history_api
    own = _config(identity)
    foreign = _config({**identity, "actor_id": "foreign"})
    seed("root", foreign if foreign_end == "root" else own)
    db.end_session("root", "compression")
    seed("tip", foreign if foreign_end == "tip" else own, parent_session_id="root")
    response = client.get("/api/sessions?limit=10&offset=0")
    assert response.status_code == 200
    assert response.json()["sessions"] == []
    assert response.json()["total"] == 0
    response = client.get("/api/sessions/root/messages")
    assert response.status_code == 404
