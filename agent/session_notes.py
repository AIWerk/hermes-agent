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

# Upper bound on the input we feed to the regex scan. The redacted output is
# sliced to _MAX_TEXT downstream anyway, and the auxiliary summarizer/title
# paths slice their transcript well below this, so anything past this cap is
# discarded regardless. Capping here keeps a single 60KB no-delimiter tool
# result from amplifying any (even linear) regex into a multi-second scan and
# closes the ReDoS amplification window in session_summarizer._format_transcript,
# which redacts the FULL message before slicing.
_MAX_SCAN = 4096

_SECRET_PATTERNS = [
    # Keyed forms: group(1) is the label and is kept; the value is redacted.
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)\S+"),
    re.compile(r"(?i)(authorization\s*:\s*basic\s+)\S+"),
    re.compile(r"(?i)(api[_-]?key\s*[=:]\s*)\S+"),
    re.compile(r"(?i)(password\s*[=:]\s*)\S+"),
    re.compile(r"(?i)(token\s*[=:]\s*)\S+"),
    re.compile(r"(?i)(token\s+)\S+"),
    # ENV-style assignments whose name looks secret-bearing:
    #   STRIPE_WEBHOOK_SECRET=...  MY_API_KEY=...  *PASSWORD=...  *TOKEN=...
    # Keep the "NAME=" label, redact the value.
    re.compile(
        r"(?i)([A-Z0-9_]{0,50}(?:SECRET|API[_-]?KEY|PASSWORD|PASSWD|TOKEN|CREDENTIAL)"
        r"[A-Z0-9_]{0,50}\s*=\s*)\S+"
    ),
    # JSON / structured quoted key:value secrets, incl. nested ones:
    #   "access_token":"...", "refresh_token":"...", "client_secret":"...",
    #   "password":"...", "api_key":"...", "Authorization":"Bearer ...".
    # Keep the quoted key + colon, redact the quoted value.
    re.compile(
        r'(?i)("(?:access[_-]?token|refresh[_-]?token|id[_-]?token|client[_-]?secret'
        r"|secret|password|passwd|api[_-]?key|apikey|auth(?:orization)?|token"
        r'|private[_-]?key|key)"\s*:\s*)"[^"]*"'
    ),
    # scheme://user:password@host — keep "scheme://user:", redact the password.
    # Scheme run is anchored (no preceding alnum) and bounded ({0,30}) so the
    # engine cannot backtrack quadratically on a long no-delimiter blob.
    re.compile(r"(?i)(?<![A-Za-z0-9])([a-z][a-z0-9+.-]{0,30}://[^\s:/@]+:)[^@/\s]+(?=@)"),
    # Value-only forms: the whole match is the secret and is fully redacted.
    re.compile(r"sk[-_][A-Za-z0-9][A-Za-z0-9_-]{6,}"),
    re.compile(r"(?i)(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{8,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9_]{12,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+"),
    re.compile(r"(?i)https://hooks\.slack\.com/services/[A-Za-z0-9/_-]+"),
    re.compile(r"AIza[0-9A-Za-z_-]{8,}"),
    # Vendor key prefixes not covered above.
    re.compile(r"(?<![A-Za-z0-9_-])xai-[A-Za-z0-9]{20,}"),          # xAI (Grok)
    re.compile(r"(?<![A-Za-z0-9_-])SG\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),  # SendGrid
    re.compile(r"(?<![A-Za-z0-9_-])hf_[A-Za-z0-9]{10,}"),           # HuggingFace
    re.compile(r"(?<![A-Za-z0-9_-])pplx-[A-Za-z0-9]{10,}"),         # Perplexity
    re.compile(r"(?<![A-Za-z0-9_-])tvly-[A-Za-z0-9]{10,}"),         # Tavily
    # Telegram bot token: bot<digits>:<token> (or bare <digits>:<token>).
    re.compile(r"(?<![A-Za-z0-9])(?:bot)?\d{8,}:[-A-Za-z0-9_]{30,}"),
    # AWS SECRET access key. Labeled form keeps the label, redacts the value.
    re.compile(r"(?i)(aws_secret_access_key\s*[=:]\s*)\S+"),
    # Bare 40-char base64-ish blob. Constrained to exactly 40 [A-Za-z0-9/+]
    # chars that contain at least one uppercase letter, "/" or "+", so a
    # lowercase-hex git SHA-1 (40 hex chars) or a lowercase 40-char identifier
    # is NOT redacted — only the mixed-case base64 shape AWS uses.
    re.compile(
        r"(?<![A-Za-z0-9/+])(?=[A-Za-z0-9/+]{40}(?![A-Za-z0-9/+=]))"
        r"[A-Za-z0-9/+]*[A-Z/+][A-Za-z0-9/+]*"
    ),
]

# Block secrets (PEM private keys) can span past _MAX_SCAN. They are redacted on
# the FULL text BEFORE the scan cap (see redact_sensitive_text), so a key whose
# END marker sits beyond the cap — or is missing — can't survive truncation.
# The header is a literal prefix (fast O(n) search) and the lazy body stops at
# the first END marker, else runs to end-of-text, so the whole block (header
# included) is always redacted without eating a benign tail after END.
_BLOCK_SECRET_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)",
    re.S,
)


def redact_sensitive_text(text: str) -> str:
    """Mask common credential shapes in note/event content.

    PEM private-key blocks are redacted on the full text first, since they can
    straddle the ``_MAX_SCAN`` cap (header inside the window, END marker beyond
    it). The remaining single-line patterns then run on the capped text to bound
    worst-case matching time on adversarial no-delimiter input; callers slice the
    output to ``_MAX_TEXT`` anyway, so the cap only discards content that would
    be dropped downstream.
    """
    # Redact whole private-key blocks BEFORE capping: the END marker may sit
    # beyond _MAX_SCAN, where the single-line patterns below would never see it.
    # Avoid running the block regex on ordinary large blobs; most note content
    # has no PEM header, and CI timing can make a linear 60KB scan trip the
    # ReDoS guard even when no secret shape is present.
    raw = str(text or "")
    if "-----BEGIN " in raw and "PRIVATE KEY-----" in raw:
        raw = _BLOCK_SECRET_RE.sub("[REDACTED]", raw)
    redacted = raw[:_MAX_SCAN]
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
            scratchpad.get("decisions", []), f"Korrektur des Nutzers: {summary}"
        )
    elif event_type == "checkpoint" and summary:
        scratchpad["open_items"] = _append_limited(
            scratchpad.get("open_items", []), f"Zwischenstand: {summary}"
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
