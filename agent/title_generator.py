"""Auto-generate short session titles from the user's opening message.

Two stages, both off the critical path: an **instant** deterministic title (written before the model
is called, cannot fail), then an **upgrade** from one small-model call (cheap tier, thinking off,
JSON-constrained). Storage enforces provenance ``derived < llm < user``: stage 2 only replaces stage 1
and neither replaces a name the user typed."""

import inspect
import json
import logging
import re
import threading
from contextlib import suppress
from typing import Any, Callable, Optional

from agent.auxiliary_client import call_llm
from agent.context_compressor import LEGACY_SUMMARY_PREFIX
from agent.message_content import flatten_message_text
from agent.session_notes import format_notes_for_prompt, redact_sensitive_text

logger = logging.getLogger(__name__)

# (task_name, exception) -> None; surfaces auxiliary failures so silent drops don't pile up as NULL titles.
FailureCallback = Callable[[str, BaseException], None]
# (title, source) -> None; source is the persisted provenance (``derived`` / ``llm``). Consumers paying a
# rate-limited remote rename per title (Discord thread, Telegram topic) should act on ``llm`` only.
TitleCallback = Callable[[str, str], None]
# () -> bool, called right before the LLM request; False skips (e.g. the user switched models and
# the request would reload one the runtime already evicted).
# Validation callback: () -> bool. See #19027.
RuntimeValidator = Callable[[], bool]

# Text budget handed to the model (Claude Code / OpenClaw converged on 1000).
MAX_TITLE_INPUT_CHARS = 1000
# Cap on the instant derived title; a raw fragment reads worse the longer it runs.
MAX_DERIVED_TITLE_CHARS = 48
# Answer-shaped guard: a tiny model sometimes answers instead of titling; longer is rejected, not truncated.
# Upper bound on accepted title word count. Titling is a 3-7 word task; a small tiny-model sometimes ignores
# the task and answers the user's message instead — that answer must never become the session title (see the
# answer-shaped output guard in generate_title; port of can1357/oh-my-pi#7306). 12 leaves headroom for
# legitimate wordy titles while excluding full-sentence answers.
_MAX_TITLE_WORDS = 12

_TITLE_PROMPT_TEMPLATE = (
    "You name chat sessions. Given the user's opening message, write a title "
    "that lets them find this conversation again in a list.\n\n"
    "Rules:\n"
    "- 3 to 7 words, sentence case (capitalize only the first word and proper nouns).\n"
    "- Name what the user wants DONE, not that they asked a question.\n"
    "- Keep technical terms, filenames, numbers, and error codes exact.\n"
    "- Drop filler words: the, this, my, a, an.\n"
    "- No trailing punctuation, no quotes, no tool names, no 'Title:' prefix.\n"
    "- Never answer the message. Name it.\n"
    "- Always produce something, even for a bare greeting.\n"
    "__LANGUAGE_RULE__\n"
    'Good: {"title": "Fix login button on mobile"}\n'
    'Good: {"title": "Postgres connection pool exhaustion"}\n'
    'Good: {"title": "Friendly greeting"}\n'
    'Too vague: {"title": "Code changes"}\n'
    'Too long: {"title": "Investigate and fix the issue where the login button '
    'does not respond on mobile devices"}\n\n'
    'Reply with JSON only: {"title": "..."}'
)

_LANGUAGE_RULE_MATCH_USER = "- Write the title in the same language as the user's message."
_LANGUAGE_RULE_PINNED = "- Write the title in {language}."

# Constrains the response to a single title field ("model answered instead of titling" failure class).
_TITLE_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {"name": "session_title", "strict": True, "schema": {
        "type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"], "additionalProperties": False}},
}

# Control-tag wrappers around machine-authored content inside a nominal "user" message (Codex CLI's
# RECOGNIZED_CONTROL_WRAPPERS): stripped, titling continues on what remains.
_CONTROL_WRAPPERS = tuple(
    (f"<{tag}>", f"</{tag}>")
    for tag in ("command-message", "command-name", "command-args", "local-command-caveat", "local-command-stderr",
                "local-command-stdout", "task-notification", "system-reminder", "ide_opened_file", "ide_selection")
)

