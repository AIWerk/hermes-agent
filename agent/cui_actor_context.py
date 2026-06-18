"""Authenticated AIWerk CUI actor-context helpers."""

from __future__ import annotations

import json
import os
from typing import Any, Dict

_ADMIN_ROLES = {"admin", "operator", "owner", "support"}


def current_cui_actor_context() -> Dict[str, str]:
    """Return sanitized actor context injected by the authenticated CUI."""
    raw = os.getenv("AIWERK_CUI_ACTOR_CONTEXT", "") or ""
    data: Dict[str, Any] = {}
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                data = parsed
        except Exception:
            data = {}
    for env_key, out_key in (
        ("AIWERK_CUI_TENANT_ID", "tenant_id"),
        ("AIWERK_CUI_ACTOR_ID", "actor_id"),
        ("AIWERK_CUI_ACTOR_ROLE", "role"),
    ):
        value = os.getenv(env_key, "")
        if value and not data.get(out_key):
            data[out_key] = value
    clean: Dict[str, str] = {}
    for key in ("tenant_id", "actor_id", "role", "display_name", "user_id", "provider"):
        value = data.get(key)
        if value is not None and str(value).strip():
            clean[key] = str(value).strip()
    return clean


def is_aiwerk_admin_actor(actor: Dict[str, str] | None = None) -> bool:
    """True for authenticated AIWerk admin/operator/support actors."""
    actor = actor if actor is not None else current_cui_actor_context()
    role = (actor.get("role") or "").strip().lower()
    actor_id = (actor.get("actor_id") or "").strip().lower()
    return role in _ADMIN_ROLES or actor_id.startswith("aiwerk:")


def cui_actor_system_prompt(actor: Dict[str, str] | None = None) -> str:
    """Prompt-visible boundary between customer and admin CUI sessions."""
    actor = actor if actor is not None else current_cui_actor_context()
    if not actor:
        return ""
    tenant_id = actor.get("tenant_id", "")
    actor_id = actor.get("actor_id") or actor.get("user_id", "")
    role = actor.get("role", "")
    display_name = actor.get("display_name") or actor.get("user_id") or actor_id
    base = (
        "Authenticated AIWerk CUI actor context: "
        f"current_human={display_name!r}, actor_id={actor_id!r}, "
        f"role={role!r}, tenant_id={tenant_id!r}."
    )
    if is_aiwerk_admin_actor(actor):
        return (
            base + " The current human is an AIWerk admin/operator, not the primary customer user. "
            "Do NOT address or model the current human as the customer user. "
            "Treat customer profile/memory as information about the tenant customer, not as the current speaker's identity. "
            "Do not write admin/operator conversation facts to customer USER.md, customer memory, or Honcho 'user' peer. "
            "Durable admin/support notes belong only in explicit audit/operator notes or sanitized AIWerk wiki/SOP after approval."
        )
    return (
        base + " The current human is the authenticated customer/user for this tenant. "
        "Customer-scoped memory may be used according to the tenant memory policy."
    )


def memory_write_blocked_for_cui_admin(function_name: str, args: dict | None) -> bool:
    """Block customer-memory writes from authenticated CUI admin sessions."""
    if not is_aiwerk_admin_actor():
        return False
    name = (function_name or "").lower()
    action = str((args or {}).get("action") or "").lower()
    if name == "memory" and action in {"add", "replace", "remove"}:
        return True
    if name in {"honcho_conclude", "mem0_conclude"}:
        return True
    if name == "fact_store" and action in {"add", "update", "remove", "delete"}:
        return True
    return False


def cui_admin_memory_block_result(function_name: str) -> str:
    return json.dumps({
        "success": False,
        "error": (
            f"{function_name} writes are disabled in AIWerk CUI admin/operator sessions. "
            "Admin/support conversation facts must not be stored in the customer user's memory. "
            "Use an explicit audit/operator note or sanitized AIWerk wiki/SOP route instead."
        ),
        "blocked_by": "cui_admin_actor_memory_guard",
    })
