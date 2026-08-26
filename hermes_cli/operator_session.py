from __future__ import annotations

import json
import os
import time
from typing import Any

ENV_OPERATOR_SESSION_CONTEXT = "HERMES_OPERATOR_SESSION_CONTEXT"
_CURRENT_OPERATOR_SESSION_CONTEXT: dict[str, Any] | None = None


def build_operator_session_context(
    *, session_id: str, interface: str = "cli", now: int | None = None,
    acting_for: str = "aiwerk", ttl_seconds: int = 900,
) -> dict[str, Any]:
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("session_id is required")
    issued_at = int(time.time()) if now is None else int(now)
    return {
        "mode": "operator_session",
        "actor_id": "unknown_cli",
        "role": "unknown",
        "acting_for": acting_for,
        "memory_scope": "none",
        "authorizing": False,
        "interface": interface,
        "session_id": session_id,
        "provenance": "trusted_cli_bootstrap",
        "issued_at": issued_at,
        "expires_at": issued_at + max(1, int(ttl_seconds)),
    }


def bootstrap_operator_session(
    *, session_id: str | None = None, acting_for: str = "aiwerk",
    interface: str = "cli", quiet: bool = False,
) -> dict[str, Any]:
    del quiet
    global _CURRENT_OPERATOR_SESSION_CONTEXT
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("session_id is required")
    context = build_operator_session_context(
        session_id=session_id, interface=interface, acting_for=acting_for,
    )
    _CURRENT_OPERATOR_SESSION_CONTEXT = dict(context)
    exported = dict(context, bootstrap_pid=os.getpid())
    os.environ[ENV_OPERATOR_SESSION_CONTEXT] = json.dumps(
        exported, sort_keys=True, separators=(",", ":"),
    )
    return dict(context)


def get_current_operator_session_context() -> dict[str, Any] | None:
    global _CURRENT_OPERATOR_SESSION_CONTEXT
    context = _CURRENT_OPERATOR_SESSION_CONTEXT
    if not isinstance(context, dict):
        return None
    if int(context.get("expires_at") or 0) <= int(time.time()):
        _CURRENT_OPERATOR_SESSION_CONTEXT = None
        return None
    return dict(context)


def load_operator_session_context_from_env() -> None:
    """Environment context is descriptive transport only, never authority."""
    return None


def clear_operator_session_context() -> None:
    global _CURRENT_OPERATOR_SESSION_CONTEXT
    _CURRENT_OPERATOR_SESSION_CONTEXT = None
    os.environ.pop(ENV_OPERATOR_SESSION_CONTEXT, None)
