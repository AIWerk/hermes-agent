from __future__ import annotations

from hermes_cli.dashboard_auth.base import Session
from hermes_cli.dashboard_auth.permissions import (
    PermissionDecision,
    PermissionLevel,
    decide_dashboard_permission,
    is_admin_only_action,
)


def _session(role: str) -> Session:
    return Session(
        user_id="u1",
        email="u@example.test",
        display_name="User",
        org_id="org1",
        provider="test",
        expires_at=9999999999,
        access_token="at",
        refresh_token="rt",
        tenant_id="tenant1",
        actor_id="actor1",
        role=role,
    )


def test_user_can_manage_own_tenant_connectors_credentials_and_automation_with_confirmation():
    user = _session("user")

    for action in [
        "connector.connect",
        "connector.reconnect",
        "credential.update_own",
        "external_write.email_send",
        "automation.cron.create_own",
        "ai_notes.publish_own",
        "memory.export_own",
        "backup.download_own",
    ]:
        decision = decide_dashboard_permission(action, session=user, scope="own_tenant")
        assert decision.allowed is True
        assert decision.level is PermissionLevel.CONFIRM
        assert decision.admin_required is False


def test_admin_only_is_limited_to_boundary_runtime_billing_and_irreversible_damage():
    user = _session("user")

    for action in [
        "identity.user_invite",
        "identity.role_change",
        "tenant.delete",
        "tenant.restore_overwrite",
        "security.policy_weaken",
        "runtime.update_shared_prod",
        "billing.spending_limit_increase",
        "external_write.bulk_email",
        "data.bulk_delete_irreversible",
        "memory.reset_all",
    ]:
        decision = decide_dashboard_permission(action, session=user, scope="own_tenant")
        assert decision.allowed is False
        assert decision.level is PermissionLevel.ADMIN_ONLY
        assert decision.admin_required is True
        assert is_admin_only_action(action) is True


def test_admin_session_may_execute_admin_only_action_with_admin_decision():
    admin = _session("admin")

    decision = decide_dashboard_permission("identity.role_change", session=admin, scope="own_tenant")

    assert decision == PermissionDecision(
        allowed=True,
        level=PermissionLevel.ADMIN_ONLY,
        admin_required=True,
        reason="admin_session",
    )


def test_cross_tenant_actions_are_admin_only_even_when_action_is_normally_user_owned():
    user = _session("user")

    decision = decide_dashboard_permission("connector.connect", session=user, scope="cross_tenant")

    assert decision.allowed is False
    assert decision.level is PermissionLevel.ADMIN_ONLY
    assert decision.admin_required is True
    assert decision.reason == "cross_tenant"


def test_unknown_write_actions_fail_safe_to_confirmation_not_admin_prompt():
    user = _session("user")

    decision = decide_dashboard_permission("custom.low_risk_write", session=user, scope="own_tenant")

    assert decision.allowed is True
    assert decision.level is PermissionLevel.CONFIRM
    assert decision.admin_required is False
    assert decision.reason == "unknown_confirm"
