"""Tests for built-in memory write bridge to external providers."""

from run_agent import AIAgent


def test_memory_write_bridge_skips_router_blocked_tool_result():
    result = (
        '{"success": false, '
        '"error": "Memory router blocked this write from prompt-injected memory."}'
    )

    assert AIAgent._memory_write_succeeded(result) is False


def test_memory_write_bridge_allows_successful_tool_result():
    result = '{"success": true, "target": "user"}'

    assert AIAgent._memory_write_succeeded(result) is True


def test_memory_write_bridge_skips_malformed_tool_result():
    assert AIAgent._memory_write_succeeded("not json") is False
