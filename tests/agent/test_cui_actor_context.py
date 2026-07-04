"""Tests for AIWerk CUI admin/customer actor memory separation."""

from __future__ import annotations

import json
from types import SimpleNamespace


def test_cui_actor_context_admin_banner_and_memory_suppression(monkeypatch):
    from agent.system_prompt import build_system_prompt_parts

    class Store:
        def format_for_system_prompt(self, target):
            return "USERBLOCK-CUSTOMER" if target == "user" else "MEMBLOCK"

    class Manager:
        def build_system_prompt(self):
            return "HONCHO-CUSTOMER-CONTEXT"

    monkeypatch.setenv(
        "AIWERK_CUI_ACTOR_CONTEXT",
        json.dumps(
            {
                "tenant_id": "example-tenant",
                "actor_id": "aiwerk:operator:admin",
                "role": "admin",
                "display_name": "Operator",
                "user_id": "Operator",
            }
        ),
    )
    agent = SimpleNamespace(
        load_soul_identity=False,
        skip_context_files=True,
        valid_tool_names=[],
        _task_completion_guidance=False,
        _tool_use_enforcement=False,
        provider="openai-codex",
        model="gpt-5.5",
        platform="cli",
        _environment_probe=False,
        pass_session_id=False,
        session_id="test",
        _memory_store=Store(),
        _memory_enabled=True,
        _user_profile_enabled=True,
        _memory_manager=Manager(),
    )

    volatile = build_system_prompt_parts(agent)["volatile"]

    assert "current_human='Operator'" in volatile
    assert "not the primary customer user" in volatile
    assert "USERBLOCK-CUSTOMER" not in volatile
    assert "HONCHO-CUSTOMER-CONTEXT" not in volatile


def test_cui_actor_context_customer_keeps_customer_memory(monkeypatch):
    from agent.system_prompt import build_system_prompt_parts

    class Store:
        def format_for_system_prompt(self, target):
            return "USERBLOCK-CUSTOMER" if target == "user" else "MEMBLOCK"

    class Manager:
        def build_system_prompt(self):
            return "HONCHO-CUSTOMER-CONTEXT"

    monkeypatch.setenv(
        "AIWERK_CUI_ACTOR_CONTEXT",
        json.dumps(
            {
                "tenant_id": "example-tenant",
                "actor_id": "example-tenant:customer:user",
                "role": "user",
                "display_name": "Customer User",
                "user_id": "Customer",
            }
        ),
    )
    agent = SimpleNamespace(
        load_soul_identity=False,
        skip_context_files=True,
        valid_tool_names=[],
        _task_completion_guidance=False,
        _tool_use_enforcement=False,
        provider="openai-codex",
        model="gpt-5.5",
        platform="cli",
        _environment_probe=False,
        pass_session_id=False,
        session_id="test",
        _memory_store=Store(),
        _memory_enabled=True,
        _user_profile_enabled=True,
        _memory_manager=Manager(),
    )

    volatile = build_system_prompt_parts(agent)["volatile"]

    assert "current_human='Customer User'" in volatile
    assert "authenticated customer/user" in volatile
    assert "USERBLOCK-CUSTOMER" in volatile
    assert "HONCHO-CUSTOMER-CONTEXT" in volatile


def test_cui_admin_memory_write_guard(monkeypatch):
    from agent.cui_actor_context import (
        current_cui_actor_context,
        is_aiwerk_admin_actor,
        memory_write_blocked_for_cui_admin,
    )

    monkeypatch.setenv(
        "AIWERK_CUI_ACTOR_CONTEXT",
        json.dumps(
            {
                "tenant_id": "example-tenant",
                "actor_id": "aiwerk:operator:admin",
                "role": "admin",
                "display_name": "Operator",
            }
        ),
    )

    assert current_cui_actor_context()["actor_id"] == "aiwerk:operator:admin"
    assert is_aiwerk_admin_actor()
    assert memory_write_blocked_for_cui_admin("memory", {"action": "add"})
    assert memory_write_blocked_for_cui_admin("memory", {"action": "replace"})
    assert memory_write_blocked_for_cui_admin("honcho_conclude", {"conclusion": "x"})
    assert memory_write_blocked_for_cui_admin("mem0_conclude", {"conclusion": "x"})
    assert memory_write_blocked_for_cui_admin("fact_store", {"action": "add"})
    assert not memory_write_blocked_for_cui_admin("honcho_search", {"query": "x"})


def test_cui_customer_memory_write_guard_allows_customer(monkeypatch):
    from agent.cui_actor_context import is_aiwerk_admin_actor, memory_write_blocked_for_cui_admin

    monkeypatch.setenv(
        "AIWERK_CUI_ACTOR_CONTEXT",
        json.dumps(
            {
                "tenant_id": "example-tenant",
                "actor_id": "example-tenant:customer:user",
                "role": "user",
                "display_name": "Customer User",
            }
        ),
    )

    assert not is_aiwerk_admin_actor()
    assert not memory_write_blocked_for_cui_admin("memory", {"action": "add"})
    assert not memory_write_blocked_for_cui_admin("honcho_conclude", {"conclusion": "x"})
