"""Root-managed profile membership policy for authenticated dashboard requests."""
from __future__ import annotations

import errno
import hashlib
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

import yaml

from hermes_cli.dashboard_auth.identity import normalize_role

REQUIRED_MARKER_PATH = "/etc/aiwerk/hermes-profile-membership.required"
PROFILE_POLICY_PATH = "/etc/aiwerk/hermes-profile-membership.yaml"
TRUSTED_ROOT_UID = 0
MAX_MARKER_BYTES = 64
MAX_POLICY_BYTES = 262_144
MAX_PROFILES = 2_048
MAX_ACTORS = 2_048
MAX_MEMBERSHIPS = 2_048
MAX_ACTIONS_PER_MEMBERSHIP = 64
_REQUIRED_MARKER = b"required-v1\n"

PROFILE_ACTIONS = (
    "profile.discover",
    "profile.launch",
    "profile.use",
    "session.list",
    "session.search",
    "session.read",
    "session.export",
    "session.resume",
    "session.mutate",
    "profile.admin",
)
_ACTION_SET = frozenset(PROFILE_ACTIONS)
_DENIED = "profile access denied"


class ProfilePolicyError(Exception):
    """The root-managed policy cannot be used safely."""


class ProfileAccessDenied(ProfilePolicyError):
    """The authenticated actor has no usable default-profile membership."""


@dataclass(frozen=True, slots=True)
class EffectiveAuthority:
    tenant_id: str
    actor_id: str
    role: str
    default_profile: str
    active_profile: str
    effective_capabilities: tuple[str, ...]
    policy_revision: str
    auth_provider: str
    auth_expires_at: int


@dataclass(frozen=True, slots=True)
class _Profile:
    tenant_id: str
    kind: str
    enabled: bool


@dataclass(frozen=True, slots=True)
class ProfilePolicy:
    """Validated policy snapshot. It contains no role-derived grants."""

    revision: str
    _profiles: Mapping[str, _Profile]
    _actors: Mapping[tuple[str, str], str]
    _grants: Mapping[tuple[str, str, str], frozenset[str]]

    def allows(self, tenant_id: str, actor_id: str, profile_id: str, action: str) -> bool:
        if action not in _ACTION_SET or (tenant_id, actor_id) not in self._actors:
            return False
        profile = self._profiles.get(profile_id)
        if profile is None or not profile.enabled or profile.tenant_id != tenant_id:
            return False
        exact = self._grants.get((tenant_id, actor_id, profile_id), frozenset())
        wildcard = self._grants.get((tenant_id, actor_id, "*"), frozenset())
        return action in exact or action in wildcard

    def authority_for(self, session: Any) -> EffectiveAuthority:
        tenant_id = _identity_string(session, "tenant_id")
        actor_id = _identity_string(session, "actor_id")
        role = normalize_role(getattr(session, "role", None))
        if not tenant_id or not actor_id or role is None:
            raise ProfileAccessDenied(_DENIED)
        default_profile = self._actors.get((tenant_id, actor_id))
        profile = self._profiles.get(default_profile or "")
        if (default_profile is None or profile is None or not profile.enabled
                or profile.tenant_id != tenant_id):
            raise ProfileAccessDenied(_DENIED)
        granted = (
            self._grants.get((tenant_id, actor_id, default_profile), frozenset())
            | self._grants.get((tenant_id, actor_id, "*"), frozenset())
        )
        capabilities = tuple(action for action in PROFILE_ACTIONS if action in granted)
        if not capabilities:
            raise ProfileAccessDenied(_DENIED)
        provider = getattr(session, "provider", "")
        expires_at = getattr(session, "expires_at", 0)
        return EffectiveAuthority(
            tenant_id=tenant_id,
            actor_id=actor_id,
            role=role,
            default_profile=default_profile,
            active_profile=default_profile,
            effective_capabilities=capabilities,
            policy_revision=self.revision,
            auth_provider=provider if isinstance(provider, str) else "",
            auth_expires_at=expires_at if type(expires_at) is int else 0,
        )


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(loader: _UniqueKeyLoader, node, deep: bool = False):
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping", node.start_mark,
                "mapping keys must be scalar", key_node.start_mark,
            ) from exc
        if duplicate:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping", node.start_mark,
                "duplicate mapping key", key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _identity_string(identity: Any, field: str) -> str:
    value = getattr(identity, field, "")
    return value if isinstance(value, str) and value and value == value.strip() else ""


