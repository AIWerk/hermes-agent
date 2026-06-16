from __future__ import annotations

from model_tools import get_tool_definitions
from tools.registry import registry


def _set_operator_check(monkeypatch, value: bool) -> None:
    entry = registry.get_entry("verify_operator_identity")
    assert entry is not None
    monkeypatch.setattr(entry, "check_fn", lambda: value)


def test_verify_operator_identity_hidden_when_not_configured(monkeypatch):
    _set_operator_check(monkeypatch, False)

    names = {tool["function"]["name"] for tool in get_tool_definitions(enabled_toolsets=None, disabled_toolsets=None, quiet_mode=True)}

    assert "verify_operator_identity" not in names


def test_verify_operator_identity_available_when_configured(monkeypatch):
    _set_operator_check(monkeypatch, True)

    names = {tool["function"]["name"] for tool in get_tool_definitions(enabled_toolsets=None, disabled_toolsets=None, quiet_mode=True)}

    assert "verify_operator_identity" in names
