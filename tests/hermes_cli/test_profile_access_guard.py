"""Action decisions are fresh, typed, immutable and independent of storage."""
import dataclasses
from types import SimpleNamespace

import pytest

from tests.profile_authorization_support import ADMIN, EMPLOYEE, OWN_ACTIONS, capability, install_policy


@pytest.fixture
def policy_env(tmp_path, monkeypatch):
    return install_policy(tmp_path, monkeypatch)


def guard():
    return capability("hermes_cli.dashboard_auth.profile_access")


@pytest.mark.parametrize("action", OWN_ACTIONS)
def test_own_actions_resolve_immutable_default_without_resource_lookup(policy_env, action):
    access = guard()
    decision = access.authorize(EMPLOYEE, action)
    assert (decision.tenant_id, decision.actor_id, decision.target_profile, decision.action) == (
        "tenant-a", "employee", "employee-home", action)
    assert decision.auth_provider == "test-idp"
    assert decision.policy_revision
    with pytest.raises(dataclasses.FrozenInstanceError):
        decision.target_profile = "peer-home"


@pytest.mark.parametrize("target", [True, False, 0, 1, [], {}, "", " employee-home", "employee-home ",
                                    ".", "..", "a/b", "a\\b", "*", "/absolute", "current", "all", "x\x00y",
                                    "missing", "peer-home", "other-home", "disabled"])
def test_invalid_or_ungranted_targets_are_generic(policy_env, target):
    access = guard()
    with pytest.raises(access.ProfileAccessDenied, match="^profile access denied$"):
        access.authorize(EMPLOYEE, "session.read", target)


@pytest.mark.parametrize("action", [None, True, 1, [], {}, "", "session.history", "SESSION.READ", "session.read "])
def test_malformed_actions_do_not_coerce(policy_env, action):
    access = guard()
    with pytest.raises(access.ProfileAccessDenied, match="^profile access denied$"):
        access.authorize(EMPLOYEE, action, "employee-home")


@pytest.mark.parametrize("actor", [None, {}, {"_restricted": "1"}, {**EMPLOYEE, "actor_id": []},
                                  {**EMPLOYEE, "actor_id": "employee "}, {**EMPLOYEE, "role": "unknown"},
                                  {**EMPLOYEE, "tenant_id": "tenant-b"}, {**EMPLOYEE, "actor_id": "unknown"}])
def test_invalid_actor_never_inherits_bound_or_environment_identity(policy_env, monkeypatch, actor):
    from agent.cui_actor_context import bind_cui_actor_context, reset_cui_actor_context
    access = guard()
    monkeypatch.setenv("AIWERK_CUI_ACTOR_ID", "employee")
    token = bind_cui_actor_context(EMPLOYEE)
    try:
        with pytest.raises(access.ProfileAccessDenied, match="^profile access denied$"):
            access.authorize(actor, "session.read")
    finally:
        reset_cui_actor_context(token)


@pytest.mark.parametrize("fault", ["revoke", "disable", "missing", "corrupt", "unsafe"])
def test_next_decision_reloads_root_authority(policy_env, fault):
    access = guard()
    first = access.authorize(EMPLOYEE, "session.read")
    if fault == "revoke":
        policy_env.revoke(action="session.read")
    elif fault == "disable":
        policy_env.document["profiles"][0]["enabled"] = False
        policy_env.save()
    elif fault == "missing":
        policy_env.path.unlink()
    elif fault == "corrupt":
        policy_env.path.write_text("not: policy")
    else:
        policy_env.path.chmod(0o666)
    with pytest.raises(access.ProfileAccessDenied, match="^profile access denied$"):
        access.authorize(EMPLOYEE, first.action, first.target_profile)


def test_legacy_requires_absent_marker_only(policy_env):
    access = guard()
    policy_env.marker.unlink()
    assert access.authorize(None, "session.read") is None
    policy_env.marker.write_bytes(b"bad")
    with pytest.raises(access.ProfileAccessDenied):
        access.authorize(None, "session.read")


@pytest.mark.parametrize("action", ["profile.use", "profile.discover", "profile.launch", "session.export", "session.resume", "session.mutate", "profile.admin"])
def test_admin_wildcard_does_not_expand_to_runtime(policy_env, action):
    access = guard()
    with pytest.raises(access.ProfileAccessDenied):
        access.authorize(ADMIN, action, "employee-home", delegated=True)


def test_delegation_requires_admin_and_explicit_target(policy_env):
    access = guard()
    assert access.authorize(ADMIN, "session.read", "employee-home", delegated=True).actor_id == "admin"
    for actor, target in [(ADMIN, None), (EMPLOYEE, "employee-home")]:
        with pytest.raises(access.ProfileAccessDenied):
            access.authorize(actor, "session.read", target, delegated=True)
    with pytest.raises(access.ProfileAccessDenied):
        access.authorize(ADMIN, "session.read", "employee-home")


def test_authoritative_live_target_conflict_denied(policy_env):
    access = guard()
    assert access.authorize(EMPLOYEE, "session.read", live_profile="employee-home").target_profile == "employee-home"
    with pytest.raises(access.ProfileAccessDenied):
        access.authorize(EMPLOYEE, "session.read", "employee-home", live_profile="peer-home")


def test_discovery_roster_from_policy_not_filesystem(policy_env):
    access = guard()
    assert access.authorized_profiles(EMPLOYEE, "profile.discover") == ("employee-home",)
    assert access.authorized_profiles(ADMIN, "profile.discover") == ("default",)
    assert not (policy_env.root / "profiles").exists()


def test_admin_exact_behavior_read_write_succeeds_for_same_tenant_target(policy_env):
    access = guard()
    for action in ("behavior.read", "behavior.write"):
        decision = access.authorize(ADMIN, action, "employee-home")
        assert (decision.actor_id, decision.target_profile, decision.action) == (
            "admin", "employee-home", action)


def test_admin_role_without_exact_behavior_grant_denied(policy_env):
    access = guard()
    policy_env.document["memberships"] = [
        row for row in policy_env.document["memberships"]
        if not (row["actor_id"] == "admin" and row["profile_id"] == "employee-home")
    ]
    policy_env.save()
    with pytest.raises(access.ProfileAccessDenied, match="^profile access denied$"):
        access.authorize(ADMIN, "behavior.read", "employee-home")


def test_employee_behavior_read_and_write_denied(policy_env):
    access = guard()
    for action in ("behavior.read", "behavior.write"):
        with pytest.raises(access.ProfileAccessDenied, match="^profile access denied$"):
            access.authorize(EMPLOYEE, action, "employee-home")


def test_unknown_disabled_cross_tenant_and_malformed_targets_share_generic_denial(policy_env):
    access = guard()
    errors = []
    for target in ("missing", "disabled", "other-home", "../employee-home", "", None):
        with pytest.raises(access.ProfileAccessDenied) as exc:
            access.authorize(ADMIN, "behavior.read", target)
        errors.append(str(exc.value))
    assert errors == ["profile access denied"] * len(errors)


def test_revoked_behavior_grant_fails_on_next_dispatch_without_restart(policy_env):
    access = guard()
    assert access.authorize(ADMIN, "behavior.write", "employee-home").policy_revision
    policy_env.revoke(actor="admin", action="behavior.write")
    with pytest.raises(access.ProfileAccessDenied, match="^profile access denied$"):
        access.authorize(ADMIN, "behavior.write", "employee-home")
