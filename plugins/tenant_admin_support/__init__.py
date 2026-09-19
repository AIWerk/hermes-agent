"""Tenant-admin delegated diagnosis and protected behavior-policy changes."""
from __future__ import annotations

import difflib
import errno
import json
import os
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable

from agent.cui_actor_context import current_bound_cui_actor_context
from hermes_cli.dashboard_auth import behavior_policy, profile_access, profile_policy
from hermes_cli.dashboard_auth.admin_session_reader import AdminReadUnavailable, read_sessions

_TOOL_NAME = "tenant_admin_support"
_PROMPT_SECTION_ID = "tenant-admin-support.behavior-policy"
_PREVIEW_TTL_SECONDS = 600.0
_MAX_PREVIEWS = 128


@dataclass(frozen=True, slots=True)
class _Preview:
    created_at: float
    tenant_id: str
    actor_id: str
    target_profile: str
    instructions: str
    digest: str
    byte_count: int
    base_revision: int
    authorization_revision: str


_previews: "OrderedDict[str, _Preview]" = OrderedDict()
_preview_lock = threading.Lock()


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _record_payload(record: behavior_policy.PolicyRecord) -> dict[str, Any]:
    return {
        "revision": record.revision,
        "content_digest": record.content_digest,
        "instructions": record.instructions,
        "created_at": record.created_at,
        "created_by": record.created_by,
        "rollback_of": record.rollback_of,
        "byte_count": len(record.instructions.encode("utf-8")),
    }


def _current_actor(explicit: Any = None) -> Any:
    # explicit is an internal test seam, never populated by registry/model arguments.
    return current_bound_cui_actor_context() if explicit is None else explicit


def _authorize(actor: Any, action: str, target_profile: Any):
    decision = profile_access.authorize(actor, action, target_profile)
    if decision is None:
        raise profile_access.ProfileAccessDenied("profile access denied")
    return decision


def _store_root_missing(exc: BaseException) -> bool:
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, OSError) and current.errno == errno.ENOENT:
            return True
        current = current.__cause__
    return False


def _read_current_for_preview(decision) -> behavior_policy.PolicyRecord | None:
    try:
        return behavior_policy.Store().read_current(decision.tenant_id, decision.target_profile)
    except behavior_policy.BehaviorPolicyStoreError as exc:
        # A deployment may enable authorization before provisioning an empty store.
        # Preview remains side-effect free; apply still requires the provisioned root.
        if _store_root_missing(exc):
            return None
        raise


def _continuation(payload: dict[str, Any], offset: int) -> dict[str, int | bool]:
    count = len(payload.get("messages", payload.get("sessions", ())))
    return {"offset": offset + count, "has_more": bool(payload.get("has_more", False))}


def _session_operation(actor: Any, operation: str, target: str, args: dict[str, Any]) -> dict[str, Any]:
    broker_operation = operation.removeprefix("session_")
    offset = args.get("offset", 0)
    payload = read_sessions(
        actor, target, broker_operation,
        session_id=args.get("session_id"), query=args.get("query"),
        limit=args.get("limit", 50), offset=offset,
    )
    payload["continuation"] = _continuation(payload, offset)
    return payload


