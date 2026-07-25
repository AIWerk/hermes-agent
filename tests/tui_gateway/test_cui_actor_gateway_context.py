import json
import os
import threading
from types import SimpleNamespace

from agent import cui_actor_context
from tui_gateway import server


def test_dispatch_session_create_stores_authenticated_cui_actor(monkeypatch, tmp_path):
    server._sessions.clear()
    monkeypatch.setattr(server, "_start_agent_build", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "_completion_cwd", lambda params=None: str(tmp_path))
    monkeypatch.setattr(server, "_load_show_reasoning", lambda: False)
    monkeypatch.setattr(server, "_load_tool_progress_mode", lambda: "collapsed")

    actor = {
        "tenant_id": "example-tenant",
        "actor_id": "aiwerk:operator:admin",
        "role": "admin",
        "display_name": "Operator",
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
            "tenant_id": "example-tenant",
            "actor_id": "aiwerk:operator:admin",
            "role": "admin",
            "display_name": "Operator",
            "provider": "dashboard_auth",
        }
        assert server.current_cui_actor_context() is None
    finally:
        for sess in list(server._sessions.values()):
            server._teardown_session(sess)
        server._sessions.clear()


def _row(source="tui", model_config=None):
    import json as _json

    return {
        "id": "sess-x",
        "source": source,
        "model_config": _json.dumps(model_config) if isinstance(model_config, dict) else model_config,
    }


def test_row_visibility_no_actor_is_unconfined():
    # Standalone TUI / loopback admin dispatch (actor=None) sees everything.
    assert server._row_visible_to_cui_actor(_row(source="cli"), None) is True
    assert server._row_visible_to_cui_actor(_row(source="cli"), {}) is True


def test_row_visibility_admin_actor_sees_legacy_but_not_other_tagged_actor():
    admin = {"tenant_id": "t1", "actor_id": "a1", "role": "admin"}
    assert server._row_visible_to_cui_actor(_row(source="cli"), admin) is True
    other_tagged = _row(
        model_config={
            "_cui_actor_context": {"tenant_id": "t1", "actor_id": "a2", "role": "user"},
        }
    )
    own_tagged = _row(
        model_config={
            "_cui_actor_context": {"tenant_id": "t1", "actor_id": "a1", "role": "admin"},
        }
    )
    assert server._row_visible_to_cui_actor(other_tagged, admin) is False
    assert server._row_visible_to_cui_actor(own_tagged, admin) is True


def test_row_visibility_customer_blocked_from_internal_and_unowned():
    cust = {"tenant_id": "example-tenant", "actor_id": "customer-1", "role": "user"}
    # Legacy internal/CLI session with no CUI metadata: fail closed.
    assert server._row_visible_to_cui_actor(_row(source="cli"), cust) is False
    # A tui row with no metadata: cannot prove ownership, fail closed.
    assert server._row_visible_to_cui_actor(_row(source="tui"), cust) is False
    # Admin-tagged session: blocked even with matching tenant.
    admin_tagged = _row(
        source="tui",
        model_config={
            "_cui_actor_context": {"tenant_id": "example-tenant", "actor_id": "admin-1", "role": "admin"},
        },
    )
    assert server._row_visible_to_cui_actor(admin_tagged, cust) is False
    # Other-tenant session: blocked.
    other_tenant = _row(
        source="tui",
        model_config={
            "_cui_actor_context": {"tenant_id": "other-co", "actor_id": "customer-1", "role": "user"},
        },
    )
    assert server._row_visible_to_cui_actor(other_tenant, cust) is False
    # Other-actor, same tenant: blocked.
    other_actor = _row(
        source="tui",
        model_config={
            "_cui_actor_context": {"tenant_id": "example-tenant", "actor_id": "customer-2", "role": "user"},
        },
    )
    assert server._row_visible_to_cui_actor(other_actor, cust) is False


def test_row_visibility_customer_sees_own_session():
    cust = {"tenant_id": "example-tenant", "actor_id": "customer-1", "role": "user"}
    own = _row(
        source="tui",
        model_config={
            "_cui_actor_context": {"tenant_id": "example-tenant", "actor_id": "customer-1", "role": "user"},
        },
    )
    assert server._row_visible_to_cui_actor(own, cust) is True


