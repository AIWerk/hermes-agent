"""HTTP routes for the dashboard-auth OAuth round trip.

Mounted at root (no prefix) by ``web_server.py``; ``gated_auth_middleware``
allowlists the public ones.

  GET  /login                  server-rendered login page
  GET  /auth/login?provider=N  302 to IDP, sets PKCE cookie
  GET  /auth/native/authorize  RFC 8252 native-app (desktop) login start
  GET  /auth/callback          completes login, sets session cookies
  POST /auth/password-login    username/password login (JSON)
  POST /auth/logout            clears cookies, best-effort revoke
  POST /auth/native/token      loopback code -> bearer tokens
  POST /auth/native/refresh    desktop-held refresh token rotation
  GET  /api/auth/providers     list registered providers (login bootstrap)
  GET  /api/auth/me            current Session as JSON (auth-required)
  POST /api/auth/ws-ticket     single-use WS upgrade ticket (auth-required)
"""
from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict, deque
from typing import Any, Deque, Dict
from urllib.parse import quote, unquote, urlencode, urlparse, urlunparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from hermes_cli.dashboard_auth import (
    get_provider, list_providers, list_session_providers, native_flow)
from hermes_cli.dashboard_auth import prefix as _prefix_mod
from hermes_cli.dashboard_auth.audit import AuditEvent, audit_log
from hermes_cli.dashboard_auth.base import (
    InvalidCodeError, InvalidCredentialsError, ProviderError, RefreshExpiredError, Session)
from hermes_cli.dashboard_auth.cookies import (
    clear_pkce_cookie, clear_session_cookies, clear_sso_attempt_cookie, detect_https,
    parse_pkce_payload, read_pkce_cookie, read_session_cookies, set_pkce_cookie,
    set_session_cookies)
from hermes_cli.dashboard_auth.login_page import render_login_html
from hermes_cli.dashboard_auth.identity import (
    greeting_identity_from_session,
    is_complete_authenticated_identity,
)
from hermes_cli.dashboard_auth.request_utils import (
    access_token_max_age,
    client_ip as _client_ip,
    is_safe_next_path,
    scan_session_providers,
)

_log = logging.getLogger(__name__)

router = APIRouter()

_NO_STORE = {"Cache-Control": "no-store, no-cache, must-revalidate"}
_NATIVE_EXPIRED_DETAIL = "Native login expired or unknown; restart sign-in."

def _assistant_mode_request(request: Request) -> bool:
    app = getattr(request, "app", None)
    state = getattr(app, "state", None)
    return getattr(state, "dashboard_mode", "admin") == "assistant"


def _require_assistant_identity(request: Request, session: object) -> None:
    if _assistant_mode_request(request) and not is_complete_authenticated_identity(session):
        raise HTTPException(status_code=403, detail="Complete authenticated identity required")


def _http(status_code: int, detail: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail=detail)


def _prefix(request: Request) -> str:
    """Normalised ``X-Forwarded-Prefix`` (cookie name/Path + redirect URLs)."""
    return _prefix_mod.prefix_from_request(request)


def _audit(request: Request, event: AuditEvent, **fields) -> None:
    audit_log(event, **fields, ip=_client_ip(request))


def _redirect_uri(request: Request) -> str:
    """Absolute ``/auth/callback`` URL handed to the IDP. An operator-declared public URL is the
    complete authority (``X-Forwarded-Prefix`` ignored so a baked-in prefix is not doubled);
    otherwise ``url_for`` (honours ``X-Forwarded-Host/Proto`` under uvicorn ``proxy_headers``)
    with the prefix prepended, which Starlette does not do."""
    public_url = _prefix_mod.resolve_public_url()
    if public_url:
        return f"{public_url}/auth/callback"
    base = str(request.url_for("auth_callback"))
    prefix = _prefix(request)
    if not prefix:
        return base
    parsed = urlparse(base)
    return urlunparse(parsed._replace(path=f"{prefix}{parsed.path}"))


def _provider_pkce_segments(cookie_payload: dict[str, str]) -> dict[str, str]:
    """Parse a provider's flat ``state=…;verifier=…`` PKCE string into a dict — the ONE place
    the flat form is parsed; :func:`set_pkce_cookie` encodes the dict."""
    flat = cookie_payload.get("hermes_session_pkce", "")
    return dict(seg.split("=", 1) for seg in flat.split(";") if "=" in seg)


