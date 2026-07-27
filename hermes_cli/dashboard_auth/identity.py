"""Authenticated actor identity helpers for private dashboard responses."""
from __future__ import annotations

import re
from typing import Any, Optional

_ADMIN_ROLES = frozenset({"admin", "owner", "aiwerk_admin", "tenant_admin", "operator", "support"})
_CUSTOMER_ROLES = frozenset({"user", "customer", "tenant_user", "member"})


def _first_name(raw: Any) -> Optional[str]:
    if not isinstance(raw, str):
        return None
    value = re.sub(r"[\x00-\x1f\x7f]+", " ", raw)
    value = re.sub(r"\s+", " ", value).strip(" \t\r\n'\"`<>;{}[]()")
    if not value or "{{" in value or "}}" in value:
        return None
    match = re.match(r"[A-Za-zÀ-ÖØ-öø-ÿ'’-]{2,40}", value)
    if not match:
        return None
    return match.group(0)


def greeting_identity_from_session(
    session: Any, *, role: Optional[str] = None,
) -> dict[str, Optional[str]]:
    """Return a fail-closed greeting identity from a verified auth session."""
    if session is None:
        return {"name": None, "context": "unknown"}
    resolved_role = str(
        role if role is not None else getattr(session, "role", "") or ""
    ).strip().lower()
    if resolved_role in _ADMIN_ROLES:
        return {"name": _first_name(getattr(session, "display_name", "")), "context": "admin"}
    if resolved_role in _CUSTOMER_ROLES:
        return {"name": _first_name(getattr(session, "display_name", "")), "context": "customer"}
    return {"name": None, "context": "unknown"}
