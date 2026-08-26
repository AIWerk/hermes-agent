"""Canonical authenticated actor context for CUI gateway flows."""

from __future__ import annotations

import contextvars
import json
import os
from typing import Any, Mapping

ACTOR_IDENTITY_KEYS = (
    "tenant_id",
    "actor_id",
    "role",
    "display_name",
    "user_id",
    "provider",
)

_actor_context: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "cui_actor_context",
    default=None,
)


def sanitize_cui_actor_context(actor: Mapping[str, Any] | None) -> dict[str, str]:
    """Return only non-empty values from the fixed actor identity allowlist."""
    clean: dict[str, str] = {}
    for key in ACTOR_IDENTITY_KEYS:
        value = (actor or {}).get(key)
        if value is not None and str(value).strip():
            clean[key] = str(value).strip()
    return clean


def bind_cui_actor_context(
    actor: Mapping[str, Any] | None,
) -> contextvars.Token[dict[str, str] | None]:
    """Bind sanitized server identity for this logical flow, including empty."""
    return _actor_context.set(sanitize_cui_actor_context(actor))


def reset_cui_actor_context(
    token: contextvars.Token[dict[str, str] | None],
) -> None:
    """Restore the binding captured by *token*."""
    _actor_context.reset(token)


def current_cui_actor_context() -> dict[str, str]:
    """Read flow-local identity, or the subprocess/CLI environment fallback."""
    bound = _actor_context.get()
    if bound is not None:
        return dict(bound)

    data: dict[str, Any] = {}
    raw = os.getenv("AIWERK_CUI_ACTOR_CONTEXT", "")
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                data = parsed
        except (TypeError, ValueError):
            pass
    for env_key, actor_key in (
        ("AIWERK_CUI_TENANT_ID", "tenant_id"),
        ("AIWERK_CUI_ACTOR_ID", "actor_id"),
        ("AIWERK_CUI_ACTOR_ROLE", "role"),
    ):
        value = os.getenv(env_key, "")
        if value and not data.get(actor_key):
            data[actor_key] = value
    return sanitize_cui_actor_context(data)
