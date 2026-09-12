"""Deterministic policy for routing candidate durable memory.

The router is local and side-effect free.  Built-in prompt memory and Honcho
mirroring are enforced destinations; tenant/wiki/skill/session destinations are
advisory classifications until a real isolated sink exists.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import re
from typing import Any, Iterable, Mapping

from agent.secret_patterns import SECRET_DETECT_RE as _SECRET_RE
from agent.secret_patterns import contains_secret as _contains_secret


class MemoryDestination(str, Enum):
    INJECT = "inject"
    STORE_HONCHO = "store_honcho"
    EXPLICIT_RECALL_ONLY = "explicit_recall_only"
    SESSION_INDEX = "session_index"
    WIKI_CANDIDATE = "wiki_candidate"
    SKILL_CANDIDATE = "skill_candidate"
    TENANT_PRIVATE = "tenant_private"
    DISCARD = "discard"


class MemorySensitivity(str, Enum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CUSTOMER = "customer"
    CREDENTIAL = "credential"
    PERSONAL = "personal"


class MemoryDurability(str, Enum):
    TEMPORARY = "temporary"
    SESSION = "session"
    DURABLE = "durable"


@dataclass(frozen=True)
class MemoryRoute:
    destinations: tuple[MemoryDestination, ...]
    sensitivity: MemorySensitivity
    durability: MemoryDurability
    confidence: float
    reason: str
    scope: str = "hermes"
    target_hint: str | None = None
    inject_allowed: bool = False
    honcho_store_allowed: bool = False
    shared_wiki_allowed: bool = False
    tenant_private_required: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def has(self, destination: MemoryDestination | str) -> bool:
        if isinstance(destination, str):
            destination = MemoryDestination(destination)
        return destination in self.destinations

    def to_dict(self) -> dict[str, Any]:
        return {
            "destinations": [destination.value for destination in self.destinations],
            "sensitivity": self.sensitivity.value,
            "durability": self.durability.value,
            "confidence": self.confidence,
            "reason": self.reason,
            "scope": self.scope,
            "target_hint": self.target_hint,
            "inject_allowed": self.inject_allowed,
            "honcho_store_allowed": self.honcho_store_allowed,
            "shared_wiki_allowed": self.shared_wiki_allowed,
            "tenant_private_required": self.tenant_private_required,
            "metadata": dict(self.metadata),
        }


_CUSTOMER_RE = re.compile(
    r"\b(customer|client|tenant|kunde|kundin|mandant|pilot customer|call handling|"
    r"receptionist script|address|private address|phone number|telefon|whatsapp)\b",
    re.IGNORECASE,
)
_AIWERK_PRODUCT_RE = re.compile(
    r"\b(AIWerk|Smart Website|Local Connector|tenant boundary|base[- ]agent|"
    r"product|architecture|SOP|strategy|onboarding|offer|go[- ]to[- ]market)\b",
    re.IGNORECASE,
)
_PROCEDURE_RE = re.compile(
    r"\b(workflow|runbook|procedure|steps?|checklist|pitfall|how to|reusable|"
    r"debugging pattern|deploy pattern|rollback-safe|preflight)\b",
    re.IGNORECASE,
)
_SESSION_PROGRESS_RE = re.compile(
    r"\b(PR #?\d+|issue #?\d+|commit [0-9a-f]{7,40}|fixed|implemented|"
    r"completed|phase \d+ done|today|yesterday|tomorrow|this session|"
    r"working tree|file count|test status|cost report|temporary TODO|in progress)\b",
    re.IGNORECASE,
)
_USER_PREF_RE = re.compile(
    r"\b(user|prefers|likes|dislikes|expects|wants|does not want|owns|household|"
    r"communication style|speaks|lives|timezone|role|building)\b",
    re.IGNORECASE,
)
_ENV_LESSON_RE = re.compile(
    r"\b(project uses|repo uses|environment|installed|tool quirk|API quirk|"
    r"config|provider|model|runtime|host|VPS|gateway|MCP|Honcho|Hermes|pytest)\b",
    re.IGNORECASE,
)
_RAW_DUMP_RE = re.compile(
    r"\b(raw transcript|conversation dump|full chat log|verbatim transcript|"
    r"memory-context|credentials dump|private dump)\b",
    re.IGNORECASE,
)
_PRIORITY_DESTINATION = {
    MemoryDestination.DISCARD: 100,
    MemoryDestination.TENANT_PRIVATE: 90,
    MemoryDestination.WIKI_CANDIDATE: 70,
    MemoryDestination.SKILL_CANDIDATE: 65,
    MemoryDestination.SESSION_INDEX: 60,
    MemoryDestination.INJECT: 50,
    MemoryDestination.STORE_HONCHO: 45,
    MemoryDestination.EXPLICIT_RECALL_ONLY: 40,
}


def _destinations(*values: MemoryDestination) -> tuple[MemoryDestination, ...]:
    return tuple(dict.fromkeys(values))


def _scope(metadata: Mapping[str, Any]) -> str:
    for key in ("scope", "agent_identity", "agent_workspace", "tenant_id", "customer_id"):
        value = metadata.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return "hermes"


def _route(
    *destinations: MemoryDestination,
    sensitivity: MemorySensitivity,
    durability: MemoryDurability,
    confidence: float,
    reason: str,
    scope: str,
    source: str,
    target: str,
    target_hint: str,
    inject_allowed: bool = False,
    honcho_store_allowed: bool = False,
    shared_wiki_allowed: bool = False,
    tenant_private_required: bool = False,
) -> MemoryRoute:
    return MemoryRoute(
        destinations=_destinations(*destinations),
        sensitivity=sensitivity,
        durability=durability,
        confidence=confidence,
        reason=reason,
        scope=scope,
        target_hint=target_hint,
        inject_allowed=inject_allowed,
        honcho_store_allowed=honcho_store_allowed,
        shared_wiki_allowed=shared_wiki_allowed,
        tenant_private_required=tenant_private_required,
        metadata={"source": source, "target": target},
    )


def classify_memory_route(
    content: str,
    *,
    source: str = "memory_tool",
    target: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> MemoryRoute:
    """Classify content conservatively without I/O or external calls."""
    text = (content or "").strip()
    meta = dict(metadata or {})
    route_scope = _scope(meta)
    route_source = source or str(meta.get("write_origin") or "memory_tool")
    route_target = (target or "").strip()
    common: dict[str, Any] = {
        "scope": route_scope,
        "source": route_source,
        "target": route_target,
    }

    if not text:
        return _route(
            MemoryDestination.DISCARD,
            sensitivity=MemorySensitivity.INTERNAL,
            durability=MemoryDurability.TEMPORARY,
            confidence=1.0,
            reason="empty content is not memory",
            target_hint="discard",
            **common,
        )
    if _contains_secret(text):
        return _route(
            MemoryDestination.DISCARD,
            sensitivity=MemorySensitivity.CREDENTIAL,
            durability=MemoryDurability.TEMPORARY,
            confidence=0.98,
            reason="credentials and secrets never enter durable or prompt-injected memory",
            target_hint="discard",
            **common,
        )
    if _RAW_DUMP_RE.search(text):
        return _route(
            MemoryDestination.SESSION_INDEX,
            MemoryDestination.DISCARD,
            sensitivity=MemorySensitivity.INTERNAL,
            durability=MemoryDurability.SESSION,
            confidence=0.92,
            reason="raw dumps belong in session search or sanitized source notes",
            target_hint="session_search_or_sanitized_source",
            **common,
        )

    has_tenant_metadata = bool(meta.get("tenant_id") or meta.get("customer_id"))
    keyword_only_customer = bool(
        _CUSTOMER_RE.search(text)
        and not _AIWERK_PRODUCT_RE.search(text)
        and not _USER_PREF_RE.search(text)
    )
    if has_tenant_metadata or keyword_only_customer:
        return _route(
            MemoryDestination.TENANT_PRIVATE,
            MemoryDestination.EXPLICIT_RECALL_ONLY,
            sensitivity=MemorySensitivity.CUSTOMER,
            durability=MemoryDurability.DURABLE,
            confidence=0.9 if has_tenant_metadata else 0.86,
            reason="customer or tenant facts require an isolated tenant-private sink",
            target_hint="tenant_private",
            tenant_private_required=True,
            **common,
        )
    if _SESSION_PROGRESS_RE.search(text):
        return _route(
            MemoryDestination.SESSION_INDEX,
            sensitivity=MemorySensitivity.INTERNAL,
            durability=MemoryDurability.SESSION,
            confidence=0.82,
            reason="temporary progress belongs in session search or project state",
            target_hint="session_search",
            **common,
        )
    if _PROCEDURE_RE.search(text):
        return _route(
            MemoryDestination.SKILL_CANDIDATE,
            MemoryDestination.EXPLICIT_RECALL_ONLY,
            sensitivity=MemorySensitivity.INTERNAL,
            durability=MemoryDurability.DURABLE,
            confidence=0.82,
            reason="reusable procedures belong in skills",
            target_hint="skill_candidate",
            **common,
        )
    if _AIWERK_PRODUCT_RE.search(text):
        return _route(
            MemoryDestination.WIKI_CANDIDATE,
            MemoryDestination.EXPLICIT_RECALL_ONLY,
            sensitivity=MemorySensitivity.INTERNAL,
            durability=MemoryDurability.DURABLE,
            confidence=0.8,
            reason="durable product or architecture knowledge belongs in sanitized wiki",
            target_hint="wiki_candidate",
            shared_wiki_allowed=True,
            **common,
        )
    if route_target == "user" and _USER_PREF_RE.search(text):
        return _route(
            MemoryDestination.INJECT,
            MemoryDestination.STORE_HONCHO,
            sensitivity=MemorySensitivity.PERSONAL,
            durability=MemoryDurability.DURABLE,
            confidence=0.78,
            reason="stable user preference or profile fact is eligible for compact injection",
            target_hint="user",
            inject_allowed=True,
            honcho_store_allowed=True,
            **common,
        )
    if route_target == "memory" and _ENV_LESSON_RE.search(text):
        return _route(
            MemoryDestination.INJECT,
            MemoryDestination.STORE_HONCHO,
            sensitivity=MemorySensitivity.INTERNAL,
            durability=MemoryDurability.DURABLE,
            confidence=0.72,
            reason="stable environment or tooling fact can reduce future steering",
            target_hint="memory",
            inject_allowed=True,
            honcho_store_allowed=True,
            **common,
        )
    return _route(
        MemoryDestination.EXPLICIT_RECALL_ONLY,
        sensitivity=MemorySensitivity.INTERNAL,
        durability=MemoryDurability.SESSION,
        confidence=0.55,
        reason="no high-signal durable-memory rule matched",
        target_hint="explicit_recall_only",
        **common,
    )


def should_write_builtin_memory(
    content: str,
    *,
    target: str,
    metadata: Mapping[str, Any] | None = None,
) -> tuple[bool, MemoryRoute]:
    route = classify_memory_route(
        content, source="memory_tool", target=target, metadata=metadata
    )
    return route.inject_allowed and route.has(MemoryDestination.INJECT), route


def should_mirror_to_honcho(
    content: str,
    *,
    target: str = "user",
    metadata: Mapping[str, Any] | None = None,
) -> tuple[bool, MemoryRoute]:
    route = classify_memory_route(
        content, source="memory_mirror", target=target, metadata=metadata
    )
    return route.honcho_store_allowed and route.has(MemoryDestination.STORE_HONCHO), route


def dominant_destination(destinations: Iterable[MemoryDestination]) -> MemoryDestination:
    return max(destinations, key=lambda value: _PRIORITY_DESTINATION.get(value, 0))


def contains_secret(content: str) -> bool:
    """Use the canonical full-payload, bounded secret detector."""
    return _contains_secret(content)


def _install_memory_tool_router_adapter() -> None:
    """Restore split memory-tool gate names without editing the tool module."""
    try:
        import tools.memory_tool as memory_tool
    except Exception:
        return
    if not hasattr(memory_tool, "_apply_batch_write_gate"):
        memory_tool._apply_batch_write_gate = getattr(
            memory_tool, "_apply_write_gate", lambda *args, **kwargs: None
        )


def _install_honcho_router_adapter() -> None:
    """Bind Honcho's write surfaces to this router when the plugin is present."""
    try:
        from agent.cui_actor_context import (
            cui_admin_memory_block_result,
            memory_write_blocked_for_cui_admin,
        )
        from plugins.memory import honcho
        from plugins.memory.honcho import HonchoMemoryProvider
        from tools.registry import tool_error
    except Exception:
        return
    if getattr(HonchoMemoryProvider, "_agent_memory_router_adapter", False):
        return

    original_sync_turn = HonchoMemoryProvider.sync_turn
    original_on_memory_write = HonchoMemoryProvider.on_memory_write
    original_tool_profile = HonchoMemoryProvider._tool_profile
    original_tool_conclude = HonchoMemoryProvider._tool_conclude

    def _on_memory_write(self, action, target, content, metadata=None):
        allowed, _route = should_mirror_to_honcho(
            content or "", target=target or "user", metadata=metadata
        )
        if not allowed:
            return None
        return original_on_memory_write(self, action, target, content, metadata=metadata)

    def _sync_turn(self, user_content, assistant_content, *, session_id=""):
        if contains_secret(user_content or ""):
            user_content = "[message withheld: contained a credential]"
        if contains_secret(assistant_content or ""):
            assistant_content = "[message withheld: contained a credential]"
        return original_sync_turn(
            self, user_content, assistant_content, session_id=session_id
        )

    def _tool_profile(self, args):
        if memory_write_blocked_for_cui_admin("honcho_profile", args):
            return cui_admin_memory_block_result("honcho_profile")
        return original_tool_profile(self, args)

    def _tool_conclude(self, args):
        if memory_write_blocked_for_cui_admin("honcho_conclude", args):
            return cui_admin_memory_block_result("honcho_conclude")
        conclusion = str((args or {}).get("conclusion") or "").strip()
        if conclusion and contains_secret(conclusion):
            return tool_error("Honcho conclusion rejected because it contains a credential/secret.")
        return original_tool_conclude(self, args)

    HonchoMemoryProvider.on_memory_write = _on_memory_write
    HonchoMemoryProvider.sync_turn = _sync_turn
    HonchoMemoryProvider._tool_profile = _tool_profile
    HonchoMemoryProvider._tool_conclude = _tool_conclude
    HonchoMemoryProvider._TOOL_HANDLERS["honcho_profile"] = _tool_profile
    HonchoMemoryProvider._TOOL_HANDLERS["honcho_conclude"] = _tool_conclude
    HonchoMemoryProvider._agent_memory_router_adapter = True
    honcho.HonchoMemoryProvider = HonchoMemoryProvider


_install_memory_tool_router_adapter()
_install_honcho_router_adapter()
