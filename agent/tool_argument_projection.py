"""Fail-closed outbound projection for tool-call arguments.

Execution retains complete arguments. Customer-facing payloads receive only the
explicit per-tool projection below. Unknown tools export no argument values.
"""

from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any

from agent.redact import redact_sensitive_text

_TOOL_DISPLAY_ARG_ALLOWLIST: dict[str, frozenset[str]] = {
    "read_file": frozenset({"path", "offset", "limit"}),
    "write_file": frozenset({"path"}),
    "patch": frozenset({"mode", "path", "replace_all"}),
    "search_files": frozenset(
        {
            "pattern",
            "target",
            "path",
            "file_glob",
            "limit",
            "offset",
            "output_mode",
            "context",
        }
    ),
    "terminal": frozenset({"command", "timeout", "workdir", "background", "pty"}),
    "process": frozenset({"action", "session_id", "timeout", "offset", "limit"}),
    "execute_code": frozenset(),
    "web_search": frozenset({"query", "limit"}),
    "web_extract": frozenset({"urls", "char_limit"}),
    "browser_navigate": frozenset({"url"}),
    "browser_click": frozenset({"ref"}),
    "browser_type": frozenset({"ref", "text"}),
    "browser_press": frozenset({"key"}),
    "browser_scroll": frozenset({"direction"}),
    "browser_snapshot": frozenset({"full"}),
    "browser_console": frozenset({"clear"}),
    "browser_vision": frozenset({"question", "annotate"}),
    "browser_get_images": frozenset(),
    "browser_back": frozenset(),
    "vision_analyze": frozenset({"question"}),
    "image_generate": frozenset(),
    "text_to_speech": frozenset({"speed", "provider"}),
    "todo": frozenset({"todos", "merge"}),
    "memory": frozenset({"action", "target"}),
    "session_search": frozenset(
        {
            "query",
            "limit",
            "sort",
            "session_id",
            "around_message_id",
            "window",
            "role_filter",
            "profile",
        }
    ),
    "delegate_task": frozenset({"goal", "tasks", "role"}),
    "skill_view": frozenset({"name", "file_path"}),
    "skills_list": frozenset({"category"}),
    "skill_manage": frozenset(
        {"action", "name", "file_path", "category", "replace_all", "absorbed_into"}
    ),
    "cronjob": frozenset(
        {
            "action",
            "job_id",
            "schedule",
            "name",
            "repeat",
            "deliver",
            "no_agent",
            "attach_to_session",
        }
    ),
    "clarify": frozenset({"question", "multi_select"}),
    "tool_search": frozenset({"query", "limit"}),
    "tool_describe": frozenset({"name"}),
    "tool_call": frozenset({"name"}),
    "computer_use": frozenset(
        {
            "action",
            "mode",
            "app",
            "element",
            "coordinate",
            "direction",
            "amount",
            "keys",
            "delivery_mode",
            "browser_pointer_action",
            "browser_dialog_action",
        }
    ),
}

_SENSITIVE_KEY_FRAGMENTS = (
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "password",
    "prompt_text",
    "secret",
    "token",
    "private_key",
    "session_cookie",
)
_MAX_DEPTH = 6
_MAX_ITEMS = 100


def _key_is_sensitive(key: Any) -> bool:
    lowered = str(key).strip().lower().replace("-", "_")
    return any(fragment in lowered for fragment in _SENSITIVE_KEY_FRAGMENTS)


def _redact_text(value: str) -> str:
    value = re.sub(r"(?i)\b(https?://)[^/@\s]+@", r"\1", value)
    return redact_sensitive_text(value, force=True, redact_url_credentials=True)


def sanitize_tool_display_text(value: Any) -> str:
    """Redact one customer-facing text payload without exporting raw on failure."""
    try:
        return _redact_text(str(value))
    except Exception:
        return "[REDACTED]"


def sanitize_tool_display_value(value: Any) -> Any:
    """Recursively sanitize an arbitrary customer-facing result payload."""
    try:
        return _sanitize(value)
    except Exception:
        return "[REDACTED]"


def _sanitize(value: Any, *, depth: int = 0) -> Any:
    if depth >= _MAX_DEPTH:
        return "[TRUNCATED]"
    if isinstance(value, str):
        return _redact_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, Mapping):
        result = {}
        for key, item in list(value.items())[:_MAX_ITEMS]:
            if _key_is_sensitive(key):
                continue
            result[str(key)] = _sanitize(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_sanitize(item, depth=depth + 1) for item in value[:_MAX_ITEMS]]
    return _redact_text(str(value))


def project_tool_args_for_display(tool_name: str, args: Any) -> dict[str, Any]:
    """Return an explicit safe display projection for one tool call."""
    if not isinstance(tool_name, str) or not isinstance(args, Mapping):
        return {}
    allowed = _TOOL_DISPLAY_ARG_ALLOWLIST.get(tool_name)
    if allowed is None:
        return {}
    try:
        return {
            key: _sanitize(args[key])
            for key in allowed
            if key in args and not _key_is_sensitive(key)
        }
    except Exception:
        return {}
