from __future__ import annotations

import dataclasses
import importlib
import inspect
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from hermes_cli.dashboard_auth.base import Session


ACTIONS = [
    "profile.discover", "profile.launch", "profile.use", "session.list",
    "session.search", "session.read", "session.export", "session.resume",
    "session.mutate", "profile.admin",
    "behavior.read", "behavior.write",
]


def _policy_module():
    return importlib.import_module("hermes_cli.dashboard_auth.profile_policy")


def _session(*, tenant_id: str = "tenant-a", actor_id: str = "employee", role: str = "user"):
    return Session(
        user_id=actor_id, email=f"{actor_id}@example.test", display_name=actor_id.title(),
        org_id=tenant_id, provider="test-idp", expires_at=4_000_000_000,
        access_token="opaque-access", refresh_token="opaque-refresh",
        tenant_id=tenant_id, actor_id=actor_id, role=role,
    )


def _document(*, memberships=None, profiles=None, actors=None):
    return {
        "version": 1,
        "profiles": profiles if profiles is not None else [
            {"profile_id": "employee-home", "tenant_id": "tenant-a", "kind": "employee", "enabled": True},
            {"profile_id": "peer-home", "tenant_id": "tenant-a", "kind": "employee", "enabled": True},
            {"profile_id": "other-home", "tenant_id": "tenant-b", "kind": "employee", "enabled": True},
        ],
        "actors": actors if actors is not None else [
            {"tenant_id": "tenant-a", "actor_id": "employee", "default_profile": "employee-home"},
            {"tenant_id": "tenant-a", "actor_id": "admin", "default_profile": "employee-home"},
        ],
        "memberships": memberships if memberships is not None else [{
            "tenant_id": "tenant-a", "actor_id": "employee", "profile_id": "employee-home",
            "actions": ["profile.discover", "profile.use", "session.list", "session.read"],
        }],
    }


def _install_fixed_policy(monkeypatch, tmp_path: Path, document=None):
    policy_mod = _policy_module()
    marker = tmp_path / "hermes-profile-membership.required"
    policy = tmp_path / "hermes-profile-membership.yaml"
    marker.write_bytes(b"required-v1\n")
    marker.chmod(0o600)
    if document is not None:
        policy.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        policy.chmod(0o600)
    monkeypatch.setattr(policy_mod, "REQUIRED_MARKER_PATH", str(marker), raising=False)
    monkeypatch.setattr(policy_mod, "PROFILE_POLICY_PATH", str(policy), raising=False)
    monkeypatch.setattr(policy_mod, "TRUSTED_ROOT_UID", os.getuid(), raising=False)
    return marker, policy


def _load_fixed_policy(monkeypatch, tmp_path, document):
    policy_mod = _policy_module()
    _install_fixed_policy(monkeypatch, tmp_path, document)
    return policy_mod.load_profile_policy()


def test_role_without_membership_is_denied_generically(tmp_path, monkeypatch):
    policy_mod = _policy_module()
    policy = _load_fixed_policy(monkeypatch, tmp_path, _document(memberships=[]))
    with pytest.raises(policy_mod.ProfileAccessDenied, match="^profile access denied$"):
        policy_mod.resolve_effective_authority(_session(role="admin"), policy)


def test_unknown_action_profile_and_cross_tenant_are_indistinguishable_denials(tmp_path, monkeypatch):
    policy = _load_fixed_policy(monkeypatch, tmp_path, _document())
    assert [
        policy.allows("tenant-a", "employee", "employee-home", "unknown.action"),
        policy.allows("tenant-a", "employee", "missing-home", "session.read"),
        policy.allows("tenant-a", "employee", "other-home", "session.read"),
        policy.allows("tenant-b", "employee", "employee-home", "session.read"),
    ] == [False, False, False, False]


def test_employee_own_profile_resolves_to_immutable_effective_authority(tmp_path, monkeypatch):
    policy_mod = _policy_module()
    policy = _load_fixed_policy(monkeypatch, tmp_path, _document())
    authority = policy_mod.resolve_effective_authority(_session(), policy)
    assert dataclasses.is_dataclass(authority)
    assert (authority.tenant_id, authority.actor_id, authority.role) == ("tenant-a", "employee", "customer")
    assert authority.default_profile == authority.active_profile == "employee-home"
    assert authority.effective_capabilities == (
        "profile.discover", "profile.use", "session.list", "session.read",
    )
    assert authority.auth_provider == "test-idp"
    assert authority.auth_expires_at == 4_000_000_000
    assert authority.policy_revision == policy.revision
    with pytest.raises(dataclasses.FrozenInstanceError):
        authority.active_profile = "peer-home"


