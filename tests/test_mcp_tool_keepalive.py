"""Tests for MCP transport keepalive behavior."""

import pytest


@pytest.mark.asyncio
async def test_mcp_keepalive_prefers_ping_over_tools_list():
    from tools.mcp_tool import MCPServerTask

    task = MCPServerTask("remote")
    task._config = {"keepalive_interval_seconds": 0.001}

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
async def test_mcp_keepalive_can_be_disabled():
    from tools.mcp_tool import MCPServerTask

    task = MCPServerTask("remote")
    task._config = {"keepalive_interval_seconds": 0}

    class _Session:
        async def send_ping(self):
            raise AssertionError("keepalive should be disabled")

        async def list_tools(self):
            raise AssertionError("keepalive should be disabled")

    task.session = _Session()
    task._shutdown_event.set()

    reason = await task._wait_for_lifecycle_event()

    assert reason == "shutdown"