def test_live_session_visibility_refuses_transport_rebind_for_non_owner():
    cust = {"tenant_id": "example-tenant", "actor_id": "customer-1", "role": "user"}
    # Live session owned by a different actor: rebind refused.
    other = {"cui_actor_context": {"tenant_id": "example-tenant", "actor_id": "customer-2", "role": "user"}}
    assert server._live_session_visible_to_cui_actor(other, cust) is False
    # Live session with no owner identity (standalone TUI): not hijackable.
    assert server._live_session_visible_to_cui_actor({"cui_actor_context": {}}, cust) is False
    assert server._live_session_visible_to_cui_actor(
        {"cui_actor_context": {}},
        {"tenant_id": "example-tenant", "actor_id": "admin-1", "role": "admin"},
    ) is False
    # Own live session: allowed.
    own = {"cui_actor_context": {"tenant_id": "example-tenant", "actor_id": "customer-1", "role": "user"}}
    assert server._live_session_visible_to_cui_actor(own, cust) is True
    user_id_owned = {
        "cui_actor_context": {
            "tenant_id": "example-tenant",
            "user_id": "customer-1",
            "role": "user",
        }
    }
    assert server._live_session_visible_to_cui_actor(
        user_id_owned,
        {"tenant_id": "example-tenant", "user_id": "customer-1", "role": "user"},
    ) is True
    assert server._live_session_visible_to_cui_actor(
        user_id_owned,
        {"tenant_id": "example-tenant", "user_id": "customer-2", "role": "user"},
    ) is False
    # A different admin actor may not hijack a tagged live session either.
    assert server._live_session_visible_to_cui_actor(
        other, {"role": "admin", "actor_id": "x", "tenant_id": "t"}
    ) is False
    # Trusted no-actor dispatch remains unconfined.
    assert server._live_session_visible_to_cui_actor(other, None) is True


def test_live_session_visibility_fails_closed_for_incomplete_actor():
    other = {
        "cui_actor_context": {
            "tenant_id": "example-tenant",
            "actor_id": "customer-2",
            "role": "user",
        }
    }
    incomplete = [
        {"role": "user", "_restricted": "1"},
        {"role": "user"},
        {"tenant_id": "example-tenant", "role": "user"},
        {"actor_id": "customer-1", "role": "user"},
        {"tenant_id": "example-tenant", "actor_id": "customer-1"},
    ]
    for actor in incomplete:
        assert server._live_session_visible_to_cui_actor(other, actor) is False


def _call_as(actor, method, params=None):
    token = server.bind_cui_actor_context(actor)
    try:
        return server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": method,
                "method": method,
                "params": params or {},
            }
        )
    finally:
        server.reset_cui_actor_context(token)


def _live_owned_by(actor_id, *, role="user"):
    return {
        "cui_actor_context": {
            "tenant_id": "example-tenant",
            "actor_id": actor_id,
            "role": role,
        },
        "created_at": 100.0,
        "history": [],
        "history_lock": threading.RLock(),
        "last_active": 100.0,
        "running": False,
        "session_key": f"stored-{actor_id}",
        "transport": "ORIGINAL",
    }


def test_active_list_only_returns_live_sessions_owned_by_actor(monkeypatch):
    requester = {"tenant_id": "example-tenant", "actor_id": "customer-1", "role": "user"}
    monkeypatch.setattr(
        server,
        "_sessions",
        {
            "own": _live_owned_by("customer-1"),
            "foreign": _live_owned_by("customer-2"),
            "legacy": {**_live_owned_by("customer-3"), "cui_actor_context": {}},
        },
    )
    monkeypatch.setattr(server, "_session_live_item", lambda sid, _session, _current: {"id": sid})

    response = _call_as(requester, "session.active_list")

    assert response["result"]["sessions"] == [{"id": "own"}]


def test_active_list_fails_closed_for_incomplete_actor(monkeypatch):
    monkeypatch.setattr(server, "_sessions", {"foreign": _live_owned_by("customer-2")})
    monkeypatch.setattr(server, "_session_live_item", lambda sid, _session, _current: {"id": sid})

    response = _call_as({"role": "user", "_restricted": "1"}, "session.active_list")

    assert response["result"]["sessions"] == []


def test_activate_rejects_foreign_session_before_touch_for_user_and_admin(monkeypatch):
    foreign = _live_owned_by("customer-2")
    touched = []
    monkeypatch.setattr(server, "_sessions", {"foreign": foreign})
    monkeypatch.setattr(
        server,
        "_live_session_payload",
        lambda *args, **kwargs: touched.append((args, kwargs)) or {},
    )

    for actor in (
        {"tenant_id": "example-tenant", "actor_id": "customer-1", "role": "user"},
        {"tenant_id": "example-tenant", "actor_id": "admin-1", "role": "admin"},
    ):
        response = _call_as(actor, "session.activate", {"session_id": "foreign"})
        assert response["error"]["code"] == 4007

    assert touched == []
    assert foreign["transport"] == "ORIGINAL"
    assert foreign["last_active"] == 100.0


