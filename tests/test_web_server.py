"""Test that start_server configures ws-ping keepalive.

The server now uses uvicorn.Server directly (not uvicorn.run) so we stub
Config + Server + asyncio.run to capture kwargs without starting an event loop.
"""

import asyncio
import contextlib

import uvicorn

from hermes_cli import web_server


def _stub_uvicorn(monkeypatch):
    """Replace uvicorn.Config/Server with fakes so start_server returns
    immediately.  Returns a dict with captured Config kwargs."""
    captured: dict = {}

    class _FakeConfig:
        loaded = True
        host = "127.0.0.1"
        port = 8000

        def __init__(self, *args, **kwargs):
            captured.update(kwargs)

        def load(self):
            pass

        class lifespan_class:
            should_exit = False
            state: dict = {}

            def __init__(self, *a, **kw):
                pass

            async def startup(self):
                pass

            async def shutdown(self):
                pass

    class _FakeServer:
        should_exit = False
        started = True
        servers: list = []
        lifespan = None

        @staticmethod
        def capture_signals():
            return contextlib.nullcontext()

        async def startup(self, sockets=None):
            pass

        async def main_loop(self):
            pass

        async def shutdown(self, sockets=None):
            pass

    monkeypatch.setattr(uvicorn, "Config", _FakeConfig)
    monkeypatch.setattr(uvicorn, "Server", lambda config: _FakeServer())
    return captured


def test_start_server_enables_ws_ping_for_half_open_detection(monkeypatch):
    """WS ping must be configured so half-open connections (reverse-proxy 524,
    dropped tunnels) raise WebSocketDisconnect into the reaping path (#32377)."""
    captured = _stub_uvicorn(monkeypatch)

    # Loopback bind => no auth gate, so this reaches the Config constructor.
    web_server.start_server(host="127.0.0.1", port=0, open_browser=False)

    assert captured["ws_ping_interval"] == 20.0
    assert captured["ws_ping_timeout"] == 20.0


def _fake_resource_payload(name: str) -> dict:
    payload = {"status": "connected", "summary": name, "items": []}
    if name in {"email", "calendar"}:
        payload["accounts"] = []
    if name == "email":
        payload["unread_count"] = 0
    if name == "shared_folder":
        payload["can_open_folder"] = False
    if name == "vault":
        payload.update({"weak_count": 0, "reused_count": 0, "compromised_count": 0})
    if name == "todos":
        payload["open_count"] = 0
    if name == "contacts":
        payload.update({"relevant": [], "frequent": [], "total_count": 0})
    return payload


def _stub_assistant_cached_resource(monkeypatch):
    def fake_cached_resource(name, ttl_seconds, cache_key, builder, **kwargs):
        meta = {"cached": False, "updated_at": "now", "expires_at": "later", "ttl_seconds": ttl_seconds}
        return _fake_resource_payload(name), meta

    monkeypatch.setattr(web_server, "_assistant_cached_resource", fake_cached_resource)


def test_assistant_resource_force_refresh_drops_mcp_bridge_sessions(monkeypatch):
    """A CUI resource refresh must recover after Google Workspace re-auth."""
    web_server._MCP_BRIDGE_SESSIONS.clear()
    web_server._MCP_BRIDGE_SESSIONS["stale"] = "session-id"
    monkeypatch.setattr(web_server, "load_config", lambda: {})
    _stub_assistant_cached_resource(monkeypatch)

    web_server._assistant_resources_payload(force_refresh=True, refresh_resource="calendar")

    assert web_server._MCP_BRIDGE_SESSIONS == {}


def test_assistant_non_bridge_resource_refresh_keeps_mcp_bridge_sessions(monkeypatch):
    web_server._MCP_BRIDGE_SESSIONS.clear()
    web_server._MCP_BRIDGE_SESSIONS["active"] = "session-id"
    monkeypatch.setattr(web_server, "load_config", lambda: {})
    _stub_assistant_cached_resource(monkeypatch)

    web_server._assistant_resources_payload(force_refresh=True, refresh_resource="shared_folder")

    assert web_server._MCP_BRIDGE_SESSIONS == {"active": "session-id"}
