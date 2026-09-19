"""Combined tenant-admin support plugin contract (initial RED)."""
from __future__ import annotations

import inspect
import json
import os
from pathlib import Path

import pytest
import yaml

from tests.profile_authorization_support import ADMIN, EMPLOYEE, capability, install_policy, seed_store


def _discover(tmp_path, monkeypatch):
    capability("hermes_cli.dashboard_auth.behavior_policy")
    plugin_dir = Path(__file__).resolve().parents[2] / "plugins" / "tenant_admin_support"
    if not (plugin_dir / "plugin.yaml").is_file() or not (plugin_dir / "__init__.py").is_file():
        pytest.fail("Missing authorization capability: plugins.tenant_admin_support")
    original_hermes_home = os.environ.get("HERMES_HOME")
    home = tmp_path / "plugin-home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    (home / "config.yaml").write_text(yaml.safe_dump({"plugins": {"enabled": ["tenant-admin-support"]}}))
    from hermes_cli.plugins import PluginManager
    manager = PluginManager()
    manager.discover_and_load()
    if original_hermes_home is None:
        monkeypatch.delenv("HERMES_HOME", raising=False)
    else:
        monkeypatch.setenv("HERMES_HOME", original_hermes_home)
    loaded = manager._plugins.get("tenant-admin-support") or manager._plugins.get("tenant_admin_support")
    assert loaded is not None and loaded.enabled and loaded.error is None
    from tools.registry import registry
    entry = registry.get_entry("tenant_admin_support", scope=manager.scope_key)
    assert entry is not None
    return manager, entry


def _dispatch(manager, operation, target_profile="employee-home", **kwargs):
    from agent.cui_actor_context import bind_cui_actor_context, reset_cui_actor_context
    from tools.registry import registry
    token = bind_cui_actor_context(ADMIN)
    try:
        raw = registry.dispatch("tenant_admin_support", {
            "operation": operation, "target_profile": target_profile, **kwargs,
        }, scope=manager.scope_key)
        return json.loads(raw) if isinstance(raw, str) else raw
    finally:
        reset_cui_actor_context(token)


def _dispatch_with_test_approval(manager, operation, target_profile="employee-home", **kwargs):
    plugin = manager._plugins.get("tenant-admin-support") or manager._plugins["tenant_admin_support"]
    raw = plugin.module.handle(
        {"operation": operation, "target_profile": target_profile, **kwargs},
        approval_fn=lambda **_metadata: "accept",
        actor=ADMIN,
    )
    return json.loads(raw) if isinstance(raw, str) else raw


def test_desktop_admin_support_tool_is_service_gated_and_registry_dispatchable(tmp_path, monkeypatch):
    manager, entry = _discover(tmp_path, monkeypatch)
    from toolsets import _HERMES_CORE_TOOLS
    from tools.registry import registry
    assert entry.toolset == "desktop_ui"
    assert entry.check_fn is not None
    assert "tenant_admin_support" not in _HERMES_CORE_TOOLS
    assert entry.toolset == "desktop_ui"
    result = _dispatch(manager, "behavior_history", limit=1)
    assert isinstance(result, dict)


def test_admin_session_list_search_read_use_delegated_broker_and_paginate(tmp_path, monkeypatch):
    env = install_policy(tmp_path, monkeypatch)
    _db, _home = seed_store(env)
    manager, _entry = _discover(tmp_path, monkeypatch)
    listed = _dispatch(manager, "session_list", limit=1, offset=0)
    searched = _dispatch(manager, "session_search", query="synthetic", limit=1, offset=0)
    read = _dispatch(manager, "session_read", session_id="owned", limit=1, offset=0)
    assert listed["sessions"][0]["id"] == "owned"
    assert searched["sessions"][0]["id"] == "owned"
    assert read["messages"][0]["content"] == "synthetic hello"
    assert all(result.get("continuation") is not None for result in (listed, searched, read))
    assert "system_prompt" not in json.dumps((listed, searched, read))


