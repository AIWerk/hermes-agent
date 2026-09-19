"""Delegated reads use actual SQLite, not the mutating SessionDB constructor."""
import json
import sqlite3
from unittest.mock import Mock

import pytest

from tests.profile_authorization_support import ADMIN, EMPLOYEE, capability, install_policy, seed_store


@pytest.fixture
def store(tmp_path, monkeypatch):
    env = install_policy(tmp_path, monkeypatch)
    db, home = seed_store(env)
    db.close()
    return env, home


def reader():
    return capability("hermes_cli.dashboard_auth.admin_session_reader")


@pytest.mark.parametrize("operation", ["list", "search", "read"])
def test_admin_reads_real_store_without_runtime_or_healing(store, monkeypatch, operation):
    env, home = store
    module = reader()
    import hermes_state
    constructor = Mock()
    monkeypatch.setattr(hermes_state, "SessionDB", constructor)
    before = dict(__import__("os").environ)
    result = module.read_sessions(ADMIN, "employee-home", operation,
                                  session_id="owned" if operation == "read" else None,
                                  query="synthetic" if operation == "search" else None,
                                  limit=1, offset=0)
    assert [row["id"] for row in result["sessions"]] == ["owned"]
    if operation == "read":
        assert result["messages"][0]["content"] == "synthetic hello"
    assert not constructor.called
    assert dict(__import__("os").environ) == before
    assert not any(k in json.dumps(result) for k in ["model_config", "system_prompt", str(home), "unmapped hidden", "ambiguous hidden"])


def test_connection_enforces_readonly_even_for_real_writable_sql(store):
    _, home = store
    module = reader()
    with module._open_readonly(home / "state.db") as connection:
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("CREATE TABLE forbidden (id INTEGER)")


def test_connection_stays_bound_to_checked_file_during_ancestor_replacement(store, monkeypatch):
    env, home = store
    module = reader()
    outside = env.home.parent / "outside-home"
    outside.mkdir()
    with sqlite3.connect(outside / "state.db") as connection:
        connection.execute("CREATE TABLE sessions (title TEXT)")
        connection.execute("INSERT INTO sessions VALUES ('OUTSIDE_SENTINEL')")
    original_connect = sqlite3.connect

    def replace_ancestor(database, **kwargs):
        home.rename(home.with_name("employee-home-old"))
        home.symlink_to(outside, target_is_directory=True)
        return original_connect(database, **kwargs)

    monkeypatch.setattr(module.sqlite3, "connect", replace_ancestor)
    with module._open_readonly(home / "state.db") as connection:
        assert connection.execute(
            "SELECT title FROM sessions WHERE id='owned'"
        ).fetchone()[0] != "OUTSIDE_SENTINEL"


def test_missing_schema_is_not_healed_and_errors_are_generic(store):
    _, home = store
    module = reader()
    path = home / "state.db"
    path.unlink()
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE sentinel (id INTEGER)")
    before = path.read_bytes()
    with pytest.raises(module.AdminReadUnavailable, match="^session data unavailable$"):
        module.read_sessions(ADMIN, "employee-home", "list")
    assert path.read_bytes() == before


@pytest.mark.parametrize("actor,target,operation", [
    (EMPLOYEE, "employee-home", "list"), (ADMIN, None, "list"),
    (ADMIN, "other-home", "read"), (ADMIN, "missing", "search"),
    (ADMIN, "employee-home", "export"), (ADMIN, "employee-home", "resume"),
    (ADMIN, "employee-home", "delete"), (ADMIN, "employee-home", "archive"),
])
def test_denial_before_home_existence_or_database(store, monkeypatch, actor, target, operation):
    module = reader()
    from hermes_cli import profiles
    resolver = Mock()
    opened = Mock()
    monkeypatch.setattr(profiles, "get_profile_dir", resolver)
    monkeypatch.setattr(module, "_open_readonly", opened)
    with pytest.raises(module.ProfileAccessDenied, match="^profile access denied$"):
        module.read_sessions(actor, target, operation, session_id="owned")
    assert not resolver.called and not opened.called


def test_fresh_bounded_pagination_and_revocation(store):
    env, _ = store
    module = reader()
    first = module.read_sessions(ADMIN, "employee-home", "list", limit=1, offset=0)
    second = module.read_sessions(ADMIN, "employee-home", "list", limit=1, offset=1)
    assert len(first["sessions"]) == 1 and second["sessions"] == []
    env.revoke("admin", "session.list")
    with pytest.raises(module.ProfileAccessDenied):
        module.read_sessions(ADMIN, "employee-home", "list", limit=1, offset=0)


