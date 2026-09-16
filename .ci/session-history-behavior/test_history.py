"""Retained authority: mounted recents -> actual RPC resume -> stored history.

Copied from qualified product tree 8929d00155c4fcc9fe6723c4f45ee8d3a8dec465;
self-contained fixtures, fixed IDs, no candidate test/configuration imports.
Production imports execute only inside the restricted retained-suite sandbox.
"""
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


@pytest.mark.parametrize('shape,role', [
    pytest.param('current', 'admin', id='current-admin'),
    pytest.param('current', 'operator', id='current-operator'),
    pytest.param('current', 'aiwerk_admin', id='current-aiwerk_admin'),
    pytest.param('current', 'owner', id='current-owner'),
    pytest.param('current', 'user', id='current-user'),
    pytest.param('current', 'customer', id='current-customer'),
    pytest.param('current', 'tenant_user', id='current-tenant_user'),
    pytest.param('current', 'member', id='current-member'),
    pytest.param('current', 'tenant_admin', id='current-tenant_admin'),
    pytest.param('current', 'support', id='current-support'),
    pytest.param('legacy-flat', 'admin', id='legacy-flat-admin'),
    pytest.param('legacy-flat', 'operator', id='legacy-flat-operator'),
    pytest.param('legacy-flat', 'aiwerk_admin', id='legacy-flat-aiwerk_admin'),
    pytest.param('legacy-flat', 'owner', id='legacy-flat-owner'),
    pytest.param('legacy-flat', 'user', id='legacy-flat-user'),
    pytest.param('legacy-flat', 'customer', id='legacy-flat-customer'),
    pytest.param('legacy-flat', 'tenant_user', id='legacy-flat-tenant_user'),
    pytest.param('legacy-flat', 'member', id='legacy-flat-member'),
    pytest.param('legacy-flat', 'tenant_admin', id='legacy-flat-tenant_admin'),
    pytest.param('legacy-flat', 'support', id='legacy-flat-support'),
    pytest.param('legacy-nested', 'admin', id='legacy-nested-admin'),
    pytest.param('legacy-nested', 'operator', id='legacy-nested-operator'),
    pytest.param('legacy-nested', 'aiwerk_admin', id='legacy-nested-aiwerk_admin'),
    pytest.param('legacy-nested', 'owner', id='legacy-nested-owner'),
    pytest.param('legacy-nested', 'user', id='legacy-nested-user'),
    pytest.param('legacy-nested', 'customer', id='legacy-nested-customer'),
    pytest.param('legacy-nested', 'tenant_user', id='legacy-nested-tenant_user'),
    pytest.param('legacy-nested', 'member', id='legacy-nested-member'),
    pytest.param('legacy-nested', 'tenant_admin', id='legacy-nested-tenant_admin'),
    pytest.param('legacy-nested', 'support', id='legacy-nested-support'),
    pytest.param('legacy-both', 'admin', id='legacy-both-admin'),
    pytest.param('legacy-both', 'operator', id='legacy-both-operator'),
    pytest.param('legacy-both', 'aiwerk_admin', id='legacy-both-aiwerk_admin'),
    pytest.param('legacy-both', 'owner', id='legacy-both-owner'),
    pytest.param('legacy-both', 'user', id='legacy-both-user'),
    pytest.param('legacy-both', 'customer', id='legacy-both-customer'),
    pytest.param('legacy-both', 'tenant_user', id='legacy-both-tenant_user'),
    pytest.param('legacy-both', 'member', id='legacy-both-member'),
    pytest.param('legacy-both', 'tenant_admin', id='legacy-both-tenant_admin'),
    pytest.param('legacy-both', 'support', id='legacy-both-support'),
])
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


