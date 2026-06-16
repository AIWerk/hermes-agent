"""Proportional role policy for AIWerk dashboard actions.

The policy is intentionally permissive for normal own-tenant work.  A
non-admin user should only hit the admin gate when an action can cross a
human/tenant boundary, weaken security, change shared runtime state, create
billing/legal exposure, or cause irreversible destructive damage.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal

from .base import Session

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


# Ordinary user-owned work. Some of these still need a visible preview and
# confirmation, but they should not force an admin login just because they are
# important settings. This preserves UX for the actual tenant user.
_USER_CONFIRM_ACTIONS: frozenset[str] = frozenset(
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

_USER_ACTIONS: frozenset[str] = frozenset(
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

# Keep this list small. These are the big red buttons only.
_ADMIN_ONLY_ACTIONS: frozenset[str] = frozenset(
    {
        # Identity and ownership boundaries.
        "identity.user_invite",
        "identity.user_remove",
        "identity.role_change",
        "identity.admin_grant",
        "identity.admin_revoke",
        "tenant.owner_transfer",
        # Tenant boundary and destructive tenant state.
        "tenant.cross_access",
        "tenant.migrate_host",
        "tenant.delete",
        "tenant.restore_overwrite",
        # Runtime and security policy.
        "runtime.update_shared_prod",
        "runtime.rollback_shared_prod",
        "runtime.restart_shared_prod",
        "tool.allowlist.change",
        "mcp.policy.change",
        "security.policy_weaken",
        "audit.disable",
        "audit.delete",
        # Cost, legal, and external blast radius.
        "billing.plan_change",
        "billing.spending_limit_increase",
        "external_write.bulk_email",
        "external_write.bulk_calendar_update",
        "invoice.issue_or_delete",
        "contract.execute",
        # Irreversible destructive operations.
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
        return PermissionDecision(False, PermissionLevel.ADMIN_ONLY, True, "no_session")

    if scope in {"cross_tenant", "shared_runtime"}:
        if is_admin_role(session.role):
            return PermissionDecision(True, PermissionLevel.ADMIN_ONLY, True, f"{scope}_admin")
        return PermissionDecision(False, PermissionLevel.ADMIN_ONLY, True, scope)

    if action in _ADMIN_ONLY_ACTIONS:
        if is_admin_role(session.role):
            return PermissionDecision(True, PermissionLevel.ADMIN_ONLY, True, "admin_session")
        return PermissionDecision(False, PermissionLevel.ADMIN_ONLY, True, "admin_only")

    if action in _USER_ACTIONS:
        return PermissionDecision(True, PermissionLevel.USER, False, "user_owned")

    if action in _USER_CONFIRM_ACTIONS:
        return PermissionDecision(True, PermissionLevel.CONFIRM, False, "user_owned_confirm")

    # Unknown own-tenant write surfaces should ask for a visible confirmation,
    # not immediately punish UX with an admin gate. Callers should promote an
    # action into _ADMIN_ONLY_ACTIONS only when it crosses one of the big-red-
    # button criteria above.
    return PermissionDecision(True, PermissionLevel.CONFIRM, False, "unknown_confirm")