# Hermes' own machine-authored openers: a compaction handoff or resumed session must not be titled after them.
_MACHINE_PREFIXES = (
    "[CONTEXT COMPACTION", LEGACY_SUMMARY_PREFIX, "[Runtime note:", "[System note:", "[SYSTEM]",
    # tui_gateway.server._MODEL_SWITCH_MARKER_PREFIX (keep in sync); persisted as role="user" because
    # strict providers reject a non-first system message.
    # Model-switch marker from tui_gateway.server._append_model_switch_marker. It is persisted with
    # role="user" (strict OpenAI-compatible providers reject a system message that is not first — #48338),
    # so without this entry it looks like a real opening turn: switching models before the first real
    # message titled the session "[System: The active model for this chat has…" instead of the user's actual
    # question.
    "[System: The active model for this chat has changed to ",
)


def _title_config() -> dict:
    """``auxiliary.title_generation`` (lazy read-only import: no hermes_cli cycle, no migration writes)."""
    from hermes_cli.config import load_config_readonly
    return ((load_config_readonly() or {}).get("auxiliary") or {}).get("title_generation") or {}


def _title_language() -> str:
    """Configured title language, or "" to match the user."""
    try:
        return str(_title_config().get("language", "")).strip()
    except Exception:
        return ""


def _auto_title_enabled() -> bool:
    try:
        from utils import is_truthy_value
        return is_truthy_value(_title_config().get("enabled"), default=True)
    except Exception:
        logger.debug("Failed to read title_generation.enabled", exc_info=True)
        return True


def strip_control_wrappers(text: str) -> str:
    """Remove leading control wrappers (nested too) so a slash-command turn reduces to the prose the user typed."""
    current = (text or "").strip()
    for _ in range(len(_CONTROL_WRAPPERS) * 2):  # bounded: each pass must remove a wrapper or we stop
        stripped = _strip_one_wrapper(current)
        if stripped == current:
            break
        current = stripped
    return current


def _strip_one_wrapper(text: str) -> str:
    lowered = text.lower()
    for open_tag, close_tag in _CONTROL_WRAPPERS:
        if not lowered.startswith(open_tag):
            continue
        end = lowered.find(close_tag)
        if end == -1:  # unterminated wrapper: drop the opening tag and keep the body
            return text[len(open_tag):].strip()
        # Prefer trailing prose; otherwise the wrapper body is all we have.
        return (text[end + len(close_tag):].strip() or text[len(open_tag):end].strip()).strip()
    return text


def _summarize_user_message(user_message: str) -> str:
    """Text worth titling: describe a ``/skill`` invocation (it embeds the whole skill body), then strip wrappers."""
    if not user_message:
        return ""
    described = None
    try:
        from agent.skill_commands import describe_skill_invocation
        described = describe_skill_invocation(user_message)
    except Exception:
        logger.debug("Skill-scaffolding summary failed; titling raw", exc_info=True)
    return strip_control_wrappers(user_message if described is None else described)


def is_titleable_user_message(user_message: str) -> bool:
    """False for machine-authored openers and turns that reduce to nothing once scaffolding is stripped."""
    return (isinstance(user_message, str) and bool(user_message.strip()) and not user_message.lstrip().startswith(_MACHINE_PREFIXES)
            and bool(_summarize_user_message(user_message).strip()))


def derive_title(user_message: str) -> Optional[str]:
    """Instant title: first meaningful line trimmed to a word boundary. No model, never fails."""
    line = " ".join(_first_line(_summarize_user_message(user_message)).split())
    if len(line) > MAX_DERIVED_TITLE_CHARS:
        cut = line[:MAX_DERIVED_TITLE_CHARS]
        space = cut.rfind(" ")
        line = (cut[:space] if space > MAX_DERIVED_TITLE_CHARS // 2 else cut).rstrip(" ,.;:—-") + "…"
    return line or None


def _strip_title_prefix(text: str) -> str:
    return text[6:].strip() if text.lower().startswith("title:") else text


def _first_line(text: str) -> str:
    return next((ln.strip() for ln in text.splitlines() if ln.strip()), "")


def _extract_title_text(content: str) -> str:
    """Strict JSON, then a loose JSON scan, then first-line prose (a provider ignoring ``response_format`` still titles)."""
    if not content:
        return ""
    raw = content.strip()
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", raw, re.DOTALL)
    if fenced:
        raw = fenced.group(1).strip()
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict) and isinstance(parsed.get("title"), str):
            return parsed["title"].strip()
    except (ValueError, TypeError):
        pass
    match = re.search(r'"title\"\s*:\s*"((?:[^"\\]|\\.)*)"', raw)
    if match:
        with suppress(ValueError):
            return json.loads(f'"{match.group(1)}"').strip()
        return match.group(1).strip()
    # Prose fallback: scrub <think> blocks so reasoning can't leak into a title.
    try:
        from agent.agent_runtime_helpers import strip_think_blocks
        raw = strip_think_blocks(None, raw).strip()
    except Exception:
        logger.debug("strip_think_blocks unavailable for title output", exc_info=True)
    return _strip_title_prefix(_first_line(raw)).strip("\"'").strip()


