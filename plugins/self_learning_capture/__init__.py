from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from agent.secret_patterns import AWS_BARE_SECRET_RE, VALUE_ONLY_RE

# ``label <sep> value`` forms — keep the label, redact the value. The key/value
# matchers are quote-aware so the JSON form this plugin actually serializes
# (e.g. ``"password": "hunter2"``) is redacted, not just the bare ``token=...`` form.
_SECRET_KEYED_PATTERNS = [
    re.compile(r"(?i)(authorization\s*:\s*bearer)\s+[^\s'\",;]+"),
    re.compile(r"(?i)(bearer)\s+[^\s'\",;]+"),
    re.compile(
        r"(?i)(\"?(?:api[_-]?key|secret|token|password|passwd|credential"
        r"|access[_-]?key|client[_-]?secret|private[_-]?key|authorization)\"?)"
        # Quote-aware value: a fully single- or double-quoted string (which may
        # contain whitespace, ``,`` or ``;``), else an UNQUOTED value that runs
        # to the next clear delimiter (newline / quote / ``,`` / ``;``). The
        # unquoted branch deliberately consumes intervening whitespace so a
        # multi-word passphrase (``password = correct horse battery staple``) is
        # redacted in full instead of leaking everything after the first space.
        r"\s*[:=]\s*(?:'[^']*'|\"[^\"]*\"|[^\n'\",;]+)"
    ),
]

# Whole-match secrets — there is no label to keep, so the entire match is the
# secret and must be replaced in full. Sourced from the canonical
# agent.secret_patterns module so this feedback-inbox sanitizer covers the same
# value-only vendor shapes (xai-, SG., hf_, pplx-, tvly-, bare AWS secret,
# Telegram bot token, ...) as the durable-memory gate and the session-notes
# index redactor — a token the curator might later promote to durable memory can
# no longer slip past here while another consumer would have caught it.
_SECRET_VALUE_PATTERNS = [
    VALUE_ONLY_RE,
    AWS_BARE_SECRET_RE,
    re.compile(r"(?is)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----"),
]

# ``scheme://user:password@host`` — keep ``scheme://user:``, redact the password.
# The user segment is optional so userless creds (``redis://:pass@host``) are
# covered too. The password class is greedy (``[^\s/]+``) and backtracks to the
# LAST ``@`` before the host, so a password that itself contains ``@``
# (``postgres://u:p@ss@host/db``) is redacted in full rather than truncated at
# the first ``@`` (which previously leaked the ``@ss@host`` remainder).
_URL_CRED_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^\s:/@]*:)[^\s/]+(@)")

_CORRECTION_PATTERNS = [
    r"\b(ne mentsd|ne írd|ne ird|ne tedd|ezt ne|nem így|nem igy|rosszul|hibás|hibas|tévedtél|tevedtel)\b",
    r"\b(javítsd|javitsd|legközelebb|legkozelebb|jegyezd meg|remember this|don't do that|do not do that)\b",
    r"\b(ez nem igaz|ez nem jó|ez nem jo|nem ezt kértem|nem ezt kertem|félreértetted|felreertetted)\b",
]
_CORRECTION_RE = re.compile("|".join(f"(?:{p})" for p in _CORRECTION_PATTERNS), re.IGNORECASE)

_MAX_EXCERPT = 1600


