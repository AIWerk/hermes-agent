"""End-to-end behavioural tests for the dashboard auth gate.

Uses ``StubAuthProvider`` so the OAuth round trip can complete in-process
without any external IDP.  Exercises:

  * `/api/status` flips from public (loopback) to gated (auth_required)
  * `/` redirects to /login when no cookie present
  * `/api/auth/providers` is the public bootstrap endpoint
  * `/login` renders HTML listing all providers
  * /assets/* still passes through unauthenticated
  * Full /auth/login → /auth/callback → / round trip with the stub
  * Invalid / missing cookies return 401 (api) or 302 (html)
  * Zero-providers + gate-on fails closed
"""
from __future__ import annotations

import secrets
import threading

import pytest

from fastapi.testclient import TestClient

from hermes_cli import web_server
from hermes_cli.dashboard_auth import clear_providers, register_provider
from hermes_cli.dashboard_auth.base import RefreshExpiredError, Session
from hermes_cli.dashboard_auth.cookies import SESSION_AT_COOKIE
from plugins.dashboard_auth import basic as basic_auth
from tests.hermes_cli.conftest_dashboard_auth import StubAuthProvider


@pytest.fixture
def gated_app():
    """Configure web_server.app for gated mode + register the stub provider."""
    clear_providers()
    register_provider(StubAuthProvider())
    prev_host = getattr(web_server.app.state, "bound_host", None)
    prev_port = getattr(web_server.app.state, "bound_port", None)
    prev_required = getattr(web_server.app.state, "auth_required", None)
    web_server.app.state.bound_host = "fly-app.fly.dev"
    web_server.app.state.bound_port = 443
    web_server.app.state.auth_required = True
    # Use https base_url so cookies pick up Secure flag and host_header
    # matches the bound interface.
    client = TestClient(web_server.app, base_url="https://fly-app.fly.dev")
    yield client
    clear_providers()
    web_server.app.state.bound_host = prev_host
    web_server.app.state.bound_port = prev_port
    web_server.app.state.auth_required = prev_required


# ---------------------------------------------------------------------------
# Allowlist (public) routes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "next_value",
    [
        "<script>alert(1)</script>",
        "javascript:alert(1)",
        "../../etc/passwd",
        "canary\r\nSet-Cookie: injected=1",
    ],
)
def test_empty_provider_login_page_is_safe_through_real_route(
    gated_app, next_value
):
    clear_providers()

    response = gated_app.get("/login", params={"next": next_value})

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "no-store" in response.headers["cache-control"]
    assert "Sign-in unavailable" in response.text
    assert "username/password provider" in response.text
    assert "OAuth provider" in response.text
    assert "--insecure" not in response.text
    assert next_value not in response.text


def test_gated_status_is_public(gated_app):
    """``/api/status`` MUST be public under the OAuth gate.

    Regression guard for the wildcard-subdomain rollout: NAS
    (``fly-provider.ts`` ``getInstanceRuntimeStatus``) hits
    ``/api/status`` without a cookie as its sole liveness probe. A 401
    here surfaces every healthy agent as STARTING/down in the portal
    UI. The endpoint returns only version + gateway/auth-gate metadata
    (no user data, no session content), so it stays in the shared
    ``PUBLIC_API_PATHS`` allowlist under both the legacy ``_SESSION_TOKEN``
    gate and the OAuth gate.

    The body also reports the gate's shape (``auth_required``,
    ``auth_providers``) so the SPA's StatusPage and external monitors
    can distinguish loopback / gated / no-providers without a separate
    round trip.
    """
    r = gated_app.get("/api/status")
    assert r.status_code == 200, (
        f"Expected 200, got {r.status_code}: {r.text}"
    )
    body = r.json()
    assert body["auth_required"] is True
    assert "version" in body
    assert "gateway_state" in body