def test_admin_wildcard_grants_only_listed_actions_within_tenant(tmp_path, monkeypatch):
    policy_mod = _policy_module()
    wildcard_actions = ["session.list", "session.search", "session.read"]
    policy = _load_fixed_policy(monkeypatch, tmp_path, _document(memberships=[{
        "tenant_id": "tenant-a", "actor_id": "admin", "profile_id": "*",
        "actions": wildcard_actions,
    }]))
    authority = policy_mod.resolve_effective_authority(_session(actor_id="admin", role="admin"), policy)
    assert authority.effective_capabilities == tuple(wildcard_actions)
    assert all(policy.allows("tenant-a", "admin", profile_id, action)
               for profile_id in ("employee-home", "peer-home") for action in wildcard_actions)
    assert not policy.allows("tenant-a", "admin", "other-home", "session.read")
    for action in ("profile.launch", "profile.use", "session.export", "session.resume", "session.mutate", "profile.admin"):
        assert not policy.allows("tenant-a", "admin", "employee-home", action)


@pytest.mark.parametrize("raw", [
    "version: 1\nprofiles: [\n",
    "version: 1\nversion: 1\nprofiles: []\nactors: []\nmemberships: []\n",
    "version: 1\nprofiles: []\nactors: []\nmemberships: []\nextra: true\n",
    "version: 1\nprofiles:\n  - profile_id: p\n    tenant_id: t\n    kind: employee\n    enabled: true\n    extra: true\nactors: []\nmemberships: []\n",
], ids=["malformed", "duplicate-key", "unknown-root-field", "unknown-nested-field"])
def test_malformed_duplicate_and_unknown_fields_fail_closed(tmp_path, monkeypatch, raw):
    policy_mod = _policy_module()
    _marker, policy = _install_fixed_policy(monkeypatch, tmp_path)
    policy.write_text(raw, encoding="utf-8")
    with pytest.raises(policy_mod.ProfilePolicyError):
        policy_mod.load_profile_policy()


@pytest.mark.parametrize("case", ["unknown-action", "unknown-profile", "unknown-actor", "tenant-mismatch"])
def test_invalid_membership_references_fail_closed(tmp_path, monkeypatch, case):
    policy_mod = _policy_module()
    membership = {"tenant_id": "tenant-a", "actor_id": "employee", "profile_id": "employee-home", "actions": ["session.read"]}
    if case == "unknown-action": membership["actions"] = ["session.destroy"]
    elif case == "unknown-profile": membership["profile_id"] = "missing-home"
    elif case == "unknown-actor": membership["actor_id"] = "missing-actor"
    else: membership["tenant_id"] = "tenant-b"
    _install_fixed_policy(monkeypatch, tmp_path, _document(memberships=[membership]))
    with pytest.raises(policy_mod.ProfilePolicyError):
        policy_mod.load_profile_policy()


def test_disabled_default_profile_is_denied_without_exposing_its_name(tmp_path, monkeypatch):
    policy_mod = _policy_module()
    document = _document()
    document["profiles"][0]["enabled"] = False
    policy = _load_fixed_policy(monkeypatch, tmp_path, document)
    with pytest.raises(policy_mod.ProfileAccessDenied) as exc_info:
        policy_mod.resolve_effective_authority(_session(), policy)
    assert str(exc_info.value) == "profile access denied"
    assert "employee-home" not in str(exc_info.value)


def test_required_marker_controls_legacy_mode_and_missing_policy_fails_closed(tmp_path, monkeypatch):
    policy_mod = _policy_module()
    marker, _policy = _install_fixed_policy(monkeypatch, tmp_path)
    marker.unlink()
    assert policy_mod.resolve_configured_authority(_session()) is None
    marker.write_bytes(b"required-v1\n")
    with pytest.raises(policy_mod.ProfilePolicyError):
        policy_mod.resolve_configured_authority(_session())


def test_policy_revision_is_deterministic_and_changes_with_exact_bytes(tmp_path, monkeypatch):
    policy_mod = _policy_module()
    _marker, policy_path = _install_fixed_policy(monkeypatch, tmp_path, _document())
    first = policy_mod.load_profile_policy().revision
    second = policy_mod.load_profile_policy().revision
    policy_path.write_bytes(policy_path.read_bytes() + b"\n")
    changed = policy_mod.load_profile_policy().revision
    assert first == second
    assert len(first) == 64 and set(first) <= set("0123456789abcdef")
    assert changed != first


def test_behavior_actions_are_separate_exact_vocabulary():
    assert tuple(_policy_module().PROFILE_ACTIONS) == tuple(ACTIONS)
    assert {"behavior.read", "behavior.write"}.isdisjoint(
        {action for action in ACTIONS if action.startswith("session.")}
    )