def _clean_title(text: str) -> Optional[str]:
    """Normalize a model-produced title, or None when nothing usable remains."""
    title = _strip_title_prefix(" ".join((text or "").split()).strip("\"'").strip()).rstrip(".!,;:")
    if len(title) > 80:
        title = title[:77].rstrip() + "..."
    return title or None


def _safe_callback(callback: Optional[Callable], args: tuple, log_fmt: str, label: str) -> None:
    """Invoke an optional consumer callback, never raising."""
    try:
        if callback is not None:
            callback(*args)
    except Exception:
        logger.debug(log_fmt, label, exc_info=True)


def _report_failure(failure_callback: Optional[FailureCallback], exc: BaseException, label: str) -> None:
    _safe_callback(failure_callback, ("title generation", exc), "%s failure_callback raised", label)


def _notify_title(title_callback: Optional[TitleCallback], title: str, source: str, label: str) -> None:
    _safe_callback(title_callback, (title, source), "%s callback failed", label)


def generate_title(
    user_message: str,
    timeout: Optional[float] = None,
    failure_callback: Optional[FailureCallback] = None,
    main_runtime: dict = None,
    runtime_validator: Optional[RuntimeValidator] = None,
) -> Optional[str]:
    """Title from the opening message alone (waiting for the assistant made this slow and bought
    nothing). ``runtime_validator`` runs right before the request; False skips silently.

    If it returns False (e.g. the user's model was switched since the background thread captured its runtime
    snapshot), the call is skipped silently — no request is sent, so a stale title request can't reload a
    model the runtime already unloaded (#19027).
    """
    if not _auto_title_enabled():
        logger.debug("Auto-title skipped: auxiliary.title_generation.enabled=false")
        return None
    try:
        if runtime_validator is not None and not runtime_validator():
            logger.debug("Title generation skipped: runtime validator returned False")
            return None
    except Exception:  # fail open: a broken validator must not disable titling
        logger.debug("Title runtime validator raised; proceeding", exc_info=True)
    user_snippet = _summarize_user_message(user_message)[:MAX_TITLE_INPUT_CHARS]
    if not user_snippet.strip():
        return None
    language = _title_language()
    # str.replace, not str.format: the prompt embeds literal JSON braces.
    prompt = _TITLE_PROMPT_TEMPLATE.replace(
        "__LANGUAGE_RULE__", _LANGUAGE_RULE_PINNED.format(language=language) if language else _LANGUAGE_RULE_MATCH_USER,
    )
    try:
        response = call_llm(
            task="title_generation",
            messages=[{"role": "system", "content": prompt}, {"role": "user", "content": user_snippet}],
            # A title is a handful of tokens; a larger ceiling let chatty models burn seconds.
            max_tokens=64, temperature=0.3, timeout=timeout, main_runtime=main_runtime,
            extra_body={"response_format": _TITLE_RESPONSE_FORMAT},
        )
        title = _clean_title(_extract_title_text(response.choices[0].message.content or ""))
        # Answer-shaped output guard: titling is a 3-7 word task, so a title with many words is a model that
        # ignored the task and answered the user's message instead ("I don't have context on X — that's not
        # something I recognize..."). Truncating would store half an assistant blob as the session title,
        # which is still an assistant blob — reject instead so the caller retries on the next exchange
        # (maybe_auto_title fires for the first two exchanges). Port of can1357/oh-my-pi#7306.
        if title is not None and len(title.split()) > _MAX_TITLE_WORDS:
            # Answer-shaped output: reject (not truncate) so the caller retries next exchange.
            logger.debug("Rejecting answer-shaped title output (%d words > %d)", len(title.split()), _MAX_TITLE_WORDS)
            return None
        return title
    except Exception as e:
        # WARNING so it shows in agent.log without debug mode; stack at debug.
        logger.warning("Title generation failed: %s", e)
        logger.debug("Title generation traceback", exc_info=True)
        _report_failure(failure_callback, e, "Title generation")
        return None


