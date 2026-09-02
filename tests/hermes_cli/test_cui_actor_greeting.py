import asyncio
import json
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import HTTPException

from hermes_cli.dashboard_auth.base import Session
from hermes_cli.dashboard_auth.identity import greeting_identity_from_session
from hermes_cli.dashboard_auth.routes import api_auth_me, api_auth_ws_ticket
from hermes_cli.dashboard_auth.ws_tickets import _reset_for_tests, consume_ticket


def _session(**overrides):
    fields = {
        "user_id": "customer-user",
        "email": "customer@example.test",
        "display_name": "Example Customer",
        "org_id": "tenant-example",
        "provider": "test",
        "expires_at": 4_000_000_000,
        "access_token": "opaque-access-token",
        "refresh_token": "opaque-refresh-token",
        "tenant_id": "tenant-example",
        "actor_id": "tenant-example:customer:user",
        "role": "user",
    }
    fields.update(overrides)
    return Session(**fields)


def _request(session: Session, *, mode: str = "assistant") -> Any:
    return cast(
        Any,
        SimpleNamespace(
            state=SimpleNamespace(session=session),
            app=SimpleNamespace(state=SimpleNamespace(dashboard_mode=mode)),
            client=None,
            headers={},
        ),
    )


def test_authenticated_customer_greeting_uses_verified_first_name():
    assert greeting_identity_from_session(_session()) == {
        "name": "Example",
        "context": "customer",
    }


def test_authenticated_admin_greeting_is_neutral_admin_context():
    assert greeting_identity_from_session(
        _session(role="aiwerk_admin", display_name="Example Operator")
    ) == {"name": "Example", "context": "admin"}


def test_auth_me_exposes_only_neutral_greeting_identity():
    response = asyncio.run(api_auth_me(_request(_session())))
    payload = json.loads(response.body)

    assert payload["greeting"] == {"name": "Example", "context": "customer"}
    assert response.headers["cache-control"] == "private, no-store"


@pytest.mark.parametrize(
    "overrides",
    [
        {"tenant_id": ""},
        {"actor_id": ""},
        {"role": ""},
        {"role": "unexpected"},
    ],
)
def test_assistant_auth_boundaries_reject_original_incomplete_session(overrides):
    request = _request(_session(**overrides), mode="assistant")

    for endpoint in (api_auth_me, api_auth_ws_ticket):
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(endpoint(request))
        assert exc_info.value.status_code == 403


def test_admin_mode_ws_ticket_preserves_legacy_identity_fallbacks():
    _reset_for_tests()
    legacy = _session(tenant_id="", actor_id="", role="")

    response = asyncio.run(api_auth_ws_ticket(_request(legacy, mode="admin")))
    identity = consume_ticket(response["ticket"])

    assert identity["tenant_id"] == legacy.org_id
    assert identity["actor_id"] == legacy.user_id
    assert identity["role"] == "user"


def test_unknown_or_incomplete_identity_has_no_invented_name():
    assert greeting_identity_from_session(
        _session(role="unexpected", display_name="Configured Customer")
    ) == {"name": None, "context": "unknown"}
    assert greeting_identity_from_session(
        _session(actor_id="", display_name="Configured Customer")
    ) == {"name": None, "context": "unknown"}
