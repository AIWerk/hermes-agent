"""Fail-closed policy for authenticated CUI mutations."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal, Mapping

from .base import Session
from .identity import (
    RUNTIME_MUTATION_ADMIN_ROLE_ALIASES,
    is_complete_authenticated_identity,
)


@dataclass(frozen=True)
class MutationDecision:
    allowed: bool
    reason: str
    requires_confirmation: bool = False


Scope = Literal["own_tenant", "cross_tenant", "shared_runtime"]


class PermissionLevel(str, Enum):
    USER = "user"
    CONFIRM = "confirm"
    ADMIN_ONLY = "admin_only"


@dataclass(frozen=True)
class PermissionDecision:
    allowed: bool
    level: PermissionLevel
    admin_required: bool
    reason: str


_USER_CONFIRM_ACTIONS = frozenset(
    {
        "connector.connect",
        "connector.reconnect",
        "connector.disconnect_own",
        "credential.update_own",
        "external_write.email_send",
        "external_write.calendar_update",
        "external_write.task_update",
        "external_write.contact_update",
        "automation.cron.create_own",
        "automation.cron.edit_own",
        "automation.cron.pause_own",
        "automation.cron.remove_own",
        "ai_notes.publish_own",
        "agent.settings.update_own",
        "memory.export_own",
        "wiki.export_own",
        "backup.download_own",
    }
)

_USER_ACTIONS = frozenset(
    {
        "chat.send",
        "task.create_own",
        "task.edit_own",
        "task.complete_own",
        "resource.read_own",
        "artifact.download_own",
        "artifact.upload_own",
        "draft.create_own",
    }
)

_ADMIN_ONLY_ACTIONS = frozenset(
    {
        "identity.user_invite",
        "identity.user_remove",
        "identity.role_change",
        "identity.admin_grant",
        "identity.admin_revoke",
        "tenant.owner_transfer",
        "tenant.cross_access",
        "tenant.migrate_host",
        "tenant.delete",
        "tenant.restore_overwrite",
        "runtime.update_shared_prod",
        "runtime.rollback_shared_prod",
        "runtime.restart_shared_prod",
        "tool.allowlist.change",
        "mcp.policy.change",
        "security.policy_weaken",
        "audit.disable",
        "audit.delete",
        "billing.plan_change",
        "billing.spending_limit_increase",
        "external_write.bulk_email",
        "external_write.bulk_calendar_update",
        "invoice.issue_or_delete",
        "contract.execute",
        "data.bulk_delete_irreversible",
        "memory.reset_all",
        "agent.reset_all",
        "backup.restore_overwrite",
    }
)


def is_admin_role(role: str | None) -> bool:
    return (role or "").strip().lower() in {"admin", "owner", "operator"}


def is_admin_only_action(action: str) -> bool:
    return action in _ADMIN_ONLY_ACTIONS


def decide_dashboard_permission(
    action: str,
    *,
    session: Session | None,
    scope: Scope = "own_tenant",
) -> PermissionDecision:
    if session is None:
        return PermissionDecision(
            False, PermissionLevel.ADMIN_ONLY, True, "no_session"
        )
    if scope in {"cross_tenant", "shared_runtime"}:
        if is_admin_role(session.role):
            return PermissionDecision(
                True, PermissionLevel.ADMIN_ONLY, True, f"{scope}_admin"
            )
        return PermissionDecision(False, PermissionLevel.ADMIN_ONLY, True, scope)
    if action in _ADMIN_ONLY_ACTIONS:
        if is_admin_role(session.role):
            return PermissionDecision(
                True, PermissionLevel.ADMIN_ONLY, True, "admin_session"
            )
        return PermissionDecision(
            False, PermissionLevel.ADMIN_ONLY, True, "admin_only"
        )
    if action in _USER_ACTIONS:
        return PermissionDecision(True, PermissionLevel.USER, False, "user_owned")
    if action in _USER_CONFIRM_ACTIONS:
        return PermissionDecision(
            True, PermissionLevel.CONFIRM, False, "user_owned_confirm"
        )
    if is_admin_role(session.role):
        return PermissionDecision(
            True, PermissionLevel.ADMIN_ONLY, True, "unknown_admin"
        )
    return PermissionDecision(
        False, PermissionLevel.ADMIN_ONLY, True, "unknown_denied"
    )


_LOW_RISK = frozenset({"session.rename"})
_ADMIN_BOUNDARY = frozenset({"gateway.restart"})
_AIWERK_ADMIN_ROLES = RUNTIME_MUTATION_ADMIN_ROLE_ALIASES


def evaluate_cui_mutation(
    *,
    action: str,
    actor: Mapping[str, Any] | None,
    target_tenant_id: str | None,
    confirmed: bool = False,
) -> MutationDecision:
    """Evaluate a write using only authenticated actor fields."""
    if not isinstance(actor, Mapping) or not is_complete_authenticated_identity(actor):
        return MutationDecision(False, "authenticated_actor_required")
    tenant_id = actor.get("tenant_id")
    if not isinstance(target_tenant_id, str) or not target_tenant_id:
        return MutationDecision(False, "target_tenant_required")
    if tenant_id != target_tenant_id:
        return MutationDecision(False, "cross_tenant_blocked")
    if action in _ADMIN_BOUNDARY:
        role = actor.get("role")
        if not isinstance(role, str) or role.strip().lower() not in _AIWERK_ADMIN_ROLES:
            return MutationDecision(False, "admin_required")
        return MutationDecision(True, "allowed")
    if action in _LOW_RISK:
        if not confirmed:
            return MutationDecision(False, "confirmation_required", True)
        return MutationDecision(True, "allowed")
    return MutationDecision(False, "unknown_write_blocked")
