#!/usr/bin/env python3
"""Fail-closed MCP topology and real-call cutover smoke.

The private JSON plan names exactly one read-only tool call per enabled MCP
server. The report records no argument values or response bodies.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable


def load_plan(path: Path) -> dict[str, Any]:
    """Load an owner-private call plan without exposing its values."""
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode != 0o600:
        raise PermissionError("MCP smoke plan must have mode 0600")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) != {"calls"}:
        raise ValueError("MCP smoke plan must contain exactly one 'calls' object")
    if not isinstance(data["calls"], dict):
        raise ValueError("MCP smoke plan 'calls' must be an object")
    return data


def _server_sets(statuses: list[dict[str, Any]]) -> tuple[set[str], set[str]]:
    names = [entry.get("name") for entry in statuses]
    if any(not isinstance(name, str) or not name for name in names):
        raise RuntimeError("MCP status contained an invalid server name")
    if len(names) != len(set(names)):
        raise RuntimeError("MCP status contained duplicate server names")
    enabled = {
        entry["name"] for entry in statuses if entry.get("disabled") is not True
    }
    connected = {
        entry["name"]
        for entry in statuses
        if entry.get("disabled") is not True and entry.get("connected") is True
    }
    return enabled, connected


def _validated_calls(plan: dict[str, Any], enabled: set[str]) -> dict[str, dict[str, Any]]:
    calls = plan.get("calls")
    if not isinstance(calls, dict) or set(calls) != enabled:
        raise RuntimeError("call-plan servers do not equal enabled config servers")
    validated: dict[str, dict[str, Any]] = {}
    for server, call in calls.items():
        if not isinstance(call, dict) or set(call) != {"tool", "arguments"}:
            raise ValueError(f"call plan for {server!r} must contain tool and arguments")
        tool = call.get("tool")
        arguments = call.get("arguments")
        if not isinstance(tool, str) or not tool or not isinstance(arguments, dict):
            raise ValueError(f"invalid tool call plan for server {server!r}")
        validated[server] = {"tool": tool, "arguments": arguments}
    return validated


def _response_is_error(response: str) -> bool:
    try:
        parsed = json.loads(response)
    except (TypeError, json.JSONDecodeError):
        return False
    return isinstance(parsed, dict) and "error" in parsed


def run_smoke(
    plan: dict[str, Any],
    *,
    discover: Callable[[], list[str]],
    get_status: Callable[[], list[dict[str, Any]]],
    get_tool_names: Callable[[str], list[str]],
    get_entry: Callable[[str], Any],
) -> dict[str, Any]:
    """Discover, prove exact topology, and execute one call per server."""
    discover()
    statuses = get_status()
    if not isinstance(statuses, list):
        raise RuntimeError("MCP status returned an invalid result")
    enabled, connected = _server_sets(statuses)
    if not enabled:
        raise RuntimeError("no enabled MCP servers; refusing vacuous smoke PASS")
    if connected != enabled:
        raise RuntimeError("connected servers do not equal enabled config servers")
    calls = _validated_calls(plan, enabled)

    results: list[dict[str, Any]] = []
    for server in sorted(enabled):
        tool = calls[server]["tool"]
        arguments = calls[server]["arguments"]
        if tool not in set(get_tool_names(server)):
            raise RuntimeError(f"tool {tool!r} is not registered by server {server!r}")
        entry = get_entry(tool)
        handler = getattr(entry, "handler", None)
        if not callable(handler):
            raise RuntimeError(f"tool {tool!r} has no callable registry handler")
        response = handler(arguments)
        if not isinstance(response, str):
            raise RuntimeError(f"tool {tool!r} returned a non-text handler result")
        if _response_is_error(response):
            raise RuntimeError(f"tool {tool!r} reported an error")
        response_bytes = response.encode("utf-8")
        results.append(
            {
                "argument_keys": sorted(str(key) for key in arguments),
                "response_chars": len(response),
                "response_sha256": hashlib.sha256(response_bytes).hexdigest(),
                "server": server,
                "success": True,
                "tool": tool,
            }
        )

    enabled_after, connected_after = _server_sets(get_status())
    if enabled_after != enabled or connected_after != enabled:
        raise RuntimeError("MCP topology changed during real-call smoke")
    if len(results) != len(enabled):
        raise RuntimeError("real-call count does not equal enabled server count")

    transports = {
        entry["name"]: entry.get("transport")
        for entry in statuses
        if entry["name"] in enabled
    }
    return {
        "called_server_count": len(results),
        "calls": results,
        "connected_server_count": len(connected),
        "enabled_server_count": len(enabled),
        "enabled_servers": sorted(enabled),
        "status": "PASS",
        "transports": {name: transports[name] for name in sorted(transports)},
    }


def _write_json_private(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--json-out", required=True, type=Path)
    return parser


def _failure_report(exc: Exception) -> dict[str, Any]:
    """Build a useful FAIL report without exposing dynamic exception values."""
    safe_messages = {
        "MCP status contained an invalid server name",
        "MCP status contained duplicate server names",
        "MCP status returned an invalid result",
        "call-plan servers do not equal enabled config servers",
        "connected servers do not equal enabled config servers",
        "no enabled MCP servers; refusing vacuous smoke PASS",
        "MCP topology changed during real-call smoke",
        "real-call count does not equal enabled server count",
    }
    message = str(exc)
    if message not in safe_messages:
        message = "MCP smoke failed"
    return {
        "error": {
            "message": message,
            "type": type(exc).__name__,
        },
        "status": "FAIL",
    }


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    plan = load_plan(args.plan)

    from tools.mcp_tool_discovery import discover_mcp_tools, get_mcp_status
    from tools.mcp_tool_lifecycle import shutdown_mcp_servers
    from tools.registry import registry

    try:
        report = run_smoke(
            plan,
            discover=discover_mcp_tools,
            get_status=get_mcp_status,
            get_tool_names=lambda server: registry.get_tool_names_for_toolset(
                f"mcp-{server}"
            ),
            get_entry=registry.get_entry,
        )
        _write_json_private(args.json_out, report)
        print(
            "MCP smoke PASS: "
            f"{report['called_server_count']}/{report['enabled_server_count']} "
            "enabled servers completed a real tool call"
        )
        return 0
    except Exception as exc:
        _write_json_private(args.json_out, _failure_report(exc))
        print(f"MCP smoke FAIL ({type(exc).__name__})", file=sys.stderr)
        return 1
    finally:
        shutdown_mcp_servers()


if __name__ == "__main__":
    raise SystemExit(main())