def _persist_session_title(
    session_db,
    session_id,
    title,
    *,
    source,
    turn_index: Optional[int] = None,
    dedupe=True,
):
    """Persist a title at *source* authority, recovering from name collisions.

    The write goes through ``set_auto_title`` (precedence check + write in one
    transaction) so a manual ``/title`` set while generation was in flight is
    never overwritten. ``ValueError`` means the name is taken by an unrelated
    session (the unique-title index); rather than leave the session untitled
    (#50537), append a ``#N`` suffix via ``get_next_title_in_lineage``.

    ``dedupe=False`` re-raises that collision instead. The derived title is the
    one write on the turn's critical path, and it is also the one that collides
    constantly — it is a slice of the user's own words, and people open sessions
    with "hi" and "help me debug this". Scanning the lineage for the next free
    "hi #N" is a widening scan, run inline, for a name the model replaces a
    second later. The background stage picks the collision back up, so nothing
    is lost by declining it here.

    Returns the title actually persisted, or None when a higher-authority
    title already held the row (nothing was written).
    """
    auto_fn = getattr(session_db, "set_auto_title", None)

    def _set(candidate):
        if auto_fn is not None:
            if not _call_title_setter(
                auto_fn,
                session_id,
                candidate,
                source=source,
                turn_index=turn_index,
            ):
                logger.debug(
                    "Skipping %s title: a higher-authority title already holds "
                    "session %s",
                    source, session_id,
                )
                return None
            return candidate
        # Older store without provenance support.
        legacy_fn = getattr(session_db, "set_auto_title_if_empty", None)
        if legacy_fn is not None:
            return candidate if legacy_fn(session_id, candidate) else None
        if session_db.set_session_title(session_id, candidate) is False:
            raise RuntimeError(f"session {session_id} not found when storing title")
        return candidate

    try:
        return _set(title)
    except ValueError:
        next_title_fn = getattr(session_db, "get_next_title_in_lineage", None)
        deduped = next_title_fn(title) if dedupe and next_title_fn is not None else None
        if not deduped or deduped == title:
            raise
        return _set(deduped)


def apply_instant_title(session_db, session_id: str, user_message: str, title_callback: Optional[TitleCallback] = None) -> Optional[str]:
    """Write the derived title inline. Returns it, or None (no usable text, or a ``derived``+ title exists). Never raises."""
    if not session_db or not session_id:
        return None
    try:
        title = derive_title(user_message) if is_titleable_user_message(user_message) else None
        persisted = _persist_session_title(session_db, session_id, title, source="derived", dedupe=False) if title else None
        if persisted:
            _notify_title(title_callback, persisted, "derived", "Instant-title")
        return persisted
    except Exception:
        logger.debug("Instant title failed", exc_info=True)
        return None


def _has_upgraded_title(session_db, session_id: str) -> bool:
    """True when the session already carries an ``llm``/``user`` title, or the check fails."""
    try:
        source_fn = getattr(session_db, "get_session_title_source", None)
        if source_fn is not None:
            return source_fn(session_id) not in (None, "derived")
        return bool(session_db.get_session_title(session_id))
    except Exception:
        return True


def auto_title_session(
    session_db,
    session_id: str,
    user_message: str,
    failure_callback: Optional[FailureCallback] = None,
    main_runtime: dict = None,
    title_callback: Optional[TitleCallback] = None,
    runtime_validator: Optional[RuntimeValidator] = None,
) -> None:
    """Generate and store the model title (daemon-thread target); skips sessions already carrying an
    ``llm``/``user`` title (a ``derived`` one is expected — upgrading it is the point). Never lets an
    exception escape (the threading excepthook would spray a traceback into the terminal); the canonical
    trigger is the post-``hermes update`` window where lazy imports read NEW source against OLD modules."""
    try:
        if not session_db or not session_id or _has_upgraded_title(session_db, session_id):
            return
    except Exception:
        return

    # This runs on a bare daemon thread spawned AFTER the turn's ambient
    # conversation context was reset, so publish it here from the session id
    # we already hold — the title-generation LLM call then carries the same
    # ``conversation=`` Portal tag as the turn it titles. Root-of-lineage for
    # consistency with the agent loop.
    from agent.aux_accounting import set_accounting_context
    from agent.portal_tags import set_conversation_context

    conversation_id = session_id
    try:
        conversation_id = session_db.get_conversation_root(session_id) or session_id
    except Exception:
        pass
    set_conversation_context(conversation_id)
    # Same for the accounting context, so the title call's token usage is
    # recorded against this session (task='title_generation', #23270).
    set_accounting_context(session_db, session_id)

    try:
        title = generate_title(
            user_message,
            failure_callback=failure_callback,
            main_runtime=main_runtime,
            runtime_validator=runtime_validator,
        )
    except Exception as exc:
        logger.warning("Title generation failed: %s", exc)
        logger.debug("Title generation traceback", exc_info=True)
        _report_failure(failure_callback, exc, "Title generation")
        return
    source = "auto_initial"
    if not title:
        return

    try:
        persisted = _persist_session_title(
            session_db,
            session_id,
            title,
            source=source,
            turn_index=1,
        )
        if persisted is None:
            return
        logger.debug("Auto-generated session title: %s", persisted)
        _notify_title_callback(title_callback, persisted, source)
    except Exception as e:
        # WARNING so operators see it in agent.log; names the likely cause.
        logger.warning("Auto-title failed (harmless; if this started after an update, restart the running Hermes process): %s", e)
        logger.debug("Auto-title traceback", exc_info=True)
        _report_failure(failure_callback, e, "Auto-title")


