from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from hermes_cli import web_server
from hermes_cli.dashboard_auth.ws_tickets import _reset_for_tests, mint_ticket
from tui_gateway import server


@pytest.fixture
def assistant_ws_app(monkeypatch, tmp_path):
    """Run the real assistant WebSocket route against isolated process state."""
    _reset_for_tests()
    server._sessions.clear()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr(web_server, "_DASHBOARD_MODE", "assistant")
    monkeypatch.setattr(web_server.app.state, "dashboard_mode", "assistant", raising=False)
    monkeypatch.setattr(web_server.app.state, "auth_required", True, raising=False)
    monkeypatch.setattr(web_server.app.state, "bound_host", "testserver", raising=False)
    monkeypatch.setattr(web_server.app.state, "bound_port", 443, raising=False)
    # session.create dispatch remains real; only its deferred agent build and
    # detached-session cap timer are suppressed so the integration test owns no
    # background workers after the WebSocket closes.
    monkeypatch.setattr(server, "_schedule_agent_build", lambda _sid: None)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda: None)

    client = TestClient(web_server.app, base_url="https://testserver")
    yield client
    client.close()

    server._sessions.clear()
    _reset_for_tests()


def _ticket(role: str) -> str:
    return mint_ticket(
        user_id=f"{role}-user",
        provider="regression-test",
        tenant_id="tenant-a",
        actor_id=f"tenant-a:{role}-actor",
        role=role,
    )


@pytest.mark.parametrize("role", ["admin", "user", "customer"])
def test_authenticated_assistant_ticket_roles_reach_real_session_create(
    assistant_ws_app: TestClient,
    role: str,
) -> None:
    ticket = _ticket(role)

    # Entering the TestClient WebSocket context proves the ASGI app completed
    # the HTTP 101 upgrade. This is the exact session.create shape emitted by
    # AiwerkAssistantPage; conflicting actor fields model an untrusted client.
    with assistant_ws_app.websocket_connect(f"/api/ws?ticket={ticket}") as ws:
        ready = ws.receive_json()
        assert ready["params"]["type"] == "gateway.ready"
        ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": f"create-{role}",
                "method": "session.create",
                "params": {
                    "source": "web",
                    "close_on_disconnect": True,
                    "_cui_actor_role": "support",
                    "_cui_actor_id": "forged-actor",
                    "_cui_tenant_id": "forged-tenant",
                    "actor_role": "owner",
                },
            }
        )
        response = ws.receive_json()

        assert response["id"] == f"create-{role}"
        session_id = response["result"]["session_id"]
        assert server._sessions[session_id]["cui_actor_context"] == {
            "tenant_id": "tenant-a",
            "actor_id": f"tenant-a:{role}-actor",
            "role": role,
            "user_id": f"{role}-user",
            "provider": "regression-test",
        }


def test_unknown_assistant_ticket_role_is_rejected_before_dispatch(
    assistant_ws_app: TestClient,
) -> None:
    ticket = _ticket("unknown")

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with assistant_ws_app.websocket_connect(f"/api/ws?ticket={ticket}"):
            pass

    # Closing before accept is surfaced by TestClient as 4403; a browser sees
    # the same ASGI pre-accept rejection as an HTTP 403 upgrade response.
    assert exc_info.value.code == 4403
    assert server._sessions == {}


@pytest.mark.parametrize(
    "role",
    [
        "admin",
        "aiwerk_admin",
        "operator",
        "owner",
        "user",
        "customer",
        "tenant_user",
        "member",
        "tenant_admin",
        "support",
    ],
)
def test_http_actor_and_session_visibility_use_canonical_recognized_roles(role: str) -> None:
    request = type(
        "Request",
        (),
        {
            "state": type(
                "State",
                (),
                {
                    "session": type(
                        "Session",
                        (),
                        {"role": role, "actor_id": "actor-a", "tenant_id": "tenant-a"},
                    )()
                },
            )()
        },
    )()

    actor = web_server._cui_actor_context_from_request(request)
    assert actor == {"role": role, "actor_id": "actor-a", "tenant_id": "tenant-a"}
    assert web_server._session_visible_to_cui_actor(
        {
            "id": "session-a",
            "model_config": {
                "_cui_visibility_scope": "customer",
                "_cui_actor_role": role,
                "_cui_actor_id": "actor-a",
                "_cui_tenant_id": "tenant-a",
            },
        },
        actor,
    ) is True
