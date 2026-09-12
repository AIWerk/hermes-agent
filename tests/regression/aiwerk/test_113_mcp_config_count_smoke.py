"""Regression contract for the repository MCP cutover smoke."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "aiwerk" / "mcp_smoke.py"
_SPEC = importlib.util.spec_from_file_location("aiwerk_mcp_smoke", _SCRIPT)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError("Failed to load scripts/aiwerk/mcp_smoke.py")
_smoke = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_smoke)


def _statuses(*, bridge_connected: bool = True) -> list[dict]:
    return [
        {
            "name": "tenant_neo4j",
            "transport": "stdio",
            "connected": True,
            "disabled": False,
            "tools": 1,
        },
        {
            "name": "aiwerk_bridge",
            "transport": "http",
            "connected": bridge_connected,
            "disabled": False,
            "tools": 1 if bridge_connected else 0,
        },
        {
            "name": "disabled_server",
            "transport": "stdio",
            "connected": False,
            "disabled": True,
            "tools": 0,
        },
    ]


def _plan() -> dict:
    return {
        "calls": {
            "tenant_neo4j": {
                "tool": "mcp__tenant_neo4j__graph_summary",
                "arguments": {},
            },
            "aiwerk_bridge": {
                "tool": "mcp__aiwerk_bridge__mcp",
                "arguments": {"action": "status"},
            },
        }
    }


def test_smoke_requires_one_real_successful_call_for_each_enabled_server() -> None:
    calls: list[tuple[str, dict]] = []
    entries = {
        "mcp__tenant_neo4j__graph_summary": SimpleNamespace(
            handler=lambda args: calls.append(("tenant_neo4j", args)) or '{"nodes": 10}'
        ),
        "mcp__aiwerk_bridge__mcp": SimpleNamespace(
            handler=lambda args: calls.append(("aiwerk_bridge", args)) or '{"status": "ok"}'
        ),
    }

    report = _smoke.run_smoke(
        _plan(),
        discover=lambda: list(entries),
        get_status=lambda: _statuses(),
        get_tool_names=lambda server: {
            "tenant_neo4j": ["mcp__tenant_neo4j__graph_summary"],
            "aiwerk_bridge": ["mcp__aiwerk_bridge__mcp"],
        }[server],
        get_entry=entries.get,
    )

    assert report["status"] == "PASS"
    assert report["enabled_server_count"] == 2
    assert report["connected_server_count"] == 2
    assert report["called_server_count"] == 2
    assert report["enabled_servers"] == ["aiwerk_bridge", "tenant_neo4j"]
    assert calls == [
        ("aiwerk_bridge", {"action": "status"}),
        ("tenant_neo4j", {}),
    ]
    assert [call["server"] for call in report["calls"]] == [
        "aiwerk_bridge",
        "tenant_neo4j",
    ]
    assert all(call["success"] for call in report["calls"])
    assert report["calls"][0]["argument_keys"] == ["action"]
    assert "arguments" not in report["calls"][0]
    assert "response" not in report["calls"][0]
    assert len(report["calls"][0]["response_sha256"]) == 64


@pytest.mark.parametrize(
    ("plan", "statuses", "message"),
    [
        (
            {"calls": {"tenant_neo4j": _plan()["calls"]["tenant_neo4j"]}},
            _statuses(),
            "call-plan servers do not equal enabled config servers",
        ),
        (
            _plan(),
            _statuses(bridge_connected=False),
            "connected servers do not equal enabled config servers",
        ),
    ],
)
def test_smoke_fails_closed_on_count_or_server_set_mismatch(
    plan: dict, statuses: list[dict], message: str
) -> None:
    with pytest.raises(RuntimeError, match=message):
        _smoke.run_smoke(
            plan,
            discover=lambda: [],
            get_status=lambda: statuses,
            get_tool_names=lambda _server: [],
            get_entry=lambda _name: None,
        )


def test_smoke_rejects_empty_config_instead_of_vacuous_pass() -> None:
    with pytest.raises(RuntimeError, match="no enabled MCP servers"):
        _smoke.run_smoke(
            {"calls": {}},
            discover=lambda: [],
            get_status=lambda: [],
            get_tool_names=lambda _server: [],
            get_entry=lambda _name: None,
        )


def test_smoke_rejects_tool_not_registered_by_the_named_server() -> None:
    with pytest.raises(RuntimeError, match="not registered by server"):
        _smoke.run_smoke(
            _plan(),
            discover=lambda: [],
            get_status=lambda: _statuses(),
            get_tool_names=lambda _server: ["different_tool"],
            get_entry=lambda _name: None,
        )


def test_smoke_rejects_protocol_error_instead_of_counting_a_call() -> None:
    entries = {
        "mcp__tenant_neo4j__graph_summary": SimpleNamespace(
            handler=lambda _args: json.dumps({"error": "transport failed"})
        ),
        "mcp__aiwerk_bridge__mcp": SimpleNamespace(
            handler=lambda _args: '{"status": "ok"}'
        ),
    }
    with pytest.raises(RuntimeError, match="reported an error"):
        _smoke.run_smoke(
            _plan(),
            discover=lambda: list(entries),
            get_status=lambda: _statuses(),
            get_tool_names=lambda server: {
                "tenant_neo4j": ["mcp__tenant_neo4j__graph_summary"],
                "aiwerk_bridge": ["mcp__aiwerk_bridge__mcp"],
            }[server],
            get_entry=entries.get,
        )


def test_private_plan_file_is_required_and_values_are_not_reported(tmp_path: Path) -> None:
    plan_path = tmp_path / "mcp-plan.json"
    plan_path.write_text(json.dumps(_plan()), encoding="utf-8")
    plan_path.chmod(0o644)
    with pytest.raises(PermissionError, match="mode 0600"):
        _smoke.load_plan(plan_path)

    plan_path.chmod(0o600)
    assert _smoke.load_plan(plan_path) == _plan()


def test_fail_report_includes_exact_safe_exception_message_without_dynamic_values() -> None:
    report = _smoke._failure_report(
        RuntimeError("call-plan servers do not equal enabled config servers")
    )

    assert report == {
        "error": {
            "message": "call-plan servers do not equal enabled config servers",
            "type": "RuntimeError",
        },
        "status": "FAIL",
    }

    sensitive_value = "secret-token-from-handler"
    redacted = _smoke._failure_report(RuntimeError(f"handler failed: {sensitive_value}"))
    assert redacted["error"] == {
        "message": "MCP smoke failed",
        "type": "RuntimeError",
    }
    assert sensitive_value not in json.dumps(redacted)


def test_main_writes_safe_failure_report_without_dynamic_exception_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_path = tmp_path / "mcp-plan.json"
    plan_path.write_text(json.dumps(_plan()), encoding="utf-8")
    plan_path.chmod(0o600)
    report_path = tmp_path / "mcp-report.json"

    mcp_tool = ModuleType("tools.mcp_tool_discovery")
    mcp_lifecycle = ModuleType("tools.mcp_tool_lifecycle")
    setattr(mcp_tool, "discover_mcp_tools", lambda: [])
    setattr(mcp_tool, "get_mcp_status", lambda: _statuses(bridge_connected=False))
    setattr(mcp_lifecycle, "shutdown_mcp_servers", lambda: None)
    registry = SimpleNamespace(
        get_tool_names_for_toolset=lambda _toolset: [],
        get_entry=lambda _name: None,
    )
    registry_module = ModuleType("tools.registry")
    setattr(registry_module, "registry", registry)
    monkeypatch.setitem(sys.modules, "tools.mcp_tool_discovery", mcp_tool)
    monkeypatch.setitem(sys.modules, "tools.mcp_tool_lifecycle", mcp_lifecycle)
    monkeypatch.setitem(sys.modules, "tools.registry", registry_module)

    assert _smoke.main(["--plan", str(plan_path), "--json-out", str(report_path)]) == 1
    assert json.loads(report_path.read_text(encoding="utf-8")) == {
        "error": {
            "message": "connected servers do not equal enabled config servers",
            "type": "RuntimeError",
        },
        "status": "FAIL",
    }

    sensitive_value = "secret-token-from-handler"
    setattr(mcp_tool, "get_mcp_status", lambda: _statuses())
    registry.get_tool_names_for_toolset = lambda toolset: {
        "mcp-tenant_neo4j": ["mcp__tenant_neo4j__graph_summary"],
        "mcp-aiwerk_bridge": ["mcp__aiwerk_bridge__mcp"],
    }[toolset]
    registry.get_entry = lambda _name: SimpleNamespace(
        handler=lambda _args: (_ for _ in ()).throw(
            RuntimeError(f"handler failed: {sensitive_value}")
        )
    )

    assert _smoke.main(["--plan", str(plan_path), "--json-out", str(report_path)]) == 1
    redacted = report_path.read_text(encoding="utf-8")
    assert json.loads(redacted)["error"] == {
        "message": "MCP smoke failed",
        "type": "RuntimeError",
    }
    assert sensitive_value not in redacted