def _preview(actor: Any, target: str, args: dict[str, Any]) -> dict[str, Any]:
    read_decision = _authorize(actor, "behavior.read", target)
    write_decision = _authorize(actor, "behavior.write", target)
    if (read_decision.tenant_id, read_decision.actor_id) != (
        write_decision.tenant_id, write_decision.actor_id
    ):
        raise profile_access.ProfileAccessDenied("profile access denied")
    instructions = behavior_policy.validate_instructions(args.get("instructions"))
    current = _read_current_for_preview(write_decision)
    base_revision = current.revision if current else 0
    expected = args.get("expected_revision")
    if expected is not None and (type(expected) is not int or expected != base_revision):
        raise behavior_policy.BehaviorPolicyConflict("behavior policy conflict")
    digest = behavior_policy.policy_digest(instructions)
    previous = current.instructions if current else ""
    diff = "".join(difflib.unified_diff(
        previous.splitlines(keepends=True), instructions.splitlines(keepends=True),
        fromfile=f"revision-{base_revision}", tofile=f"revision-{base_revision + 1}", n=3,
    ))
    token = uuid.uuid4().hex
    preview = _Preview(
        created_at=time.monotonic(), tenant_id=write_decision.tenant_id,
        actor_id=write_decision.actor_id, target_profile=write_decision.target_profile,
        instructions=instructions, digest=digest,
        byte_count=len(instructions.encode("utf-8")), base_revision=base_revision,
        authorization_revision=write_decision.policy_revision,
    )
    with _preview_lock:
        _prune_previews_locked(time.monotonic())
        _previews[token] = preview
        while len(_previews) > _MAX_PREVIEWS:
            _previews.popitem(last=False)
    behavior_policy.audit_behavior_event(
        actor=actor, target_profile=target, operation="preview", result="allowed",
        authorization_revision=write_decision.policy_revision, base_revision=base_revision,
        content_digest=digest, byte_count=preview.byte_count,
    )
    return {
        "current_revision": base_revision,
        "proposed_revision": base_revision + 1,
        "proposed_text": instructions,
        "diff": diff,
        "digest": digest,
        "byte_count": preview.byte_count,
        "preview_token": token,
    }


def _prune_previews_locked(now: float) -> None:
    expired = [token for token, item in _previews.items()
               if now - item.created_at > _PREVIEW_TTL_SECONDS]
    for token in expired:
        _previews.pop(token, None)


def expire_previews_for_test(seconds: float) -> None:
    """Age process-local previews without changing the system clock."""
    with _preview_lock:
        for token, item in tuple(_previews.items()):
            _previews[token] = _Preview(
                item.created_at - seconds, item.tenant_id, item.actor_id,
                item.target_profile, item.instructions, item.digest, item.byte_count,
                item.base_revision, item.authorization_revision,
            )


def _resolve_preview(token: Any, actor: Any, target: str) -> _Preview:
    if type(token) is not str or not token:
        raise ValueError("preview token required")
    now = time.monotonic()
    with _preview_lock:
        item = _previews.get(token)
        if item is not None and now - item.created_at > _PREVIEW_TTL_SECONDS:
            _previews.pop(token, None)
            raise TimeoutError("preview expired")
    if item is None:
        raise ValueError("invalid preview token")
    actor_tenant = actor.get("tenant_id") if isinstance(actor, dict) else getattr(actor, "tenant_id", None)
    actor_id = actor.get("actor_id") if isinstance(actor, dict) else getattr(actor, "actor_id", None)
    if (actor_tenant, actor_id, target) != (item.tenant_id, item.actor_id, item.target_profile):
        raise profile_access.ProfileAccessDenied("profile access denied")
    return item


def _approval(metadata: dict[str, Any]) -> str:
    from tools.approval_prompt import request_elicitation_consent

    summary = (
        f"Apply tenant behavior policy to {metadata['target_profile']} at revision "
        f"{metadata['proposed_revision']} (sha256 {metadata['content_digest']}, "
        f"{metadata['byte_count']} bytes)?"
    )
    return request_elicitation_consent(
        summary,
        "Approve this behavior-only policy mutation for newly created sessions.",
        surface="tenant-behavior-policy",
    )


