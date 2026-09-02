"""Regression tests for the restored hosted AIWerk MCP bridge backend."""

from __future__ import annotations

import json
import threading
import time
from email.message import Message

import pytest

import hermes_cli.web_server as web_server


_REQUIRED_BRIDGE_SYMBOLS = {
    "_MCP_BRIDGE_SESSIONS",
    "_mcp_bridge_config",
    "_mcp_bridge_session_key",
    "_mcp_bridge_next_request_id",
    "_mcp_bridge_rpc",
    "_mcp_bridge_initialize",
    "_mcp_bridge_session",
    "_mcp_bridge_forget_session",
    "_mcp_bridge_forget_all_sessions",
    "_mcp_bridge_router_call",
    "_call_aiwerk_bridge_tool",
    "_bridge_error_status",
    "_parse_gmail_bridge_date",
    "_gmail_bridge_metadata_to_items",
    "_parse_gmail_bridge_metadata_blocks",
    "_gmail_bridge_search_message_ids",
    "_gmail_bridge_metadata_items_for_ids",
    "_gmail_bridge_message_items",
    "_aiwerk_bridge_subserver_label",
    "_aiwerk_bridge_subserver_description",
    "_aiwerk_bridge_catalog_slug",
    "_normalize_aiwerk_bridge_subserver_status",
    "_aiwerk_bridge_subserver_item",
    "_aiwerk_bridge_live_subservers",
    "_aiwerk_bridge_subservers",
    "_vault_bridge_summary",
    "_assistant_resources_payload",
    "get_assistant_resources",
}


def test_hosted_aiwerk_bridge_backend_surface_is_present():
    missing = sorted(name for name in _REQUIRED_BRIDGE_SYMBOLS if not hasattr(web_server, name))
    assert missing == []
    assert any(
        getattr(route, "path", None) == "/api/assistant/resources"
        for route in web_server.app.routes
    )
    assert web_server._assistant_api_allowed(
        "/api/assistant/resources", "GET"
    ) is True
    assert web_server._assistant_api_allowed(
        "/api/assistant/attachments/resource", "POST"
    ) is True


def test_mcp_bridge_rpc_accepts_streamable_http_sse(monkeypatch):
    payload = {"jsonrpc": "2.0", "id": 7, "result": {"ok": True}}
    raw = f"event: message\ndata: {json.dumps(payload)}\n\n".encode()
    headers = Message()
    headers["Content-Type"] = "text/event-stream"
    headers["MCP-Session-Id"] = "session-sse"

    class FakeResponse:
        def __init__(self):
            self.headers = headers

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return raw

    response = FakeResponse()
    monkeypatch.setattr(web_server.urllib.request, "urlopen", lambda *_a, **_k: response)

    result, session_id = web_server._mcp_bridge_rpc(
        {"mcp_servers": {"aiwerk_bridge": {"url": "https://bridge.example.test/mcp"}}},
        "tools/call",
        {"name": "mcp", "arguments": {}},
        request_id=7,
    )

    assert result == payload
    assert session_id == "session-sse"


def test_mcp_bridge_sse_requires_exact_request_id_match():
    raw = (
        'data: {"jsonrpc":"2.0","id":7,'
        '"result":{"foreign":"secret"}}\n\n'
    )

    with pytest.raises(RuntimeError, match="request id"):
        web_server._parse_mcp_bridge_response(
            raw, content_type="text/event-stream", request_id=8
        )


def test_mcp_bridge_json_requires_exact_request_id_match():
    for raw in (
        '{"jsonrpc":"2.0","id":7,"result":{"foreign":"secret"}}',
        '{"jsonrpc":"2.0","result":{"missing":"id"}}',
        '{"jsonrpc":"2.0","id":true,"result":{"wrong":"type"}}',
    ):
        with pytest.raises(RuntimeError, match="request id"):
            web_server._parse_mcp_bridge_response(
                raw, content_type="application/json", request_id=8
            )