@pytest.mark.parametrize('fault,role', [
    pytest.param('foreign-actor', 'admin', id='foreign-actor-admin'),
    pytest.param('foreign-actor', 'user', id='foreign-actor-user'),
    pytest.param('foreign-tenant', 'admin', id='foreign-tenant-admin'),
    pytest.param('foreign-tenant', 'user', id='foreign-tenant-user'),
    pytest.param('foreign-role', 'admin', id='foreign-role-admin'),
    pytest.param('foreign-role', 'user', id='foreign-role-user'),
    pytest.param('unowned', 'admin', id='unowned-admin'),
    pytest.param('unowned', 'user', id='unowned-user'),
    pytest.param('missing-actor', 'admin', id='missing-actor-admin'),
    pytest.param('missing-actor', 'user', id='missing-actor-user'),
    pytest.param('missing-tenant', 'admin', id='missing-tenant-admin'),
    pytest.param('missing-tenant', 'user', id='missing-tenant-user'),
    pytest.param('missing-role', 'admin', id='missing-role-admin'),
    pytest.param('missing-role', 'user', id='missing-role-user'),
    pytest.param('unknown-row-role', 'admin', id='unknown-row-role-admin'),
    pytest.param('unknown-row-role', 'user', id='unknown-row-role-user'),
    pytest.param('conflicting-scope', 'admin', id='conflicting-scope-admin'),
    pytest.param('conflicting-scope', 'user', id='conflicting-scope-user'),
    pytest.param('null-scope', 'admin', id='null-scope-admin'),
    pytest.param('null-scope', 'user', id='null-scope-user'),
    pytest.param('internal-scope', 'admin', id='internal-scope-admin'),
    pytest.param('internal-scope', 'user', id='internal-scope-user'),
    pytest.param('conflicting-nested-actor', 'admin', id='conflicting-nested-actor-admin'),
    pytest.param('conflicting-nested-actor', 'user', id='conflicting-nested-actor-user'),
    pytest.param('conflicting-nested-tenant', 'admin', id='conflicting-nested-tenant-admin'),
    pytest.param('conflicting-nested-tenant', 'user', id='conflicting-nested-tenant-user'),
    pytest.param('conflicting-nested-role', 'admin', id='conflicting-nested-role-admin'),
    pytest.param('conflicting-nested-role', 'user', id='conflicting-nested-role-user'),
    pytest.param('partial-nested', 'admin', id='partial-nested-admin'),
    pytest.param('partial-nested', 'user', id='partial-nested-user'),
    pytest.param('partial-flat', 'admin', id='partial-flat-admin'),
    pytest.param('partial-flat', 'user', id='partial-flat-user'),
    pytest.param('invalid-nested', 'admin', id='invalid-nested-admin'),
    pytest.param('invalid-nested', 'user', id='invalid-nested-user'),
    pytest.param('unknown-request-role', 'admin', id='unknown-request-role-admin'),
    pytest.param('unknown-request-role', 'user', id='unknown-request-role-user'),
    pytest.param('missing-request-actor', 'admin', id='missing-request-actor-admin'),
    pytest.param('missing-request-actor', 'user', id='missing-request-actor-user'),
    pytest.param('missing-request-tenant', 'admin', id='missing-request-tenant-admin'),
    pytest.param('missing-request-tenant', 'user', id='missing-request-tenant-user'),
])
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


