"""Rule-based memory routing policy.

This module separates stored memory, explicit recall, automatic prompt
injection, wiki/skill candidates, tenant-private facts, and discard paths.
It is intentionally deterministic and local: no LLM calls, no network calls,
and no writes. Callers can use it before writing memory or mirroring facts to
external providers.

Enforcement model (important): the only destinations enforced by current
callers are INJECT (``should_write_builtin_memory``) and STORE_HONCHO
(``should_mirror_to_honcho``). The remaining destinations — TENANT_PRIVATE,
WIKI_CANDIDATE, SKILL_CANDIDATE, SESSION_INDEX — are advisory *classification*:
content routed to them is simply kept out of prompt-injected built-in memory
(it stays EXPLICIT_RECALL_ONLY). There is no separate in-process tenant-private
store; tenant isolation is structural — each customer runs an isolated agent
home / Honcho namespace, so a clone's own memory *is* its tenant-private memory.
The CREDENTIAL rule (secrets -> DISCARD, never injected/mirrored) is the one
hard security guarantee.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import re
from typing import Any, Iterable, Mapping


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
            "destinations": [d.value for d in self.destinations],
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
            "metadata": dict(self.metadata or {}),
        }


_SECRET_RE = re.compile(
    r"(api[_ -]?key|secret|token|password|passwd|credential|private[_ -]?key|"
    r"BEGIN (RSA|OPENSSH|EC|DSA)? ?PRIVATE KEY|"
    r"sk[-_][A-Za-z0-9]|(sk|pk|rk)_(live|test)_[A-Za-z0-9]{8,}|"
    r"xox[baprs]-|gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]{20,}|"
    r"AIza[0-9A-Za-z_-]{20,}|(AKIA|ASIA)[0-9A-Z]{16}|"
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+|"
    r"hooks\.slack\.com/services/|"
    # credentials embedded in URLs: scheme://user:password@host
    r"[a-z][a-z0-9+.-]*://[^\s:/@]+:[^@/\s]+@)",
    re.IGNORECASE,
)

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
    r"\b(user|Attila|prefers|likes|dislikes|expects|wants|does not want|"
    r"communication style|speaks|lives|timezone|role|building)\b",
    re.IGNORECASE,
)

_ENV_LESSON_RE = re.compile(
    r"\b(project uses|repo uses|environment|installed|tool quirk|API quirk|"
    r"config|provider|model|runtime|host|VPS|gateway|MCP|Honcho|Hermes)\b",
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


def _first(*values: Any, default: str = "") -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return default


def _dest_tuple(*destinations: MemoryDestination) -> tuple[MemoryDestination, ...]:
    seen: list[MemoryDestination] = []
    for destination in destinations:
        if destination not in seen:
            seen.append(destination)
    return tuple(seen)


def _metadata_scope(metadata: Mapping[str, Any] | None) -> str:
    if not metadata:
        return "hermes"
    return _first(
        metadata.get("scope"),
        metadata.get("agent_identity"),
        metadata.get("agent_workspace"),
        metadata.get("tenant_id"),
        metadata.get("customer_id"),
        default="hermes",
    )


def classify_memory_route(
    content: str,
    *,
    source: str = "memory_tool",
    target: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> MemoryRoute:
    """Classify a candidate memory fact into deterministic destinations.

    The router is conservative. Built-in memory is prompt-injected on the next
    session, so only high-signal user preferences and stable environment facts
    should route to INJECT. Operational progress, wiki-worthy product knowledge,
    reusable procedures, credentials, raw dumps, and customer-private material
    are routed elsewhere.
    """
    text = (content or "").strip()
    meta = dict(metadata or {})
    scope = _metadata_scope(meta)
    source = source or str(meta.get("write_origin") or "memory_tool")
    target = (target or "").strip()

    if not text:
        return MemoryRoute(
            destinations=_dest_tuple(MemoryDestination.DISCARD),
            sensitivity=MemorySensitivity.INTERNAL,
            durability=MemoryDurability.TEMPORARY,
            confidence=1.0,
            reason="empty content is not memory",
            scope=scope,
            target_hint="discard",
            metadata={"source": source, "target": target},
        )

    if _SECRET_RE.search(text):
        return MemoryRoute(
            destinations=_dest_tuple(MemoryDestination.DISCARD),
            sensitivity=MemorySensitivity.CREDENTIAL,
            durability=MemoryDurability.TEMPORARY,
            confidence=0.98,
            reason="credentials and secrets never route to shared memory, prompt injection, or wiki",
            scope=scope,
            target_hint="discard",
            metadata={"source": source, "target": target},
        )

    if _RAW_DUMP_RE.search(text):
        return MemoryRoute(
            destinations=_dest_tuple(MemoryDestination.SESSION_INDEX, MemoryDestination.DISCARD),
            sensitivity=MemorySensitivity.INTERNAL,
            durability=MemoryDurability.SESSION,
            confidence=0.92,
            reason="raw transcripts and memory-context dumps belong in session search or sanitized source notes, not prompt-injected memory",
            scope=scope,
            target_hint="session_search_or_sanitized_wiki_source",
            metadata={"source": source, "target": target},
        )

    if (meta.get("tenant_id") or meta.get("customer_id") or _CUSTOMER_RE.search(text)) and not _AIWERK_PRODUCT_RE.search(text) and not _USER_PREF_RE.search(text):
        return MemoryRoute(
            destinations=_dest_tuple(MemoryDestination.TENANT_PRIVATE, MemoryDestination.EXPLICIT_RECALL_ONLY),
            sensitivity=MemorySensitivity.CUSTOMER,
            durability=MemoryDurability.DURABLE,
            confidence=0.86,
            reason="customer or tenant-specific facts must stay in isolated tenant-private memory",
            scope=scope,
            target_hint="tenant_private",
            tenant_private_required=True,
            metadata={"source": source, "target": target},
        )

    if _SESSION_PROGRESS_RE.search(text):
        return MemoryRoute(
            destinations=_dest_tuple(MemoryDestination.SESSION_INDEX),
            sensitivity=MemorySensitivity.INTERNAL,
            durability=MemoryDurability.SESSION,
            confidence=0.82,
            reason="temporary implementation progress belongs in session_search or project files, not long-term memory",
            scope=scope,
            target_hint="session_search",
            metadata={"source": source, "target": target},
        )

    if _PROCEDURE_RE.search(text):
        return MemoryRoute(
            destinations=_dest_tuple(MemoryDestination.SKILL_CANDIDATE, MemoryDestination.EXPLICIT_RECALL_ONLY),
            sensitivity=MemorySensitivity.INTERNAL,
            durability=MemoryDurability.DURABLE,
            confidence=0.82,
            reason="reusable procedures belong in skills rather than prompt-injected base memory",
            scope=scope,
            target_hint="skill_candidate",
            metadata={"source": source, "target": target},
        )

    if _AIWERK_PRODUCT_RE.search(text):
        return MemoryRoute(
            destinations=_dest_tuple(MemoryDestination.WIKI_CANDIDATE, MemoryDestination.EXPLICIT_RECALL_ONLY),
            sensitivity=MemorySensitivity.INTERNAL,
            durability=MemoryDurability.DURABLE,
            confidence=0.8,
            reason="durable AIWerk product, architecture, SOP, or strategy knowledge belongs in sanitized wiki",
            scope=scope,
            target_hint="wiki_candidate",
            shared_wiki_allowed=True,
            metadata={"source": source, "target": target},
        )

    if target == "user" or _USER_PREF_RE.search(text):
        return MemoryRoute(
            destinations=_dest_tuple(MemoryDestination.INJECT, MemoryDestination.STORE_HONCHO),
            sensitivity=MemorySensitivity.PERSONAL,
            durability=MemoryDurability.DURABLE,
            confidence=0.78,
            reason="stable user preference or profile fact is eligible for compact peer-card style injection",
            scope=scope,
            target_hint="user",
            inject_allowed=True,
            honcho_store_allowed=True,
            metadata={"source": source, "target": target},
        )

    if target == "memory" and _ENV_LESSON_RE.search(text):
        return MemoryRoute(
            destinations=_dest_tuple(MemoryDestination.INJECT, MemoryDestination.STORE_HONCHO),
            sensitivity=MemorySensitivity.INTERNAL,
            durability=MemoryDurability.DURABLE,
            confidence=0.72,
            reason="stable environment or tooling fact can reduce future steering",
            scope=scope,
            target_hint="memory",
            inject_allowed=True,
            honcho_store_allowed=True,
            metadata={"source": source, "target": target},
        )

    return MemoryRoute(
        destinations=_dest_tuple(MemoryDestination.EXPLICIT_RECALL_ONLY),
        sensitivity=MemorySensitivity.INTERNAL,
        durability=MemoryDurability.SESSION,
        confidence=0.55,
        reason="no high-signal prompt-injection rule matched; keep searchable only unless the operator routes it explicitly",
        scope=scope,
        target_hint="explicit_recall_only",
        metadata={"source": source, "target": target},
    )


def should_write_builtin_memory(
    content: str,
    *,
    target: str,
    metadata: Mapping[str, Any] | None = None,
) -> tuple[bool, MemoryRoute]:
    """Return whether content may be written to built-in prompt-injected memory."""
    route = classify_memory_route(content, source="memory_tool", target=target, metadata=metadata)
    return route.inject_allowed and route.has(MemoryDestination.INJECT), route


def should_mirror_to_honcho(
    content: str,
    *,
    target: str = "user",
    metadata: Mapping[str, Any] | None = None,
) -> tuple[bool, MemoryRoute]:
    """Return whether a built-in memory write may be mirrored to Honcho."""
    route = classify_memory_route(content, source="memory_mirror", target=target, metadata=metadata)
    return route.honcho_store_allowed and route.has(MemoryDestination.STORE_HONCHO), route


def dominant_destination(destinations: Iterable[MemoryDestination]) -> MemoryDestination:
    return max(destinations, key=lambda d: _PRIORITY_DESTINATION.get(d, 0))