@pytest.mark.parametrize("path", [
    "/api/health",
    "/api/config/defaults",
    "/api/config/schema",
    "/api/model/info",
    "/api/dashboard/themes",
    "/api/dashboard/plugins",
])
def test_other_public_api_paths_are_public_under_gate(gated_app, path):
    """The remaining ``PUBLIC_API_PATHS`` entries must also bypass the
    gate. They're documented as non-sensitive read-only endpoints that
    the SPA pre-loads before login (themes, config schema, model
    metadata). A 401 / 302-to-login here would block the dashboard
    shell from rendering pre-auth.

    Accept any non-auth-failure status: 200 when the route succeeds,
    or any route-specific error (e.g. 400 / 404 / 500 from a missing
    dependency) — but NEVER 401, and NEVER a 302 to ``/login``.
    """
    r = gated_app.get(path, follow_redirects=False)
    assert r.status_code != 401, (
        f"{path} returned 401 under the OAuth gate — should be public"
    )
    if r.status_code == 302:
        location = r.headers.get("location", "")
        assert "/login" not in location, (
            f"{path} redirected to {location} — should be public, "
            "not bounced to /login"
        )


# ---------------------------------------------------------------------------
# OAuth round trip
# ---------------------------------------------------------------------------




def _complete_stub_login(client) -> None:
    """Walk the stub OAuth round trip so ``client`` carries a valid session.

    TestClient persists Set-Cookie across calls, so after this returns the
    client's cookie jar holds ``hermes_session_at`` / ``hermes_session_rt``
    and subsequent gated requests authenticate.
    """
    r1 = client.get("/auth/login?provider=stub", follow_redirects=False)
    assert r1.status_code == 302
    state = r1.headers["location"].split("state=")[1]
    r2 = client.get(
        f"/auth/callback?code=stub_code&state={state}",
        follow_redirects=False,
    )
    assert r2.status_code == 302


def test_gated_require_token_endpoint_accepts_cookie_session(gated_app):
    """Regression: ``_require_token`` endpoints must work under the OAuth gate.

    In gated mode the legacy ``_SESSION_TOKEN`` is NOT injected into the SPA
    (it authenticates with the session cookie). Endpoints that call
    ``_require_token`` directly — plugin install/enable/disable,
    ``/api/dashboard/plugins/hub``, and others — used to re-check the absent
    token and 401 every cookie-authenticated request, making them permanently
    unreachable behind the gate (the dashboard surfaced a
    ``401: {"detail":"Unauthorized"}`` popup on plugin install). The fix makes
    ``_require_token`` defer to the gate, which has already verified the cookie
    and attached ``request.state.session`` before the handler runs.

    We POST a deliberately invalid plugin identifier: a passing auth layer
    lets the request reach the handler, which rejects the identifier with a
    400. The assertion is simply "not 401" — proving auth succeeded without
    coupling to the validation message.
    """
    _complete_stub_login(gated_app)
    r = gated_app.post(
        "/api/dashboard/agent-plugins/install",
        json={"identifier": "definitely not a valid identifier",
              "force": False, "enable": False},
    )
    assert r.status_code != 401, (
        "A _require_token endpoint 401'd a cookie-authenticated request under "
        f"the OAuth gate (the install-popup bug). Body: {r.text}"
    )
    # And specifically: it reached the handler's own validation.
    assert r.status_code == 400, (
        f"Expected the install handler's 400 (bad identifier), got "
        f"{r.status_code}: {r.text}"
    )


# A representative spread of the OTHER ``_require_token`` endpoints (there are
# 14 in total). The install popup was just the reported symptom; the same bug
# made API-key reveal, provider validation, the OAuth-provider connect flow,
# and the rest of plugin management unreachable behind the gate. Each entry is
# (method, path, json_body); we assert only that a logged-in request is NOT
# 401'd — i.e. it cleared the auth layer and reached the handler. The
# handler's own status (400/404/429/etc.) is route-specific and not asserted.
_GATED_REQUIRE_TOKEN_ROUTES = [
    ("get", "/api/dashboard/plugins/hub", None),
    ("post", "/api/env/reveal", {"key": "NONEXISTENT_ENV_VAR_FOR_TEST"}),
    ("post", "/api/providers/validate", {"key": "OPENAI_API_KEY", "value": ""}),
    ("delete", "/api/providers/oauth/__not_a_real_provider__", None),
    ("post", "/api/dashboard/agent-plugins/__nope__/enable", None),
]