def _validate_post_login_target(raw: str) -> str:
    """``raw`` (URL-decoded) if it is a safe same-origin path, else ``""``. Re-validated
    at every hop because a ``next=`` value can re-enter via a crafted URL."""
    decoded = unquote(raw) if raw else ""
    return decoded if decoded and is_safe_next_path(decoded) else ""


def _set_pkce(resp, request: Request, payload: dict[str, str]) -> None:
    set_pkce_cookie(resp, payload=payload, use_https=detect_https(request), prefix=_prefix(request))


def _set_session(resp, request: Request, session: Session) -> None:
    set_session_cookies(
        resp, access_token=session.access_token, refresh_token=session.refresh_token,
        access_token_expires_in=access_token_max_age(session), use_https=detect_https(request),
        prefix=_prefix(request), provider=session.provider)


def _bearer_payload(session: Session) -> dict[str, Any]:
    """JSON body for the native token/refresh endpoints (tokens in body, no cookie)."""
    return {
        "access_token": session.access_token, "refresh_token": session.refresh_token,
        "token_type": "Bearer", "expires_at": session.expires_at,
        "provider": session.provider, "user_id": session.user_id}


def _finish_native_login(
    request: Request, *, broker_state: str, session: Session, provider: str) -> str:
    """Mint the one-time loopback code and return the desktop's ``redirect_uri?code=…&state=…``.
    No session cookies on the native path — the desktop redeems at ``/auth/native/token``."""
    try:
        pending = native_flow.get_pending(broker_state)
        gw_code = native_flow.complete_pending(broker_state, session=session)
    except native_flow.NativeFlowError:
        _audit(request, AuditEvent.NATIVE_TOKEN_FAILURE, provider=provider,
               reason="pending_not_found")
        raise _http(400, _NATIVE_EXPIRED_DETAIL)
    sep = "&" if "?" in pending.redirect_uri else "?"
    query = urlencode({'code': gw_code, 'state': pending.client_state})
    _audit(request, AuditEvent.NATIVE_CODE_ISSUED, provider=provider, user_id=session.user_id)
    return f"{pending.redirect_uri}{sep}{query}"


def _login_failure(request: Request, provider: str, reason: str, **extra) -> None:
    _audit(request, AuditEvent.LOGIN_FAILURE, provider=provider, reason=reason, **extra)


def _login_success(request: Request, session: Session, provider: str) -> None:
    _audit(request, AuditEvent.LOGIN_SUCCESS, provider=provider, user_id=session.user_id,
           email=session.email, org_id=session.org_id)


def _complete_login(request: Request, provider: str, session: Session, *, broker_state: str,
                    next_raw: str) -> tuple:
    """Shared tail of the callback + password routes after credentials verified: audit success,
    then either the native loopback redirect (no cookies) or the landing path. Returns
    ``(target_url, native)``."""
    _login_success(request, session, provider)
    if broker_state:
        return _finish_native_login(
            request, broker_state=broker_state, session=session, provider=provider), True
    return _validate_post_login_target(next_raw) or "/", False


def _start_upstream_login(request: Request, p, *, audit_failure: bool, extra_pkce: dict[str, str]):
    """Run ``start_login`` and 302 to the IDP with the PKCE cookie set. That cookie is the only
    server-controlled channel surviving the round trip (IDPs echo back only code+state), so it
    carries the provider name plus ``extra_pkce``."""
    try:
        ls = p.start_login(redirect_uri=_redirect_uri(request))
    except ProviderError as e:
        if audit_failure:
            _login_failure(request, p.name, "provider_unreachable")
        raise _http(503, f"Provider unreachable: {e}")
    resp = RedirectResponse(url=ls.redirect_url, status_code=302)
    pkce = _provider_pkce_segments(ls.cookie_payload)
    pkce.setdefault("provider", p.name)
    pkce.update(extra_pkce)
    _set_pkce(resp, request, pkce)
    return resp


# --- Public: login page + provider list ------------------------------------

@router.get("/login", name="login_page")
async def login_page(request: Request) -> HTMLResponse:
    # ``next=`` is set by the gate's redirect but /login is reachable directly.
    next_path = _validate_post_login_target(request.query_params.get("next", ""))
    return HTMLResponse(render_login_html(next_path=next_path), headers=_NO_STORE)


@router.get("/api/auth/providers", name="auth_providers")
async def api_auth_providers() -> Any:
    # Only interactive providers are sign-in options; fail closed on zero.
    providers = list_session_providers()
    if not providers:
        return JSONResponse({"detail": "no auth providers registered"}, status_code=503)
    return {"providers": [
        {"name": p.name, "display_name": p.display_name,
         "supports_password": bool(getattr(p, "supports_password", False))}
        for p in providers]}