def test_admin_support_session_ops_succeed_with_wildcard_delegated_read_but_direct_session_tools_still_deny(tmp_path, monkeypatch):
    env = install_policy(tmp_path, monkeypatch)
    seed_store(env)
    manager, _entry = _discover(tmp_path, monkeypatch)
    assert _dispatch(manager, "session_read", session_id="owned")["messages"]
    access = capability("hermes_cli.dashboard_auth.profile_access")
    for action in ("session.read", "session.resume", "session.mutate"):
        with pytest.raises(access.ProfileAccessDenied, match="^profile access denied$"):
            access.authorize(ADMIN, action, "employee-home")


def test_employee_and_cross_tenant_support_reads_are_generic_denials(tmp_path, monkeypatch):
    install_policy(tmp_path, monkeypatch)
    manager, _entry = _discover(tmp_path, monkeypatch)
    from agent.cui_actor_context import bind_cui_actor_context, reset_cui_actor_context
    messages = []
    for actor, target in ((EMPLOYEE, "peer-home"), (ADMIN, "other-home")):
        token = bind_cui_actor_context(actor)
        try:
            from tools.registry import registry
            raw = registry.dispatch("tenant_admin_support", {
                "operation": "session_list", "target_profile": target,
            }, scope=manager.scope_key)
            result = json.loads(raw) if isinstance(raw, str) else raw
        finally:
            reset_cui_actor_context(token)
        messages.append(result["error"])
    assert messages == ["profile access denied", "profile access denied"]


def test_support_read_never_constructs_employee_agent_memory_soul_config_or_credentials(tmp_path, monkeypatch):
    env = install_policy(tmp_path, monkeypatch)
    seed_store(env)
    manager, _entry = _discover(tmp_path, monkeypatch)
    def forbidden(*_a, **_k):
        raise AssertionError("support read touched forbidden employee runtime state")
    monkeypatch.setattr("run_agent.AIAgent", forbidden)
    monkeypatch.setattr("agent.memory_manager.MemoryManager", forbidden)
    monkeypatch.setattr("hermes_cli.config.load_config", forbidden)
    result = _dispatch(manager, "session_read", session_id="owned")
    assert result["messages"][0]["content"] == "synthetic hello"


def test_transcript_is_transient_and_admin_memory_write_remains_blocked(tmp_path, monkeypatch):
    env = install_policy(tmp_path, monkeypatch)
    seed_store(env)
    def memory_snapshot():
        return {
            path.relative_to(env.home): path.read_bytes()
            for path in env.home.rglob("*")
            if path.is_file() and (
                "memories" in path.parts or path.name in {"MEMORY.md", "USER.md"}
            )
        }
    before = memory_snapshot()
    manager, _entry = _discover(tmp_path, monkeypatch)
    assert _dispatch(manager, "session_read", session_id="owned")["messages"]
    after = memory_snapshot()
    assert after == before
    audit_path = env.root / "logs" / "dashboard-auth.log"
    assert audit_path.is_file() and "delegated_session_read" in audit_path.read_text()
    from agent.cui_actor_context import bind_cui_actor_context, cui_admin_memory_block_result, memory_write_blocked_for_cui_admin, reset_cui_actor_context
    token = bind_cui_actor_context(ADMIN)
    try:
        assert memory_write_blocked_for_cui_admin("memory", {"action": "add", "content": "synthetic"})
        assert "cui_admin_actor_memory_guard" in cui_admin_memory_block_result("memory")
    finally:
        reset_cui_actor_context(token)


def test_tool_schema_routes_facts_documents_and_code_defects_away_from_behavior_policy(tmp_path, monkeypatch):
    _manager, entry = _discover(tmp_path, monkeypatch)
    schema_text = json.dumps(entry.schema).lower()
    for required in ("behavior", "facts", "documents", "code", "implementation", "separately authorized"):
        assert required in schema_text
    assert entry.schema["parameters"]["properties"]["target_profile"].get("default") is None
    assert set(entry.schema["parameters"]["required"]) >= {"operation", "target_profile"}


