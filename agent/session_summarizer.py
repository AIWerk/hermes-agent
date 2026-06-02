"""Generate compact per-session search notes.

This is deliberately small: a session summary is a search/index aid, not
memory, not a wiki page, and not a raw transcript archive.

The module is intentionally inert unless a caller invokes
``maybe_update_session_summary`` or ``update_session_summary`` explicitly.
It must not be wired into session-end, reset, or gateway lifecycle paths without
an explicit product decision, because doing so creates background LLM calls,
latency, and cost at session boundaries.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from typing import Any, Dict, List, Optional

from agent.auxiliary_client import call_llm
from agent.session_notes import format_notes_for_prompt, redact_sensitive_text

logger = logging.getLogger(__name__)

_SUMMARY_TASK = "title_generation"  # reuse the verified cheap title route + fallback
_MAX_INPUT_CHARS = 12_000
_MAX_OUTLINE_ITEMS = 8
_MAX_TOPICS = 8

_SYSTEM_PROMPT = """Erstelle eine kompakte Sitzungsnotiz für die spätere Suche.
Write all human-readable values in German, even when the transcript contains another language.
Return ONLY valid JSON with exactly these keys:
{
  "short_summary": "1-2 concise German sentences about what happened",
  "outline": ["3-8 short German bullets of the session flow"],
  "topics": ["3-8 lowercase German or stable technical search tags"]
}
Do not include secrets, credentials, private addresses, or raw transcript dumps.
This is a session index note, not durable memory or wiki content.
"""


def _message_text(message: Dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                text = part.get("text") or part.get("content")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(part, str):
                parts.append(part)
        return " ".join(parts)
    if content is None:
        return ""
    return str(content)


def _format_transcript(messages: List[Dict[str, Any]]) -> Optional[str]:
    lines: List[str] = []
    for message in messages or []:
        role = str(message.get("role") or "").strip().lower()
        if role not in {"user", "assistant"}:
            continue
        text = re.sub(r"\s+", " ", _message_text(message)).strip()
        text = redact_sensitive_text(text)
        if not text:
            continue
        lines.append(f"{role}: {text[:2000]}")
    if not lines:
        return None
    transcript = "\n".join(lines)
    if len(transcript) > _MAX_INPUT_CHARS:
        transcript = transcript[-_MAX_INPUT_CHARS:]
    return transcript


def _extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    raw = (text or "").strip()
    if not raw:
        return None
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw).strip()
    if not raw.startswith("{"):
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            raw = raw[start:end + 1]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _clean_list(value: Any, *, limit: int) -> List[str]:
    if not isinstance(value, list):
        return []
    cleaned: List[str] = []
    for item in value:
        text = re.sub(r"\s+", " ", str(item or "")).strip()
        if not text:
            continue
        cleaned.append(text[:160])
        if len(cleaned) >= limit:
            break
    return cleaned


def _parse_summary_payload(text: str, model: Optional[str]) -> Optional[Dict[str, Any]]:
    data = _extract_json_object(text)
    if not data:
        return None
    short_summary = re.sub(r"\s+", " ", str(data.get("short_summary") or "")).strip()
    if not short_summary:
        return None
    return {
        "short_summary": short_summary[:1000],
        "outline": _clean_list(data.get("outline"), limit=_MAX_OUTLINE_ITEMS),
        "topics": [t.lower() for t in _clean_list(data.get("topics"), limit=_MAX_TOPICS)],
        "model": model,
    }


def generate_session_summary(
    messages: List[Dict[str, Any]],
    *,
    title: Optional[str] = None,
    events: Optional[List[Dict[str, Any]]] = None,
    scratchpad: Optional[Dict[str, Any]] = None,
    timeout: float = 45.0,
    main_runtime: Optional[dict] = None,
) -> Optional[Dict[str, Any]]:
    """Generate a compact session summary dict from persisted messages and notes."""
    transcript = _format_transcript(messages)
    if not transcript:
        return None

    title_line = f"Title: {redact_sensitive_text(title)}\n\n" if title else ""
    notes = format_notes_for_prompt(events, scratchpad)
    if notes:
        notes = redact_sensitive_text(notes)
    notes_block = f"Incremental notes:\n{notes}\n\n" if notes else ""
    prompt = f"{title_line}{notes_block}Transcript:\n{transcript}"
    try:
        response = call_llm(
            task=_SUMMARY_TASK,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            max_tokens=700,
            temperature=0.2,
            timeout=timeout,
            main_runtime=main_runtime,
        )
        content = (response.choices[0].message.content or "").strip()
        model = getattr(response, "model", None)
        return _parse_summary_payload(content, model)
    except Exception as exc:
        logger.warning("Session summary generation failed: %s", exc)
        logger.debug("Session summary generation traceback", exc_info=True)
        return None


def update_session_summary(
    session_db,
    session_id: str,
    *,
    main_runtime: Optional[dict] = None,
    timeout: float = 45.0,
    final_title_refinement: bool = False,
) -> bool:
    """Generate and persist the compact summary for one stored session."""
    if not session_db or not session_id:
        return False
    try:
        messages = session_db.get_messages(session_id)
        if not messages:
            return False
        title = session_db.get_session_title(session_id)
        events = []
        scratchpad = None
        try:
            events = session_db.get_session_events(session_id, limit=30)
            scratchpad = session_db.get_session_scratchpad(session_id)
        except Exception:
            logger.debug("Session incremental notes unavailable", exc_info=True)
        summary = generate_session_summary(
            messages,
            title=title,
            events=events,
            scratchpad=scratchpad,
            timeout=timeout,
            main_runtime=main_runtime,
        )
        if not summary:
            return False
        persisted = bool(session_db.set_session_summary(
            session_id,
            short_summary=summary["short_summary"],
            outline=summary.get("outline") or [],
            topics=summary.get("topics") or [],
            model=summary.get("model"),
        ))
        if persisted and final_title_refinement:
            try:
                from agent.title_generator import finalize_session_title
                finalize_session_title(
                    session_db,
                    session_id,
                    summary,
                    main_runtime=main_runtime,
                )
            except Exception:
                logger.debug("Final session title refinement failed", exc_info=True)
        return persisted
    except Exception as exc:
        logger.warning("Session summary persistence failed: %s", exc)
        logger.debug("Session summary persistence traceback", exc_info=True)
        return False


def maybe_update_session_summary(
    session_db,
    session_id: str,
    *,
    main_runtime: Optional[dict] = None,
    synchronous: bool = False,
    timeout: float = 45.0,
    final_title_refinement: bool = False,
) -> Optional[threading.Thread | bool]:
    """Update the session-summary index now or in a daemon thread.

    Interactive/gateway paths should use background mode. Oneshot/exit paths
    pass synchronous=True so the note is committed before process shutdown.
    """
    if synchronous:
        return update_session_summary(
            session_db,
            session_id,
            main_runtime=main_runtime,
            timeout=timeout,
            final_title_refinement=final_title_refinement,
        )

    thread = threading.Thread(
        target=update_session_summary,
        args=(session_db, session_id),
        kwargs={
            "main_runtime": main_runtime,
            "timeout": timeout,
            "final_title_refinement": final_title_refinement,
        },
        daemon=True,
        name="session-summary",
    )
    thread.start()
    return thread