# --- Public: OAuth round trip ----------------------------------------------

@router.get("/auth/login", name="auth_login")
async def auth_login(request: Request, provider: str, next: str = ""):
    p = get_provider(provider)
    if p is None:
        raise _http(404, f"Unknown provider: {provider!r}")
    if not getattr(p, "supports_session", True):
        raise _http(404, f"Provider does not support interactive login: {provider!r}")
    safe_next = _validate_post_login_target(next)
    if getattr(p, "supports_password", False):
        login_url = f"{_prefix(request)}/login"
        if safe_next:
            login_url = f"{login_url}?next={quote(safe_next, safe='')}"
        return RedirectResponse(url=login_url, status_code=302)
    resp = _start_upstream_login(
        request, p, audit_failure=True, extra_pkce={"next": safe_next} if safe_next else {})
    _audit(request, AuditEvent.LOGIN_START, provider=provider)
    return resp


# --- Public: RFC 8252 native-app authorization (system browser + loopback + PKCE)

def _validate_loopback_redirect_uri(raw: str) -> str:
    """Accept only ``http://127.0.0.1[:port]/…`` / ``http://[::1][:port]/…``. Security boundary:
    the route is public, so a non-loopback host would make the callback an open redirect leaking
    a live code. ``localhost`` is rejected (RFC 8252 §8.3)."""
    if not raw:
        raise _http(400, "redirect_uri required")
    parsed = urlparse(raw)
    if parsed.scheme != "http":
        raise _http(400, "native redirect_uri must be http:// on the loopback interface")
    if (parsed.hostname or "").lower() not in ("127.0.0.1", "::1"):
        raise _http(400, "native redirect_uri host must be a loopback IP literal (127.0.0.1 / ::1)")
    return raw


def _select_native_provider(provider: str):
    """Resolve the provider for a native authorize request. An empty ``provider`` auto-selects
    the single brokerable (non-password) session provider — so an OIDC+basic deployment does not
    fail with "Unknown provider"; with none, a lone password provider is still returned so the
    caller emits a 400 rather than 404."""
    if provider:
        return get_provider(provider)
    sess_providers = list_session_providers()
    native_eligible = [pp for pp in sess_providers if not getattr(pp, "supports_password", False)]
    candidates = native_eligible or sess_providers
    return candidates[0] if len(candidates) == 1 else None


@router.get("/auth/native/authorize", name="auth_native_authorize")
async def auth_native_authorize(
    request: Request, provider: str = "", code_challenge: str = "",
    code_challenge_method: str = "", redirect_uri: str = "", state: str = ""):
    """Begin an RFC 8252 native-app login: stash a pending broker authorization keyed by an
    opaque ``broker_state`` riding in the gateway's own PKCE cookie (the desktop's
    challenge/state never touch it), then run the normal upstream round trip. Password providers
    go to the ``/login`` form instead."""
    if code_challenge_method.upper() != "S256":
        raise _http(400, "code_challenge_method must be S256")
    if not code_challenge:
        raise _http(400, "code_challenge required")
    _validate_loopback_redirect_uri(redirect_uri)
    p = _select_native_provider(provider)
    if p is None:
        raise _http(404, f"Unknown provider: {provider!r}")
    if not getattr(p, "supports_session", True):
        raise _http(400, f"Provider does not support native login: {p.name!r}")
    try:
        broker_state = native_flow.register_pending(
            code_challenge=code_challenge, redirect_uri=redirect_uri, client_state=state,
            client_ip=_client_ip(request))
    except native_flow.NativeFlowError as e:
        raise _http(503, str(e))
    if getattr(p, "supports_password", False):
        _audit(request, AuditEvent.NATIVE_AUTHORIZE_START, provider=p.name)
        resp = RedirectResponse(url=f"{_prefix(request)}/login", status_code=302)
        _set_pkce(resp, request, {"provider": p.name, "broker": broker_state})
        return resp
    resp = _start_upstream_login(
        request, p, audit_failure=False, extra_pkce={"broker": broker_state})
    _audit(request, AuditEvent.NATIVE_AUTHORIZE_START, provider=p.name)
    return resp