def test_ordinary_admin_prompt_without_tool_apply_does_not_write_policy(tmp_path, monkeypatch):
    env = install_policy(tmp_path, monkeypatch)
    seed_store(env)
    manager, _entry = _discover(tmp_path, monkeypatch)
    bp = capability("hermes_cli.dashboard_auth.behavior_policy")
    root = tmp_path / "central"
    monkeypatch.setattr(bp, "BEHAVIOR_POLICY_ROOT", str(root))
    _dispatch(manager, "session_read", session_id="owned")
    assert not root.exists()


def test_admin_behavior_preview_returns_diff_digest_token_and_performs_no_write(tmp_path, monkeypatch):
    install_policy(tmp_path, monkeypatch)
    manager, _entry = _discover(tmp_path, monkeypatch)
    bp = capability("hermes_cli.dashboard_auth.behavior_policy")
    root = tmp_path / "central"
    monkeypatch.setattr(bp, "BEHAVIOR_POLICY_ROOT", str(root))
    result = _dispatch(manager, "behavior_preview", instructions="Ask one clarifying question.")
    assert {"current_revision", "proposed_revision", "proposed_text", "diff", "digest", "byte_count", "preview_token"} <= set(result)
    assert result["proposed_text"] == "Ask one clarifying question."
    assert not root.exists() or not any(root.rglob("current.json"))


def test_preview_token_is_actor_tenant_target_content_revision_bound_and_expires(tmp_path, monkeypatch):
    install_policy(tmp_path, monkeypatch)
    manager, _entry = _discover(tmp_path, monkeypatch)
    preview = _dispatch(manager, "behavior_preview", instructions="Be concise.")
    token = preview["preview_token"]
    for changed in ({**ADMIN, "actor_id": "other"}, {**ADMIN, "tenant_id": "tenant-b"}):
        from agent.cui_actor_context import bind_cui_actor_context, reset_cui_actor_context
        bound = bind_cui_actor_context(changed)
        try:
            from tools.registry import registry
            raw = registry.dispatch("tenant_admin_support", {
                "operation": "behavior_apply", "target_profile": "employee-home", "preview_token": token,
            }, scope=manager.scope_key)
            denied = json.loads(raw) if isinstance(raw, str) else raw
        finally:
            reset_cui_actor_context(bound)
        assert denied["error"] == "profile access denied"
    plugin = manager._plugins.get("tenant-admin-support") or manager._plugins["tenant_admin_support"]
    plugin.module.expire_previews_for_test(601)
    assert _dispatch(manager, "behavior_apply", preview_token=token)["error"] == "preview expired"


def test_apply_cannot_accept_policy_text_or_skip_preview(tmp_path, monkeypatch):
    install_policy(tmp_path, monkeypatch)
    manager, entry = _discover(tmp_path, monkeypatch)
    apply_schema = entry.schema["parameters"]
    assert "instructions" not in json.dumps(apply_schema.get("dependentSchemas", {}).get("behavior_apply", {}))
    for args in ({"instructions": "write me"}, {"preview_token": "unknown"}):
        result = _dispatch(manager, "behavior_apply", **args)
        assert result.get("error") and result.get("revision") is None


def test_apply_rejects_raw_instructions_even_with_valid_preview_token(tmp_path, monkeypatch):
    install_policy(tmp_path, monkeypatch)
    manager, _entry = _discover(tmp_path, monkeypatch)
    preview = _dispatch(manager, "behavior_preview", instructions="preview-bound")
    plugin = manager._plugins.get("tenant-admin-support") or manager._plugins["tenant_admin_support"]
    approvals = []
    raw = plugin.module.handle(
        {
            "operation": "behavior_apply",
            "target_profile": "employee-home",
            "preview_token": preview["preview_token"],
            "instructions": "RAW-TEXT-SUPPLIED-AT-APPLY",
        },
        approval_fn=lambda **metadata: approvals.append(metadata) or "accept",
        actor=ADMIN,
    )
    result = json.loads(raw)
    assert result["error"] == "invalid request"
    assert approvals == []