def test_mcp_bridge_initialize_completes_initialized_notification(monkeypatch):
    calls = []

    def fake_rpc(_config, method, params, *, session_id=None, request_id=1):
        calls.append((method, params, session_id, request_id))
        if method == "initialize":
            return {"result": {"protocolVersion": "2025-03-26"}}, "session-123"
        return {}, session_id

    monkeypatch.setattr(web_server, "_mcp_bridge_rpc", fake_rpc)

    session_id = web_server._mcp_bridge_initialize(
        {"mcp_servers": {"aiwerk_bridge": {"url": "https://bridge.example.test/mcp"}}},
        session_key="scope:key",
    )

    assert session_id == "session-123"
    assert [call[0] for call in calls] == ["initialize", "notifications/initialized"]
    assert calls[1][2] == "session-123"
    assert calls[1][3] is None


def test_aiwerk_bridge_reuses_session_across_cui_tool_calls(monkeypatch):
    calls = []

    def fake_rpc(_config, method, params, *, session_id=None, request_id=1):
        calls.append(
            {
                "method": method,
                "session_id": session_id,
                "request_id": request_id,
                "params": params,
            }
        )
        if method == "initialize":
            return {"result": {}}, "session-123"
        return {
            "result": {"content": [{"text": json.dumps({"ok": True})}]}
        }, session_id

    config = {
        "mcp_servers": {
            "aiwerk_bridge": {"url": "https://bridge.example.test/u/demo/mcp"}
        }
    }
    monkeypatch.setattr(web_server, "_mcp_bridge_rpc", fake_rpc)

    first = web_server._call_aiwerk_bridge_tool(
        config,
        server="google-workspace-aiwerk",
        tool="search_gmail_messages",
        params={},
    )
    second = web_server._call_aiwerk_bridge_tool(
        config,
        server="google-workspace-aiwerk",
        tool="get_gmail_messages_content_batch",
        params={},
    )

    assert first == {"ok": True}
    assert second == {"ok": True}
    assert [call["method"] for call in calls] == [
        "initialize",
        "notifications/initialized",
        "tools/call",
        "tools/call",
    ]
    assert calls[1]["session_id"] == "session-123"
    assert calls[1]["request_id"] is None
    assert calls[2]["session_id"] == "session-123"
    assert calls[3]["session_id"] == "session-123"
    assert [call["request_id"] for call in calls] == [1, None, 2, 3]


def test_aiwerk_bridge_config_expands_header_env_refs(monkeypatch):
    monkeypatch.delenv("AIWERK_BRIDGE_MCP_TOKEN", raising=False)
    monkeypatch.setattr(
        web_server,
        "load_env",
        lambda: {"AIWERK_BRIDGE_MCP_TOKEN": "test-token"},
    )

    bridge = web_server._mcp_bridge_config(
        {
            "mcp_servers": {
                "aiwerk_bridge": {
                    "url": "https://bridge.example.test/${AIWERK_BRIDGE_MCP_TOKEN}/mcp",
                    "headers": {
                        "Authorization": "Bearer ${AIWERK_BRIDGE_MCP_TOKEN}"
                    },
                }
            }
        }
    )

    assert bridge["url"] == "https://bridge.example.test/test-token/mcp"
    assert bridge["headers"]["Authorization"] == "Bearer test-token"


def test_bridge_session_and_resource_cache_keys_are_actor_scoped_and_secret_free():
    config = {
        "mcp_servers": {
            "aiwerk_bridge": {
                "url": "https://bridge.example.test/mcp",
                "headers": {"Authorization": "Bearer secret-test-value"},
            }
        }
    }

    token = web_server._current_http_cui_actor.set(
        {"tenant_id": "tenant-a", "actor_id": "user-1", "role": "user"}
    )
    try:
        session_a = web_server._mcp_bridge_session_key(config)
        cache_a = web_server._assistant_resource_config_signature(config, None)
    finally:
        web_server._current_http_cui_actor.reset(token)

    token = web_server._current_http_cui_actor.set(
        {"tenant_id": "tenant-b", "actor_id": "user-2", "role": "user"}
    )
    try:
        session_b = web_server._mcp_bridge_session_key(config)
        cache_b = web_server._assistant_resource_config_signature(config, None)
    finally:
        web_server._current_http_cui_actor.reset(token)

    assert session_a != session_b
    assert cache_a != cache_b
    for value in (session_a, session_b, cache_a, cache_b):
        assert "secret-test-value" not in value
        assert "Authorization" not in value


