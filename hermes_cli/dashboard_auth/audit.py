"""Audit log for dashboard-auth events: ``$HERMES_HOME/logs/dashboard-auth.log``, one JSON object
per line. Token-like fields are stripped before serialisation so refresh tokens / JWTs never
reach disk. Minimal import surface (no ``hermes_constants`` at import time) so early-loading
middleware can import it."""
from __future__ import annotations

import datetime as _dt
import enum
import json
import logging
import re
import threading
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)
_write_lock = threading.Lock()

# Field names that must never appear in the log raw; matching kwargs are dropped.
_REDACTED_FIELDS: frozenset = frozenset({
    "access_token", "refresh_token", "code", "code_verifier",
    "state", "ticket", "cookie", "Authorization", "authorization"})


class AuditEvent(enum.Enum):
    """Event types; values are the literal ``event`` field on the JSON line."""
    DELEGATED_SESSION_READ = "delegated_session_read"
    BEHAVIOR_POLICY = "behavior_policy"
    LOGIN_START = "login_start"
    LOGIN_SUCCESS = "login_success"
    LOGIN_FAILURE = "login_failure"
    LOGOUT = "logout"
    REFRESH_SUCCESS = "refresh_success"
    REFRESH_FAILURE = "refresh_failure"
    REVOKE = "revoke"
    SESSION_VERIFY_FAILURE = "session_verify_failure"
    WS_TICKET_MINTED = "ws_ticket_minted"
    WS_TICKET_REJECTED = "ws_ticket_rejected"
    TOKEN_AUTH_SUCCESS = "token_auth_success"
    TOKEN_AUTH_FAILURE = "token_auth_failure"
    # RFC 8252 native-app (system-browser + loopback + PKCE) flow.
    NATIVE_AUTHORIZE_START = "native_authorize_start"
    NATIVE_CODE_ISSUED = "native_code_issued"
    NATIVE_TOKEN_SUCCESS = "native_token_success"
    NATIVE_TOKEN_FAILURE = "native_token_failure"


def _bounded_verified_identity(actor):
    from collections.abc import Mapping
    from hermes_cli.dashboard_auth.identity import is_complete_authenticated_identity

    if not is_complete_authenticated_identity(actor):
        return "", ""
    get = actor.get if isinstance(actor, Mapping) else lambda key: getattr(actor, key, None)
    actor_id, tenant_id = get("actor_id"), get("tenant_id")
    pattern = r"[A-Za-z0-9][A-Za-z0-9._:@|/-]{0,255}"
    if (not isinstance(actor_id, str) or not isinstance(tenant_id, str)
            or actor_id != actor_id.strip() or tenant_id != tenant_id.strip()
            or re.fullmatch(pattern, actor_id) is None
            or re.fullmatch(pattern, tenant_id) is None):
        return "", ""
    return actor_id, tenant_id


def delegated_session_read(actor, target, action, decision, result, count, *, correlation_id):
    """Emit only policy-recognized identity/target values, never request text."""
    import uuid
    from hermes_cli.dashboard_auth import profile_access

    authority = decision
    snapshot = decision._policy if decision is not None else None
    if authority is None:
        try:
            snapshot, authority = profile_access._snapshot(actor)
        except profile_access.ProfileAccessDenied:
            pass
    safe_target = ""
    if (snapshot is not None and profile_access.valid_profile(target)
            and target in snapshot._profiles
            and authority is not None
            and snapshot._profiles[target].tenant_id == authority.tenant_id):
        safe_target = target
    actor_id, tenant_id = _bounded_verified_identity(authority if authority is not None else actor)
    audit_log(
        AuditEvent.DELEGATED_SESSION_READ,
        event_id=uuid.uuid4().hex,
        actor_id=actor_id,
        tenant_id=tenant_id,
        target_profile=safe_target,
        action=action if action in profile_access.READ_ACTIONS else "",
        policy_revision=snapshot.revision if snapshot is not None else "unavailable",
        result=result, count=count, correlation_id=correlation_id,
    )


def _resolve_log_path() -> Path:
    """Lazy leaf import: honours profile overrides + the native-Windows ``%LOCALAPPDATA%`` fallback."""
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "logs" / "dashboard-auth.log"


def audit_log(event: AuditEvent, **fields: Any) -> None:
    """Append one event; token-like fields dropped, log dir created. Write failures are logged at
    WARNING but never raise — auth must not fail because the audit logger broke."""
    if event is AuditEvent.DELEGATED_SESSION_READ:
        allowed = {"event_id", "actor_id", "tenant_id", "target_profile", "action",
                   "policy_revision", "result", "count", "correlation_id"}
        fields = {key: value for key, value in fields.items() if key in allowed}
    elif event is AuditEvent.BEHAVIOR_POLICY:
        allowed = {"event_id", "correlation_id", "actor_id", "tenant_id",
                   "target_profile", "operation", "authorization_revision",
                   "base_revision", "result_revision", "content_digest", "result",
                   "byte_count"}
        fields = {key: value for key, value in fields.items() if key in allowed}
    entry = {
        "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "event": event.value,
        **{k: v for k, v in fields.items() if k not in _REDACTED_FIELDS}}
    line = json.dumps(entry, separators=(",", ":")) + "\n"
    path = _resolve_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _write_lock, open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception as e:
        _log.warning("dashboard-auth audit log write failed: %s", e)


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.
import os  # noqa: F401,E402
# ---- END PLUGIN-COMPAT ----
