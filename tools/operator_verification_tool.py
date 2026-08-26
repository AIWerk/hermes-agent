from __future__ import annotations

import json
from typing import Any

from hermes_cli.operator_verification import (
    cache_operator_verification,
    current_operator_verification_subject,
    get_cached_operator_verification,
    load_operator_verification_config,
    run_operator_verifier,
)
from tools.registry import registry, tool_result


def verify_operator_identity(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    """Verify the local human operator without exposing the secret to the model."""
    args = args or {}
    requested_role = str(args.get("requested_role") or kwargs.get("requested_role") or "").strip().lower()
    subject = current_operator_verification_subject(requested_role)
    if subject is None:
        return tool_result(success=False, verified=False, reason="trusted_subject_unavailable")
    cached = get_cached_operator_verification(**subject)
    if cached is not None:
        if requested_role and str(cached.role).strip().lower() != requested_role:
            return tool_result(success=False, verified=False, reason="requested_role_not_granted")
        return tool_result(
            success=True,
            verified=True,
            actor_id=cached.actor_id,
            role=cached.role,
            expires_at=cached.expires_at,
            cached=True,
        )

    cfg = load_operator_verification_config()
    result = run_operator_verifier(cfg, subject=subject)
    if requested_role and str(result.role).strip().lower() != requested_role:
        return tool_result(success=False, verified=False, reason="requested_role_not_granted")
    if result.is_valid(**subject):
        cache_operator_verification(result)
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
                "enum": ["operator", "admin"],
                "description": "Mandatory role required for the sensitive action.",
            },
        },
        "required": ["requested_role"],
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