def test_login_non_interactive_provider_returns_404_not_500(gated_app):
    """Regression: a token-only provider (drain) has no login flow, so
    /auth/login?provider=drain-secret must 404 (not 500 on start_login) and it
    must not appear in the /api/auth/providers bootstrap.
    """
    import secrets

    import plugins.dashboard_auth.drain as drain_plugin

    register_provider(
        drain_plugin.DrainSecretProvider(secret=secrets.token_urlsafe(48))
    )

    r = gated_app.get(
        "/auth/login?provider=drain-secret&next=%2F", follow_redirects=False
    )
    assert r.status_code == 404, (
        f"drain-secret login should 404, not 500: {r.status_code} {r.text}"
    )

    bootstrap = gated_app.get("/api/auth/providers")
    assert bootstrap.status_code == 200
    names = {p["name"] for p in bootstrap.json()["providers"]}
    assert "drain-secret" not in names
    assert "stub" in names


def test_callback_invalid_code_returns_400(gated_app):
    r1 = gated_app.get("/auth/login?provider=stub", follow_redirects=False)
    state = r1.headers["location"].split("state=")[1]
    r2 = gated_app.get(
        f"/auth/callback?code=BAD_CODE&state={state}",
        follow_redirects=False,
    )
    assert r2.status_code == 400


# ---------------------------------------------------------------------------
# Cookie validation
# ---------------------------------------------------------------------------


def test_invalid_cookie_returns_401_on_api(gated_app):
    gated_app.cookies.set(SESSION_AT_COOKIE, "garbage-not-a-real-token")
    r = gated_app.get("/api/sessions")
    assert r.status_code == 401




# ---------------------------------------------------------------------------
# Identity probe
# ---------------------------------------------------------------------------


def test_session_carries_explicit_tenant_actor_and_role_fields():
    session = Session(
        user_id="legacy-user",
        email="user@example.test",
        display_name="User",
        org_id="legacy-org",
        provider="test",
        expires_at=123,
        access_token="access",
        refresh_token="refresh",
        tenant_id="tenant-1",
        actor_id="actor-1",
        role="customer",
    )

    assert session.tenant_id == "tenant-1"
    assert session.actor_id == "actor-1"
    assert session.role == "customer"


def test_api_auth_me_returns_session_after_login(gated_app):
    r1 = gated_app.get("/auth/login?provider=stub", follow_redirects=False)
    state = r1.headers["location"].split("state=")[1]
    gated_app.get(
        f"/auth/callback?code=stub_code&state={state}",
        follow_redirects=False,
    )
    r = gated_app.get("/api/auth/me")
    assert r.status_code == 200
    body = r.json()
    assert body["user_id"] == "stub-user-1"
    assert body["email"] == "stub@example.test"
    assert body["display_name"] == "Stub User"
    assert body["provider"] == "stub"
    assert body["org_id"] == "stub-org-1"
    assert body["tenant_id"] == "stub-org-1"
    assert body["actor_id"] == "stub-user-1"
    assert body["role"] == "user"
    assert "expires_at" in body
    cache_control = r.headers.get("cache-control", "")
    assert "private" in cache_control
    assert "no-store" in cache_control


def test_public_auth_provider_bootstrap_contains_no_actor_identity(gated_app):
    r = gated_app.get("/api/auth/providers")
    assert r.status_code == 200
    body = r.json()
    serialized = repr(body)
    for private_value in (
        "stub-user-1",
        "stub@example.test",
        "stub-org-1",
        "tenant_id",
        "actor_id",
        "role",
    ):
        assert private_value not in serialized