@router.get("/auth/callback", name="auth_callback")
async def auth_callback(
    request: Request, code: str = "", state: str = "", error: str = "",
    error_description: str = ""):
    pkce_raw = read_pkce_cookie(request)
    if not pkce_raw:
        _audit(request, AuditEvent.LOGIN_FAILURE, reason="missing_pkce_cookie")
        raise _http(400, "Missing PKCE state cookie")
    # ``next`` and ``broker`` come from the server-set cookie ONLY: the IDP
    # echoes back just code+state, so any such query param is attacker controlled.
    parts = parse_pkce_payload(pkce_raw)
    provider_name = parts.get("provider", "")
    p = get_provider(provider_name)
    if p is None:
        raise _http(400, f"Unknown provider in cookie: {provider_name!r}")
    if error:
        _login_failure(request, provider_name, "idp_error", error=error)
        raise _http(400, f"OAuth error from provider: {error} ({error_description})")
    if not state or state != parts.get("state", ""):
        _login_failure(request, provider_name, "state_mismatch")
        raise _http(400, "OAuth state mismatch (CSRF check failed)")
    try:
        session = p.complete_login(
            code=code, state=state, code_verifier=parts.get("verifier", ""),
            redirect_uri=_redirect_uri(request))
    except InvalidCodeError as e:
        _login_failure(request, provider_name, "invalid_code")
        raise _http(400, f"Invalid code: {e}")
    except ProviderError as e:
        _login_failure(request, provider_name, "provider_unreachable")
        raise _http(503, f"Provider unreachable: {e}")
    target, native = _complete_login(
        request, provider_name, session, broker_state=parts.get("broker", ""),
        next_raw=parts.get("next", ""))
    resp = RedirectResponse(url=target, status_code=302)
    if not native:
        _set_session(resp, request, session)
    prefix = _prefix(request)
    clear_pkce_cookie(resp, use_https=detect_https(request), prefix=prefix)
    # Clear the one-shot auto-SSO loop-guard so it never suppresses a future silent attempt.
    clear_sso_attempt_cookie(resp, prefix=prefix)
    return resp


def _validate_post_login_target(raw: str) -> str:
    """Return ``raw`` if it's a safe same-origin path, else empty string.

    The ``next`` query param survives a full OAuth round trip — the gate
    encodes it into the /login redirect, the login page emits it back into
    /auth/login, and the IDP preserves it across /authorize/callback. We
    have to re-validate here because the value came back in via the
    URL (an attacker could craft a /auth/callback URL with their own
    ``next=https://evil.example``).
    """
    if not raw:
        return ""
    from urllib.parse import unquote
    decoded = unquote(raw)
    if "\\" in decoded:
        return ""
    if not decoded.startswith("/") or decoded.startswith("//"):
        return ""
    # Don't loop back to login pages or auth flow.
    if any(
        decoded == p or decoded.startswith(p)
        for p in ("/login", "/auth/", "/api/auth/")
    ):
        return ""
    # Reject any ``/api/*`` target. The gate's ``_safe_next_target``
    # already filters these out before they reach the cookie, but a
    # malicious or stale ``next=`` value that re-enters via the
    # callback URL must not be honoured: a successful redirect to an
    # API endpoint renders raw JSON in the browser address bar — never
    # a useful post-login destination, and indistinguishable from an
    # attacker trying to weaponise the redirect.
    if decoded == "/api" or decoded.startswith("/api/"):
        return ""
    return decoded


# ---------------------------------------------------------------------------
# Public: password (non-redirect) login
# ---------------------------------------------------------------------------
#
# Brute-force throttle. The OAuth flow has no guessable secret on our side
# (the IDP owns credentials), but ``/auth/password-login`` accepts a
# password we verify locally, so it's a credential-stuffing target. A
# simple in-process sliding-window limiter per client IP raises the cost
# of online guessing without any external dependency. It is intentionally
# best-effort: process-local (resets on restart), and behind a trusting
# proxy the IP is the proxy's unless X-Forwarded-For is set — which is why
# this is defence-in-depth on top of the provider's own constant-time
# verify, not the only line of defence.

_PW_RATE_MAX_ATTEMPTS = 10
_PW_RATE_WINDOW_SEC = 60.0
_pw_attempts: Dict[str, Deque[float]] = defaultdict(deque)
_pw_attempts_lock = threading.Lock()


def _password_rate_limited(ip: str) -> bool:
    """True if ``ip`` exceeded the budget; records the attempt when allowed. An empty IP shares
    one bucket — fail-safe toward throttling."""
    now = time.monotonic()
    cutoff = now - _PW_RATE_WINDOW_SEC
    with _pw_attempts_lock:
        bucket = _pw_attempts[ip or "_unknown_"]
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= _PW_RATE_MAX_ATTEMPTS:
            return True
        bucket.append(now)
        return False


