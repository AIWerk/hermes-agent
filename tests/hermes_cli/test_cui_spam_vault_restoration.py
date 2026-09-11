"""Behavioral contracts for the bounded spam/vault restoration."""
import json

import pytest

from hermes_cli import config as config_owner
from hermes_cli import web_server as ws
import hermes_constants


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("AIWERK_CUI_VAULT_SUMMARY_JSON", raising=False)
    monkeypatch.setattr(config_owner, "load_env", lambda: {})
    monkeypatch.setattr(ws, "load_config", lambda: {})
    monkeypatch.setattr(ws.subprocess, "run", lambda *a, **k: pytest.fail("unexpected subprocess"))


def test_spam_filtered_from_both_panel_and_accounts():
    blocked = next(iter(ws._ASSISTANT_EMAIL_BLOCKED_SENDER_DOMAINS))
    normal = {"id": "normal", "sender": "Friend <friend@example.org>", "subject": "Meeting", "unread": True}
    spam = {"id": "spam", "sender": f"Bad <bad@{blocked}>", "subject": "Offer", "unread": True}
    source = {"status": "connected", "items": [spam, normal], "unread_count": 2}
    result = ws._merge_email_summaries([source])
    assert [item["id"] for item in result["items"]] == ["normal"]
    assert [item["id"] for item in result["accounts"][0]["items"]] == ["normal"]
    assert result["filtered_count"] == result["accounts"][0]["filtered_count"] == 1
    assert result["unread_count"] == result["accounts"][0]["unread_count"] == 1
    assert len(source["items"]) == 2


def test_vault_uses_bridge_health_aggregates(monkeypatch):
    calls = []
    def transport(config, *, server, tool, params):
        calls.append((server, tool, params))
        return {"result": {"content": [{"type": "text", "text": json.dumps({
            "status": "ok", "authenticated": True,
            "exposed_collection_visible": True, "agent_created_collection_visible": True,
            "items_in_exposed": 3, "items_in_agent_created": 2,
            "vault_url": "https://vault.example.test/api", "password": "never-expose-this",
        })}]}}
    monkeypatch.setattr(ws, "_call_aiwerk_bridge_tool", transport)
    result = ws._vaultwarden_summary({"mcp_servers": {"aiwerk_bridge": {"url": "https://bridge.example.test/mcp"}}})
    assert calls == [("vault", "health_check", {})]
    assert result["status"] == "connected"
    assert result["source"] == "aiwerk_bridge"
    assert result["item_count"] == 5
    assert result["exposed_count"] == 3
    assert result["agent_created_count"] == 2
    assert "never-expose-this" not in json.dumps(result)


def test_vault_no_config_does_not_call_transport(monkeypatch):
    monkeypatch.setattr(ws, "_call_aiwerk_bridge_tool", lambda *a, **k: pytest.fail("unexpected bridge"))
    assert ws._vaultwarden_summary({})["status"] == "not_configured"


def test_vault_env_override_precedes_bridge(monkeypatch):
    payload = {"status": "connected", "item_count": 7}
    monkeypatch.setenv("AIWERK_CUI_VAULT_SUMMARY_JSON", json.dumps(payload))
    monkeypatch.setattr(ws, "_call_aiwerk_bridge_tool", lambda *a, **k: pytest.fail("unexpected bridge"))
    assert ws._vaultwarden_summary({}) == payload


def test_vault_transport_error_does_not_expose_secret(monkeypatch):
    calls = []
    def fail(*args, **kwargs):
        calls.append(True)
        raise RuntimeError("Bearer secret-value")
    monkeypatch.setattr(ws, "_call_aiwerk_bridge_tool", fail)
    result = ws._vaultwarden_summary({"mcp_servers": {"aiwerk_bridge": {"url": "https://bridge.example.test/mcp"}}})
    assert calls == [True]
    assert "secret-value" not in json.dumps(result)
    assert result["status"] != "connected"


def test_upload_root_resolves_defining_owner(monkeypatch, tmp_path):
    target = tmp_path / "owner-home"
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: target)
    assert ws._assistant_upload_root().is_relative_to(target)


@pytest.mark.parametrize("config", [
    {"vault_url": "https://vault.example.test"},
    {"vault": {"url": "https://vault.example.test"}},
    {"dashboard": {"vault": {"url": "https://vault.example.test"}}},
    {"assistant": {"vault": {"vault_url": "https://vault.example.test"}}},
])
def test_vault_local_fallback_uses_only_fake_process(monkeypatch, config):
    from types import SimpleNamespace
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name: "/fake/bw")
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        payload = {"status": "unlocked"} if command[1] == "status" else [{"login": {"password": "private-password"}}]
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
    monkeypatch.setattr(ws.subprocess, "run", run)
    result = ws._vaultwarden_summary(config)
    assert result["item_count"] == 1
    assert result["vault_url"] == "https://vault.example.test"
    assert calls == [["/fake/bw", "status"], ["/fake/bw", "list", "items"]]
    assert "private-password" not in json.dumps(result)
