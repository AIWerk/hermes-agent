import asyncio
from types import SimpleNamespace

import pytest


@pytest.fixture
def assistant_identity() -> dict[str, str]:
    return {
        "role": "user",
        "actor_id": "tenant-a:user-1",
        "tenant_id": "tenant-a",
    }


@pytest.fixture
def web_server(monkeypatch):
    import hermes_cli.web_server as module

    monkeypatch.setattr(module, "_DASHBOARD_MODE", "admin", raising=False)
    return module


def test_assistant_http_allowlist_is_method_specific_and_default_deny(web_server) -> None:
    assert hasattr(web_server, "_assistant_api_allowed")
    allowed = web_server._assistant_api_allowed

    assert allowed("/api/status", "GET") is True
    assert allowed("/api/auth/me", "GET") is True
    assert allowed("/api/auth/ws-ticket", "POST") is True
    assert allowed("/api/auth/ws-ticket", "GET") is False
    assert allowed("/api/config", "GET") is False
    assert allowed("/api/env", "GET") is False
    assert allowed("/api/plugins", "GET") is False
    assert allowed("/api/status/extra", "GET") is False


def test_assistant_http_middleware_allows_customer_status_and_hides_admin_api(
    web_server, monkeypatch
) -> None:
    from starlette.testclient import TestClient

    monkeypatch.setattr(web_server, "_DASHBOARD_MODE", "assistant")
    web_server.app.state.auth_required = False
    client = TestClient(web_server.app)
    client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN

    assert client.get("/api/status").status_code == 200
    assert client.get("/api/config").status_code == 404


@pytest.mark.parametrize(
    "method",
    [
        "shell.exec",
        "cli.exec",
        "cron.manage",
        "skills.manage",
        "browser.manage",
        "model.save_key",
        "unknown.method",
    ],
)
def test_assistant_ws_gate_denies_admin_and_unknown_methods(web_server, method: str) -> None:
    assert hasattr(web_server, "_assistant_ws_request_gate")

    reason = web_server._assistant_ws_request_gate({"method": method, "params": {}})

    assert reason is not None
    assert "assistant mode" in reason


@pytest.mark.parametrize(
    "method,params",
    [
        ("gateway.ping", {}),
        ("session.create", {"source": "web", "close_on_disconnect": True}),
        ("prompt.submit", {"session_id": "sid", "text": "hello"}),
        (
            "approval.respond",
            {"session_id": "sid", "request_id": "req", "choice": "once"},
        ),
        ("slash.exec", {"session_id": "sid", "command": "/stop"}),
    ],
)
def test_assistant_ws_gate_allows_exact_customer_contract(
    web_server, assistant_identity, method: str, params: dict
) -> None:
    request = {"method": method, "params": params}

    assert web_server._assistant_ws_request_gate(request, assistant_identity) is None
    assert request["params"]["_cui_actor_id"] == "tenant-a:user-1"


@pytest.mark.parametrize(
    "method,params",
    [
        ("session.resume", {"session_id": "sid"}),
        ("config.get", {"key": "reasoning"}),
        ("config.set", {"key": "yolo", "value": True}),
        ("session.events.since", {"session_id": "sid", "last_seen": 0}),
        ("session.create", {"profile": "other"}),
        ("session.create", {"cwd": "/tmp"}),
        ("prompt.submit", {"session_id": "sid", "text": "hi", "profile": "other"}),
        (
            "approval.respond",
            {"session_id": "sid", "request_id": "req", "choice": "always"},
        ),
        (
            "approval.respond",
            {"session_id": "sid", "request_id": "req", "choice": "once", "all": True},
        ),
        ("session.events.since", {"session_id": "sid", "last_seen": 0, "profile": "other"}),
    ],
)
def test_assistant_ws_gate_rejects_extra_or_privileged_contract(
    web_server, assistant_identity, method: str, params: dict
) -> None:
    assert web_server._assistant_ws_request_gate(
        {"method": method, "params": params}, assistant_identity
    ) is not None


def test_assistant_ws_gate_requires_complete_server_identity(web_server) -> None:
    request = {"method": "session.create", "params": {"source": "web", "close_on_disconnect": True}}

    for identity in (None, {}, {"role": "user"}, {"role": "user", "actor_id": "a"}):
        assert web_server._assistant_ws_request_gate(request, identity) is not None


def test_assistant_ws_gate_restricts_slash_exec(web_server, assistant_identity) -> None:
    assert hasattr(web_server, "_assistant_ws_request_gate")
    gate = lambda request: web_server._assistant_ws_request_gate(request, assistant_identity)

    for command in ("/stop", "/compress", "/reload-mcp"):
        assert gate({"method": "slash.exec", "params": {"session_id": "sid", "command": command}}) is None
    for command in ("/config", "/model opus", "/skills install x", "/cron list", ""):
        assert gate({"method": "slash.exec", "params": {"session_id": "sid", "command": command}}) is not None


def test_assistant_ws_gate_overwrites_client_actor_spoof_with_server_identity(web_server) -> None:
    assert hasattr(web_server, "_assistant_ws_request_gate")
    request = {
        "method": "session.create",
        "params": {
            "source": "web",
            "close_on_disconnect": True,
            "_cui_actor_role": "admin",
            "_cui_actor_id": "attacker",
            "_cui_tenant_id": "other",
            "actor_role": "admin",
        },
    }
    identity = {
        "role": "user",
        "actor_id": "tenant-a:user-1",
        "tenant_id": "tenant-a",
        "user_id": "user-1",
    }

    assert web_server._assistant_ws_request_gate(request, identity) is None
    assert request["params"] == {
        "source": "web",
        "close_on_disconnect": True,
        "_cui_actor_role": "user",
        "_cui_actor_id": "tenant-a:user-1",
        "_cui_tenant_id": "tenant-a",
    }