def test_session_history_central_gate_rejects_foreign_incomplete_and_unowned(monkeypatch):
    foreign = _live_owned_by("customer-2")
    foreign["history"] = [{"role": "assistant", "content": "foreign secret"}]
    unowned = {**_live_owned_by("legacy"), "cui_actor_context": {}}
    unowned["history"] = [{"role": "assistant", "content": "legacy secret"}]
    monkeypatch.setattr(server, "_sessions", {"foreign": foreign, "unowned": unowned})

    cases = (
        ({"tenant_id": "example-tenant", "actor_id": "customer-1", "role": "user"}, "foreign"),
        ({"tenant_id": "example-tenant", "actor_id": "admin-1", "role": "admin"}, "foreign"),
        ({"role": "user", "_restricted": "1"}, "foreign"),
        ({"tenant_id": "example-tenant", "role": "user"}, "foreign"),
        ({"tenant_id": "example-tenant", "actor_id": "customer-1", "role": "user"}, "unowned"),
        ({"tenant_id": "example-tenant", "actor_id": "admin-1", "role": "admin"}, "unowned"),
    )
    for actor, sid in cases:
        response = _call_as(actor, "session.history", {"session_id": sid})
        assert response["error"]["code"] == 4007
        assert "secret" not in str(response)


def test_prompt_submit_rejects_foreign_session_before_attachment_or_rebind(monkeypatch):
    foreign = _live_owned_by("customer-2")
    foreign["running"] = True
    processed = []
    monkeypatch.setattr(server, "_sessions", {"foreign": foreign})
    monkeypatch.setattr(
        server,
        "_process_prompt_attachments",
        lambda attachments, sid: processed.append((attachments, sid)) or ([], ""),
    )
    monkeypatch.setattr(server, "_handle_busy_submit", lambda *a, **k: server._ok("x", {}))
    requester = {"tenant_id": "example-tenant", "actor_id": "customer-1", "role": "user"}

    response = _call_as(
        requester,
        "prompt.submit",
        {"session_id": "foreign", "text": "take over", "attachments": [{"path": "x"}]},
    )

    assert response["error"]["code"] == 4007
    assert processed == []
    assert foreign["transport"] == "ORIGINAL"
    assert foreign["cui_actor_context"]["actor_id"] == "customer-2"


def test_compress_rejects_foreign_session_before_compute_or_local_mutation(monkeypatch):
    foreign = _live_owned_by("customer-2")
    compute_checks = []
    local_checks = []
    monkeypatch.setattr(server, "_sessions", {"foreign": foreign})
    monkeypatch.setattr(
        server,
        "_session_uses_compute_host",
        lambda session: compute_checks.append(session) or False,
    )
    monkeypatch.setattr(
        server,
        "_sess",
        lambda params, rid: local_checks.append((params, rid)) or (foreign, None),
    )

    for actor in (
        {"tenant_id": "example-tenant", "actor_id": "customer-1", "role": "user"},
        {"tenant_id": "example-tenant", "actor_id": "admin-1", "role": "admin"},
    ):
        response = _call_as(actor, "session.compress", {"session_id": "foreign"})
        assert response["error"]["code"] == 4007

    assert compute_checks == []
    assert local_checks == []


