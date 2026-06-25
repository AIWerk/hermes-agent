from __future__ import annotations

import json
from typing import Any

from hermes_cli.operator_verification import (
    cache_operator_verification,
    get_cached_operator_verification,
    load_operator_verification_config,
    run_operator_verifier,
)
from tools.registry import registry, tool_result


def verify_operator_identity(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    """Verify the local human operator without exposing the secret to the model."""
    args = args or {}
    session_id = str(args.get("session_id") or kwargs.get("session_id") or "") or None
    cached = get_cached_operator_verification(session_id=session_id)
    if cached is not None:
        return tool_result(
            success=True,
            verified=True,
            actor_id=cached.actor_id,
            role=cached.role,
            expires_at=cached.expires_at,
            cached=True,
        )

    cfg = load_operator_verification_config()
    result = run_operator_verifier(cfg)
    if result.is_valid():
        cache_operator_verification(result, session_id=session_id)
        return tool_result(
            success=True,
            verified=True,
            actor_id=result.actor_id,
            role=result.role,
            expires_at=result.expires_at,
            cached=False,
            interface=cfg.interface,
        )

    return tool_result(
        success=False,
        verified=False,
        reason=result.reason or "verification_failed",
    )


_OPERATOR_VERIFY_SCHEMA = {
    "name": "verify_operator_identity",
    "description": (
        "Verify local operator identity for sensitive CLI/TUI/admin actions. "
        "Use this instead of asking the user to paste a secret into chat. "
        "The verifier handles the secret out-of-band and returns only a "
        "sanitized verification result."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "Short human-readable reason for the verification request.",
            },
            "requested_role": {
                "type": "string",
                "description": "Optional role being requested, e.g. operator or admin.",
            },
        },
        "additionalProperties": False,
    },
}


def check_operator_verification_requirements() -> bool:
    cfg = load_operator_verification_config()
    return bool(cfg.enabled)


registry.register(
    name="verify_operator_identity",
    toolset="security",
    schema=_OPERATOR_VERIFY_SCHEMA,
    handler=verify_operator_identity,
    check_fn=check_operator_verification_requirements,
    description="Verify local operator identity without exposing secrets",
    emoji="🔐",
)
