"""Auto-generate and refine short session titles.

Title lifecycle:
1. Initial auto-title after the first exchange.
2. Mid-session retitle after meaningful drift, throttled by turn count.
3. Final refinement from the compact session summary when a session is closed
   or one-shot mode is about to exit.

Manual titles are protected: automatic retitle steps never overwrite a title
whose ``sessions.title_source`` is ``manual``.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any, Callable, Dict, List, Optional

from agent.auxiliary_client import call_llm
from agent.session_notes import format_notes_for_prompt, redact_sensitive_text

logger = logging.getLogger(__name__)

# Callback signature: (task_name, exception) -> None. Used to surface
# auxiliary failures to the user through AIAgent._emit_auxiliary_failure
# so silent-drops (e.g. OpenRouter 402 exhausting the fallback chain)
# become visible instead of piling up as NULL session titles.
FailureCallback = Callable[[str, BaseException], None]
TitleCallback = Callable[[str], None]

_TITLE_PROMPT = (
    "Generate a short, descriptive title (3-7 words) for a conversation that starts with the "
    "following exchange. The title should capture the main topic or intent. "
    "Write the title in the same language the user is writing in. "
    "Return ONLY the title text, nothing else. No quotes, no punctuation at the end, no prefixes."
)

_RETITLE_PROMPT = (
    "Generate a concise updated session title (3-8 words). Capture the overall current topic, "
    "not just the first exchange. Return ONLY the title text, no quotes, no punctuation, no prefix."
)

_TITLE_PROMPT_PINNED_LANGUAGE = (
    "Generate a short, descriptive title (3-7 words) for a conversation that starts with the "
    "following exchange. The title should capture the main topic or intent. "
    "Write the title in {language}. "
    "Return ONLY the title text, nothing else. No quotes, no punctuation at the end, no prefixes."
)


def _title_language() -> str:
    """Return configured title language, or empty string to match the user."""
    try:
        from hermes_cli.config import load_config

        return str(
            ((load_config() or {}).get("auxiliary") or {})
            .get("title_generation", {})
            .get("language", "")
        ).strip()
    except Exception:
        return ""


_FINAL_TITLE_PROMPT = (
    "Generate the final concise session title (3-8 words) from this compact session summary. "
    "Prefer a title broad enough to cover all major work. Return ONLY the title text, no quotes, "
    "no punctuation, no prefix."
)

_AUTO_SOURCES = {"auto_initial", "auto_mid", "auto_final", None, ""}
_MANUAL_SOURCE = "manual"
_MID_RETITLE_MIN_USER_TURNS = 5
_MID_RETITLE_TURN_INTERVAL = 5


def _clean_title(title: str) -> Optional[str]:
    title = re.sub(r"\s+", " ", str(title or "")).strip().strip('"\'')
    if title.lower().startswith("title:"):
        title = title[6:].strip()
    title = re.sub(r"[\s.?!:;,-]+$", "", title).strip()
    if len(title) > 80:
        title = title[:77].rstrip() + "..."
    return title or None


def _title_words(title: str) -> set[str]:
    return {w for w in re.findall(r"[\wÀ-ž]+", (title or "").lower()) if len(w) > 2}


def _materially_different(old_title: Optional[str], new_title: Optional[str]) -> bool:
    if not new_title:
        return False
    if not old_title:
        return True
    old = re.sub(r"\W+", " ", old_title.lower()).strip()
    new = re.sub(r"\W+", " ", new_title.lower()).strip()
    if old == new or old in new or new in old:
        return False
    old_words = _title_words(old_title)
    new_words = _title_words(new_title)
    if not old_words or not new_words:
        return True
    overlap = len(old_words & new_words) / max(1, min(len(old_words), len(new_words)))
    return overlap < 0.60


def _message_text(message: Dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
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


def _format_recent_exchange(messages: List[Dict[str, Any]], max_messages: int = 10) -> str:
    lines: List[str] = []
    for message in (messages or [])[-max_messages:]:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "").lower()
        if role not in {"user", "assistant"}:
            continue
        text = re.sub(r"\s+", " ", _message_text(message)).strip()
        text = redact_sensitive_text(text)
        if text:
            lines.append(f"{role}: {text[:700]}")
    return "\n".join(lines)


def _call_title_llm(
    *,
    system_prompt: str,
    user_prompt: str,
    timeout: Optional[float],
    failure_callback: Optional[FailureCallback],
    main_runtime: Optional[dict],
) -> Optional[str]:
    try:
        response = call_llm(
            task="title_generation",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=500,
            temperature=0.3,
            timeout=timeout,
            main_runtime=main_runtime,
        )
        content = response.choices[0].message.content or ""
        # Think-enabled models can emit reasoning XML even for title generation.
        # Strip it before the normal title cleanup so it never leaks into session titles.
        from agent.agent_runtime_helpers import strip_think_blocks
        return _clean_title(strip_think_blocks(None, content))
    except Exception as e:
        logger.warning("Title generation failed: %s", e)
        logger.debug("Title generation traceback", exc_info=True)
        if failure_callback is not None:
            try:
                failure_callback("title generation", e)
            except Exception:
                logger.debug("Title generation failure_callback raised", exc_info=True)
        return None


def generate_title(
    user_message: str,
    assistant_response: str,
    timeout: Optional[float] = None,
    failure_callback: Optional[FailureCallback] = None,
    main_runtime: dict = None,
) -> Optional[str]:
    """Generate a session title from the first exchange."""
    user_snippet = redact_sensitive_text(user_message[:500] if user_message else "")
    assistant_snippet = redact_sensitive_text(assistant_response[:500] if assistant_response else "")
    language = _title_language()
    prompt = _TITLE_PROMPT_PINNED_LANGUAGE.format(language=language) if language else _TITLE_PROMPT
    return _call_title_llm(
        system_prompt=prompt,
        user_prompt=f"User: {user_snippet}\n\nAssistant: {assistant_snippet}",
        timeout=timeout,
        failure_callback=failure_callback,
        main_runtime=main_runtime,
    )


def generate_retitle(
    *,
    current_title: Optional[str],
    messages: List[Dict[str, Any]],
    events: Optional[List[Dict[str, Any]]] = None,
    scratchpad: Optional[Dict[str, Any]] = None,
    timeout: float = 30.0,
    failure_callback: Optional[FailureCallback] = None,
    main_runtime: Optional[dict] = None,
) -> Optional[str]:
    """Generate a mid-session title from recent transcript and notes."""
    transcript = _format_recent_exchange(messages)
    notes = redact_sensitive_text(format_notes_for_prompt(events, scratchpad) or "")
    if not transcript and not notes:
        return None
    prompt = (
        f"Current title: {redact_sensitive_text(current_title or '(none)')}\n\n"
        f"Incremental notes:\n{notes[:4000]}\n\n"
        f"Recent transcript:\n{transcript[-5000:]}"
    )
    return _call_title_llm(
        system_prompt=_RETITLE_PROMPT,
        user_prompt=prompt,
        timeout=timeout,
        failure_callback=failure_callback,
        main_runtime=main_runtime,
    )


def generate_final_title(
    *,
    current_title: Optional[str],
    summary: Dict[str, Any],
    timeout: float = 30.0,
    failure_callback: Optional[FailureCallback] = None,
    main_runtime: Optional[dict] = None,
) -> Optional[str]:
    """Generate a final title from the compact stored session summary."""
    outline = summary.get("outline") or []
    topics = summary.get("topics") or []
    prompt = (
        f"Current title: {redact_sensitive_text(current_title or '(none)')}\n"
        f"Summary: {redact_sensitive_text(str(summary.get('short_summary') or ''))}\n"
        f"Outline: {redact_sensitive_text('; '.join(map(str, outline)))}\n"
        f"Topics: {redact_sensitive_text(', '.join(map(str, topics)))}"
    )
    return _call_title_llm(
        system_prompt=_FINAL_TITLE_PROMPT,
        user_prompt=prompt,
        timeout=timeout,
        failure_callback=failure_callback,
        main_runtime=main_runtime,
    )


def _safe_set_title(
    session_db,
    session_id: str,
    title: str,
    *,
    source: str,
    turn_index: Optional[int] = None,
    title_callback: Optional[TitleCallback] = None,
) -> bool:
    try:
        ok = session_db.set_session_title(
            session_id,
            title,
            source=source,
            turn_index=turn_index,
        )
    except ValueError:
        try:
            fallback = session_db.get_next_title_in_lineage(title)
            ok = session_db.set_session_title(
                session_id,
                fallback,
                source=source,
                turn_index=turn_index,
            )
            title = fallback
        except Exception:
            logger.debug("Failed to set generated session title", exc_info=True)
            return False
    except Exception:
        logger.debug("Failed to set generated session title", exc_info=True)
        return False
    if ok and title_callback is not None:
        try:
            title_callback(title)
        except Exception:
            logger.debug("Auto-title callback failed", exc_info=True)
    return bool(ok)


def _title_meta(session_db, session_id: str) -> Optional[Dict[str, Any]]:
    if hasattr(session_db, "get_session_title_metadata"):
        meta = session_db.get_session_title_metadata(session_id)
        if isinstance(meta, dict):
            return meta
    title = session_db.get_session_title(session_id)
    return {"title": title, "title_source": None, "title_turn_index": None}


def _is_manual(meta: Dict[str, Any]) -> bool:
    return (meta or {}).get("title_source") == _MANUAL_SOURCE


def auto_title_session(
    session_db,
    session_id: str,
    user_message: str,
    assistant_response: str,
    failure_callback: Optional[FailureCallback] = None,
    main_runtime: dict = None,
    title_callback: Optional[TitleCallback] = None,
) -> None:
    """Generate and set the initial session title if no title exists."""
    if not session_db or not session_id:
        return
    try:
        meta = _title_meta(session_db, session_id) or {}
        if meta.get("title"):
            return
    except Exception:
        return

    title = generate_title(
        user_message,
        assistant_response,
        failure_callback=failure_callback,
        main_runtime=main_runtime,
    )
    if not title:
        return
    if _safe_set_title(
        session_db,
        session_id,
        title,
        source="auto_initial",
        turn_index=1,
        title_callback=title_callback,
    ):
        logger.debug("Auto-generated initial session title: %s", title)


def maybe_auto_title(
    session_db,
    session_id: str,
    user_message: str,
    assistant_response: str,
    conversation_history: list,
    failure_callback: Optional[FailureCallback] = None,
    main_runtime: dict = None,
    title_callback: Optional[TitleCallback] = None,
) -> None:
    """Fire-and-forget initial title generation after the first exchange."""
    if not session_db or not session_id or not user_message or not assistant_response:
        return

    user_msg_count = sum(1 for m in (conversation_history or []) if m.get("role") == "user")
    if user_msg_count > 2:
        return

    thread = threading.Thread(
        target=auto_title_session,
        args=(session_db, session_id, user_message, assistant_response),
        kwargs={
            "failure_callback": failure_callback,
            "main_runtime": main_runtime,
            "title_callback": title_callback,
        },
        daemon=True,
        name="auto-title",
    )
    thread.start()


def retitle_session(
    session_db,
    session_id: str,
    messages: List[Dict[str, Any]],
    *,
    turn_index: Optional[int] = None,
    failure_callback: Optional[FailureCallback] = None,
    main_runtime: Optional[dict] = None,
    title_callback: Optional[TitleCallback] = None,
) -> bool:
    """Generate and set a mid-session title if the old auto-title drifted."""
    if not session_db or not session_id:
        return False
    try:
        meta = _title_meta(session_db, session_id) or {}
        if _is_manual(meta) or meta.get("title_source") == "auto_final":
            return False
        current_title = meta.get("title")
        events = session_db.get_session_events(session_id, limit=30)
        scratchpad = session_db.get_session_scratchpad(session_id)
    except Exception:
        logger.debug("mid-session retitle metadata unavailable", exc_info=True)
        return False

    new_title = generate_retitle(
        current_title=current_title,
        messages=messages,
        events=events,
        scratchpad=scratchpad,
        failure_callback=failure_callback,
        main_runtime=main_runtime,
    )
    if not new_title or not _materially_different(current_title, new_title):
        return False
    return _safe_set_title(
        session_db,
        session_id,
        new_title,
        source="auto_mid",
        turn_index=turn_index,
        title_callback=title_callback,
    )


def maybe_retitle_session(
    session_db,
    session_id: str,
    messages: List[Dict[str, Any]],
    conversation_history: Optional[List[Dict[str, Any]]] = None,
    *,
    turn_index: Optional[int] = None,
    failure_callback: Optional[FailureCallback] = None,
    main_runtime: Optional[dict] = None,
    title_callback: Optional[TitleCallback] = None,
    synchronous: bool = False,
) -> Optional[threading.Thread | bool]:
    """Throttled mid-session retitle hook.

    The cheap deterministic gate runs every turn; the LLM call only fires after
    enough user turns have accumulated since the last automatic title update.
    """
    if not session_db or not session_id or not messages:
        return False if synchronous else None
    user_msg_count = sum(1 for m in (messages or []) if isinstance(m, dict) and m.get("role") == "user")
    if user_msg_count < _MID_RETITLE_MIN_USER_TURNS:
        return False if synchronous else None
    try:
        meta = _title_meta(session_db, session_id) or {}
    except Exception:
        return False if synchronous else None
    if _is_manual(meta) or meta.get("title_source") == "auto_final":
        return False if synchronous else None
    last_turn = meta.get("title_turn_index")
    try:
        last_turn_int = int(last_turn) if last_turn is not None else 0
    except (TypeError, ValueError):
        last_turn_int = 0
    current_turn = int(turn_index or user_msg_count)
    if current_turn - last_turn_int < _MID_RETITLE_TURN_INTERVAL:
        return False if synchronous else None

    if synchronous:
        return retitle_session(
            session_db,
            session_id,
            messages,
            turn_index=current_turn,
            failure_callback=failure_callback,
            main_runtime=main_runtime,
            title_callback=title_callback,
        )

    thread = threading.Thread(
        target=retitle_session,
        args=(session_db, session_id, list(messages)),
        kwargs={
            "turn_index": current_turn,
            "failure_callback": failure_callback,
            "main_runtime": main_runtime,
            "title_callback": title_callback,
        },
        daemon=True,
        name="mid-session-retitle",
    )
    thread.start()
    return thread


def finalize_session_title(
    session_db,
    session_id: str,
    summary: Dict[str, Any],
    *,
    failure_callback: Optional[FailureCallback] = None,
    main_runtime: Optional[dict] = None,
    title_callback: Optional[TitleCallback] = None,
) -> bool:
    """Refine the title from the final compact session summary."""
    if not session_db or not session_id or not summary:
        return False
    try:
        meta = _title_meta(session_db, session_id) or {}
        if _is_manual(meta):
            return False
        current_title = meta.get("title")
    except Exception:
        return False
    new_title = generate_final_title(
        current_title=current_title,
        summary=summary,
        failure_callback=failure_callback,
        main_runtime=main_runtime,
    )
    if not new_title or not _materially_different(current_title, new_title):
        return False
    return _safe_set_title(
        session_db,
        session_id,
        new_title,
        source="auto_final",
        turn_index=None,
        title_callback=title_callback,
    )