def test_eager_resume_race_rechecks_live_owner_before_touch(monkeypatch):
    target = "stored-session"
    requester = {"tenant_id": "example-tenant", "actor_id": "customer-1", "role": "user"}
    stored = _row(model_config={"_cui_actor_context": requester})
    stored["id"] = target
    foreign = _live_owned_by("customer-2")
    foreign["session_key"] = target
    touched = []
    closed = []
    released = []
    lookups = iter((None, ("foreign", foreign)))

    class _DB:
        def get_session(self, session_id):
            return stored if session_id == target else None

        def resolve_resume_session_id(self, session_id):
            return session_id

        def reopen_session(self, _session_id):
            return None

        def get_resume_conversations(self, _session_id):
            return ([], [])

        def get_ancestor_display_prefix(self, _session_id):
            return []

    class _Lease:
        def release(self):
            released.append(True)

    agent = SimpleNamespace(close=lambda: closed.append(True))
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_find_live_session", lambda _target: None)
    monkeypatch.setattr(server, "_find_live_session_by_key", lambda _target: next(lookups))
    monkeypatch.setattr(server, "_get_db", lambda: _DB())
    monkeypatch.setattr(server, "_profile_home", lambda _profile: None)
    monkeypatch.setattr(server, "_claim_active_session_slot", lambda *a, **k: (_Lease(), None))
    monkeypatch.setattr(server, "_enable_gateway_prompts", lambda: None)
    monkeypatch.setattr(server, "_make_agent", lambda *a, **k: agent)
    monkeypatch.setattr(server, "_stored_session_runtime_overrides", lambda _row: {})
    monkeypatch.setattr(
        server,
        "_live_session_payload",
        lambda *args, **kwargs: touched.append((args, kwargs)) or {},
    )

    response = _call_as(requester, "session.resume", {"session_id": target, "eager_build": True})

    assert response["error"]["code"] == 4007
    assert touched == []
    assert closed == [True]
    assert released == [True]
    assert foreign["transport"] == "ORIGINAL"
    assert foreign["last_active"] == 100.0


def test_resume_alias_live_session_cannot_bypass_actor_visibility(monkeypatch):
    target = "stored-session"
    requester = {
        "tenant_id": "example-tenant",
        "actor_id": "customer-1",
        "role": "user",
    }
    stored = _row(
        model_config={
            "_cui_actor_context": requester,
        }
    )
    stored["id"] = target

    class _DB:
        def get_session(self, session_id):
            return stored if session_id == target else None

        def resolve_resume_session_id(self, session_id):
            return session_id

    live = {
        "session_key": "different-live-key",
        "agent": SimpleNamespace(session_id=target),
        "cui_actor_context": {
            "tenant_id": "example-tenant",
            "actor_id": "customer-2",
            "role": "user",
        },
        "transport": "ORIGINAL",
        "last_active": 123,
    }
    touched = []
    monkeypatch.setattr(server, "_sessions", {"live-sid": live})
    monkeypatch.setattr(server, "_get_db", lambda: _DB())
    monkeypatch.setattr(server, "_profile_home", lambda _profile: None)
    monkeypatch.setattr(
        server,
        "_live_session_payload",
        lambda *args, **kwargs: touched.append((args, kwargs)) or {},
    )

    token = server.bind_cui_actor_context(requester)
    try:
        response = server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": "alias-resume",
                "method": "session.resume",
                "params": {"session_id": target},
            }
        )
    finally:
        server.reset_cui_actor_context(token)

    assert response["error"]["code"] == 4007
    assert touched == []
    assert live["transport"] == "ORIGINAL"
    assert live["last_active"] == 123


def test_resume_404s_cross_actor_stored_session(tmp_path, monkeypatch):
    import json as _json

    import hermes_state

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    db = hermes_state.SessionDB()
    monkeypatch.setattr(server, "_db", db)
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)
    server._sessions.clear()

    # An ADMIN onboarding session with a predictable title, owned by an admin.
    db.create_session(
        "admin-onboarding-1",
        source="tui",
        model_config=_json.dumps(
            {"_cui_actor_context": {"tenant_id": "aiwerk", "actor_id": "admin-1", "role": "admin"}}
        ),
    )
    db.set_session_title("admin-onboarding-1", "Tenant Setup Example")

    customer = {"tenant_id": "example-tenant", "actor_id": "customer-1", "role": "user"}

    def _resume(target, actor, **extra):
        # session.resume is a long handler (dispatch returns None and writes via
        # transport). Bind the actor context like dispatch does and call the
        # handler directly so we get the response dict back.
        token = server.bind_cui_actor_context(actor)
        try:
            return server.handle_request(
                {"jsonrpc": "2.0", "id": "r", "method": "session.resume",
                 "params": {"session_id": target, **extra}}
            )
        finally:
            server.reset_cui_actor_context(token)

    try:
        # Resume by id → 404 (not visible to the customer).
        resp = _resume("admin-onboarding-1", customer)
        assert resp["error"]["code"] == 4007

        # Resume by human-readable TITLE → 404 (title lookup disabled for
        # confined customers; admin titles are predictable).
        resp_title = _resume("Tenant Setup Example", customer)
        assert resp_title["error"]["code"] == 4007

        # An admin actor CAN resolve the title (affordance preserved): the
        # visibility gate does not 404 it (it resolves to the real id).
        admin = {"tenant_id": "aiwerk", "actor_id": "admin-1", "role": "admin"}
        resp_admin = _resume("Tenant Setup Example", admin, eager_build=False)
        assert resp_admin.get("result") is not None
        assert resp_admin["result"]["resumed"] == "admin-onboarding-1"
    finally:
        for sess in list(server._sessions.values()):
            try:
                server._teardown_session(sess)
            except Exception:
                pass
        server._sessions.clear()


