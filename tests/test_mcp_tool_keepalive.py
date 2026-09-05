"""Tests for MCP transport keepalive behavior."""

import pytest


@pytest.mark.asyncio
async def test_mcp_keepalive_prefers_ping_over_tools_list(monkeypatch):
    import tools.mcp_tool as mcp_tool
    from tools.mcp_tool import MCPServerTask

    monkeypatch.setattr(mcp_tool, "_MIN_KEEPALIVE_INTERVAL", 0.001)
    task = MCPServerTask("remote")
    task._config = {"keepalive_interval": 0.001}

    class _Session:
        def __init__(self):
            self.pings = 0
            self.list_tools_calls = 0

        async def send_ping(self):
            self.pings += 1
            task._shutdown_event.set()

        async def list_tools(self):
            self.list_tools_calls += 1
            task._shutdown_event.set()

    session = _Session()
    task.session = session

    reason = await task._wait_for_lifecycle_event()

    assert reason == "shutdown"
    assert session.pings == 1
    assert session.list_tools_calls == 0


@pytest.mark.asyncio
async def test_mcp_keepalive_zero_is_floored_not_disabled(monkeypatch):
    import tools.mcp_tool as mcp_tool
    from tools.mcp_tool import MCPServerTask

    monkeypatch.setattr(mcp_tool, "_MIN_KEEPALIVE_INTERVAL", 0.001)
    task = MCPServerTask("remote")
    task._config = {"keepalive_interval": 0}

    class _Session:
        def __init__(self):
            self.pings = 0

        async def send_ping(self):
            self.pings += 1
            task._shutdown_event.set()

    session = _Session()
    task.session = session

    reason = await task._wait_for_lifecycle_event()

    assert reason == "shutdown"
    assert session.pings == 1