def _record(record: Any, *, fields: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(record, dict) or set(record) != fields:
        raise ProfilePolicyError(f"invalid {label}")
    return record


def _identifier(value: Any, *, label: str, wildcard: bool = False) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ProfilePolicyError(f"invalid {label}")
    if value == "*" and not wildcard:
        raise ProfilePolicyError(f"invalid {label}")
    return value


def _records(document: dict[str, Any], key: str, *, limit: int) -> list[Any]:
    value = document.get(key)
    if not isinstance(value, list) or len(value) > limit:
        raise ProfilePolicyError(f"invalid {key}")
    return value


def _reject_yaml_indirection(text: str) -> None:
    try:
        for event in yaml.parse(text):
            if isinstance(event, yaml.events.AliasEvent) or getattr(event, "anchor", None) is not None:
                raise ProfilePolicyError("invalid profile policy")
    except yaml.YAMLError as exc:
        raise ProfilePolicyError("invalid profile policy") from exc


def _parse_policy(raw: bytes) -> ProfilePolicy:
    if len(raw) > MAX_POLICY_BYTES:
        raise ProfilePolicyError("profile policy unavailable")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProfilePolicyError("invalid profile policy") from exc
    _reject_yaml_indirection(text)
    try:
        document = yaml.load(text, Loader=_UniqueKeyLoader)
    except yaml.YAMLError as exc:
        raise ProfilePolicyError("invalid profile policy") from exc
    if not isinstance(document, dict) or set(document) != {
        "version", "profiles", "actors", "memberships"
    }:
        raise ProfilePolicyError("invalid profile policy")
    if type(document["version"]) is not int or document["version"] != 1:
        raise ProfilePolicyError("unsupported profile policy version")

    profiles: dict[str, _Profile] = {}
    for item in _records(document, "profiles", limit=MAX_PROFILES):
        row = _record(item, fields=frozenset({"profile_id", "tenant_id", "kind", "enabled"}), label="profile")
        profile_id = _identifier(row["profile_id"], label="profile_id")
        tenant_id = _identifier(row["tenant_id"], label="tenant_id")
        kind = _identifier(row["kind"], label="kind")
        if type(row["enabled"]) is not bool or profile_id in profiles:
            raise ProfilePolicyError("invalid profile")
        profiles[profile_id] = _Profile(tenant_id, kind, row["enabled"])

    actors: dict[tuple[str, str], str] = {}
    for item in _records(document, "actors", limit=MAX_ACTORS):
        row = _record(item, fields=frozenset({"tenant_id", "actor_id", "default_profile"}), label="actor")
        tenant_id = _identifier(row["tenant_id"], label="tenant_id")
        actor_id = _identifier(row["actor_id"], label="actor_id")
        default_profile = _identifier(row["default_profile"], label="default_profile")
        profile = profiles.get(default_profile)
        key = (tenant_id, actor_id)
        if profile is None or profile.tenant_id != tenant_id or key in actors:
            raise ProfilePolicyError("invalid actor")
        actors[key] = default_profile

    grants: dict[tuple[str, str, str], frozenset[str]] = {}
    for item in _records(document, "memberships", limit=MAX_MEMBERSHIPS):
        row = _record(item, fields=frozenset({"tenant_id", "actor_id", "profile_id", "actions"}), label="membership")
        tenant_id = _identifier(row["tenant_id"], label="tenant_id")
        actor_id = _identifier(row["actor_id"], label="actor_id")
        profile_id = _identifier(row["profile_id"], label="profile_id", wildcard=True)
        actions = row["actions"]
        grant_key = (tenant_id, actor_id, profile_id)
        if ((tenant_id, actor_id) not in actors or not isinstance(actions, list)
                or not actions or len(actions) > MAX_ACTIONS_PER_MEMBERSHIP
                or grant_key in grants):
            raise ProfilePolicyError("invalid membership")
        if profile_id != "*":
            profile = profiles.get(profile_id)
            if profile is None or profile.tenant_id != tenant_id:
                raise ProfilePolicyError("invalid membership")
        action_set: set[str] = set()
        for action in actions:
            if not isinstance(action, str) or action not in _ACTION_SET or action in action_set:
                raise ProfilePolicyError("invalid membership")
            action_set.add(action)
        grants[grant_key] = frozenset(action_set)

    return ProfilePolicy(
        revision=hashlib.sha256(raw).hexdigest(),
        _profiles=MappingProxyType(profiles),
        _actors=MappingProxyType(actors),
        _grants=MappingProxyType(grants),
    )


def _read_trusted_file(path: str, *, max_bytes: int, missing_ok: bool) -> bytes | None:
    if not isinstance(path, str) or not os.path.isabs(path):
        raise ProfilePolicyError("profile policy unavailable")
    flags = os.O_RDONLY
    for flag_name in ("O_CLOEXEC", "O_NOFOLLOW", "O_NONBLOCK"):
        flags |= getattr(os, flag_name, 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if missing_ok and exc.errno == errno.ENOENT:
            return None
        raise ProfilePolicyError("profile policy unavailable") from exc
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != TRUSTED_ROOT_UID
                or before.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                or before.st_size < 0 or before.st_size > max_bytes):
            raise ProfilePolicyError("profile policy unavailable")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(fd, min(65_536, remaining))
            if not chunk:
                raise ProfilePolicyError("profile policy unavailable")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(fd, 1):
            raise ProfilePolicyError("profile policy unavailable")
        after = os.fstat(fd)
        raw = b"".join(chunks)
        if ((before.st_dev, before.st_ino, before.st_size)
                != (after.st_dev, after.st_ino, after.st_size)
                or len(raw) != before.st_size or len(raw) > max_bytes):
            raise ProfilePolicyError("profile policy unavailable")
        return raw
    except OSError as exc:
        raise ProfilePolicyError("profile policy unavailable") from exc
    finally:
        os.close(fd)


def load_profile_policy() -> ProfilePolicy:
    """Load the exact bounded root-managed policy snapshot."""
    raw = _read_trusted_file(PROFILE_POLICY_PATH, max_bytes=MAX_POLICY_BYTES, missing_ok=False)
    if raw is None:  # pragma: no cover - missing_ok is false
        raise ProfilePolicyError("profile policy unavailable")
    return _parse_policy(raw)


def resolve_effective_authority(session: Any, policy: ProfilePolicy) -> EffectiveAuthority:
    return policy.authority_for(session)


def resolve_configured_authority(session: Any) -> EffectiveAuthority | None:
    """Resolve fixed root authority, or legacy mode only when its marker is absent."""
    marker = _read_trusted_file(REQUIRED_MARKER_PATH, max_bytes=MAX_MARKER_BYTES, missing_ok=True)
    if marker is None:
        return None
    if marker != _REQUIRED_MARKER:
        raise ProfilePolicyError("profile policy unavailable")
    return resolve_effective_authority(session, load_profile_policy())
