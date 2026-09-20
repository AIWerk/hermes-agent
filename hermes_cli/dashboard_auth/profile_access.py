"""Fresh action authorization; no profile filesystem or ambient actor lookup."""
from __future__ import annotations

import re
from contextvars import ContextVar

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import SimpleNamespace

from hermes_cli.dashboard_auth import profile_policy as policy
from hermes_cli.dashboard_auth.profile_policy import ProfileAccessDenied
from hermes_cli.dashboard_auth.public_paths import PUBLIC_API_PATHS

http_decision: ContextVar["ProfileDecision | None"] = ContextVar("http_profile_decision", default=None)

READ_ACTIONS = frozenset({"session.list", "session.search", "session.read"})
_DENIED = "profile access denied"


def valid_profile(value):
    return (type(value) is str and value not in {"current", "all"}
            and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", value) is not None)


def _session(actor):
    if isinstance(actor, Mapping):
        return SimpleNamespace(**{key: actor.get(key) for key in (
            "actor_id", "tenant_id", "role", "provider", "expires_at", "_restricted")})
    return actor


def _snapshot(actor):
    try:
        marker = policy._read_trusted_file(
            policy.REQUIRED_MARKER_PATH, max_bytes=policy.MAX_MARKER_BYTES, missing_ok=True)
        if marker is None:
            return None, None
        if marker != policy._REQUIRED_MARKER:
            raise ProfileAccessDenied(_DENIED)
        snapshot = policy.load_profile_policy()
        session = _session(actor)
        if getattr(session, "_restricted", False):
            raise ProfileAccessDenied(_DENIED)
        authority = snapshot.authority_for(session)
        if not valid_profile(authority.default_profile):
            raise ProfileAccessDenied(_DENIED)
        return snapshot, authority
    except (policy.ProfilePolicyError, TypeError, ValueError, AttributeError):
        raise ProfileAccessDenied(_DENIED) from None


@dataclass(frozen=True, slots=True)
class ProfileDecision:
    tenant_id: str
    actor_id: str
    role: str
    default_profile: str
    target_profile: str
    action: str
    policy_revision: str
    auth_provider: str
    auth_expires_at: int
    _policy: policy.ProfilePolicy = field(repr=False, compare=False)


def _allowed(snapshot, authority, action, target, delegated):
    if not snapshot.allows(authority.tenant_id, authority.actor_id, target, action):
        return False
    if delegated:
        return authority.role == "admin" and action in READ_ACTIONS
    # Wildcard read grants are usable only at the dedicated broker, never runtime.
    return action in snapshot._grants.get(
        (authority.tenant_id, authority.actor_id, target), frozenset())


def authorize(actor, action, target=None, delegated=False, live_profile=None):
    snapshot, authority = _snapshot(actor)
    if snapshot is None or authority is None:
        return None
    if (type(action) is not str or action not in policy.PROFILE_ACTIONS
            or type(delegated) is not bool
            or ((delegated or action.startswith("behavior.")) and target is None)):
        raise ProfileAccessDenied(_DENIED)
    target = authority.default_profile if target is None else target
    if (not valid_profile(target)
            or (live_profile is not None and
                (not valid_profile(live_profile) or live_profile != target))
            or not _allowed(snapshot, authority, action, target, delegated)):
        raise ProfileAccessDenied(_DENIED)
    return ProfileDecision(
        authority.tenant_id, authority.actor_id, authority.role,
        authority.default_profile, target, action, snapshot.revision,
        authority.auth_provider, authority.auth_expires_at, snapshot)



# Ordered transport contract, independent of endpoint function names and mounts.
_HTTP_ACTIONS = (
    ("GET", r"/api/sessions(?:/empty/count|/stats)?", "session.list"),
    ("GET", r"/api/sessions/search", "session.search"),
    ("GET", r"/api/sessions/[^/]+/export", "session.export"),
    ("GET", r"/api/sessions/[^/]+(?:/messages|/latest-descendant)?", "session.read"),
    ("POST|PATCH|DELETE", r"/api/sessions(?:/.*)?", "session.mutate"),
    ("GET", r"/api/profiles/sessions(?:/sidebar)?", "session.list"),
    ("GET", r"/api/profiles/projects/tree", "session.list"),
    ("POST", r"/api/profiles/sessions/pull-requests", "session.read"),
    ("GET", r"/api/profiles(?:/active)?", "profile.discover"),
    ("POST", r"/api/profiles/[^/]+/open-terminal", "profile.launch"),
    ("GET", r"/api/profiles/[^/]+/(?:soul|desktop-overlay)", "profile.use"),
    ("GET|POST|PATCH|PUT|DELETE", r"/api/profiles(?:/.*)?", "profile.admin"),
    ("GET|POST|PATCH|PUT|DELETE", r"/api/(?:config|env|mcp|model|skills|tools)(?:/.*)?", "profile.admin"),
)


async def http_authorize(request, call_next):
    """Authorize after identity verification and before every resource consumer."""
    from fastapi.responses import JSONResponse
    from urllib.parse import urlencode

    path = request.url.path.rstrip("/")
    if path in PUBLIC_API_PATHS:
        return await call_next(request)
    # Broker owns its denial audit and never enters the normal profile context.
    if path.startswith("/api/admin/session-reader/"):
        return await call_next(request)
    if not path.startswith("/api/") or path.startswith("/api/auth/"):
        return await call_next(request)
    actor = getattr(request.state, "session", None)
    try:
        snapshot, _ = _snapshot(actor)
        if snapshot is None:
            return await call_next(request)
        action = next((action for methods, pattern, action in _HTTP_ACTIONS
                       if re.fullmatch(methods, request.method) and re.fullmatch(pattern, path)),
                      "profile.admin")
        targets = request.query_params.getlist("profile")
        targets += request.query_params.getlist("recents_profile")
        body = {}
        if request.method in {"POST", "PATCH", "PUT", "DELETE"}:
            try:
                body = await request.json()
            except ValueError:
                body = {}
            if isinstance(body, dict):
                if "profile" in body:
                    targets.append(body["profile"])
                if path in {"/api/profiles", "/api/profiles/active", "/api/profiles/import"} and "name" in body:
                    targets.append(body["name"])
        match = re.fullmatch(r"/api/profiles/([^/]+)(?:/.*)?", path)
        if match and match[1] not in {"sessions", "projects", "active", "import"}:
            targets.append(match[1])
        if any(not valid_profile(t) for t in targets) or any(t != targets[0] for t in targets):
            raise ProfileAccessDenied(_DENIED)
        decision = authorize(actor, action, targets[0] if targets else None)
        if decision is None:
            raise ProfileAccessDenied(_DENIED)
        if isinstance(body, dict):
            for key in ("clone_from", "new_name"):
                if key in body:
                    authorize(actor, "profile.admin", body[key])
        # These legacy whole-store/host operations have no actor-safe execution contract.
        if path in {"/api/sessions/prune", "/api/sessions/owner-backfill",
                    "/api/profiles/active", "/api/profiles/sessions/pull-requests"} and request.method != "GET":
            raise ProfileAccessDenied(_DENIED)
        if path == "/api/profiles" and request.method == "POST":
            # Provisioning includes implicit clone/default sources and host aliases.
            raise ProfileAccessDenied(_DENIED)
        if path.endswith("/open-terminal"):
            authorize(actor, "profile.admin", decision.target_profile)
        if path.startswith("/api/profiles/") and path.endswith("/export"):
            authorize(actor, "session.export", decision.target_profile)
        supported_config = re.fullmatch(r"/api/(?:config|env|mcp|model|skills|tools)(?:/.*)?", path)
        if action == "profile.admin" and not path.startswith("/api/profiles") and not supported_config:
            raise ProfileAccessDenied(_DENIED)
        query = [(k, v) for k, v in request.query_params.multi_items() if k != "profile"]
        query.append(("profile", decision.target_profile))
        request.scope["query_string"] = urlencode(query).encode()
        request.state.profile_decision = decision
    except ProfileAccessDenied:
        return JSONResponse({"detail": _DENIED}, status_code=403)
    token = http_decision.set(decision)
    try:
        if supported_config:
            from hermes_cli.profiles import get_profile_dir
            from hermes_cli.web_server_profiles import _hermes_home_scope
            with _hermes_home_scope(get_profile_dir(decision.target_profile)):
                return await call_next(request)
        return await call_next(request)
    finally:
        http_decision.reset(token)


def recheck_http_decision():
    """Revalidate bound authority at delayed DB/stream execution, never from env."""
    decision = http_decision.get()
    if decision is None:
        return None
    actor = {"tenant_id": decision.tenant_id, "actor_id": decision.actor_id,
             "role": decision.role, "provider": decision.auth_provider,
             "expires_at": decision.auth_expires_at}
    from fastapi import HTTPException
    try:
        current = authorize(actor, decision.action, decision.target_profile)
        if current is None:
            raise ProfileAccessDenied(_DENIED)
        return current
    except ProfileAccessDenied:
        raise HTTPException(403, _DENIED) from None


async def deny_unsafe_socket(ws):
    """Legacy raw sockets cannot enforce actor-scoped operations."""
    try:
        snapshot, _ = _snapshot(None)
        if snapshot is None:
            return False
    except ProfileAccessDenied:
        pass
    await ws.close(code=4403, reason=_DENIED)
    return True


def authorized_profiles(actor, action):
    snapshot, authority = _snapshot(actor)
    if snapshot is None or authority is None:
        return None
    if type(action) is not str or action not in policy.PROFILE_ACTIONS:
        raise ProfileAccessDenied(_DENIED)
    return tuple(sorted(target for target in snapshot._profiles
                        if valid_profile(target) and
                        _allowed(snapshot, authority, action, target, False)))