@pytest.mark.parametrize('shape,stored_role,request_role', [
    pytest.param('current', 'admin', 'admin', id='current-admin-admin'),
    pytest.param('current', 'operator', 'operator', id='current-operator-operator'),
    pytest.param('current', 'aiwerk_admin', 'aiwerk_admin', id='current-aiwerk_admin-aiwerk_admin'),
    pytest.param('current', 'owner', 'owner', id='current-owner-owner'),
    pytest.param('current', 'user', 'user', id='current-user-user'),
    pytest.param('current', 'customer', 'customer', id='current-customer-customer'),
    pytest.param('current', 'tenant_user', 'tenant_user', id='current-tenant_user-tenant_user'),
    pytest.param('current', 'member', 'member', id='current-member-member'),
    pytest.param('current', 'tenant_admin', 'tenant_admin', id='current-tenant_admin-tenant_admin'),
    pytest.param('current', 'support', 'support', id='current-support-support'),
    pytest.param('current', 'user', 'customer', id='current-user-customer'),
    pytest.param('current', 'customer', 'member', id='current-customer-member'),
    pytest.param('current', 'admin', 'aiwerk_admin', id='current-admin-aiwerk_admin'),
    pytest.param('current', 'aiwerk_admin', 'operator', id='current-aiwerk_admin-operator'),
    pytest.param('legacy-flat', 'admin', 'admin', id='legacy-flat-admin-admin'),
    pytest.param('legacy-flat', 'operator', 'operator', id='legacy-flat-operator-operator'),
    pytest.param('legacy-flat', 'aiwerk_admin', 'aiwerk_admin', id='legacy-flat-aiwerk_admin-aiwerk_admin'),
    pytest.param('legacy-flat', 'owner', 'owner', id='legacy-flat-owner-owner'),
    pytest.param('legacy-flat', 'user', 'user', id='legacy-flat-user-user'),
    pytest.param('legacy-flat', 'customer', 'customer', id='legacy-flat-customer-customer'),
    pytest.param('legacy-flat', 'tenant_user', 'tenant_user', id='legacy-flat-tenant_user-tenant_user'),
    pytest.param('legacy-flat', 'member', 'member', id='legacy-flat-member-member'),
    pytest.param('legacy-flat', 'tenant_admin', 'tenant_admin', id='legacy-flat-tenant_admin-tenant_admin'),
    pytest.param('legacy-flat', 'support', 'support', id='legacy-flat-support-support'),
    pytest.param('legacy-flat', 'user', 'customer', id='legacy-flat-user-customer'),
    pytest.param('legacy-flat', 'customer', 'member', id='legacy-flat-customer-member'),
    pytest.param('legacy-flat', 'admin', 'aiwerk_admin', id='legacy-flat-admin-aiwerk_admin'),
    pytest.param('legacy-flat', 'aiwerk_admin', 'operator', id='legacy-flat-aiwerk_admin-operator'),
    pytest.param('legacy-nested', 'admin', 'admin', id='legacy-nested-admin-admin'),
    pytest.param('legacy-nested', 'operator', 'operator', id='legacy-nested-operator-operator'),
    pytest.param('legacy-nested', 'aiwerk_admin', 'aiwerk_admin', id='legacy-nested-aiwerk_admin-aiwerk_admin'),
    pytest.param('legacy-nested', 'owner', 'owner', id='legacy-nested-owner-owner'),
    pytest.param('legacy-nested', 'user', 'user', id='legacy-nested-user-user'),
    pytest.param('legacy-nested', 'customer', 'customer', id='legacy-nested-customer-customer'),
    pytest.param('legacy-nested', 'tenant_user', 'tenant_user', id='legacy-nested-tenant_user-tenant_user'),
    pytest.param('legacy-nested', 'member', 'member', id='legacy-nested-member-member'),
    pytest.param('legacy-nested', 'tenant_admin', 'tenant_admin', id='legacy-nested-tenant_admin-tenant_admin'),
    pytest.param('legacy-nested', 'support', 'support', id='legacy-nested-support-support'),
    pytest.param('legacy-nested', 'user', 'customer', id='legacy-nested-user-customer'),
    pytest.param('legacy-nested', 'customer', 'member', id='legacy-nested-customer-member'),
    pytest.param('legacy-nested', 'admin', 'aiwerk_admin', id='legacy-nested-admin-aiwerk_admin'),
    pytest.param('legacy-nested', 'aiwerk_admin', 'operator', id='legacy-nested-aiwerk_admin-operator'),
    pytest.param('legacy-both', 'admin', 'admin', id='legacy-both-admin-admin'),
    pytest.param('legacy-both', 'operator', 'operator', id='legacy-both-operator-operator'),
    pytest.param('legacy-both', 'aiwerk_admin', 'aiwerk_admin', id='legacy-both-aiwerk_admin-aiwerk_admin'),
    pytest.param('legacy-both', 'owner', 'owner', id='legacy-both-owner-owner'),
    pytest.param('legacy-both', 'user', 'user', id='legacy-both-user-user'),
    pytest.param('legacy-both', 'customer', 'customer', id='legacy-both-customer-customer'),
    pytest.param('legacy-both', 'tenant_user', 'tenant_user', id='legacy-both-tenant_user-tenant_user'),
    pytest.param('legacy-both', 'member', 'member', id='legacy-both-member-member'),
    pytest.param('legacy-both', 'tenant_admin', 'tenant_admin', id='legacy-both-tenant_admin-tenant_admin'),
    pytest.param('legacy-both', 'support', 'support', id='legacy-both-support-support'),
    pytest.param('legacy-both', 'user', 'customer', id='legacy-both-user-customer'),
    pytest.param('legacy-both', 'customer', 'member', id='legacy-both-customer-member'),
    pytest.param('legacy-both', 'admin', 'aiwerk_admin', id='legacy-both-admin-aiwerk_admin'),
    pytest.param('legacy-both', 'aiwerk_admin', 'operator', id='legacy-both-aiwerk_admin-operator'),
])
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