def _is_real_user_turn(message: Any) -> bool:
    """A question a person actually asked (Hermes persists machinery under ``role="user"``)."""
    if not isinstance(message, dict) or message.get("role") != "user":
        return False
    content = message.get("content")
    return is_titleable_user_message(content if isinstance(content, str) else flatten_message_text(content))


def _session_is_untitled(session_db, session_id: str) -> bool:
    """No title of any provenance; False when it can't tell (no model call per turn for an unreadable title)."""
    getter = getattr(session_db, "get_session_title", None)
    try:
        return callable(getter) and not str(getter(session_id) or "").strip()
    except Exception:
        logger.debug("Untitled check failed for %s", session_id, exc_info=True)
        return False


def maybe_auto_title(
    session_db,
    session_id: str,
    user_message: str,
    conversation_history: Optional[list] = None,
    failure_callback: Optional[FailureCallback] = None,
    main_runtime: dict = None,
    title_callback: Optional[TitleCallback] = None,
    runtime_validator: Optional[RuntimeValidator] = None,
) -> None:
    """Instant inline title, then a daemon-thread upgrade. Call at the START of a turn, before the model."""
    if not session_db or not session_id or not user_message:
        return
    # A title is committed only after a successful first exchange. A user-only
    # history can still fail or be cancelled and must remain untitled.
    if not any(
        isinstance(message, dict) and message.get("role") == "assistant"
        for message in (conversation_history or [])
    ):
        return

    # Count the real questions behind us to detect the opening turn.
    # ``conversation_history`` is the state BEFORE this turn's message is
    # appended when called from the turn prologue, and after it when called
    # post-response, so accept both.
    #
    # Two things have to be true to skip: we are past the opening turn AND the
    # session already has a name. Either alone gets it wrong. The count alone
    # left a session that opened with machinery permanently nameless, because
    # nothing reconsidered it. The title alone would never title at all on a
    # store too old to report one.
    user_msg_count = sum(1 for m in (conversation_history or []) if _is_real_user_turn(m))
    if (user_msg_count > 1 and not _session_is_untitled(session_db, session_id)) or not is_titleable_user_message(user_message):
        return
    if not _auto_title_enabled():  # config read after the cheap guards so the file isn't touched every turn
        logger.debug("Auto-title skipped: auxiliary.title_generation.enabled=false")
        return

    thread = threading.Thread(
        target=auto_title_session,
        args=(session_db, session_id, user_message),
        kwargs=dict(failure_callback=failure_callback, main_runtime=main_runtime, title_callback=title_callback, runtime_validator=runtime_validator),
        daemon=True,
        name="auto-title",
    )
    thread.start()


_RETITLE_PROMPT = (
    "Generate a concise updated session title (3-8 words). Capture the overall "
    "current topic, not just the first exchange. Return JSON only."
)

_FINAL_TITLE_PROMPT = (
    "Generate the final concise session title (3-8 words) from this compact "
    "session summary. Prefer a title broad enough to cover all major work. "
    "Return JSON only."
)

