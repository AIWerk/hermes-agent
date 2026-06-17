from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

from hermes_cli.operator_verification import (
    OperatorVerificationResult,
    cache_operator_verification,
    run_operator_verifier,
)

ENV_OPERATOR_SESSION_CONTEXT = "HERMES_OPERATOR_SESSION_CONTEXT"
_CURRENT_OPERATOR_SESSION_CONTEXT: dict[str, Any] | None = None


def build_operator_session_context(
    result: OperatorVerificationResult,
    *,
    acting_for: str = "aiwerk",
) -> dict[str, Any]:
    """Return the sanitized identity context for a verified operator session."""
    if not result.is_valid():
        raise ValueError(result.reason or "operator verification failed")
    return {
        "mode": "operator",
        "actor_id": result.actor_id,
        "role": result.role,
        "acting_for": acting_for,
        "memory_scope": "operator",
        "verified_at": result.verified_at,
        "expires_at": result.expires_at,
    }


def serialize_operator_session_context(context: dict[str, Any]) -> str:
    allowed = {
        "mode",
        "actor_id",
        "role",
        "acting_for",
        "memory_scope",
        "verified_at",
        "expires_at",
        "bootstrap_pid",
    }
    payload = {k: context[k] for k in allowed if k in context}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _validated_operator_context(payload: dict[str, Any]) -> dict[str, Any] | None:
    if payload.get("mode") != "operator":
        return None
    actor_id = payload.get("actor_id")
    role = payload.get("role")
    if not isinstance(actor_id, str) or not actor_id.strip():
        return None
    if not isinstance(role, str) or not role.strip():
        return None
    if payload.get("memory_scope") != "operator":
        return None
    try:
        raw_verified_at = payload.get("verified_at")
        raw_expires_at = payload.get("expires_at")
        if raw_verified_at is None or raw_expires_at is None:
            return None
        verified_at = int(raw_verified_at)
        expires_at = int(raw_expires_at)
    except (TypeError, ValueError):
        return None
    if verified_at <= 0 or int(time.time()) >= expires_at:
        return None
    acting_for = payload.get("acting_for", "aiwerk")
    if not isinstance(acting_for, str) or not acting_for.strip():
        acting_for = "aiwerk"
    return {
        "mode": "operator",
        "actor_id": actor_id.strip(),
        "role": role.strip(),
        "acting_for": acting_for.strip(),
        "memory_scope": "operator",
        "verified_at": verified_at,
        "expires_at": expires_at,
    }


def get_current_operator_session_context() -> dict[str, Any] | None:
    global _CURRENT_OPERATOR_SESSION_CONTEXT
    if _CURRENT_OPERATOR_SESSION_CONTEXT is None:
        return None
    validated = _validated_operator_context(_CURRENT_OPERATOR_SESSION_CONTEXT)
    if validated is None:
        _CURRENT_OPERATOR_SESSION_CONTEXT = None
        return None
    return validated


def load_operator_session_context_from_env() -> dict[str, Any] | None:
    raw = os.environ.get(ENV_OPERATOR_SESSION_CONTEXT, "").strip()
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("bootstrap_pid") != os.getpid():
        return None
    return _validated_operator_context(payload)


def bootstrap_operator_session(
    *,
    session_id: str | None = None,
    acting_for: str = "aiwerk",
    quiet: bool = False,
) -> dict[str, Any]:
    """Verify the local human before the first turn and export operator context.

    The verifier owns any secret prompt. Hermes records only sanitized identity
    metadata, never the secret or challenge response.
    """
    result = run_operator_verifier()
    if not result.is_valid():
        if not quiet:
            print(
                "Operator verification failed: "
                f"{result.reason or 'verification_failed'}",
                file=sys.stderr,
            )
        raise SystemExit(1)

    global _CURRENT_OPERATOR_SESSION_CONTEXT
    cache_operator_verification(result, session_id=session_id)
    context = build_operator_session_context(result, acting_for=acting_for)
    _CURRENT_OPERATOR_SESSION_CONTEXT = dict(context)
    env_context = {**context, "bootstrap_pid": os.getpid()}
    os.environ[ENV_OPERATOR_SESSION_CONTEXT] = serialize_operator_session_context(env_context)
    if not quiet:
        print(
            f"Operator session verified: {context['actor_id']} ({context['role']})",
            file=sys.stderr,
        )
    return context