def _reset_password_rate_limit() -> None:
    """Test-only: clear all rate-limit buckets."""
    with _pw_attempts_lock:
        _pw_attempts.clear()


class _PasswordLoginBody(BaseModel):
    provider: str
    username: str
    password: str
    next: str = ""


@router.post("/auth/password-login", name="auth_password_login")
async def auth_password_login(request: Request, body: _PasswordLoginBody):
    """Authenticate a username/password against a password provider.

    Returns ``{"ok": true, "next": <path>}`` (the form POSTs via fetch, which follows a 302
    opaquely) and sets the session cookies; with a native ``broker`` handle in the PKCE cookie,
    ``next`` is the desktop's loopback redirect and NO cookies are set. Failures are deliberately
    generic (no username/provider oracle): unknown/non-password provider 404, bad credentials
    401, store unreachable 503, rate limited 429.
    """
    if _password_rate_limited(_client_ip(request)):
        _login_failure(request, body.provider, "rate_limited")
        raise _http(429, "Too many login attempts. Try again shortly.")
    p = get_provider(body.provider)
    if p is None or not getattr(p, "supports_password", False):
        _login_failure(request, body.provider, "unknown_password_provider")
        raise _http(404, "Unknown provider")
    # The native broker handle also records WHICH provider the flow was started for. Enforce
    # equality BEFORE verifying credentials so a flow started for provider A cannot be completed
    # with provider B's credentials.
    pkce_raw = read_pkce_cookie(request)
    pkce_parts = parse_pkce_payload(pkce_raw) if pkce_raw else {}
    broker_state = pkce_parts.get("broker", "")
    if broker_state and pkce_parts.get("provider", "") != body.provider:
        _audit(request, AuditEvent.NATIVE_TOKEN_FAILURE, provider=body.provider,
               reason="provider_mismatch")
        raise _http(400, "This native sign-in was started for a different provider; "
                         "use that provider's form or restart sign-in.")
    try:
        session = p.complete_password_login(username=body.username, password=body.password)
    except InvalidCredentialsError:
        _login_failure(request, body.provider, "invalid_credentials")
        raise _http(401, "Invalid credentials")
    except NotImplementedError:
        # supports_password True but method not implemented: a provider bug.
        raise _http(500, "Provider misconfigured")
    except ProviderError as e:
        _login_failure(request, body.provider, "provider_unreachable")
        raise _http(503, f"Provider unreachable: {e}")
    target, native = _complete_login(
        request, body.provider, session, broker_state=broker_state, next_raw=body.next)
    resp = JSONResponse({"ok": True, "next": target})
    if native:
        clear_pkce_cookie(resp, use_https=detect_https(request), prefix=_prefix(request))
    else:
        _set_session(resp, request, session)
    return resp


@router.post("/auth/logout", name="auth_logout")
async def auth_logout(request: Request):
    _at, rt = read_session_cookies(request)
    # Best-effort revoke on every provider; failures logged, never raised.
    for provider in list_providers() if rt else ():
        try:
            provider.revoke_session(refresh_token=rt)
        except Exception as e:  # noqa: BLE001 — best-effort
            _log.warning("dashboard-auth: revoke on %r failed: %s", provider.name, e)
    sess = getattr(request.state, "session", None)
    _audit(request, AuditEvent.LOGOUT, provider=(sess.provider if sess else "unknown"),
           user_id=(sess.user_id if sess else ""))
    prefix = _prefix(request)
    resp = RedirectResponse(url=f"{prefix}/login", status_code=302)
    clear_session_cookies(resp, prefix=prefix)
    clear_pkce_cookie(resp, use_https=detect_https(request), prefix=prefix)
    return resp


# --- Auth-required: identity probe + WS ticket for the SPA -----------------

def _require_session(request: Request):
    sess = getattr(request.state, "session", None)
    if sess is None:
        raise _http(401, "Unauthorized")
    return sess


@router.get("/api/auth/me", name="auth_me")
async def api_auth_me(request: Request):
    """Return the verified session as JSON. Auth-required (gate enforces)."""
    sess = getattr(request.state, "session", None)
    if sess is None:
        raise HTTPException(status_code=401, detail="Unauthorized")
    _require_assistant_identity(request, sess)
    tenant_id = getattr(sess, "tenant_id", "") or sess.org_id
    actor_id = getattr(sess, "actor_id", "") or sess.user_id
    role = getattr(sess, "role", "") or "user"
    return JSONResponse(
        {
            "user_id": sess.user_id,
            "email": sess.email,
            "display_name": sess.display_name,
            "org_id": sess.org_id,
            "provider": sess.provider,
            "expires_at": sess.expires_at,
            "tenant_id": tenant_id,
            "actor_id": actor_id,
            "role": role,
            "greeting": greeting_identity_from_session(sess),
        },
        headers={"Cache-Control": "private, no-store"},
    )


