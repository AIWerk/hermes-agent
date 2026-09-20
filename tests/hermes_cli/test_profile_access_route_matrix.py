"""Exercise registered HTTP routes, not facade copies or source strings."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.profile_authorization_support import EMPLOYEE, install_policy, seed_store

# Exact primary session action obligations, including body-only and maintenance routes.
SESSION_ROUTES = [
    ("GET", "/api/sessions", None, "session.list"),
    ("GET", "/api/sessions/empty/count", None, "session.list"),
    ("GET", "/api/sessions/stats", None, "session.list"),
    ("DELETE", "/api/sessions/empty", None, "session.mutate"),
    ("GET", "/api/sessions/search?query=synthetic", None, "session.search"),
    ("GET", "/api/sessions/owned", None, "session.read"),
    ("GET", "/api/sessions/owned/messages", None, "session.read"),
    ("GET", "/api/sessions/owned/latest-descendant", None, "session.read"),
    ("GET", "/api/sessions/owned/export", None, "session.export"),
    ("POST", "/api/sessions/bulk-delete", {"ids": ["owned"]}, "session.mutate"),
    ("DELETE", "/api/sessions/owned", None, "session.mutate"),
    ("PATCH", "/api/sessions/owned", {"title": "new"}, "session.mutate"),
    ("PATCH", "/api/sessions/owned", {"archived": True}, "session.mutate"),
    ("POST", "/api/sessions/prune", {}, "session.mutate"),
    ("POST", "/api/sessions/owner-backfill", {}, "session.mutate"),
    ("POST", "/api/sessions/import", {"sessions": []}, "session.mutate"),
    ("GET", "/api/profiles/sessions", None, "session.list"),
    ("GET", "/api/profiles/sessions/sidebar", None, "session.list"),
]
SIBLING_ROUTES = [
    ("GET", "/api/config"), ("GET", "/api/config/raw"), ("GET", "/api/env"),
    ("GET", "/api/mcp/servers"), ("GET", "/api/cron/jobs"),
    ("GET", "/api/memory"), ("GET", "/api/files"), ("GET", "/api/model/options"),
    ("GET", "/api/analytics/usage"), ("GET", "/api/logs"),
    ("GET", "/api/skills"), ("GET", "/api/tools/toolsets"),
]


@pytest.fixture
def api(tmp_path, monkeypatch):
    env = install_policy(tmp_path, monkeypatch)
    from starlette.testclient import TestClient
    from hermes_cli import web_server
    from hermes_cli.dashboard_auth import middleware
    identity = dict(EMPLOYEE)

    async def verified(request, call_next):
        if identity:
            request.state.session = SimpleNamespace(**identity)
        return await call_next(request)

    monkeypatch.setattr(middleware, "gated_auth_middleware", verified)
    monkeypatch.setattr(web_server.app.state, "auth_required", True, raising=False)
    db, home = seed_store(env)
    db.close()
    client = TestClient(web_server.app, raise_server_exceptions=False)
    client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
    yield SimpleNamespace(client=client, env=env, identity=identity, home=home, web=web_server)
    client.close()


@pytest.mark.parametrize("method,path,body,action", SESSION_ROUTES)
def test_registered_session_routes_deny_before_any_profile_resource(api, monkeypatch, method, path, body, action):
    api.env.revoke(action=action)
    spies = {}
    from hermes_cli import web_server_cron
    for name in ["_cron_profile_home", "_open_session_db_for_profile", "_maybe_auto_archive_for_profile", "_resolve_profile_dir"]:
        spies[name] = Mock(return_value=None)
        target = web_server_cron if name == "_cron_profile_home" else api.web
        monkeypatch.setattr(target, name, spies[name])
    response = api.client.request(method, path, json=body)
    assert not any(spy.called for spy in spies.values()), "profile resource reached before authorization"
    assert response.status_code == 403, response.text
    assert response.json() == {"detail": "profile access denied"}


@pytest.mark.parametrize("method,path", SIBLING_ROUTES)
def test_sibling_consumers_cannot_bypass_action_adapter(api, monkeypatch, method, path):
    api.env.revoke()
    spies = [Mock(return_value=None), Mock(return_value={})]
    monkeypatch.setattr(api.web, "_resolve_profile_dir", spies[0])
    monkeypatch.setattr(api.web, "load_config", spies[1])
    response = api.client.request(method, path)
    assert response.status_code == 403, response.text
    assert not any(spy.called for spy in spies)


@pytest.mark.parametrize("selector", ["employee-home", None])
def test_own_list_detail_and_history_positive_without_archive(api, monkeypatch, selector):
    archive = Mock()
    monkeypatch.setattr(api.web, "_maybe_auto_archive_for_profile", archive)
    params = {} if selector is None else {"profile": selector}
    result = api.client.get("/api/sessions", params=params)
    assert result.status_code == 200, result.text
    assert [row["id"] for row in result.json()["sessions"]] == ["owned"]
    assert result.json()["total"] == 1
    for suffix in ["", "/messages", "/latest-descendant"]:
        response = api.client.get("/api/sessions/owned" + suffix, params=params)
        assert response.status_code == 200, response.text
    assert not archive.called


@pytest.mark.parametrize("target", ["peer-home", "other-home", "disabled", "missing", "current", "all", "../x", " employee-home"])
def test_http_target_denials_are_indistinguishable(api, target):
    response = api.client.get("/api/sessions", params={"profile": target})
    assert response.status_code == 403
    assert response.json() == {"detail": "profile access denied"}


def test_discovery_and_sidebar_recheck_before_warmed_cache(api, monkeypatch):
    from hermes_cli import profiles
    roster = Mock(return_value=[])
    monkeypatch.setattr(profiles, "list_profiles", roster)
    first = api.client.get("/api/profiles")
    assert first.status_code == 200, first.text
    assert "employee-home" in first.text and "peer-home" not in first.text
    assert not roster.called
    assert api.client.get("/api/profiles/sessions/sidebar").status_code == 200
    api.env.revoke(action="session.list")
    assert api.client.get("/api/profiles/sessions/sidebar").status_code == 403


@pytest.mark.parametrize("body", [{"name": "peer-home"}, {"name": []}, {"name": "employee-home", "clone_from": "peer-home"}])
def test_profile_manager_body_and_clone_denied_before_resolver(api, body, monkeypatch):
    resolver = Mock(return_value=None)
    monkeypatch.setattr(api.web, "_resolve_profile_dir", resolver)
    response = api.client.post("/api/profiles", json=body)
    assert response.status_code in (403, 422), response.text
    assert not resolver.called


def test_absent_verified_identity_is_not_internal_token_bypass(api):
    api.identity.clear()
    response = api.client.get("/api/sessions")
    assert response.status_code == 403


def test_public_model_info_bypasses_profile_authorization_without_identity(api):
    api.identity.clear()
    response = api.client.get("/api/model/info")
    assert response.status_code == 200, response.text
    assert response.json()["agent_name"]


def test_own_mutation_and_denied_export_are_separate_grants(api):
    response = api.client.patch("/api/sessions/owned", json={"title": "safe title"})
    assert response.status_code == 200, response.text
    assert api.client.get("/api/sessions/owned").json()["title"] == "safe title"
    assert api.client.get("/api/sessions/owned/export").status_code == 403
    assert api.client.delete("/api/sessions/owned").status_code == 200


def test_marker_absent_preserves_legacy_local_http(api, monkeypatch):
    api.env.marker.unlink()
    api.identity.clear()
    monkeypatch.setattr(api.web.app.state, "auth_required", False)
    response = api.client.get("/api/sessions")
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("suffix", ["", "/messages", "/latest-descendant", "/export"])
@pytest.mark.parametrize("session", ["ambiguous", "foreign"])
def test_membership_does_not_grant_foreign_or_ambiguous_rows(api, suffix, session):
    response = api.client.get("/api/sessions/" + session + suffix,
                              params={"profile": "employee-home"})
    assert response.status_code in (403, 404)
    assert "hidden" not in response.text


@pytest.mark.parametrize("fault", ["missing", "corrupt", "unsafe", "disabled"])
def test_http_reloads_root_policy_after_successful_discovery(api, fault):
    # First dispatch deliberately warms the real mounted discovery surface.
    assert api.client.get("/api/profiles").status_code == 200
    if fault == "missing":
        api.env.path.unlink()
    elif fault == "corrupt":
        api.env.path.write_text("broken: policy")
    elif fault == "unsafe":
        api.env.path.chmod(0o666)
    else:
        api.env.document["profiles"][0]["enabled"] = False
        api.env.save()
    response = api.client.get("/api/sessions")
    assert response.status_code == 403
    assert response.json() == {"detail": "profile access denied"}


@pytest.mark.parametrize("method,path,body", [
    ("POST", "/api/profiles/employee-home/open-terminal", None),
    ("GET", "/api/profiles/employee-home/setup-command", None),
    ("POST", "/api/profiles/active", {"name": "peer-home"}),
    ("PATCH", "/api/profiles/employee-home", {"new_name": "peer-home"}),
    ("DELETE", "/api/profiles/employee-home", None),
])
def test_profile_management_denies_before_launch_or_name_resolution(api, monkeypatch, method, path, body):
    from hermes_cli import profiles
    from hermes_cli.web_routers import profiles as routes
    sinks = []
    for target, name in [(profiles, "set_active_profile"), (profiles, "rename_profile"),
                         (profiles, "delete_profile"), (routes, "_profile_setup_command")]:
        spy = Mock(side_effect=RuntimeError("forbidden management sink"))
        monkeypatch.setattr(target, name, spy)
        sinks.append(spy)
    response = api.client.request(method, path, json=body)
    assert response.status_code == 403, response.text
    assert not any(s.called for s in sinks)


def test_project_tree_preserves_restricted_empty_disposition(api, monkeypatch):
    from hermes_cli.web_routers import profiles
    scan = Mock(side_effect=RuntimeError("must not scan projects"))
    monkeypatch.setattr(profiles, "_profile_targets", scan)
    response = api.client.get("/api/profiles/projects/tree")
    assert response.status_code == 200, response.text
    assert response.json().get("projects") == []
    assert not scan.called


@pytest.mark.parametrize("selector", ["foreign", "foreign-title"])
def test_http_alias_title_denies_before_mutation(api, selector):
    import sqlite3
    with sqlite3.connect(api.home / "state.db") as db:
        db.execute("UPDATE sessions SET title='foreign-title' WHERE id='foreign'")
    response = api.client.patch("/api/sessions/" + selector, params={"profile": "employee-home"}, json={"hidden": True})
    assert response.status_code in (403, 404), response.text
    with sqlite3.connect(api.home / "state.db") as db:
        assert db.execute("SELECT hidden FROM sessions WHERE id='foreign'").fetchone()[0] == 0


def test_mixed_owner_bulk_denies_before_first_mutation(api, monkeypatch):
    from hermes_state import SessionDB
    mutate = Mock(side_effect=RuntimeError("first mutation forbidden"))
    monkeypatch.setattr(SessionDB, "delete_sessions", mutate)
    response = api.client.post("/api/sessions/bulk-delete",
                               json={"ids": ["owned", "foreign"], "profile": "employee-home"})
    assert response.status_code in (403, 404), response.text
    assert not mutate.called


@pytest.mark.parametrize("suffix", ["/latest-descendant", "/messages"])
def test_http_foreign_continuation_is_not_projected(api, suffix):
    import sqlite3
    with sqlite3.connect(api.home / "state.db") as db:
        db.execute("UPDATE sessions SET parent_session_id='owned' WHERE id='foreign'")
        db.execute("UPDATE sessions SET end_reason='compression' WHERE id='owned'")
    response = api.client.get("/api/sessions/owned" + suffix, params={"profile": "employee-home"})
    assert response.status_code in (200, 403, 404), response.text
    assert "unmapped hidden" not in response.text
    assert '"foreign"' not in response.text