def test_behavior_write_wildcard_policy_is_rejected(tmp_path, monkeypatch):
    policy_mod = _policy_module()
    document = _document(memberships=[{
        "tenant_id": "tenant-a", "actor_id": "admin", "profile_id": "*",
        "actions": ["behavior.write"],
    }])
    _install_fixed_policy(monkeypatch, tmp_path, document)
    with pytest.raises(policy_mod.ProfilePolicyError):
        policy_mod.load_profile_policy()


def test_fixed_production_authority_paths_and_caps_are_declared():
    policy_mod = _policy_module()
    assert policy_mod.REQUIRED_MARKER_PATH == "/etc/aiwerk/hermes-profile-membership.required"
    assert policy_mod.PROFILE_POLICY_PATH == "/etc/aiwerk/hermes-profile-membership.yaml"
    assert policy_mod.TRUSTED_ROOT_UID == 0
    assert policy_mod.MAX_MARKER_BYTES == 64
    assert policy_mod.MAX_POLICY_BYTES == 262_144
    assert policy_mod.MAX_PROFILES == 2_048
    assert policy_mod.MAX_ACTORS == 2_048
    assert policy_mod.MAX_MEMBERSHIPS == 2_048
    assert policy_mod.MAX_ACTIONS_PER_MEMBERSHIP == 64


def test_trusted_root_uid_matches_namespace_root_projection():
    policy_mod = _policy_module()
    assert policy_mod._trusted_root_uid() == os.stat("/").st_uid


def test_namespace_root_projection_accepts_root_owned_fixed_files(monkeypatch, tmp_path):
    policy_mod = _policy_module()
    _install_fixed_policy(monkeypatch, tmp_path, _document())
    monkeypatch.setattr(policy_mod, "TRUSTED_ROOT_UID", 0)
    real_fstat = os.fstat
    real_stat = os.stat

    def projected_fstat(fd):
        result = real_fstat(fd)
        return SimpleNamespace(
            st_mode=result.st_mode,
            st_uid=65534,
            st_size=result.st_size,
            st_dev=result.st_dev,
            st_ino=result.st_ino,
        )

    def projected_stat(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if os.fspath(path) != "/":
            return result
        return SimpleNamespace(
            st_mode=result.st_mode,
            st_uid=65534,
            st_size=result.st_size,
            st_dev=result.st_dev,
            st_ino=result.st_ino,
        )

    monkeypatch.setattr(os, "fstat", projected_fstat)
    monkeypatch.setattr(os, "stat", projected_stat)
    authority = policy_mod.resolve_configured_authority(_session())
    assert authority.active_profile == "employee-home"


def test_trusted_file_rejects_group_or_world_writable_parent(monkeypatch, tmp_path):
    policy_mod = _policy_module()
    _install_fixed_policy(monkeypatch, tmp_path, _document())
    original_mode = tmp_path.stat().st_mode & 0o777
    tmp_path.chmod(0o777)
    try:
        with pytest.raises(policy_mod.ProfilePolicyError):
            policy_mod.resolve_configured_authority(_session())
    finally:
        tmp_path.chmod(original_mode)


def test_production_resolvers_expose_no_path_or_uid_inputs():
    policy_mod = _policy_module()

    assert tuple(inspect.signature(policy_mod.load_profile_policy).parameters) == ()
    assert tuple(inspect.signature(policy_mod.resolve_configured_authority).parameters) == ("session",)


def test_trusted_reader_uses_required_descriptor_flags(tmp_path, monkeypatch):
    policy_mod = _policy_module()
    _install_fixed_policy(monkeypatch, tmp_path, _document())
    real_open = os.open
    seen_flags = []

    def recording_open(path, flags, *args, **kwargs):
        seen_flags.append(flags)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", recording_open)
    policy_mod.resolve_configured_authority(_session())

    common = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    nonblocking = common | getattr(os, "O_NONBLOCK", 0)
    assert len(seen_flags) == 4
    assert all(flags & common == common for flags in seen_flags)
    assert seen_flags[1] & nonblocking == nonblocking
    assert seen_flags[3] & nonblocking == nonblocking


def test_trusted_reader_rejects_metadata_change_during_exact_read(tmp_path, monkeypatch):
    policy_mod = _policy_module()
    _install_fixed_policy(monkeypatch, tmp_path, _document())
    real_fstat = os.fstat
    regular_calls = {}

    def changing_fstat(fd):
        result = real_fstat(fd)
        if stat.S_ISREG(result.st_mode):
            regular_calls[fd] = regular_calls.get(fd, 0) + 1
        if regular_calls.get(fd) == 2:
            return SimpleNamespace(
                st_mode=result.st_mode,
                st_uid=result.st_uid,
                st_size=result.st_size + 1,
                st_dev=result.st_dev,
                st_ino=result.st_ino,
            )
        return result

    monkeypatch.setattr(os, "fstat", changing_fstat)
    with pytest.raises(policy_mod.ProfilePolicyError):
        policy_mod.resolve_configured_authority(_session())


@pytest.mark.parametrize("target", ["marker", "policy"])
@pytest.mark.parametrize("unsafe", ["relative", "symlink", "fifo", "mode", "owner", "oversize"])
def test_fixed_authority_files_reject_unsafe_sources(tmp_path, monkeypatch, target, unsafe):
    policy_mod = _policy_module()
    marker, policy = _install_fixed_policy(monkeypatch, tmp_path, _document())
    selected = marker if target == "marker" else policy
    if unsafe == "relative":
        monkeypatch.setattr(policy_mod, "REQUIRED_MARKER_PATH" if target == "marker" else "PROFILE_POLICY_PATH", selected.name)
    elif unsafe == "symlink":
        real = selected.with_suffix(".real")
        selected.rename(real)
        selected.symlink_to(real)
    elif unsafe == "fifo":
        selected.unlink()
        os.mkfifo(selected)
    elif unsafe == "mode":
        selected.chmod(0o666)
    elif unsafe == "owner":
        monkeypatch.setattr(policy_mod, "TRUSTED_ROOT_UID", os.getuid() + 1)
    else:
        limit = policy_mod.MAX_MARKER_BYTES if target == "marker" else policy_mod.MAX_POLICY_BYTES
        selected.write_bytes(b"x" * (limit + 1))
    with pytest.raises(policy_mod.ProfilePolicyError):
        policy_mod.resolve_configured_authority(_session())


def test_marker_requires_exact_schema_and_non_enoent_open_errors_fail_closed(tmp_path, monkeypatch):
    policy_mod = _policy_module()
    marker, _policy = _install_fixed_policy(monkeypatch, tmp_path, _document())
    marker.write_bytes(b"required-v2\n")
    with pytest.raises(policy_mod.ProfilePolicyError):
        policy_mod.resolve_configured_authority(_session())
    marker.write_bytes(b"required-v1\n")
    real_open = os.open
    def denied(path, flags, *args, **kwargs):
        if os.fspath(path) == marker.name and kwargs.get("dir_fd") is not None:
            raise PermissionError("denied")
        return real_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, "open", denied)
    with pytest.raises(policy_mod.ProfilePolicyError):
        policy_mod.resolve_configured_authority(_session())


