from __future__ import annotations

import json
import os
import sys
from typing import Any

from hermes_cli.operator_verification import (
    OperatorVerificationResult,
    cache_operator_verification,
    run_operator_verifier,
)

ENV_OPERATOR_SESSION_CONTEXT = "HERMES_OPERATOR_SESSION_CONTEXT"


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
    }
    payload = {k: context[k] for k in allowed if k in context}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


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
    if payload.get("mode") != "operator" or not payload.get("actor_id"):
        return None
    if payload.get("memory_scope") != "operator":
        return None
    return payload


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

    cache_operator_verification(result, session_id=session_id)
    context = build_operator_session_context(result, acting_for=acting_for)
    os.environ[ENV_OPERATOR_SESSION_CONTEXT] = serialize_operator_session_context(context)
    if not quiet:
        print(
            f"Operator session verified: {context['actor_id']} ({context['role']})",
            file=sys.stderr,
        )
    return context
