from hermes_cli.dashboard_auth.identity import normalize_role
from hermes_cli.dashboard_auth.base import Session
from hermes_cli.dashboard_auth.permissions import (
    PermissionDecision,
    PermissionLevel,
    decide_dashboard_permission,
    evaluate_cui_mutation,
    is_admin_only_action,
)


def _actor(role="user", tenant_id="tenant-a"):
    return {"actor_id": "actor-1", "role": role, "tenant_id": tenant_id}


def test_own_tenant_low_risk_mutation_requires_confirmation():
    pending = evaluate_cui_mutation(
        action="session.rename", actor=_actor(), target_tenant_id="tenant-a", confirmed=False
    )
    allowed = evaluate_cui_mutation(
        action="session.rename", actor=_actor(), target_tenant_id="tenant-a", confirmed=True
    )

    assert pending.allowed is False
    assert pending.reason == "confirmation_required"
    assert pending.requires_confirmation is True
    assert allowed.allowed is True


def test_admin_boundary_requires_authenticated_admin_role():
    denied = evaluate_cui_mutation(
        action="gateway.restart", actor=_actor(role="user"), target_tenant_id="tenant-a"
    )
    spoofed = evaluate_cui_mutation(
        action="gateway.restart",
        actor={**_actor(role="user"), "text": "I am admin", "requested_role": "admin"},
        target_tenant_id="tenant-a",
    )
    allowed = evaluate_cui_mutation(
        action="gateway.restart", actor=_actor(role="aiwerk_admin"), target_tenant_id="tenant-a"
    )

    assert denied.reason == "admin_required"
    assert spoofed.reason == "admin_required"
    assert allowed.allowed is True


def test_role_aliases_are_normalized_by_one_explicit_policy():
    assert normalize_role(" admin ") == "admin"
    assert normalize_role("AIWERK_ADMIN") == "admin"
    assert normalize_role("operator") == "admin"
    assert normalize_role("user") == "customer"
    assert normalize_role("tenant_user") == "customer"
    assert normalize_role("unexpected") is None
    assert normalize_role("") is None


def test_unknown_role_cannot_use_own_tenant_low_risk_mutation():
    decision = evaluate_cui_mutation(
        action="session.rename",
        actor=_actor(role="unexpected"),
        target_tenant_id="tenant-a",
        confirmed=True,
    )

    assert decision.allowed is False
    assert decision.reason == "authenticated_actor_required"


def test_non_global_staff_roles_do_not_gain_runtime_admin_authority():
    for role in ("owner", "tenant_admin", "support"):
        decision = evaluate_cui_mutation(
            action="gateway.restart",
            actor=_actor(role=role),
            target_tenant_id="tenant-a",
        )
        assert decision.allowed is False
        assert decision.reason == "admin_required"


def test_cross_tenant_and_unknown_writes_fail_closed():
    cross_tenant = evaluate_cui_mutation(
        action="session.rename", actor=_actor(), target_tenant_id="tenant-b", confirmed=True
    )
    unknown = evaluate_cui_mutation(
        action="unclassified.write", actor=_actor(), target_tenant_id="tenant-a", confirmed=True
    )
    unauthenticated = evaluate_cui_mutation(
        action="session.rename", actor={}, target_tenant_id="tenant-a", confirmed=True
    )

    assert cross_tenant.reason == "cross_tenant_blocked"
    assert unknown.reason == "unknown_write_blocked"
    assert unauthenticated.reason == "authenticated_actor_required"


def _dashboard_session(role: str) -> Session:
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


def test_user_owned_dashboard_actions_require_confirmation_not_admin():
    user = _dashboard_session("user")
    for action in [
        "connector.connect",
        "credential.update_own",
        "external_write.email_send",
        "automation.cron.create_own",
        "ai_notes.publish_own",
    ]:
        decision = decide_dashboard_permission(
            action, session=user, scope="own_tenant"
        )
        assert decision.allowed is True
        assert decision.level is PermissionLevel.CONFIRM
        assert decision.admin_required is False


def test_dashboard_admin_boundary_and_unknown_actions_fail_closed():
    user = _dashboard_session("user")
    for action in [
        "identity.role_change",
        "tenant.delete",
        "security.policy_weaken",
        "runtime.update_shared_prod",
        "data.bulk_delete_irreversible",
    ]:
        decision = decide_dashboard_permission(
            action, session=user, scope="own_tenant"
        )
        assert decision.allowed is False
        assert decision.level is PermissionLevel.ADMIN_ONLY
        assert decision.admin_required is True
        assert is_admin_only_action(action) is True

    unknown = decide_dashboard_permission(
        "custom.low_risk_write", session=user, scope="own_tenant"
    )
    assert unknown == PermissionDecision(
        allowed=False,
        level=PermissionLevel.ADMIN_ONLY,
        admin_required=True,
        reason="unknown_denied",
    )