@pytest.mark.parametrize('fault,shape,request_role,stored_role', [
    pytest.param('tenant', 'current-alias', 'user', 'user', id='tenant-current-alias-user-user'),
    pytest.param('tenant', 'current-alias', 'customer', 'user', id='tenant-current-alias-customer-user'),
    pytest.param('tenant', 'current-alias', 'aiwerk_admin', 'admin', id='tenant-current-alias-aiwerk_admin-admin'),
    pytest.param('tenant', 'legacy-flat', 'user', 'user', id='tenant-legacy-flat-user-user'),
    pytest.param('tenant', 'legacy-flat', 'customer', 'user', id='tenant-legacy-flat-customer-user'),
    pytest.param('tenant', 'legacy-flat', 'aiwerk_admin', 'admin', id='tenant-legacy-flat-aiwerk_admin-admin'),
    pytest.param('tenant', 'legacy-nested', 'user', 'user', id='tenant-legacy-nested-user-user'),
    pytest.param('tenant', 'legacy-nested', 'customer', 'user', id='tenant-legacy-nested-customer-user'),
    pytest.param('tenant', 'legacy-nested', 'aiwerk_admin', 'admin', id='tenant-legacy-nested-aiwerk_admin-admin'),
    pytest.param('tenant', 'legacy-both', 'user', 'user', id='tenant-legacy-both-user-user'),
    pytest.param('tenant', 'legacy-both', 'customer', 'user', id='tenant-legacy-both-customer-user'),
    pytest.param('tenant', 'legacy-both', 'aiwerk_admin', 'admin', id='tenant-legacy-both-aiwerk_admin-admin'),
    pytest.param('actor', 'current-alias', 'user', 'user', id='actor-current-alias-user-user'),
    pytest.param('actor', 'current-alias', 'customer', 'user', id='actor-current-alias-customer-user'),
    pytest.param('actor', 'current-alias', 'aiwerk_admin', 'admin', id='actor-current-alias-aiwerk_admin-admin'),
    pytest.param('actor', 'legacy-flat', 'user', 'user', id='actor-legacy-flat-user-user'),
    pytest.param('actor', 'legacy-flat', 'customer', 'user', id='actor-legacy-flat-customer-user'),
    pytest.param('actor', 'legacy-flat', 'aiwerk_admin', 'admin', id='actor-legacy-flat-aiwerk_admin-admin'),
    pytest.param('actor', 'legacy-nested', 'user', 'user', id='actor-legacy-nested-user-user'),
    pytest.param('actor', 'legacy-nested', 'customer', 'user', id='actor-legacy-nested-customer-user'),
    pytest.param('actor', 'legacy-nested', 'aiwerk_admin', 'admin', id='actor-legacy-nested-aiwerk_admin-admin'),
    pytest.param('actor', 'legacy-both', 'user', 'user', id='actor-legacy-both-user-user'),
    pytest.param('actor', 'legacy-both', 'customer', 'user', id='actor-legacy-both-customer-user'),
    pytest.param('actor', 'legacy-both', 'aiwerk_admin', 'admin', id='actor-legacy-both-aiwerk_admin-admin'),
    pytest.param('role', 'current-alias', 'user', 'user', id='role-current-alias-user-user'),
    pytest.param('role', 'current-alias', 'customer', 'user', id='role-current-alias-customer-user'),
    pytest.param('role', 'current-alias', 'aiwerk_admin', 'admin', id='role-current-alias-aiwerk_admin-admin'),
    pytest.param('role', 'legacy-flat', 'user', 'user', id='role-legacy-flat-user-user'),
    pytest.param('role', 'legacy-flat', 'customer', 'user', id='role-legacy-flat-customer-user'),
    pytest.param('role', 'legacy-flat', 'aiwerk_admin', 'admin', id='role-legacy-flat-aiwerk_admin-admin'),
    pytest.param('role', 'legacy-nested', 'user', 'user', id='role-legacy-nested-user-user'),
    pytest.param('role', 'legacy-nested', 'customer', 'user', id='role-legacy-nested-customer-user'),
    pytest.param('role', 'legacy-nested', 'aiwerk_admin', 'admin', id='role-legacy-nested-aiwerk_admin-admin'),
    pytest.param('role', 'legacy-both', 'user', 'user', id='role-legacy-both-user-user'),
    pytest.param('role', 'legacy-both', 'customer', 'user', id='role-legacy-both-customer-user'),
    pytest.param('role', 'legacy-both', 'aiwerk_admin', 'admin', id='role-legacy-both-aiwerk_admin-admin'),
    pytest.param('incomplete', 'current-alias', 'user', 'user', id='incomplete-current-alias-user-user'),
    pytest.param('incomplete', 'current-alias', 'customer', 'user', id='incomplete-current-alias-customer-user'),
    pytest.param('incomplete', 'current-alias', 'aiwerk_admin', 'admin', id='incomplete-current-alias-aiwerk_admin-admin'),
    pytest.param('incomplete', 'legacy-flat', 'user', 'user', id='incomplete-legacy-flat-user-user'),
    pytest.param('incomplete', 'legacy-flat', 'customer', 'user', id='incomplete-legacy-flat-customer-user'),
    pytest.param('incomplete', 'legacy-flat', 'aiwerk_admin', 'admin', id='incomplete-legacy-flat-aiwerk_admin-admin'),
    pytest.param('incomplete', 'legacy-nested', 'user', 'user', id='incomplete-legacy-nested-user-user'),
    pytest.param('incomplete', 'legacy-nested', 'customer', 'user', id='incomplete-legacy-nested-customer-user'),
    pytest.param('incomplete', 'legacy-nested', 'aiwerk_admin', 'admin', id='incomplete-legacy-nested-aiwerk_admin-admin'),
    pytest.param('incomplete', 'legacy-both', 'user', 'user', id='incomplete-legacy-both-user-user'),
    pytest.param('incomplete', 'legacy-both', 'customer', 'user', id='incomplete-legacy-both-customer-user'),
    pytest.param('incomplete', 'legacy-both', 'aiwerk_admin', 'admin', id='incomplete-legacy-both-aiwerk_admin-admin'),
    pytest.param('conflict', 'current-alias', 'user', 'user', id='conflict-current-alias-user-user'),
    pytest.param('conflict', 'current-alias', 'customer', 'user', id='conflict-current-alias-customer-user'),
    pytest.param('conflict', 'current-alias', 'aiwerk_admin', 'admin', id='conflict-current-alias-aiwerk_admin-admin'),
    pytest.param('conflict', 'legacy-flat', 'user', 'user', id='conflict-legacy-flat-user-user'),
    pytest.param('conflict', 'legacy-flat', 'customer', 'user', id='conflict-legacy-flat-customer-user'),
    pytest.param('conflict', 'legacy-flat', 'aiwerk_admin', 'admin', id='conflict-legacy-flat-aiwerk_admin-admin'),
    pytest.param('conflict', 'legacy-nested', 'user', 'user', id='conflict-legacy-nested-user-user'),
    pytest.param('conflict', 'legacy-nested', 'customer', 'user', id='conflict-legacy-nested-customer-user'),
    pytest.param('conflict', 'legacy-nested', 'aiwerk_admin', 'admin', id='conflict-legacy-nested-aiwerk_admin-admin'),
    pytest.param('conflict', 'legacy-both', 'user', 'user', id='conflict-legacy-both-user-user'),
    pytest.param('conflict', 'legacy-both', 'customer', 'user', id='conflict-legacy-both-customer-user'),
    pytest.param('conflict', 'legacy-both', 'aiwerk_admin', 'admin', id='conflict-legacy-both-aiwerk_admin-admin'),
])
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


@pytest.mark.parametrize('foreign_end', [
    pytest.param('root', id='root'),
    pytest.param('tip', id='tip'),
])
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
