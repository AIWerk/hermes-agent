from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import Response

from hermes_cli import web_server
from hermes_cli.dashboard_auth.routes import api_auth_me


def _session(**overrides):
    fields = {
        "tenant_id": "tenant-example",
        "actor_id": "tenant-example:customer:user",
        "user_id": "customer",
        "role": "user",
        "display_name": "Example Customer",
        "email": "customer@example.invalid",
        "org_id": "tenant-example",
        "provider": "password",
        "expires_at": 9999999999,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _request(session) -> Any:
    return SimpleNamespace(state=SimpleNamespace(session=session))


async def _auth_me(request):
    response = Response()
    payload = await cast(Any, api_auth_me)(request, response)
    assert response.headers["cache-control"] == "private, no-store"
    return payload


@pytest.mark.asyncio
async def test_auth_me_returns_authenticated_customer_greeting_identity():
    data = await _auth_me(_request(_session()))

    assert data["greeting"] == {"name": "Example", "context": "customer"}


@pytest.mark.asyncio
async def test_auth_me_missing_display_name_has_no_customer_fallback():
    data = await _auth_me(_request(_session(display_name="")))

    assert data["greeting"] == {"name": None, "context": "customer"}


@pytest.mark.asyncio
async def test_auth_me_returns_authenticated_admin_greeting_identity():
    data = await _auth_me(
        _request(
            _session(
                actor_id="aiwerk:operator:admin",
                user_id="operator",
                role="aiwerk_admin",
                display_name="Example Operator",
            )
        )
    )

    assert data["greeting"] == {"name": "Example", "context": "admin"}


@pytest.mark.asyncio
async def test_auth_me_tenant_admin_uses_admin_context():
    response = await _auth_me(
        _request(_session(role="tenant_admin", display_name="Taylor Admin"))
    )

    assert response["greeting"] == {"name": "Taylor", "context": "admin"}


@pytest.mark.asyncio
async def test_auth_me_unknown_role_is_neutral():
    data = await _auth_me(
        _request(_session(role="unexpected", display_name="Configured Customer"))
    )

    assert data["greeting"] == {"name": None, "context": "unknown"}


def test_public_model_info_never_contains_session_identity(monkeypatch):
    monkeypatch.setattr(
        web_server,
        "load_config",
        lambda: {
            "model": "",
            "dashboard": {"user_name": "Configured Customer"},
        },
    )

    data = web_server.get_model_info(_request(_session()))

    assert data["user_display_name"] is None
    assert data["greeting_context"] == "unknown"
