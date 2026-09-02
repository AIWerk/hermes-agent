"""Canonical authenticated actor context for CUI gateway flows."""

from __future__ import annotations

import contextvars
import json
import os
from typing import Any, Mapping

from hermes_cli.dashboard_auth.identity import (
    RUNTIME_MUTATION_ADMIN_ROLE_ALIASES,
    is_authenticated_actor_identity,
    is_complete_authenticated_identity,
    normalize_role,
)

ACTOR_IDENTITY_KEYS = (
    "tenant_id",
    "actor_id",
    "role",
    "display_name",
    "user_id",
    "provider",
)

RESTRICTED_ACTOR_CONTEXT = {"_restricted": "1"}


class _TrustedNoActorContext(dict[str, str]):
    """Falsey provenance marker for the intentional internal no-actor path."""


_actor_context: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "cui_actor_context",
    default=None,
)


def sanitize_cui_actor_context(actor: Mapping[str, Any] | None) -> dict[str, str]:
    """Sanitize authority while distinguishing absent from explicitly invalid."""
    if actor is None or isinstance(actor, _TrustedNoActorContext):
        return _TrustedNoActorContext()
    if not isinstance(actor, Mapping):
        return dict(RESTRICTED_ACTOR_CONTEXT)
    clean: dict[str, str] = {}
    for key in ACTOR_IDENTITY_KEYS:
        value = actor.get(key)
        if value is not None and str(value).strip():
            clean[key] = str(value).strip()
    if is_authenticated_actor_identity(clean):
        return clean
    return dict(RESTRICTED_ACTOR_CONTEXT)


def bind_cui_actor_context(
    actor: Mapping[str, Any] | None,
) -> contextvars.Token[dict[str, str] | None]:
    """Bind sanitized server identity for this logical flow, including empty."""
    return _actor_context.set(sanitize_cui_actor_context(actor))


def reset_cui_actor_context(
    token: contextvars.Token[dict[str, str] | None],
) -> None:
    """Restore the binding captured by *token*."""
    _actor_context.reset(token)


def current_bound_cui_actor_context() -> dict[str, str]:
    """Read only flow-bound server identity; process environment is excluded."""
    bound = _actor_context.get()
    if bound is None:
        return _TrustedNoActorContext()
    if isinstance(bound, _TrustedNoActorContext):
        return _TrustedNoActorContext()
    return dict(bound)


def current_cui_actor_context() -> dict[str, str]:
    """Read flow-local identity, or the subprocess/CLI environment fallback."""
    bound = _actor_context.get()
    if bound is not None:
        if isinstance(bound, _TrustedNoActorContext):
            return _TrustedNoActorContext()
        return dict(bound)

    data: dict[str, Any] = {}
    provided = False
    raw = os.getenv("AIWERK_CUI_ACTOR_CONTEXT", "")
    if raw:
        provided = True
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                data = parsed
        except (TypeError, ValueError):
            pass
    for env_key, actor_key in (
        ("AIWERK_CUI_TENANT_ID", "tenant_id"),
        ("AIWERK_CUI_ACTOR_ID", "actor_id"),
        ("AIWERK_CUI_ACTOR_ROLE", "role"),
    ):
        value = os.getenv(env_key, "")
        if value:
            provided = True
        if value and not data.get(actor_key):
            data[actor_key] = value
    return sanitize_cui_actor_context(data if provided else None)


def _sanitize_actor(data: Mapping[str, Any] | None) -> dict[str, str]:
    """Compatibility name for the canonical fail-closed actor sanitizer."""
    return sanitize_cui_actor_context(data)


def _policy_actor(actor: Mapping[str, Any] | None) -> dict[str, str]:
    return (
        current_cui_actor_context()
        if actor is None
        else sanitize_cui_actor_context(actor)
    )


def is_aiwerk_admin_actor(actor: Mapping[str, Any] | None = None) -> bool:
    """Return true only for complete tenant-bound runtime-admin identities."""
    clean = _policy_actor(actor)
    role = str(clean.get("role") or "").strip().lower()
    return bool(
        is_complete_authenticated_identity(clean)
        and role in RUNTIME_MUTATION_ADMIN_ROLE_ALIASES
    )


def cui_actor_system_prompt(actor: Mapping[str, Any] | None = None) -> str:
    """Return a static prompt boundary without interpolating identity fields."""
    clean = _policy_actor(actor)
    if isinstance(clean, _TrustedNoActorContext) or not clean:
        return ""
    if clean.get("_restricted") == "1" or not is_complete_authenticated_identity(clean):
        return (
            "The current CUI actor identity is restricted or incomplete. "
            "Do not infer customer identity, expose tenant data, or write customer memory."
        )
    role = normalize_role(clean.get("role"))
    if role == "customer":
        return (
            "The current human is the authenticated customer for this tenant. "
            "Use only customer-scoped data and memory according to tenant policy."
        )
    if is_aiwerk_admin_actor(clean):
        return (
            "The current human is an authenticated AIWerk admin/operator, not the "
            "tenant customer. Treat customer profile and memory as tenant information; "
            "do not write operator conversation facts to customer memory."
        )
    return (
        "The current human is authenticated non-customer staff, not the tenant customer. "
        "Do not grant runtime-admin authority or write staff conversation facts to "
        "customer memory."
    )


def _is_customer_memory_mutation(function_name: str, args: Mapping[str, Any] | None) -> bool:
    name = str(function_name or "").strip().lower()
    payload = args or {}
    action = str(payload.get("action") or "").strip().lower()
    if name == "memory":
        if action in {"add", "replace", "remove"}:
            return True
        operations = payload.get("operations")
        return isinstance(operations, list) and any(
            isinstance(operation, Mapping)
            and str(operation.get("action") or "").strip().lower()
            in {"add", "replace", "remove"}
            for operation in operations
        )
    if name in {"honcho_conclude", "mem0_conclude"}:
        return True
    if name == "honcho_profile":
        return payload.get("card") is not None
    return name == "fact_store" and action in {"add", "update", "remove", "delete"}


def memory_write_blocked_for_cui_admin(
    function_name: str,
    args: Mapping[str, Any] | None,
) -> bool:
    """Block customer-memory mutation from non-customer or restricted CUI flows."""
    if not _is_customer_memory_mutation(function_name, args):
        return False
    actor = current_bound_cui_actor_context()
    if isinstance(actor, _TrustedNoActorContext) or not actor:
        return False
    if actor.get("_restricted") == "1" or not is_complete_authenticated_identity(actor):
        return True
    return normalize_role(actor.get("role")) != "customer"


def cui_admin_memory_block_result(function_name: str) -> str:
    """Return a stable customer-safe denial payload for blocked memory writes."""
    return json.dumps(
        {
            "success": False,
            "error": (
                f"{function_name} writes are disabled for this non-customer CUI "
                "actor. Operator/staff facts must not enter customer memory."
            ),
            "blocked_by": "cui_admin_actor_memory_guard",
        }
    )