def test_bridge_rejects_mutation_tool_before_transport(monkeypatch):
    transport_calls = []
    monkeypatch.setattr(
        web_server,
        "_mcp_bridge_router_call",
        lambda *_args, **_kwargs: transport_calls.append((_args, _kwargs)),
    )

    with pytest.raises(ValueError, match="read-only"):
        web_server._call_aiwerk_bridge_tool(
            {"mcp_servers": {"aiwerk_bridge": {"url": "https://bridge.example.test/mcp"}}},
            server="google-workspace-aiwerk",
            tool="delete_gmail_message",
            params={"message_id": "m-1"},
        )

    assert transport_calls == []


def _fake_resource_payload(name: str) -> dict:
    payload = {"status": "connected", "summary": name, "items": []}
    if name in {"email", "calendar"}:
        payload["accounts"] = []
    if name == "email":
        payload["unread_count"] = 0
    if name == "shared_folder":
        payload["can_open_folder"] = False
    if name == "vault":
        payload.update(
            {"weak_count": 0, "reused_count": 0, "compromised_count": 0}
        )
    if name == "todos":
        payload["open_count"] = 0
    if name == "contacts":
        payload.update({"relevant": [], "frequent": [], "total_count": 0})
    return payload


def _stub_cached_resources(monkeypatch):
    def fake_cached_resource(name, ttl_seconds, cache_key, builder, **kwargs):
        return _fake_resource_payload(name), {
            "cached": False,
            "updated_at": "now",
            "expires_at": "later",
            "ttl_seconds": ttl_seconds,
        }

    monkeypatch.setattr(web_server, "_assistant_cached_resource", fake_cached_resource)


def test_bridge_resource_refresh_invalidates_only_requesting_actor(monkeypatch):
    config = {
        "mcp_servers": {
            "aiwerk_bridge": {"url": "https://bridge.example.test/mcp"}
        }
    }
    monkeypatch.setattr(web_server, "load_config", lambda: config)
    _stub_cached_resources(monkeypatch)
    web_server._MCP_BRIDGE_SESSIONS.clear()
    web_server._MCP_BRIDGE_REQUEST_IDS.clear()

    token = web_server._current_http_cui_actor.set(
        {"tenant_id": "tenant-a", "actor_id": "user-1", "role": "user"}
    )
    try:
        key_a = web_server._mcp_bridge_session_key(config)
    finally:
        web_server._current_http_cui_actor.reset(token)
    token = web_server._current_http_cui_actor.set(
        {"tenant_id": "tenant-b", "actor_id": "user-2", "role": "user"}
    )
    try:
        key_b = web_server._mcp_bridge_session_key(config)
    finally:
        web_server._current_http_cui_actor.reset(token)

    web_server._MCP_BRIDGE_SESSIONS.update(
        {key_a: "session-a", key_b: "session-b"}
    )
    web_server._MCP_BRIDGE_REQUEST_IDS.update({key_a: 3, key_b: 7})

    token = web_server._current_http_cui_actor.set(
        {"tenant_id": "tenant-a", "actor_id": "user-1", "role": "user"}
    )
    try:
        web_server._assistant_resources_payload(
            force_refresh=True, refresh_resource="calendar"
        )
    finally:
        web_server._current_http_cui_actor.reset(token)

    assert key_a not in web_server._MCP_BRIDGE_SESSIONS
    assert key_a not in web_server._MCP_BRIDGE_REQUEST_IDS
    assert web_server._MCP_BRIDGE_SESSIONS[key_b] == "session-b"
    assert web_server._MCP_BRIDGE_REQUEST_IDS[key_b] == 7