def _apply(
    actor: Any, target: str, args: dict[str, Any],
    approval_fn: Callable[..., str] | None,
) -> dict[str, Any]:
    item = _resolve_preview(args.get("preview_token"), actor, target)
    first = _authorize(actor, "behavior.write", target)
    if (first.tenant_id, first.actor_id, first.target_profile) != (
        item.tenant_id, item.actor_id, item.target_profile
    ):
        raise profile_access.ProfileAccessDenied("profile access denied")
    metadata = {
        "target_profile": target,
        "proposed_revision": item.base_revision + 1,
        "revision": item.base_revision + 1,
        "expected_revision": item.base_revision,
        "content_digest": item.digest,
        "byte_count": item.byte_count,
    }
    consent = approval_fn(**metadata) if approval_fn is not None else _approval(metadata)
    if consent != "accept":
        behavior_policy.audit_behavior_event(
            actor=actor, target_profile=target, operation="apply", result="declined",
            authorization_revision=first.policy_revision, base_revision=item.base_revision,
            content_digest=item.digest, byte_count=item.byte_count,
        )
        return {"error": "approval denied"}
    second = _authorize(actor, "behavior.write", target)
    if (second.tenant_id, second.actor_id, second.target_profile) != (
        item.tenant_id, item.actor_id, item.target_profile
    ):
        raise profile_access.ProfileAccessDenied("profile access denied")
    store = behavior_policy.Store()
    committed = store.commit(
        tenant_id=item.tenant_id, target_profile=item.target_profile,
        instructions=item.instructions, actor_id=item.actor_id,
        expected_revision=item.base_revision,
    )
    readback = store.read_current(item.tenant_id, item.target_profile)
    if (readback is None or readback.revision != committed.revision
            or readback.content_digest != committed.content_digest
            or readback.content_digest != item.digest):
        raise behavior_policy.BehaviorPolicyStoreError("behavior policy store unavailable")
    with _preview_lock:
        _previews.pop(args.get("preview_token"), None)
    behavior_policy.audit_behavior_event(
        actor=actor, target_profile=target, operation="apply", result="success",
        authorization_revision=second.policy_revision, base_revision=item.base_revision,
        result_revision=readback.revision, content_digest=readback.content_digest,
        byte_count=item.byte_count,
    )
    return {
        "revision": readback.revision,
        "digest": readback.content_digest,
        "effective": "new_sessions_only",
    }


def _rollback(
    actor: Any, target: str, args: dict[str, Any],
    approval_fn: Callable[..., str] | None,
) -> dict[str, Any]:
    revision = args.get("revision")
    expected_revision = args.get("expected_revision")
    decision = _authorize(actor, "behavior.write", target)

    def approve(**metadata: Any) -> str:
        safe = {
            "target_profile": target,
            "proposed_revision": expected_revision + 1 if type(expected_revision) is int else None,
            **metadata,
        }
        return approval_fn(**safe) if approval_fn is not None else _approval(safe)

    record = behavior_policy.rollback(
        actor, target, revision, expected_revision, approval_fn=approve,
    )
    if record is None:
        return {"error": "approval denied"}
    behavior_policy.audit_behavior_event(
        actor=actor, target_profile=target, operation="rollback", result="success",
        authorization_revision=decision.policy_revision,
        base_revision=expected_revision, result_revision=record.revision,
        content_digest=record.content_digest,
        byte_count=len(record.instructions.encode("utf-8")),
    )
    return {
        "revision": record.revision,
        "digest": record.content_digest,
        "rollback_of": record.rollback_of,
        "effective": "new_sessions_only",
    }


def _behavior_read(actor: Any, operation: str, target: str, args: dict[str, Any]) -> dict[str, Any]:
    if operation == "behavior_history":
        try:
            items = behavior_policy.history(
                actor, target, limit=args.get("limit", 100),
                before_revision=args.get("before_revision"),
            )
        except behavior_policy.BehaviorPolicyStoreError as exc:
            if not _store_root_missing(exc):
                raise
            items = []
        return {"items": items}
    record = behavior_policy.read_current(actor, target, revision=args.get("revision"))
    return {"policy": None} if record is None else _record_payload(record)


def handle(
    args: dict[str, Any], *, approval_fn: Callable[..., str] | None = None,
    actor: Any = None,
) -> str:
    """Dispatch one operation; runtime identity always comes from bound CUI context."""
    operation = args.get("operation") if isinstance(args, dict) else None
    target = args.get("target_profile") if isinstance(args, dict) else None
    current_actor = _current_actor(actor)
    try:
        if not profile_access.valid_profile(target):
            raise profile_access.ProfileAccessDenied("profile access denied")
        if operation in {"session_list", "session_search", "session_read"}:
            result = _session_operation(current_actor, operation, target, args)
        elif operation in {"behavior_get", "behavior_history"}:
            result = _behavior_read(current_actor, operation, target, args)
        elif operation == "behavior_preview":
            result = _preview(current_actor, target, args)
        elif operation == "behavior_apply":
            if "instructions" in args:
                raise ValueError("behavior_apply accepts preview_token only")
            result = _apply(current_actor, target, args, approval_fn)
        elif operation == "behavior_rollback":
            result = _rollback(current_actor, target, args, approval_fn)
        else:
            result = {"error": "invalid operation"}
    except (profile_access.ProfileAccessDenied, behavior_policy.BehaviorPolicyDenied):
        result = {"error": "profile access denied"}
    except TimeoutError as exc:
        result = {"error": str(exc)}
    except behavior_policy.BehaviorPolicyConflict:
        result = {"error": "behavior policy conflict"}
    except behavior_policy.BehaviorPolicyValidationError:
        result = {"error": "invalid behavior policy request"}
    except behavior_policy.BehaviorPolicyStoreError:
        result = {"error": "behavior policy store unavailable"}
    except AdminReadUnavailable:
        result = {"error": "session data unavailable"}
    except (TypeError, ValueError):
        result = {"error": "invalid request"}
    return _json(result)