def test_ensure_session_db_row_stamps_cui_actor_context(tmp_path, monkeypatch):
    import json as _json

    import hermes_state

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    db = hermes_state.SessionDB()
    monkeypatch.setattr(server, "_db", db)
    monkeypatch.setattr(server, "_resolve_model", lambda *a, **k: "test-model")

    session = {
        "session_key": "owned-1",
        "model_override": None,
        "cui_actor_context": {"tenant_id": "example-tenant", "actor_id": "customer-1", "role": "user"},
        "explicit_cwd": False,
        "parent_session_id": None,
    }
    server._ensure_session_db_row(session)
    row = db.get_session("owned-1")
    cfg = _json.loads(row["model_config"]) if isinstance(row.get("model_config"), str) else row.get("model_config")
    assert cfg["_cui_actor_context"]["actor_id"] == "customer-1"
    assert cfg["_cui_tenant_id"] == "example-tenant"
    assert cfg["_cui_actor_role"] == "user"
    # And the persisted row is visible to its owner, not to others.
    owner = {"tenant_id": "example-tenant", "actor_id": "customer-1", "role": "user"}
    intruder = {"tenant_id": "example-tenant", "actor_id": "customer-2", "role": "user"}
    assert server._row_visible_to_cui_actor(row, owner) is True
    assert server._row_visible_to_cui_actor(row, intruder) is False


def test_apply_cui_actor_env_binds_contextvar_not_identity_env(monkeypatch):
    # Actor identity and the launch-time autonomy flag must both remain
    # process-stable; per-turn binding may mutate neither.
    monkeypatch.delenv("AIWERK_CUI_ACTOR_CONTEXT", raising=False)
    monkeypatch.delenv("AIWERK_CUI_TENANT_ID", raising=False)
    monkeypatch.delenv("AIWERK_CUI_ACTOR_ID", raising=False)
    monkeypatch.setenv("AIWERK_CUI_MANAGED_AUTONOMY", "1")
    monkeypatch.setenv("AIWERK_CUI_ACTOR_ROLE", "old-role")
    actor = {
        "tenant_id": "example-tenant",
        "actor_id": "aiwerk:operator:admin",
        "role": "admin",
        "display_name": "Operator",
        "ignored": "must-not-leak",
    }

    token = server._apply_cui_actor_env(actor)
    try:
        # Identity is readable via the canonical helper (contextvar-backed).
        ctx = server.current_cui_actor_context()
        assert ctx["actor_id"] == "aiwerk:operator:admin"
        assert ctx["role"] == "admin"
        assert ctx["display_name"] == "Operator"
        assert "ignored" not in ctx
        # Identity env vars are NOT mutated (no more process-global clobber).
        assert "AIWERK_CUI_ACTOR_CONTEXT" not in os.environ
        assert "AIWERK_CUI_TENANT_ID" not in os.environ
        assert "AIWERK_CUI_ACTOR_ID" not in os.environ
        assert os.environ["AIWERK_CUI_ACTOR_ROLE"] == "old-role"
        # The pre-existing launch flag is unchanged.
        assert os.environ["AIWERK_CUI_MANAGED_AUTONOMY"] == "1"
    finally:
        server._clear_cui_actor_env(token)

    # Restored cleanly: the contextvar binding is gone, so reads fall back to
    # os.environ — where the pre-existing role and launch flag remain.
    assert server.current_cui_actor_context() == {"role": "old-role"}
    assert os.environ["AIWERK_CUI_ACTOR_ROLE"] == "old-role"
    assert os.environ["AIWERK_CUI_MANAGED_AUTONOMY"] == "1"


def test_apply_cui_actor_env_empty_actor_is_noop():
    token = server._apply_cui_actor_env(None)
    assert token is None
    # Clearing a no-op token must be safe.
    server._clear_cui_actor_env(token)
    assert server.current_cui_actor_context() is None