_MANUAL_SOURCES = {"manual", "user"}
_AUTO_SOURCE_TO_STORAGE = {
    "auto_initial": "llm",
    "auto_mid": "llm",
    "auto_final": "llm",
}
_LIFECYCLE_TITLE_RANK = {
    None: 0,
    "derived": 0,
    "auto_initial": 1,
    "llm": 1,
    "auto_mid": 2,
    "auto_final": 3,
    "manual": 4,
    "user": 4,
}

_MID_RETITLE_MIN_USER_TURNS = 5

_MID_RETITLE_TURN_INTERVAL = 5


def _install_title_lifecycle_store_adapter() -> None:
    """Adapt older SessionDB title APIs to the lifecycle-source vocabulary."""
    try:
        from hermes_state import SessionDB
    except Exception:
        return
    if getattr(SessionDB, "_agent_title_lifecycle_adapter", False):
        return

    original_rank = SessionDB._title_rank
    original_set_session_title = SessionDB.set_session_title
    original_set_auto_title = SessionDB.set_auto_title

    def _rank(cls, source):
        if source == "manual":
            return original_rank(cls.TITLE_SOURCE_USER)
        if source in _AUTO_SOURCE_TO_STORAGE:
            return original_rank(_AUTO_SOURCE_TO_STORAGE[source])
        return original_rank(source)

    def _stamp(self, session_id: str, source: str | None, turn_index: Optional[int]) -> None:
        self._execute_write(
            lambda conn: conn.execute(
                "UPDATE sessions SET title_source = ?, title_updated_at = strftime('%s','now'), "
                "title_turn_index = ? WHERE id = ? AND title IS NOT NULL",
                (source, turn_index, session_id),
            ).rowcount
        )

    def _set_lifecycle_title(self, session_id: str, title: str, source: str,
                             turn_index: Optional[int], *, force: bool = False) -> bool:
        title = self.sanitize_title(title)
        new_rank = _LIFECYCLE_TITLE_RANK.get(source, 0)

        def _do(conn):
            current = conn.execute(
                "SELECT title, title_source, hidden FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if current is None:
                return 0
            if (
                (current["title"] or "") == self.CANONICAL_BOT_CHAT_TITLE
                and bool(current["hidden"])
                and title != self.CANONICAL_BOT_CHAT_TITLE
            ):
                return 0
            current_rank = _LIFECYCLE_TITLE_RANK.get(current["title_source"], 0)
            if not force and current["title"] is not None and current_rank >= new_rank:
                return 0
            if title:
                conflict = conn.execute(
                    "SELECT id FROM sessions WHERE title = ? AND id != ?",
                    (title, session_id),
                ).fetchone()
                if conflict:
                    conflict_id = conflict["id"]
                    if self._is_compression_ancestor(
                        conn, ancestor_id=conflict_id, descendant_id=session_id
                    ):
                        conn.execute("UPDATE sessions SET title = NULL WHERE id = ?", (conflict_id,))
                    else:
                        raise ValueError(
                            f"Title '{title}' is already in use by session {conflict_id}"
                        )
            return conn.execute(
                "UPDATE sessions SET title = ?, title_source = ?, "
                "title_updated_at = strftime('%s','now'), title_turn_index = ? "
                "WHERE id = ? AND title IS ? AND title_source IS ?",
                (
                    title,
                    source if title else None,
                    turn_index,
                    session_id,
                    current["title"],
                    current["title_source"],
                ),
            ).rowcount

        return self._execute_write(_do) > 0

    def _set_session_title(self, session_id: str, title: str, *, source: str | None = None,
                           turn_index: Optional[int] = None) -> bool:
        if source is None:
            ok = original_set_session_title(self, session_id, title)
            if ok:
                _stamp(self, session_id, "manual", turn_index)
            return ok
        if source in _AUTO_SOURCE_TO_STORAGE:
            return _set_lifecycle_title(self, session_id, title, source, turn_index, force=True)
        if source == "manual":
            ok = original_set_session_title(self, session_id, title)
            if ok:
                _stamp(self, session_id, source, turn_index)
            return ok
        return original_set_session_title(self, session_id, title)

    def _set_auto_title(self, session_id: str, title: str, *, source: str,
                        turn_index: Optional[int] = None) -> bool:
        if source in _AUTO_SOURCE_TO_STORAGE:
            return _set_lifecycle_title(self, session_id, title, source, turn_index)
        return original_set_auto_title(self, session_id, title, source=source)

    def _get_session_title_metadata(self, session_id: str) -> Optional[dict[str, Any]]:
        row = self._read_one(
            "SELECT title, title_source, title_updated_at, title_turn_index "
            "FROM sessions WHERE id = ?",
            (session_id,),
        )
        if row is None:
            return None
        return {
            "title": row["title"],
            "title_source": row["title_source"] if row["title"] is not None else None,
            "title_updated_at": row["title_updated_at"],
            "title_turn_index": row["title_turn_index"],
        }

    SessionDB._title_rank = classmethod(_rank)
    SessionDB.set_session_title = _set_session_title
    SessionDB.set_auto_title = _set_auto_title
    SessionDB.get_session_title_metadata = _get_session_title_metadata
    SessionDB._agent_title_lifecycle_adapter = True


_install_title_lifecycle_store_adapter()

def _call_lifecycle_title_llm(
    *,
    system_prompt: str,
    user_prompt: str,
    timeout: float,
    failure_callback: Optional[FailureCallback],
    main_runtime: Optional[dict],
) -> Optional[str]:
    """Use the upstream structured-output path for lifecycle refinements."""
    try:
        response = call_llm(
            task="title_generation",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=64,
            temperature=0.3,
            timeout=timeout,
            main_runtime=main_runtime,
            extra_body={"response_format": _TITLE_RESPONSE_FORMAT},
        )
        content = response.choices[0].message.content or ""
        return _clean_title(_extract_title_text(content))
    except Exception as exc:
        logger.warning("Title generation failed: %s", exc)
        logger.debug("Title generation traceback", exc_info=True)
        if failure_callback is not None:
            try:
                failure_callback("title generation", exc)
            except Exception:
                logger.debug("Title generation failure_callback raised", exc_info=True)
        return None

def _title_words(title: str) -> set[str]:
    return {
        word
        for word in re.findall(r"[\wÀ-ž]+", (title or "").lower())
        if len(word) > 2
    }

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


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return flatten_message_text(content)
    return "" if content is None else str(content)


def _format_recent_exchange(messages: list[dict[str, Any]], max_messages: int = 10) -> str:
    lines = []
    for message in (messages or [])[-max_messages:]:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "").lower()
        if role not in {"user", "assistant"}:
            continue
        text = redact_sensitive_text(re.sub(r"\s+", " ", _message_text(message)).strip())
        if text:
            lines.append(f"{role}: {text[:700]}")
    return "\n".join(lines)