def test_non_bridge_resource_refresh_keeps_bridge_sessions(monkeypatch):
    config = {
        "mcp_servers": {
            "aiwerk_bridge": {"url": "https://bridge.example.test/mcp"}
        }
    }
    monkeypatch.setattr(web_server, "load_config", lambda: config)
    _stub_cached_resources(monkeypatch)
    web_server._MCP_BRIDGE_SESSIONS.clear()
    web_server._MCP_BRIDGE_REQUEST_IDS.clear()

    token = web_server._current_http_cui_actor.set(
        {"tenant_id": "tenant-a", "actor_id": "user-1", "role": "user"}
    )
    try:
        key = web_server._mcp_bridge_session_key(config)
        web_server._MCP_BRIDGE_SESSIONS[key] = "session-a"
        web_server._MCP_BRIDGE_REQUEST_IDS[key] = 4
        web_server._assistant_resources_payload(
            force_refresh=True, refresh_resource="shared_folder"
        )
    finally:
        web_server._current_http_cui_actor.reset(token)

    assert web_server._MCP_BRIDGE_SESSIONS[key] == "session-a"
    assert web_server._MCP_BRIDGE_REQUEST_IDS[key] == 4


def test_stale_background_resource_refresh_cannot_overwrite_force_refresh():
    name = "email"
    cache_key = "tenant-generation-test"
    full_key = f"{name}:{cache_key}"
    web_server._ASSISTANT_RESOURCE_CACHE.pop(full_key, None)
    web_server._ASSISTANT_RESOURCE_REFRESHING.discard(full_key)
    web_server._assistant_write_resource_cache(
        full_key, {"summary": "seed"}, ttl_seconds=0
    )

    old_started = threading.Event()
    release_old = threading.Event()

    def old_builder():
        old_started.set()
        assert release_old.wait(timeout=5)
        return {"summary": "stale-old"}

    payload, meta = web_server._assistant_cached_resource(
        name,
        60,
        cache_key,
        old_builder,
        stale_while_revalidate=True,
    )
    assert payload["summary"] == "seed"
    assert meta["refreshing"] is True
    assert old_started.wait(timeout=5)

    fresh, _ = web_server._assistant_cached_resource(
        name,
        60,
        cache_key,
        lambda: {"summary": "fresh-new"},
        force_refresh=True,
    )
    assert fresh["summary"] == "fresh-new"
    release_old.set()

    deadline = time.monotonic() + 5
    while full_key in web_server._ASSISTANT_RESOURCE_REFRESHING:
        assert time.monotonic() < deadline
        time.sleep(0.01)

    assert web_server._ASSISTANT_RESOURCE_CACHE[full_key]["payload"]["summary"] == "fresh-new"


def test_background_resource_refresh_preserves_authenticated_actor_context():
    full_key = "email:tenant-context-test"
    web_server._ASSISTANT_RESOURCE_REFRESHING.discard(full_key)
    observed = {}
    finished = threading.Event()

    def builder():
        observed.update(web_server._mcp_bridge_actor_scope())
        finished.set()
        return {"summary": "actor-bound"}

    token = web_server._current_http_cui_actor.set(
        {"tenant_id": "tenant-a", "actor_id": "user-1", "role": "user"}
    )
    try:
        assert web_server._assistant_schedule_resource_refresh(
            full_key, builder, 60
        ) is True
    finally:
        web_server._current_http_cui_actor.reset(token)

    assert finished.wait(timeout=5)
    assert observed == {
        "tenant_id": "tenant-a",
        "actor_id": "user-1",
        "role": "user",
    }


def test_live_bridge_inventory_normalizes_subserver_status(monkeypatch):
    monkeypatch.setattr(
        web_server,
        "_mcp_bridge_router_call",
        lambda *_args, **_kwargs: {
            "result": {
                "content": [
                    {
                        "text": json.dumps(
                            {
                                "servers": [
                                    {
                                        "name": "google-workspace-aiwerk",
                                        "status": "connected",
                                    }
                                ]
                            }
                        )
                    }
                ]
            }
        },
    )

    items = web_server._aiwerk_bridge_live_subservers(
        {
            "mcp_servers": {
                "aiwerk_bridge": {"url": "https://bridge.example.test/mcp"}
            }
        }
    )

    assert items[0]["id"] == "aiwerk-bridge-google-workspace-aiwerk"
    assert items[0]["status"] == "connected"