def test_revocation_during_approval_wait_denies_after_fresh_reauthorization(tmp_path, monkeypatch):
    env = install_policy(tmp_path, monkeypatch)
    manager, _entry = _discover(tmp_path, monkeypatch)
    preview = _dispatch(manager, "behavior_preview", instructions="Be concise.")
    plugin = manager._plugins.get("tenant-admin-support") or manager._plugins["tenant_admin_support"]
    def approve(*_a, **_k):
        env.revoke(actor="admin", action="behavior.write")
        return "accept"
    result = plugin.module.handle({"operation": "behavior_apply", "target_profile": "employee-home",
                                   "preview_token": preview["preview_token"]}, approval_fn=approve, actor=ADMIN)
    assert json.loads(result)["error"] == "profile access denied"


def test_competing_apply_after_preview_returns_cas_conflict_and_preserves_winner(tmp_path, monkeypatch):
    install_policy(tmp_path, monkeypatch)
    manager, _entry = _discover(tmp_path, monkeypatch)
    first = _dispatch(manager, "behavior_preview", instructions="first")
    second = _dispatch(manager, "behavior_preview", instructions="second")
    assert _dispatch_with_test_approval(manager, "behavior_apply", preview_token=first["preview_token"])["revision"] == 1
    loser = _dispatch_with_test_approval(manager, "behavior_apply", preview_token=second["preview_token"])
    assert loser["error"] == "behavior policy conflict"
    assert _dispatch(manager, "behavior_get")["instructions"] == "first"


def test_apply_readback_reports_success_only_for_exact_committed_revision_digest(tmp_path, monkeypatch):
    install_policy(tmp_path, monkeypatch)
    manager, _entry = _discover(tmp_path, monkeypatch)
    preview = _dispatch(manager, "behavior_preview", instructions="exact bytes")
    result = _dispatch_with_test_approval(manager, "behavior_apply", preview_token=preview["preview_token"])
    current = _dispatch(manager, "behavior_get")
    assert (result["revision"], result["digest"]) == (current["revision"], current["content_digest"])
    assert result["effective"] == "new_sessions_only"


def test_successful_rollback_emits_sanitized_behavior_audit(tmp_path, monkeypatch):
    install_policy(tmp_path, monkeypatch)
    manager, _entry = _discover(tmp_path, monkeypatch)
    bp = capability("hermes_cli.dashboard_auth.behavior_policy")
    first = _dispatch(manager, "behavior_preview", instructions="first")
    committed_first = _dispatch_with_test_approval(
        manager, "behavior_apply", preview_token=first["preview_token"]
    )
    second = _dispatch(manager, "behavior_preview", instructions="second")
    committed_second = _dispatch_with_test_approval(
        manager, "behavior_apply", preview_token=second["preview_token"]
    )
    records = []
    monkeypatch.setattr(bp, "audit_behavior_event", lambda **fields: records.append(fields) or fields)
    plugin = manager._plugins.get("tenant-admin-support") or manager._plugins["tenant_admin_support"]
    raw = plugin.module.handle(
        {
            "operation": "behavior_rollback",
            "target_profile": "employee-home",
            "revision": committed_first["revision"],
            "expected_revision": committed_second["revision"],
        },
        approval_fn=lambda **_metadata: "accept",
        actor=ADMIN,
    )
    result = json.loads(raw)
    assert result["rollback_of"] == committed_first["revision"]
    assert len(records) == 1
    assert records[0]["operation"] == "rollback" and records[0]["result"] == "success"
    assert "instructions" not in records[0]