def generate_retitle(
    *,
    current_title: Optional[str],
    messages: list[dict[str, Any]],
    events: Optional[list[dict[str, Any]]] = None,
    scratchpad: Optional[dict[str, Any]] = None,
    timeout: float = 30.0,
    failure_callback: Optional[FailureCallback] = None,
    main_runtime: Optional[dict] = None,
) -> Optional[str]:
    """Generate a mid-session title from recent transcript and durable notes."""
    if not _auto_title_enabled():
        return None
    transcript = _format_recent_exchange(messages)
    notes = redact_sensitive_text(format_notes_for_prompt(events, scratchpad) or "")
    if not transcript and not notes:
        return None
    prompt = (
        f"Current title: {redact_sensitive_text(current_title or '(none)')}\n\n"
        f"Incremental notes:\n{notes[:4000]}\n\n"
        f"Recent transcript:\n{transcript[-5000:]}"
    )
    return _call_lifecycle_title_llm(
        system_prompt=_RETITLE_PROMPT,
        user_prompt=prompt,
        timeout=timeout,
        failure_callback=failure_callback,
        main_runtime=main_runtime,
    )

def generate_final_title(
    *,
    current_title: Optional[str],
    summary: dict[str, Any],
    timeout: float = 30.0,
    failure_callback: Optional[FailureCallback] = None,
    main_runtime: Optional[dict] = None,
) -> Optional[str]:
    """Generate a final title from a compact stored session summary."""
    if not _auto_title_enabled():
        return None
    outline = summary.get("outline") or []
    topics = summary.get("topics") or []
    prompt = (
        f"Current title: {redact_sensitive_text(current_title or '(none)')}\n"
        f"Summary: {redact_sensitive_text(str(summary.get('short_summary') or ''))}\n"
        f"Outline: {redact_sensitive_text('; '.join(map(str, outline)))}\n"
        f"Topics: {redact_sensitive_text(', '.join(map(str, topics)))}"
    )
    return _call_lifecycle_title_llm(
        system_prompt=_FINAL_TITLE_PROMPT,
        user_prompt=prompt,
        timeout=timeout,
        failure_callback=failure_callback,
        main_runtime=main_runtime,
    )

