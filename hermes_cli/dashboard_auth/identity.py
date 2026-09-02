"""Canonical authenticated dashboard identity and role policy."""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Literal

NormalizedRole = Literal["admin", "customer", "owner", "tenant_admin", "support"]

ADMIN_ROLE_ALIASES = frozenset({"admin", "aiwerk_admin", "operator"})
CUSTOMER_ROLE_ALIASES = frozenset({"user", "customer", "tenant_user", "member"})
STAFF_ROLE_ALIASES: dict[str, NormalizedRole] = {
    "owner": "owner",
    "tenant_admin": "tenant_admin",
    "support": "support",
}
SESSION_ADMIN_ROLE_ALIASES = frozenset({"admin", "owner", "operator"})
RUNTIME_MUTATION_ADMIN_ROLE_ALIASES = ADMIN_ROLE_ALIASES


def normalize_role(role: Any) -> NormalizedRole | None:
    """Map an explicit recognized alias to its canonical authorization role."""
    if not isinstance(role, str):
        return None
    value = role.strip().lower()
    if value in ADMIN_ROLE_ALIASES:
        return "admin"
    if value in CUSTOMER_ROLE_ALIASES:
        return "customer"
    return STAFF_ROLE_ALIASES.get(value)


def _identity_value(identity: Any, key: str) -> Any:
    if isinstance(identity, Mapping):
        return identity.get(key)
    return getattr(identity, key, None)


def is_authenticated_actor_identity(identity: Any) -> bool:
    """Require a non-empty actor identifier and an explicitly recognized role."""
    actor_id = _identity_value(identity, "actor_id")
    return (
        isinstance(actor_id, str)
        and bool(actor_id.strip())
        and normalize_role(_identity_value(identity, "role")) is not None
    )


def is_complete_authenticated_identity(identity: Any) -> bool:
    """Require tenant authority in addition to an authenticated actor."""
    if not is_authenticated_actor_identity(identity):
        return False
    tenant_id = _identity_value(identity, "tenant_id")
    return isinstance(tenant_id, str) and bool(tenant_id.strip())


def _first_name(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    value = re.sub(r"[\x00-\x1f\x7f]+", " ", raw)
    value = re.sub(r"\s+", " ", value).strip(" \t\r\n'\"`<>;{}[]()")
    if not value or "{{" in value or "}}" in value:
        return None
    match = re.match(r"[A-Za-zÀ-ÖØ-öø-ÿ'’-]{2,40}", value)
    return match.group(0) if match else None


def greeting_identity_from_session(session: Any) -> dict[str, str | None]:
    """Return a neutral greeting identity derived only from a complete session."""
    if not is_complete_authenticated_identity(session):
        return {"name": None, "context": "unknown"}
    role = normalize_role(_identity_value(session, "role"))
    context = "customer" if role == "customer" else "admin"
    return {
        "name": _first_name(_identity_value(session, "display_name")),
        "context": context,
    }