def _home() -> Path:
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def _load_plugin_config() -> dict[str, Any]:
    try:
        from hermes_cli.config import cfg_get, load_config

        cfg = load_config()
        raw = cfg_get(cfg, "self_learning_capture", default={})
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _truthy(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
    return default


def _settings(config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    cfg: Mapping[str, Any] = config if isinstance(config, Mapping) else _load_plugin_config()
    return {
        "enabled": _truthy(cfg.get("enabled"), default=True),
        "feedback_inbox": str(cfg.get("feedback_inbox") or "").strip(),
    }


def _feedback_inbox(config: Mapping[str, Any] | None = None) -> Path:
    configured = _settings(config).get("feedback_inbox") or ""
    if configured:
        return Path(configured).expanduser()
    wiki = os.environ.get("WIKI_PATH")
    if not wiki:
        wiki = str(Path.home() / "wiki")
    return Path(wiki).expanduser() / "feedback" / "_inbox.md"


def _state_dir() -> Path:
    return _home() / "state" / "self_learning_capture"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sanitize(text: str) -> str:
    value = text.replace("\x00", "")
    for pattern in _SECRET_KEYED_PATTERNS:
        value = pattern.sub(lambda m: m.group(1) + "=[REDACTED]", value)
    value = _URL_CRED_RE.sub(lambda m: m.group(1) + "[REDACTED]" + m.group(2), value)
    for pattern in _SECRET_VALUE_PATTERNS:
        value = pattern.sub("[REDACTED]", value)
    return value


def _excerpt(value: Any, limit: int = _MAX_EXCERPT) -> str:
    try:
        if isinstance(value, str):
            text = value
        else:
            text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = str(value)
    text = _sanitize(text.strip())
    if len(text) > limit:
        return text[:limit] + "…"
    return text


def _hash(kind: str, session_id: str, text: str) -> str:
    digest = hashlib.sha256(f"{kind}\n{session_id}\n{text}".encode("utf-8", "ignore")).hexdigest()
    return digest[:20]


def _already_seen(key: str) -> bool:
    state = _state_dir()
    state.mkdir(parents=True, exist_ok=True)
    marker = state / f"{key}.seen"
    if marker.exists():
        return True
    marker.write_text(_now() + "\n", encoding="utf-8")
    return False


def _append_inbox(kind: str, session_id: str, body: str, key: str, *, config: Mapping[str, Any] | None = None) -> None:
    inbox = _feedback_inbox(config)
    inbox.parent.mkdir(parents=True, exist_ok=True)
    if not inbox.exists():
        inbox.write_text("# Feedback Inbox\n\n", encoding="utf-8")
    entry = (
        f"\n## [{_now()}] {kind} | {key}\n"
        f"- session_id: `{_sanitize(session_id or 'unknown')}`\n"
        f"- status: candidate\n"
        f"- routing_hint: daily-memory-curator should classify as user memory, Hermes memory, wiki, skill, or discard.\n\n"
        f"{body.strip()}\n"
    )
    with inbox.open("a", encoding="utf-8") as fh:
        fh.write(entry)


def _is_failure_result(result: Any) -> bool:
    if result is None:
        return False
    data: Any = None
    if isinstance(result, str):
        stripped = result.strip()
        if not stripped:
            return False
        try:
            data = json.loads(stripped)
        except Exception:
            low = stripped.lower()
            return any(token in low for token in ["traceback", "exception", "error executing", "command failed"])
    else:
        data = result
    if isinstance(data, dict):
        if data.get("success") is False:
            return True
        if data.get("error"):
            return True
        if isinstance(data.get("exit_code"), int) and data.get("exit_code") != 0:
            return True
        if isinstance(data.get("returncode"), int) and data.get("returncode") != 0:
            return True
    return False


def pre_llm_call(**kwargs: Any) -> None:
    config = kwargs.get("config") if isinstance(kwargs.get("config"), Mapping) else None
    if not _settings(config)["enabled"]:
        return None
    message = str(kwargs.get("user_message") or "")
    if not message.strip() or not _CORRECTION_RE.search(message):
        return None
    session_id = str(kwargs.get("session_id") or "")
    key = _hash("correction", session_id, message)
    if _already_seen(key):
        return None
    body = (
        "Detected a possible user correction or preference signal. Do not treat this as already durable; "
        "classify it later with the normal memory routing rules.\n\n"
        "```text\n"
        f"{_excerpt(message)}\n"
        "```\n"
    )
    _append_inbox("correction-detector", session_id, body, key, config=config)
    return None


def post_tool_call(**kwargs: Any) -> None:
    config = kwargs.get("config") if isinstance(kwargs.get("config"), Mapping) else None
    if not _settings(config)["enabled"]:
        return None
    result = kwargs.get("result")
    if not _is_failure_result(result):
        return None
    tool_name = str(kwargs.get("tool_name") or "unknown")
    session_id = str(kwargs.get("session_id") or "")
    args = kwargs.get("args") or {}
    duration_ms = kwargs.get("duration_ms")
    raw = f"{tool_name}\n{args}\n{result}"
    key = _hash("failure", session_id, raw)
    if _already_seen(key):
        return None
    body = (
        "Detected a failed tool call. This is a learning candidate only. Save it only if it reveals a reusable workflow, "
        "tooling quirk, stable environment fact, or skill patch. Discard transient command errors.\n\n"
        f"- tool: `{_sanitize(tool_name)}`\n"
        f"- duration_ms: `{duration_ms}`\n\n"
        "Args excerpt:\n\n"
        "```json\n"
        f"{_excerpt(args, 1000)}\n"
        "```\n\n"
        "Result excerpt:\n\n"
        "```text\n"
        f"{_excerpt(result, 1400)}\n"
        "```\n"
    )
    _append_inbox("failure-capture", session_id, body, key, config=config)
    return None


def register(ctx: Any) -> None:
    ctx.register_hook("pre_llm_call", pre_llm_call)
    ctx.register_hook("post_tool_call", post_tool_call)
