"""Deterministic incremental session note helpers.

These helpers intentionally avoid per-turn LLM calls. The runtime records small
structured events, then this module keeps a compact mutable scratchpad that the
final session summarizer can consume later.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any, Dict, Optional

_MAX_ITEMS = 12
_MAX_TEXT = 500

_SECRET_PATTERNS = [
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)\S+"),
    re.compile(r"(?i)(api[_-]?key\s*[=:]\s*)\S+"),
    re.compile(r"(?i)(password\s*[=:]\s*)\S+"),
    re.compile(r"(?i)(token\s*[=:]\s*)\S+"),
    re.compile(r"(?i)(token\s+)\S+"),
    re.compile(r"sk-[A-Za-z0-9][A-Za-z0-9_-]{6,}"),
    re.compile(r"AIza[0-9A-Za-z_-]{8,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
]


def redact_sensitive_text(text: str) -> str:
    """Mask common credential shapes in note/event content."""
    redacted = str(text or "")
    for pattern in _SECRET_PATTERNS:
        def repl(match: re.Match[str]) -> str:
            if match.lastindex:
                return f"{match.group(1)}[REDACTED]"
            return "[REDACTED]"
        redacted = pattern.sub(repl, redacted)
    return redacted


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_sensitive_text(value)[:_MAX_TEXT]
    if isinstance(value, list):
        return [_redact_value(item) for item in value[:_MAX_ITEMS]]
    if isinstance(value, dict):
        return {str(k): _redact_value(v) for k, v in value.items()}
    return value


def _append_limited(items: list[Any], item: Any) -> list[Any]:
    next_items = list(items or []) + [item]
    return next_items[-_MAX_ITEMS:]


def get_session_scratchpad(session_db, session_id: str) -> Optional[Dict[str, Any]]:
    """Return the current deterministic scratchpad for a session."""
    if not session_db or not session_id:
        return None
    return session_db.get_session_scratchpad(session_id)


def update_session_scratchpad(
    session_db,
    session_id: str,
    event_type: str,
    content: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Update the mutable scratchpad from one structured event."""
    if not session_db or not session_id:
        return None
    scratchpad = session_db.get_session_scratchpad(session_id) or {
        "current_goal": None,
        "decisions": [],
        "artifacts": [],
        "open_items": [],
        "candidates": [],
    }
    scratchpad = copy.deepcopy(scratchpad)
    summary = str(content.get("summary") or "").strip()

    if event_type == "decision" and summary:
        scratchpad["decisions"] = _append_limited(scratchpad.get("decisions", []), summary)
    elif event_type == "artifact":
        artifact = {k: content.get(k) for k in ("artifact_type", "path", "action", "summary") if content.get(k) is not None}
        if artifact:
            scratchpad["artifacts"] = _append_limited(scratchpad.get("artifacts", []), artifact)
    elif event_type == "open_question" and summary:
        scratchpad["open_items"] = _append_limited(scratchpad.get("open_items", []), summary)
    elif event_type == "candidate":
        candidate = {k: content.get(k) for k in ("type", "summary", "route") if content.get(k) is not None}
        if candidate:
            scratchpad["candidates"] = _append_limited(scratchpad.get("candidates", []), candidate)
    elif event_type == "turn_note" and content.get("current_goal"):
        scratchpad["current_goal"] = str(content.get("current_goal"))[:_MAX_TEXT]
    elif event_type == "user_correction" and summary:
        scratchpad["decisions"] = _append_limited(
            scratchpad.get("decisions", []), f"User correction: {summary}"
        )
    elif event_type == "checkpoint" and summary:
        scratchpad["open_items"] = _append_limited(
            scratchpad.get("open_items", []), f"Checkpoint: {summary}"
        )

    session_db.set_session_scratchpad(session_id, scratchpad)
    return session_db.get_session_scratchpad(session_id)


def record_session_event(
    session_db,
    session_id: str,
    event_type: str,
    content: Dict[str, Any],
    *,
    turn_index: int = None,
    source: str = "runtime",
) -> Optional[int]:
    """Record one redacted structured event and update the scratchpad."""
    if not session_db or not session_id:
        return None
    safe_content = _redact_value(content if isinstance(content, dict) else {"summary": str(content)})
    event_id = session_db.add_session_event(
        session_id,
        event_type,
        safe_content,
        turn_index=turn_index,
        source=source,
    )
    update_session_scratchpad(session_db, session_id, event_type, safe_content)
    return event_id


def format_notes_for_prompt(events: list[Dict[str, Any]] | None, scratchpad: Dict[str, Any] | None) -> Optional[str]:
    """Format notes compactly for the finalizer prompt."""
    if not events and not scratchpad:
        return None
    payload = {
        "scratchpad": scratchpad or {},
        "events": [
            {
                "event_type": event.get("event_type"),
                "turn_index": event.get("turn_index"),
                "content": event.get("content", {}),
            }
            for event in (events or [])[-30:]
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)[:6000]