def test_assistant_mode_default_denies_non_gateway_websockets(web_server, monkeypatch) -> None:
    monkeypatch.setattr(web_server, "_DASHBOARD_MODE", "assistant")

    class FakeWebSocket:
        client = SimpleNamespace(host="test")
        query_params = {}

        def __init__(self) -> None:
            self.closed = None

        async def close(self, *, code: int, reason: str = "") -> None:
            self.closed = (code, reason)

    for endpoint in (
        web_server.speak_stream_ws,
        web_server.console_ws,
        web_server.pub_ws,
        web_server.events_ws,
    ):
        socket = FakeWebSocket()
        asyncio.run(endpoint(socket))
        assert socket.closed == (4403, "websocket disabled in assistant mode")


def test_assistant_gateway_rejects_legacy_token_without_actor_identity(
    web_server, monkeypatch
) -> None:
    import tui_gateway.ws as gateway_transport

    socket = SimpleNamespace(_hermes_auth_identity=None, closed=None)

    async def close(*, code: int, reason: str = "") -> None:
        socket.closed = (code, reason)

    reached_transport = False

    async def fake_handle_ws(_ws, **_kwargs) -> None:
        nonlocal reached_transport
        reached_transport = True

    socket.close = close
    monkeypatch.setattr(web_server, "_DASHBOARD_MODE", "assistant")
    monkeypatch.setattr(web_server, "_ws_auth_ok", lambda _ws: True)
    monkeypatch.setattr(web_server, "_ws_request_is_allowed", lambda _ws: True)
    monkeypatch.setattr(gateway_transport, "handle_ws", fake_handle_ws)

    asyncio.run(web_server.gateway_ws(socket))

    assert socket.closed == (4403, "authenticated customer identity required")
    assert reached_transport is False


def test_assistant_mode_blocks_pty_before_auth_or_spawn(web_server, monkeypatch) -> None:
    assert hasattr(web_server, "_assistant_mode_enabled")
    monkeypatch.setattr(web_server, "_DASHBOARD_MODE", "assistant")

    class FakeWebSocket:
        client = SimpleNamespace(host="test")

        def __init__(self) -> None:
            self.closed = None

        async def close(self, *, code: int, reason: str = "") -> None:
            self.closed = (code, reason)

    socket = FakeWebSocket()
    asyncio.run(web_server.pty_ws(socket))

    assert socket.closed == (4403, "pty disabled in assistant mode")


def test_spa_bootstrap_injects_exact_dashboard_mode(web_server, monkeypatch) -> None:
    assert hasattr(web_server, "_dashboard_mode_bootstrap_js")

    monkeypatch.setattr(web_server, "_DASHBOARD_MODE", "assistant")
    assert web_server._dashboard_mode_bootstrap_js() == (
        'window.__HERMES_DASHBOARD_MODE__="assistant";'
    )
    monkeypatch.setattr(web_server, "_DASHBOARD_MODE", "admin")
    assert web_server._dashboard_mode_bootstrap_js() == (
        'window.__HERMES_DASHBOARD_MODE__="admin";'
    )


def test_set_dashboard_mode_accepts_only_explicit_modes(web_server) -> None:
    assert hasattr(web_server, "_set_dashboard_mode")

    web_server._set_dashboard_mode("assistant")
    assert web_server._DASHBOARD_MODE == "assistant"
    web_server._set_dashboard_mode("admin")
    assert web_server._DASHBOARD_MODE == "admin"
    with pytest.raises(SystemExit, match="Unsupported dashboard mode"):
        web_server._set_dashboard_mode("spoofed")


def test_gateway_ws_installs_assistant_gate_with_server_identity(
    web_server, monkeypatch
) -> None:
    import tui_gateway.ws as gateway_transport

    identity = {"role": "user", "actor_id": "actor-a", "tenant_id": "tenant-a"}
    socket = SimpleNamespace(
        _hermes_auth_identity=identity,
        _hermes_ws_subprotocol=None,
    )
    captured: dict[str, object] = {}

    monkeypatch.setattr(web_server, "_DASHBOARD_MODE", "assistant")
    monkeypatch.setattr(web_server, "_ws_auth_ok", lambda _ws: True)
    monkeypatch.setattr(web_server, "_ws_request_is_allowed", lambda _ws: True)

    async def fake_handle_ws(_ws, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(gateway_transport, "handle_ws", fake_handle_ws)

    asyncio.run(web_server.gateway_ws(socket))

    gate = captured["request_gate"]
    request = {
        "method": "session.create",
        "params": {
            "source": "web",
            "close_on_disconnect": True,
            "_cui_actor_role": "admin",
        },
    }
    assert gate(request) is None
    assert request["params"]["_cui_actor_role"] == "user"


def test_gateway_ws_keeps_admin_transport_ungated(web_server, monkeypatch) -> None:
    import tui_gateway.ws as gateway_transport

    socket = SimpleNamespace(
        _hermes_auth_identity=None,
        _hermes_ws_subprotocol=None,
    )
    captured: dict[str, object] = {}

    monkeypatch.setattr(web_server, "_DASHBOARD_MODE", "admin")
    monkeypatch.setattr(web_server, "_ws_auth_ok", lambda _ws: True)
    monkeypatch.setattr(web_server, "_ws_request_is_allowed", lambda _ws: True)

    async def fake_handle_ws(_ws, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(gateway_transport, "handle_ws", fake_handle_ws)

    asyncio.run(web_server.gateway_ws(socket))

    assert captured.get("request_gate") is None