def _service_ready() -> bool:
    """Passive process-wide readiness only; actor authorization stays in handlers."""
    try:
        marker = profile_policy._read_trusted_file(
            profile_policy.REQUIRED_MARKER_PATH,
            max_bytes=profile_policy.MAX_MARKER_BYTES,
            missing_ok=True,
        )
        if marker != profile_policy._REQUIRED_MARKER:
            return False
        root = os.stat(behavior_policy.BEHAVIOR_POLICY_ROOT, follow_symlinks=False)
        return root.st_uid == behavior_policy.TRUSTED_SERVICE_UID and not (
            root.st_mode & 0o077
        )
    except (OSError, profile_policy.ProfilePolicyError, TypeError, ValueError):
        return False


def _render_behavior_section(session_info) -> str | None:
    profile_name = session_info.get("profile_name")
    if not profile_access.valid_profile(profile_name):
        return None
    return behavior_policy.render_for_profile(profile_name)


_SCHEMA = {
    "name": _TOOL_NAME,
    "description": (
        "Diagnose an explicitly targeted employee session through the delegated read broker and manage "
        "behavior-only assistant instructions. Use behavior policy only for response tone, interaction "
        "procedure, and user-facing handling. Tenant facts, prices, catalog content, and document truth "
        "must use a separately authorized knowledge/document flow; do not silently invoke one. Software "
        "or code defects require an implementation workflow, not behavior policy; do not silently invoke it."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "operation": {
                "type": "string",
                "enum": [
                    "session_list", "session_search", "session_read", "behavior_get",
                    "behavior_history", "behavior_preview", "behavior_apply", "behavior_rollback",
                ],
                "description": "Exact delegated-read or behavior-policy operation.",
            },
            "target_profile": {
                "type": "string",
                "description": "Explicit typed employee profile id; never infer, default, use current, all, or wildcard.",
            },
            "session_id": {"type": "string", "description": "Required only for session_read."},
            "query": {"type": "string", "description": "Required only for session_search."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            "offset": {"type": "integer", "minimum": 0, "maximum": 1000000},
            "revision": {"type": "integer", "minimum": 1},
            "before_revision": {"type": "integer", "minimum": 1},
            "expected_revision": {"type": "integer", "minimum": 0},
            "instructions": {
                "type": "string",
                "description": "Exact behavior-only text for behavior_preview; never facts, documents, prices, catalog truth, or code fixes.",
            },
            "preview_token": {
                "type": "string",
                "description": "Opaque token from behavior_preview; behavior_apply accepts this token and no policy text.",
            },
        },
        "required": ["operation", "target_profile"],
        "dependentSchemas": {
            "behavior_apply": {
                "properties": {"preview_token": {"type": "string"}},
                "required": ["preview_token"],
            }
        },
    },
}


def register(ctx) -> None:
    ctx.register_tool(
        name=_TOOL_NAME,
        toolset="desktop_ui",
        schema=_SCHEMA,
        handler=handle,
        check_fn=_service_ready,
        description="Delegated tenant diagnosis and protected behavior policy management.",
        emoji="🛡️",
    )
    ctx.register_system_prompt_section(
        _PROMPT_SECTION_ID,
        _render_behavior_section,
        position="after_memory",
        max_chars=4_000,
    )