def _notify_title_callback(
    callback: Optional[TitleCallback], title: str, source: str
) -> None:
    """Support upstream two-argument and legacy one-argument callbacks."""
    if callback is None:
        return
    callback_source = "derived" if source == "derived" else "llm"
    try:
        signature = inspect.signature(callback)
    except (TypeError, ValueError):
        signature = None
    try:
        if signature is not None:
            try:
                signature.bind(title, callback_source)
            except TypeError:
                callback(title)
            else:
                callback(title, callback_source)
        else:
            callback(title, callback_source)
    except Exception:
        logger.debug("Auto-title callback failed", exc_info=True)

def _call_title_setter(
    setter,
    session_id: str,
    title: str,
    *,
    source: str,
    turn_index: Optional[int],
):
    """Call current or legacy store signatures without retrying body errors."""
    variants = [
        {"source": source, "turn_index": turn_index},
        {"source": source},
        {},
    ]
    try:
        signature = inspect.signature(setter)
    except (TypeError, ValueError):
        # Unknown signatures get one modern call only.  Never catch a TypeError
        # from the function body and replay a potentially stateful write.
        return setter(session_id, title, **variants[0])
    for kwargs in variants:
        try:
            signature.bind(session_id, title, **kwargs)
        except TypeError:
            continue
        return setter(session_id, title, **kwargs)
    raise TypeError("title setter has no compatible signature")

def _safe_set_title(
    session_db,
    session_id: str,
    title: str,
    *,
    source: str,
    turn_index: Optional[int] = None,
    title_callback: Optional[TitleCallback] = None,
) -> bool:
    """Persist a lifecycle update through the store's atomic precedence path."""
    auto_setter = getattr(session_db, "set_auto_title", None)

    def _write(candidate: str) -> bool:
        if callable(auto_setter):
            return bool(
                _call_title_setter(
                    auto_setter,
                    session_id,
                    candidate,
                    source=source,
                    turn_index=turn_index,
                )
            )
        return bool(
            _call_title_setter(
                session_db.set_session_title,
                session_id,
                candidate,
                source=source,
                turn_index=turn_index,
            )
        )

    try:
        ok = _write(title)
    except ValueError:
        try:
            fallback = session_db.get_next_title_in_lineage(title)
            ok = _write(fallback)
            title = fallback
        except Exception:
            logger.debug("Failed to set generated session title", exc_info=True)
            return False
    except Exception:
        logger.debug("Failed to set generated session title", exc_info=True)
        return False
    if ok:
        _notify_title_callback(title_callback, title, source)
    return bool(ok)

def _title_meta(session_db, session_id: str) -> Optional[dict[str, Any]]:
    getter = getattr(session_db, "get_session_title_metadata", None)
    if callable(getter):
        meta = getter(session_id)
        if isinstance(meta, dict):
            return meta
    title = session_db.get_session_title(session_id)
    return {"title": title, "title_source": None, "title_turn_index": None}

def _is_manual(meta: dict[str, Any]) -> bool:
    return (meta or {}).get("title_source") in _MANUAL_SOURCES

def retitle_session(
    session_db,
    session_id: str,
    messages: list[dict[str, Any]],
    *,
    turn_index: Optional[int] = None,
    failure_callback: Optional[FailureCallback] = None,
    main_runtime: Optional[dict] = None,
    title_callback: Optional[TitleCallback] = None,
) -> bool:
    """Generate and atomically store a mid-session lifecycle title."""
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
    messages: list[dict[str, Any]],
    conversation_history: Optional[list[dict[str, Any]]] = None,
    *,
    turn_index: Optional[int] = None,
    failure_callback: Optional[FailureCallback] = None,
    main_runtime: Optional[dict] = None,
    title_callback: Optional[TitleCallback] = None,
    synchronous: bool = False,
) -> Optional[threading.Thread | bool]:
    """Run the cheap manual/final/throttle gate before a mid-session LLM call."""
    if not session_db or not session_id or not messages:
        return False if synchronous else None
    user_msg_count = sum(
        1 for message in messages if _is_real_user_turn(message)
    )
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
    summary: dict[str, Any],
    *,
    failure_callback: Optional[FailureCallback] = None,
    main_runtime: Optional[dict] = None,
    title_callback: Optional[TitleCallback] = None,
) -> bool:
    """Refine an automatic title from the final compact session summary."""
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