def test_current_cui_actor_context_falls_back_to_os_environ(monkeypatch):
    # No contextvar bound (CLI/cron/subprocess path) -> read os.environ.
    monkeypatch.setenv(
        "AIWERK_CUI_ACTOR_CONTEXT",
        json.dumps({"tenant_id": "acme", "actor_id": "u1", "role": "user"}),
    )
    monkeypatch.delenv("AIWERK_CUI_TENANT_ID", raising=False)
    # The canonical helper (and server's wrapper) read the env fallback.
    assert cui_actor_context.current_cui_actor_context() == {
        "tenant_id": "acme",
        "actor_id": "u1",
        "role": "user",
    }
    assert server.current_cui_actor_context() == {
        "tenant_id": "acme",
        "actor_id": "u1",
        "role": "user",
    }


def test_contextvar_overrides_os_environ(monkeypatch):
    # A bound contextvar must win over a (stale/clobbered) os.environ value,
    # proving in-process turns are isolated from the process-global bridge.
    monkeypatch.setenv(
        "AIWERK_CUI_ACTOR_CONTEXT",
        json.dumps({"tenant_id": "env-tenant", "actor_id": "env-actor", "role": "user"}),
    )
    token = cui_actor_context.bind_cui_actor_context(
        {"tenant_id": "ctx-tenant", "actor_id": "ctx-actor", "role": "admin"}
    )
    try:
        assert cui_actor_context.current_cui_actor_context()["tenant_id"] == "ctx-tenant"
        assert cui_actor_context.current_cui_actor_context()["actor_id"] == "ctx-actor"
    finally:
        cui_actor_context.reset_cui_actor_context(token)
    # After reset, the env fallback is visible again.
    assert cui_actor_context.current_cui_actor_context()["tenant_id"] == "env-tenant"


def test_concurrent_threads_do_not_bleed_actor_context(monkeypatch):
    # Two threads bind two different actor contexts and read back concurrently.
    # With the old os.environ bridge, one thread's write clobbered the other's
    # (cross-customer bleed). The contextvar is isolated per thread, so each
    # thread must only ever observe its OWN actor.
    monkeypatch.delenv("AIWERK_CUI_ACTOR_CONTEXT", raising=False)

    barrier = threading.Barrier(2)
    results: dict[str, list[str]] = {"a": [], "b": []}
    errors: list[str] = []

    def worker(name: str, tenant: str) -> None:
        actor = {"tenant_id": tenant, "actor_id": f"{tenant}-actor", "role": "user"}
        token = server._apply_cui_actor_env(actor)
        try:
            barrier.wait(timeout=5)
            for _ in range(200):
                seen = server.current_cui_actor_context()
                if seen is None or seen.get("tenant_id") != tenant:
                    errors.append(f"{name} saw {seen!r}, expected tenant {tenant!r}")
                    break
                results[name].append(seen["tenant_id"])
        finally:
            server._clear_cui_actor_env(token)

    ta = threading.Thread(target=worker, args=("a", "tenant-a"))
    tb = threading.Thread(target=worker, args=("b", "tenant-b"))
    ta.start()
    tb.start()
    ta.join(timeout=10)
    tb.join(timeout=10)

    assert not errors, errors
    assert results["a"] and all(t == "tenant-a" for t in results["a"])
    assert results["b"] and all(t == "tenant-b" for t in results["b"])
    # No leftover binding on the main thread.
    assert server.current_cui_actor_context() is None
    # The shared (non-identifying) env flag may survive a racy concurrent
    # restore; clean it so it does not leak into later tests.
    os.environ.pop("AIWERK_CUI_MANAGED_AUTONOMY", None)


def test_row_visibility_customer_can_resume_linked_telegram_session(monkeypatch):
    cfg = {
        "dashboard": {
            "basic_auth": {
                "users": [
                    {
                        "actor_id": "example-tenant:customer:user",
                        "user_id": "Customer",
                        "tenant_id": "example-tenant",
                        "role": "user",
                        "telegram_user_ids": ["1461953838"],
                    }
                ]
            }
        }
    }
    monkeypatch.setattr(server, "_load_dashboard_user_config", lambda: cfg)
    cust = {
        "tenant_id": "example-tenant",
        "actor_id": "example-tenant:customer:user",
        "role": "user",
        "user_id": "Customer",
    }

    own = {"id": "own-tg", "source": "telegram", "user_id": "1461953838", "model_config": None}
    other = {"id": "other-tg", "source": "telegram", "user_id": "1392690488", "model_config": None}

    assert server._row_visible_to_cui_actor(own, cust) is True
    assert server._row_visible_to_cui_actor(other, cust) is False