def test_registered_basic_provider_reloads_current_membership_for_each_decision(
    monkeypatch,
):
    password_hash = basic_auth.hash_password("hunter2")
    current = {
        "section": {
            "secret": "test-signing-secret-that-is-long-enough",
            "users": [
                {
                    "username": "alice",
                    "password_hash": password_hash,
                    "tenant_id": "tenant-a",
                    "actor_id": "actor-a",
                    "role": "admin",
                }
            ],
        }
    }
    loads = 0

    def load_section():
        nonlocal loads
        loads += 1
        return current["section"]

    class RegistrationContext:
        provider = None

        def register_dashboard_auth_provider(self, provider):
            self.provider = provider

    for name in (
        "HERMES_DASHBOARD_BASIC_AUTH_USERNAME",
        "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH",
        "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD",
        "HERMES_DASHBOARD_BASIC_AUTH_SECRET",
        "HERMES_DASHBOARD_BASIC_AUTH_TTL_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(basic_auth, "_load_config_basic_auth_section", load_section)

    ctx = RegistrationContext()
    basic_auth.register(ctx)
    provider = ctx.provider
    assert provider is not None
    original = provider.complete_password_login(
        username="alice", password="hunter2"
    )
    assert original.role == "admin"

    current["section"]["users"][0]["role"] = "user"
    verified = provider.verify_session(access_token=original.access_token)
    assert verified is not None
    assert verified.role == "user"
    refreshed = provider.refresh_session(refresh_token=original.refresh_token)
    assert refreshed.role == "user"

    current["section"]["users"] = []
    assert provider.verify_session(access_token=original.access_token) is None
    with pytest.raises(RefreshExpiredError):
        provider.refresh_session(refresh_token=original.refresh_token)
    assert loads >= 6  # register plus login/verify/refresh/removal decisions


def test_basic_provider_uses_one_coherent_authority_snapshot_per_verification():
    source = {
        "alice": {
            "password_hash": basic_auth.hash_password("hunter2"),
            "tenant_id": "tenant-a",
            "actor_id": "actor-a",
            "role": "admin",
            "display_name": "Alice",
            "email": "alice@example.test",
        }
    }
    snapshot_taken = threading.Event()
    mutation_complete = threading.Event()
    resolver_calls = 0

    def authority_resolver():
        nonlocal resolver_calls
        resolver_calls += 1
        snapshot = {username: dict(record) for username, record in source.items()}
        if resolver_calls == 2:
            snapshot_taken.set()
            assert mutation_complete.wait(timeout=5)
        return snapshot

    provider = basic_auth.BasicAuthProvider(
        secret=secrets.token_bytes(32),
        users=source,
        authority_resolver=authority_resolver,
    )
    original = provider.complete_password_login(
        username="alice", password="hunter2"
    )
    result = []
    worker = threading.Thread(
        target=lambda: result.append(
            provider.verify_session(access_token=original.access_token)
        )
    )
    worker.start()
    assert snapshot_taken.wait(timeout=5)
    source["alice"].update(
        tenant_id="tenant-b", actor_id="actor-b", role="user"
    )
    mutation_complete.set()
    worker.join(timeout=5)
    assert not worker.is_alive()

    assert len(result) == 1
    assert result[0] is not None
    assert (result[0].tenant_id, result[0].actor_id, result[0].role) == (
        "tenant-a",
        "actor-a",
        "admin",
    )
    assert resolver_calls == 2  # login once, verification once


def test_basic_provider_static_authority_is_defensively_copied_and_roles_are_validated():
    users = {
        "alice": {
            "password_hash": basic_auth.hash_password("hunter2"),
            "tenant_id": "tenant-a",
            "actor_id": "actor-a",
            "role": "admin",
        }
    }
    provider = basic_auth.BasicAuthProvider(
        secret=secrets.token_bytes(32), users=users
    )
    original = provider.complete_password_login(
        username="alice", password="hunter2"
    )
    users["alice"]["role"] = "user"
    del users["alice"]
    verified = provider.verify_session(access_token=original.access_token)
    assert verified is not None
    assert verified.role == "admin"

    with pytest.raises(ValueError, match="role"):
        basic_auth.BasicAuthProvider(
            secret=secrets.token_bytes(32),
            users={
                "mallory": {
                    "password_hash": basic_auth.hash_password("hunter2"),
                    "role": "superadmin",
                }
            },
        )


def test_api_auth_me_requires_auth(gated_app):
    # No cookies.
    r = gated_app.get("/api/auth/me")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Zero-providers fail-closed
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Multi-provider verify: a ProviderError from one provider must not abort the
# chain when another provider can verify the token.
# ---------------------------------------------------------------------------


class _UnreachableProvider(StubAuthProvider):
    """A provider whose IDP is unreachable: verify_session always raises.

    Models the real-world bug — a self-hosted-OIDC session hits the ``nous``
    provider first, which tries to reach Nous Portal's JWKS; if that's
    unreachable ``nous`` raises ProviderError. The gate must keep trying the
    remaining providers rather than 503-ing the whole request.
    """

    name = "unreachable"
    display_name = "Unreachable IdP (test only)"

    def verify_session(self, *, access_token: str):
        from hermes_cli.dashboard_auth.base import ProviderError

        raise ProviderError("simulated: IDP/JWKS unreachable")

    def refresh_session(self, *, refresh_token: str):
        from hermes_cli.dashboard_auth.base import ProviderError

        raise ProviderError("simulated: IDP/JWKS unreachable")


def _mint_stub_at(stub: StubAuthProvider) -> str:
    """Mint a valid access-token cookie value from a StubAuthProvider via its
    own login round trip (so the HMAC signature matches what verify expects)."""
    ls = stub.start_login(redirect_uri="https://fly-app.fly.dev/auth/callback")
    state = dict(
        seg.split("=", 1)
        for seg in ls.cookie_payload["hermes_session_pkce"].split(";")
        if "=" in seg
    )["state"]
    verifier = dict(
        seg.split("=", 1)
        for seg in ls.cookie_payload["hermes_session_pkce"].split(";")
        if "=" in seg
    )["verifier"]
    session = stub.complete_login(
        code="stub_code",
        state=state,
        code_verifier=verifier,
        redirect_uri="https://fly-app.fly.dev/auth/callback",
    )
    return session.access_token


@pytest.fixture
def _gated_state():
    """Bare gated app-state setup WITHOUT registering any provider, so each
    test controls provider registration order itself. Yields a factory that
    builds the TestClient after providers are registered."""
    clear_providers()
    prev_host = getattr(web_server.app.state, "bound_host", None)
    prev_port = getattr(web_server.app.state, "bound_port", None)
    prev_required = getattr(web_server.app.state, "auth_required", None)
    web_server.app.state.bound_host = "fly-app.fly.dev"
    web_server.app.state.bound_port = 443
    web_server.app.state.auth_required = True

    def _client() -> TestClient:
        return TestClient(web_server.app, base_url="https://fly-app.fly.dev")

    yield _client
    clear_providers()
    web_server.app.state.bound_host = prev_host
    web_server.app.state.bound_port = prev_port
    web_server.app.state.auth_required = prev_required




def test_all_providers_unreachable_returns_503(_gated_state):
    """If NO provider can verify the token AND at least one was unreachable,
    surface 503 (transient outage) rather than forcing a needless re-login."""
    register_provider(_UnreachableProvider())
    client = _gated_state()
    # Any non-empty cookie — the unreachable provider raises before parsing.
    client.cookies.set(SESSION_AT_COOKIE, "some-opaque-token")
    r = client.get("/api/auth/me")
    assert r.status_code == 503
    assert "unreachable" in r.text.lower()