@pytest.mark.parametrize("limit,offset", [(True, 0), (1, False), (0, 0), (101, 0), (1, -1), ("1", 0), ([], 0)])
def test_pagination_is_typed_and_bounded_before_open(store, monkeypatch, limit, offset):
    module = reader()
    opened = Mock()
    monkeypatch.setattr(module, "_open_readonly", opened)
    with pytest.raises(module.ProfileAccessDenied):
        module.read_sessions(ADMIN, "employee-home", "list", limit=limit, offset=offset)
    assert not opened.called


def test_lineage_and_known_actor_profile_ownership_remain_independent(store):
    env, home = store
    module = reader()
    with sqlite3.connect(home / "state.db") as db:
        db.execute("UPDATE sessions SET parent_session_id='foreign' WHERE id='owned'")
    assert module.read_sessions(ADMIN, "employee-home", "list")["sessions"] == []
    with pytest.raises(module.AdminReadUnavailable):
        module.read_sessions(ADMIN, "employee-home", "read", session_id="owned")


def test_audit_success_denial_allowlist_and_sanitized_projection(store, monkeypatch):
    env, home = store
    module = reader()
    from hermes_cli.dashboard_auth import audit
    records = []
    monkeypatch.setattr(audit, "audit_log", lambda event, **fields: records.append((event.value, fields)))
    secret = "sk-" + "z" * 48
    with sqlite3.connect(home / "state.db") as db:
        db.execute("UPDATE messages SET content=? WHERE session_id='owned'", ("Authorization: Bearer " + secret,))
    result = module.read_sessions(ADMIN, "employee-home", "read", session_id="owned", query="private search")
    assert secret not in json.dumps(result)
    with pytest.raises(module.ProfileAccessDenied):
        module.read_sessions(EMPLOYEE, "employee-home", "search", query="private search")
    assert len(records) == 2
    required = {"event_id", "actor_id", "tenant_id", "target_profile", "action", "policy_revision", "result", "count", "correlation_id"}
    for event, fields in records:
        assert event == "delegated_session_read"
        assert set(fields) == required
        assert fields["result"] in ("allowed", "denied")
        assert type(fields["count"]) is int and 0 <= fields["count"] <= 100
        assert not any(s in json.dumps(fields) for s in [secret, "private search", str(home)])
    assert {fields["result"] for _, fields in records} == {"allowed", "denied"}


def test_policy_unavailable_denial_keeps_only_verified_bounded_attribution(store, monkeypatch):
    env, _ = store
    module = reader()
    from hermes_cli.dashboard_auth import audit
    records = []
    monkeypatch.setattr(audit, "audit_log", lambda event, **fields: records.append(fields))
    env.marker.write_bytes(b"invalid\n")
    with pytest.raises(module.ProfileAccessDenied):
        module.read_sessions(ADMIN, "employee-home", "list")
    assert (records[-1]["actor_id"], records[-1]["tenant_id"], records[-1]["result"],
            records[-1]["policy_revision"]) == ("admin", "tenant-a", "denied", "unavailable")

    audit.delegated_session_read(
        {"actor_id": " Bearer credential ", "tenant_id": "tenant-a", "role": "admin"},
        "employee-home", "session.list", None, "denied", 0, correlation_id="synthetic")
    assert records[-1]["actor_id"] == records[-1]["tenant_id"] == ""


def test_dedicated_admin_router_is_registered_and_only_read_operations(store, monkeypatch):
    from starlette.testclient import TestClient
    from types import SimpleNamespace
    from hermes_cli import web_server
    from hermes_cli.dashboard_auth import middleware

    async def verified(request, call_next):
        request.state.session = SimpleNamespace(**ADMIN)
        return await call_next(request)

    monkeypatch.setattr(middleware, "gated_auth_middleware", verified)
    with TestClient(web_server.app, raise_server_exceptions=False) as client:
        client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
        response = client.get("/api/admin/session-reader/employee-home/sessions")
        assert response.status_code == 200, response.text
        assert [row["id"] for row in response.json()["sessions"]] == ["owned"]
        for method in ["POST", "PATCH", "DELETE"]:
            assert client.request(method, "/api/admin/session-reader/employee-home/sessions").status_code == 405


