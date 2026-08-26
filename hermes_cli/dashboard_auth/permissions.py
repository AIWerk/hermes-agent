"""Fail-closed policy for authenticated CUI mutations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Any


@dataclass(frozen=True)
class MutationDecision:
    allowed: bool
    reason: str
    requires_confirmation: bool = False


_LOW_RISK = frozenset({"session.rename"})
_ADMIN_BOUNDARY = frozenset({"gateway.restart"})
_ADMIN_ROLES = frozenset({"admin", "aiwerk_admin", "operator"})


def evaluate_cui_mutation(
    *,
    action: str,
    actor: Mapping[str, Any] | None,
    target_tenant_id: str | None,
    confirmed: bool = False,
) -> MutationDecision:
    """Evaluate a write using only authenticated actor fields."""
    if not isinstance(actor, Mapping):
        return MutationDecision(False, "authenticated_actor_required")
    actor_id = actor.get("actor_id")
    role = actor.get("role")
    tenant_id = actor.get("tenant_id")
    if not isinstance(actor_id, str) or not actor_id:
        return MutationDecision(False, "authenticated_actor_required")
    if not isinstance(role, str) or not role:
        return MutationDecision(False, "authenticated_actor_required")
    if not isinstance(tenant_id, str) or not tenant_id:
        return MutationDecision(False, "authenticated_actor_required")
    if not isinstance(target_tenant_id, str) or not target_tenant_id:
        return MutationDecision(False, "target_tenant_required")
    if tenant_id != target_tenant_id:
        return MutationDecision(False, "cross_tenant_blocked")
    normalized_role = role.strip().lower()
    if action in _ADMIN_BOUNDARY:
        if normalized_role not in _ADMIN_ROLES:
            return MutationDecision(False, "admin_required")
        return MutationDecision(True, "allowed")
    if action in _LOW_RISK:
        if not confirmed:
            return MutationDecision(False, "confirmation_required", True)
        return MutationDecision(True, "allowed")
    return MutationDecision(False, "unknown_write_blocked")