@pytest.mark.parametrize("raw", [
    b"version: 1\nprofiles: &p []\nactors: []\nmemberships: []\n",
    b"version: 1\nprofiles: &p []\nactors: *p\nmemberships: []\n",
    b"version: 1\nprofiles: []\nactors: []\nmemberships: []\n<<: {}\n",
], ids=["anchor", "alias", "merge-key"])
def test_yaml_anchors_aliases_and_merge_keys_are_rejected(tmp_path, monkeypatch, raw):
    policy_mod = _policy_module()
    _marker, policy = _install_fixed_policy(monkeypatch, tmp_path)
    policy.write_bytes(raw)
    with pytest.raises(policy_mod.ProfilePolicyError):
        policy_mod.load_profile_policy()


@pytest.mark.parametrize("collection", ["profiles", "actors", "memberships"])
def test_policy_record_caps_are_enforced(tmp_path, monkeypatch, collection):
    policy_mod = _policy_module()
    document = _document(profiles=[], actors=[], memberships=[])
    if collection == "profiles":
        document[collection] = [{"profile_id": f"p{i}", "tenant_id": "t", "kind": "e", "enabled": True}
                                for i in range(policy_mod.MAX_PROFILES + 1)]
    elif collection == "actors":
        document["profiles"] = [{"profile_id": "p", "tenant_id": "t", "kind": "e", "enabled": True}]
        document[collection] = [{"tenant_id": "t", "actor_id": f"a{i}", "default_profile": "p"}
                                for i in range(policy_mod.MAX_ACTORS + 1)]
    else:
        document["profiles"] = [{"profile_id": "p", "tenant_id": "t", "kind": "e", "enabled": True}]
        document["actors"] = [{"tenant_id": "t", "actor_id": "a", "default_profile": "p"}]
        document[collection] = [{"tenant_id": "t", "actor_id": "a", "profile_id": "p", "actions": ["session.read"]}
                                for _ in range(policy_mod.MAX_MEMBERSHIPS + 1)]
    _install_fixed_policy(monkeypatch, tmp_path, document)
    with pytest.raises(policy_mod.ProfilePolicyError):
        policy_mod.load_profile_policy()
