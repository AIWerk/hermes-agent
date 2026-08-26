from hermes_cli.dashboard_auth.permissions import evaluate_cui_mutation


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