def test_detail_route_pages_complete_transcript_with_bounded_continuation(store, monkeypatch):
    _, home = store
    with sqlite3.connect(home / "state.db") as connection:
        connection.execute("DELETE FROM messages WHERE session_id='owned'")
        connection.executemany(
            "INSERT INTO messages (session_id,role,content,timestamp) VALUES ('owned','user',?,0)",
            [(f"synthetic-{index}",) for index in range(51)],
        )
    from starlette.testclient import TestClient
    from types import SimpleNamespace
    from hermes_cli import web_server
    from hermes_cli.dashboard_auth import middleware

    async def verified(request, call_next):
        request.state.session = SimpleNamespace(**ADMIN)
        return await call_next(request)

    monkeypatch.setattr(middleware, "gated_auth_middleware", verified)
    with TestClient(web_server.app, raise_server_exceptions=False) as client:
        client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
        first = client.get("/api/admin/session-reader/employee-home/sessions/owned?limit=50&offset=0")
        second = client.get("/api/admin/session-reader/employee-home/sessions/owned?limit=50&offset=50")
        assert first.status_code == second.status_code == 200
        assert len(first.json()["messages"]) == 50
        assert first.json()["has_more"] is True and first.json()["next_offset"] == 50
        assert [message["content"] for message in second.json()["messages"]] == ["synthetic-50"]
        assert second.json()["has_more"] is False and second.json()["next_offset"] is None
        for query in ("limit=true", "limit=-1", "limit=101", "offset=-1", "offset=true"):
            assert client.get(
                f"/api/admin/session-reader/employee-home/sessions/owned?{query}"
            ).status_code in {403, 422}


@pytest.mark.parametrize("fault", ["missing", "corrupt", "symlink-db", "symlink-home"])
def test_broker_store_faults_do_not_create_heal_or_follow_outside_home(store, fault):
    env, home = store
    path = home / "state.db"
    outside = env.home.parent / "outside.db"
    outside.write_bytes(path.read_bytes())
    before = outside.read_bytes()
    path.unlink()
    if fault == "corrupt":
        path.write_bytes(b"not a sqlite database")
    elif fault == "symlink-db":
        path.symlink_to(outside)
    elif fault == "symlink-home":
        import shutil
        shutil.rmtree(home)
        outside_home = env.home.parent / "outside-home"
        outside_home.mkdir()
        (outside_home / "state.db").write_bytes(before)
        home.symlink_to(outside_home, target_is_directory=True)
    module = reader()
    with pytest.raises(module.AdminReadUnavailable, match="^session data unavailable$"):
        module.read_sessions(ADMIN, "employee-home", "list")
    assert outside.read_bytes() == before
    if fault == "missing":
        assert not path.exists()
    if fault == "corrupt":
        assert path.read_bytes() == b"not a sqlite database"


def test_broker_recursive_projection_and_search_do_not_leak_credentials(store):
    _, home = store
    secret = "sk-" + "q" * 48
    content = json.dumps({"message": "searchneedle", "nested": [{"api_key": secret, "Authorization": "Bearer " + secret}]})
    with sqlite3.connect(home / "state.db") as db:
        db.execute("UPDATE messages SET content=? WHERE session_id='owned'", (content,))
    module = reader()
    for operation in ["read", "search"]:
        result = module.read_sessions(ADMIN, "employee-home", operation, session_id="owned" if operation == "read" else None,
                                      query="searchneedle" if operation == "search" else None)
        assert [row["id"] for row in result["sessions"]] == ["owned"]
        assert "searchneedle" in json.dumps(result)
        assert secret not in json.dumps(result)
        assert "model_config" not in json.dumps(result)


def test_broker_audit_attributes_allow_and_deny_to_actual_caller(store, monkeypatch):
    env, _ = store
    module = reader()
    from hermes_cli.dashboard_auth import audit
    records = []
    monkeypatch.setattr(audit, "audit_log", lambda event, **fields: records.append(fields))
    module.read_sessions(ADMIN, "employee-home", "list")
    with pytest.raises(module.ProfileAccessDenied):
        module.read_sessions(EMPLOYEE, "employee-home", "list")
    assert len(records) == 2
    for record, actor, result in zip(records, [ADMIN, EMPLOYEE], ["allowed", "denied"]):
        assert (record["actor_id"], record["tenant_id"], record["target_profile"], record["action"], record["result"]) == (
            actor["actor_id"], actor["tenant_id"], "employee-home", "session.list", result)
        assert record["policy_revision"] and record["event_id"] and record["correlation_id"]


@pytest.mark.parametrize("parent", ["foreign", "ambiguous", "missing"])
def test_broker_lineage_rejects_unprovable_ancestor_for_search_and_read(store, parent):
    _, home = store
    with sqlite3.connect(home / "state.db") as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("UPDATE sessions SET parent_session_id=? WHERE id='owned'", (parent,))
    module = reader()
    assert module.read_sessions(ADMIN, "employee-home", "search", query="synthetic")["sessions"] == []
    with pytest.raises(module.AdminReadUnavailable):
        module.read_sessions(ADMIN, "employee-home", "read", session_id="owned")