# ---------------------------------------------------------------------------
# Auth-required: WS upgrade ticket (Phase 5)
# ---------------------------------------------------------------------------


@router.post("/api/auth/ws-ticket", name="auth_ws_ticket")
async def api_auth_ws_ticket(request: Request):
    """Mint a short-lived single-use ticket for the authenticated session.

    Browsers cannot set ``Authorization`` on a WebSocket upgrade, so in
    gated mode the SPA POSTs this endpoint to get a ``?ticket=`` value to
    append to ``/api/pty``, ``/api/console``, ``/api/ws``, ``/api/pub``, or
    ``/api/events``.

    The ticket has a 30-second TTL and is single-use. Calling this endpoint
    multiple times in quick succession (e.g. one ticket per WS) is the
    expected pattern.
    """
    sess = getattr(request.state, "session", None)
    if sess is None:
        # Middleware should already have rejected, but check defensively.
        raise HTTPException(status_code=401, detail="Unauthorized")
    _require_assistant_identity(request, sess)

    # Import here so the routes module stays usable in test contexts that
    # don't load the ticket store.
    from hermes_cli.dashboard_auth.ws_tickets import TTL_SECONDS, mint_ticket

    ticket = mint_ticket(
        user_id=sess.user_id,
        provider=sess.provider,
        tenant_id=getattr(sess, "tenant_id", "") or sess.org_id,
        actor_id=getattr(sess, "actor_id", "") or sess.user_id,
        role=getattr(sess, "role", "") or "user",
        display_name=sess.display_name,
        email=sess.email,
        org_id=sess.org_id,
    )
    audit_log(
        AuditEvent.WS_TICKET_MINTED,
        provider=sess.provider,
        user_id=sess.user_id,
        ip=_client_ip(request),
    )
    return {"ticket": ticket, "ttl_seconds": TTL_SECONDS}


# --- Public: RFC 8252 native-app token exchange + refresh ------------------

class _NativeTokenBody(BaseModel):
    code: str
    code_verifier: str


@router.post("/auth/native/token", name="auth_native_token")
async def auth_native_token(request: Request, body: _NativeTokenBody):
    """Exchange a loopback gateway code + PKCE verifier for bearer tokens. The code is consumed
    on every path (no verifier oracle, no replay); any failure is a generic 400. Tokens go in
    the JSON body; no cookie is set."""
    try:
        session = native_flow.redeem_code(code=body.code, code_verifier=body.code_verifier)
    except native_flow.CodeInvalid:
        _audit(request, AuditEvent.NATIVE_TOKEN_FAILURE, reason="invalid_code_or_pkce")
        raise _http(400, "Invalid or expired authorization code.")
    _audit(request, AuditEvent.NATIVE_TOKEN_SUCCESS, provider=session.provider,
           user_id=session.user_id)
    return _bearer_payload(session)


class _NativeRefreshBody(BaseModel):
    refresh_token: str
    provider: str = ""


@router.post("/auth/native/refresh", name="auth_native_refresh")
async def auth_native_refresh(request: Request, body: _NativeRefreshBody):
    """Rotate a desktop-held refresh token (mirrors the gate's ``_attempt_refresh``): every
    provider rejecting the RT -> 401 ``session_expired`` (desktop re-logs); none rotated and one
    unreachable -> 503."""
    if not body.refresh_token:
        raise _http(400, "refresh_token required")
    try:
        session = scan_session_providers(
            body.provider, lambda p: p.refresh_session(refresh_token=body.refresh_token),
            phase="native refresh", log=_log, swallow=(RefreshExpiredError,))
    except ProviderError as e:
        raise _http(503, f"Auth provider {str(e)!r} unreachable")
    if session is not None:
        _audit(request, AuditEvent.REFRESH_SUCCESS, provider=session.provider,
               user_id=session.user_id)
        return _bearer_payload(session)
    _audit(request, AuditEvent.REFRESH_FAILURE, reason="all_providers_rejected_rt")
    return JSONResponse(
        {"error": "session_expired",
         "detail": "Refresh token expired or invalid; start a new sign-in."}, status_code=401)
