#!/usr/bin/env python3
"""
Todo Tool Module - Planning & Task Management

Provides an in-memory task list the agent uses to decompose complex tasks,
track progress, and maintain focus across long conversations. The state
lives on the AIAgent instance (one per session) and is re-injected into
the conversation after context compression events.

Design:
- Single `todo` tool: provide `todos` param to write, omit to read
- Every call returns the full current list
- No system prompt mutation, no tool response modification
- Behavioral guidance lives entirely in the tool schema description
"""

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Dict, Any, List, Optional

from utils import atomic_replace

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None


# Valid status values for todo items
VALID_STATUSES = {"pending", "in_progress", "completed", "cancelled"}

# Bounds on persisted todo state. The todo list is a planning aid the model
# re-reads after every context-compression event (see format_for_injection),
# so unbounded item content or count defeats the compression it rides through.
# These caps keep a single oversized item (whether authored by the model or
# replayed from caller-supplied history on the API server) from inflating the
# re-injection block. Generous relative to real plans — a todo item is a short
# task description, and active lists are a handful of items, not hundreds.
MAX_TODO_CONTENT_CHARS = 4000
MAX_TODO_ITEMS = 256
# Upper bound on a single todo tool-result payload accepted during history
# hydration. The gateway/API server replays caller-supplied conversation
# history to rebuild the store, so an oversized forged result is dropped
# before it is parsed and re-injected (see AIAgent._hydrate_todo_store).
MAX_TODO_RESULT_CHARS = 512_000
_TRUNCATION_MARKER = "… [truncated]"
# Persisted as ordinary message content. ContextCompressor uses this stable
# header to distinguish the synthetic post-compaction row from a real user.
TODO_INJECTION_HEADER = (
    "[Your active task list was preserved across context compression]"
)
_HERMES_META_COMMENT_RE = re.compile(
    r"<!--\s*hermes:id=\S+\s+status=\S+\s*-->"
)


def default_todo_markdown_path() -> Path:
    """Return the shared TODO.md path used by the agent and CUI."""
    raw = os.environ.get("AIWERK_CUI_TODO_PATH") or os.environ.get(
        "HERMES_TODO_PATH"
    )
    if raw:
        return Path(raw).expanduser()
    try:
        from hermes_constants import get_hermes_home

        return get_hermes_home() / "TODO.md"
    except Exception:
        return Path.home() / ".hermes" / "TODO.md"


class TodoStore:
    """
    In-memory todo list. One instance per AIAgent (one per session).

    Items are ordered -- list position is priority. Each item has:
      - id: unique string identifier (agent-chosen)
      - content: task description
      - status: pending | in_progress | completed | cancelled
    """

    def __init__(self, markdown_path: Optional[str | Path] = None):
        self._items: List[Dict[str, str]] = []
        self._markdown_path = (
            Path(markdown_path).expanduser() if markdown_path else None
        )
        self._markdown_mtime_ns: Optional[int] = None
        self._load_markdown_if_available()

    def write(self, todos: List[Dict[str, Any]], merge: bool = False) -> List[Dict[str, str]]:
        """
        Write todos. Returns the full current list after writing.

        Args:
            todos: list of {id, content, status} dicts
            merge: if False, replace the entire list. If True, update
                   existing items by id and append new ones.
        """
        self._refresh_from_markdown_if_changed()
        if not merge:
            # Replace mode: new list entirely
            self._items = self._normalize_order(
                [self._validate(t) for t in self._dedupe_by_id(todos)]
            )
        else:
            # Merge mode: update existing items by id, append new ones
            existing = {item["id"]: item for item in self._items}
            for t in self._dedupe_by_id(todos):
                item_id = self._normalize_id(t.get("id", ""))
                if not item_id:
                    continue  # Can't merge without an id

                if item_id in existing:
                    # Update only the fields the LLM actually provided
                    if "content" in t and t["content"]:
                        existing[item_id]["content"] = self._cap_content(str(t["content"]).strip())
                    if "status" in t and t["status"]:
                        status = str(t["status"]).strip().lower()
                        if status in VALID_STATUSES:
                            existing[item_id]["status"] = status
                else:
                    # New item -- validate fully and append to end
                    validated = self._validate(t)
                    existing[validated["id"]] = validated
                    self._items.append(validated)
            # Rebuild _items preserving order for existing items
            seen = set()
            rebuilt = []
            for item in self._items:
                current = existing.get(item["id"], item)
                if current["id"] not in seen:
                    rebuilt.append(current)
                    seen.add(current["id"])
            self._items = self._normalize_order(rebuilt)
        # Bound total item count so a replayed/oversized list can't grow the
        # re-injection block without limit. Keep the highest-priority head
        # (list order is priority).
        if len(self._items) > MAX_TODO_ITEMS:
            self._items = self._items[:MAX_TODO_ITEMS]
        self._sync_markdown()
        return self.read()

    def read(self) -> List[Dict[str, str]]:
        """Return a safe copy, refreshing file-backed state when changed."""
        self._refresh_from_markdown_if_changed()
        if self._markdown_path is None:
            return [item.copy() for item in self._items]
        return [self._sanitize_item_for_read(item) for item in self._items]

    @classmethod
    def _sanitize_item_for_read(cls, item: Dict[str, str]) -> Dict[str, str]:
        safe = item.copy()
        safe["content"] = cls._sanitize_for_injection(safe.get("content", ""))
        safe["id"] = cls._normalize_id(safe.get("id", "")) or "?"
        return safe

    def markdown_path(self) -> Optional[Path]:
        return self._markdown_path

    def has_items(self) -> bool:
        """Check if there are any items in the list."""
        return bool(self._items)

    def format_for_injection(self) -> Optional[str]:
        """
        Render the todo list for post-compression injection.

        Returns a human-readable string to append to the compressed
        message history, or None if the list is empty.
        """
        self._refresh_from_markdown_if_changed()
        if not self._items:
            return None
        markers = {
            "completed": "[x]",
            "in_progress": "[>]",
            "pending": "[ ]",
            "cancelled": "[~]",
        }

        # Only inject pending/in_progress items — completed/cancelled ones
        # cause the model to re-do finished work after compression.
        active_items = [
            item for item in self._items
            if item["status"] in {"pending", "in_progress"}
        ]
        if not active_items:
            return None

        lines = [TODO_INJECTION_HEADER]
        for item in active_items:
            marker = markers.get(item["status"], "[?]")
            content = self._sanitize_for_injection(item["content"])
            safe_id = self._normalize_id(item["id"]) or "?"
            lines.append(
                f"- {marker} {safe_id}. {content} ({item['status']})"
            )

        return "\n".join(lines)

    @staticmethod
    def _sanitize_for_injection(content: str) -> str:
        if not content:
            return content
        from tools.threat_patterns import scan_for_threats

        findings = scan_for_threats(content, scope="strict")
        if findings:
            return (
                "[BLOCKED: TODO.md task contained threat pattern(s): "
                f"{', '.join(findings)}. Removed from model context.]"
            )
        return content

    @staticmethod
    def _cap_content(content: str) -> str:
        """Truncate oversized todo content to MAX_TODO_CONTENT_CHARS.

        A single huge item would otherwise inflate the post-compression
        re-injection block (format_for_injection) without bound. Keep the
        head — the actionable part of a task description — plus a marker.
        """
        if len(content) > MAX_TODO_CONTENT_CHARS:
            keep = MAX_TODO_CONTENT_CHARS - len(_TRUNCATION_MARKER)
            return content[:keep] + _TRUNCATION_MARKER
        return content

    @staticmethod
    def _validate(item: Dict[str, Any]) -> Dict[str, str]:
        """
        Validate and normalize a todo item.

        Ensures required fields exist and status is valid.
        Returns a clean dict with only {id, content, status}.
        """
        if not isinstance(item, dict):
            return {"id": "?", "content": "(invalid item)", "status": "pending"}

        item_id = TodoStore._normalize_id(item.get("id", ""))
        if not item_id:
            item_id = "?"

        content = str(item.get("content", "")).strip()
        if not content:
            content = "(no description)"
        else:
            content = TodoStore._cap_content(content)

        status = str(item.get("status", "pending")).strip().lower()
        if status not in VALID_STATUSES:
            status = "pending"

        return {"id": item_id, "content": content, "status": status}

    @staticmethod
    def _dedupe_by_id(todos: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Collapse duplicate ids, keeping the last occurrence in its position."""
        last_index: Dict[str, int] = {}
        for i, item in enumerate(todos):
            if not isinstance(item, dict):
                # Non-dict items get a synthetic key so _validate can handle them
                last_index[f"__invalid_{i}"] = i
                continue
            item_id = TodoStore._normalize_id(item.get("id", "")) or "?"
            last_index[item_id] = i
        return [todos[i] for i in sorted(last_index.values())]

    @staticmethod
    def _normalize_id(item_id: Any) -> str:
        collapsed = re.sub(r"\s+", "_", str(item_id or "").strip())
        return re.sub(r"[^A-Za-z0-9_-]", "", collapsed)

    @staticmethod
    def _normalize_order(items: List[Dict[str, str]]) -> List[Dict[str, str]]:
        """Lift the active step ahead of any earlier unfinished placeholders."""
        active_index = next(
            (i for i, item in enumerate(items) if item["status"] == "in_progress"),
            None,
        )
        if active_index is None:
            return items

        pending_index = next(
            (
                i for i, item in enumerate(items[:active_index])
                if item["status"] == "pending"
            ),
            None,
        )
        if pending_index is None:
            return items

        normalized = items.copy()
        active_item = normalized.pop(active_index)
        normalized.insert(pending_index, active_item)
        return normalized

    @staticmethod
    def _content_key(content: str) -> str:
        return re.sub(r"\s+", " ", str(content or "")).strip().casefold()

    @classmethod
    def _parse_markdown_text(cls, text: str) -> List[Dict[str, str]]:
        items: List[Dict[str, str]] = []
        for line_no, line in enumerate(text.splitlines(), start=1):
            match = re.match(r"^\s*[-*]\s+\[([ xX])\]\s+(.+?)\s*$", line)
            if not match:
                continue
            raw = match.group(2)
            meta = re.search(
                r"<!--\s*hermes:id=(\S+)\s+status=(\S+)\s*-->\s*$", raw
            )
            content = _HERMES_META_COMMENT_RE.sub("", raw).strip()
            if not content:
                continue
            checked = match.group(1).lower() == "x"
            meta_status = meta.group(2) if meta else None
            if checked:
                status = (
                    meta_status
                    if meta_status in {"completed", "cancelled"}
                    else "completed"
                )
            else:
                status = (
                    meta_status
                    if meta_status in {"pending", "in_progress"}
                    else "pending"
                )
            item_id = cls._normalize_id(meta.group(1)) if meta else ""
            items.append(
                {
                    "id": item_id or f"todo-{line_no}",
                    "content": cls._cap_content(content),
                    "status": status,
                }
            )
        return cls._normalize_order(items[:MAX_TODO_ITEMS])

    def _markdown_mtime(self) -> Optional[int]:
        if self._markdown_path is None:
            return None
        try:
            return self._markdown_path.stat().st_mtime_ns
        except OSError:
            return None

    def _load_markdown_if_available(self) -> None:
        if self._markdown_path is None:
            return
        try:
            text = self._markdown_path.read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            return
        self._items = self._parse_markdown_text(text)
        self._markdown_mtime_ns = self._markdown_mtime()

    def _refresh_from_markdown_if_changed(self) -> bool:
        current_mtime = self._markdown_mtime()
        if current_mtime is None or current_mtime == self._markdown_mtime_ns:
            return False
        self._load_markdown_if_available()
        return True

    def _merge_external_open_items(
        self, on_disk: List[Dict[str, str]]
    ) -> None:
        seen_ids = {item["id"] for item in self._items}
        seen_content = {
            self._content_key(item["content"]) for item in self._items
        }
        for item in on_disk:
            if item["status"] not in {"pending", "in_progress"}:
                continue
            content_key = self._content_key(item["content"])
            if item["id"] in seen_ids or content_key in seen_content:
                continue
            self._items.append(item.copy())
            seen_ids.add(item["id"])
            seen_content.add(content_key)
        self._items = self._normalize_order(self._items[:MAX_TODO_ITEMS])

    @staticmethod
    def _strip_meta_comments(content: str) -> str:
        return _HERMES_META_COMMENT_RE.sub("", str(content or "")).strip()

    def _render_markdown(self) -> str:
        lines = [
            "# Agent TODO",
            "",
            "<!-- Managed by Hermes todo tool. -->",
            "",
        ]
        for item in self._items:
            marker = "x" if item["status"] in {"completed", "cancelled"} else " "
            content = self._strip_meta_comments(
                item["content"].replace("\n", " ")
            )
            meta = f"<!-- hermes:id={item['id']} status={item['status']} -->"
            lines.append(f"- [{marker}] {content} {meta}")
        return "\n".join(lines).rstrip() + "\n"

    def _sync_markdown(self) -> None:
        if self._markdown_path is None:
            return
        try:
            self._markdown_path.parent.mkdir(parents=True, exist_ok=True)
            lock_path = self._markdown_path.with_name(
                self._markdown_path.name + ".lock"
            )
            lock_fd = None
            try:
                if fcntl is not None:
                    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
                    fcntl.flock(lock_fd, fcntl.LOCK_EX)
                try:
                    current_text = self._markdown_path.read_text(
                        encoding="utf-8", errors="replace"
                    )
                except OSError:
                    current_text = ""
                self._merge_external_open_items(
                    self._parse_markdown_text(current_text)
                )
                self._atomic_write_text(
                    self._markdown_path, self._render_markdown()
                )
            finally:
                if lock_fd is not None:
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    finally:
                        os.close(lock_fd)
            self._markdown_mtime_ns = self._markdown_mtime()
        except Exception:
            # Disk sync is supplementary and must not break the todo tool.
            return

    @staticmethod
    def _atomic_write_text(path: Path, text: str) -> None:
        fd, tmp_path = tempfile.mkstemp(
            dir=str(path.parent), prefix=".todo_", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            atomic_replace(tmp_path, path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise


def todo_tool(
    todos: Optional[List[Dict[str, Any]]] = None,
    merge: bool = False,
    store: Optional[TodoStore] = None,
) -> str:
    """
    Single entry point for the todo tool. Reads or writes depending on params.

    Args:
        todos: if provided, write these items. If None, read current list.
        merge: if True, update by id. If False (default), replace entire list.
        store: the TodoStore instance from the AIAgent.

    Returns:
        JSON string with the full current list and summary metadata.
    """
    if store is None:
        return tool_error("TodoStore not initialized")

    if todos is not None:
        # Guard: LLM sometimes sends todos as a JSON string instead of a list
        if isinstance(todos, str):
            try:
                todos = json.loads(todos)
            except (json.JSONDecodeError, TypeError):
                return tool_error("todos must be a list of objects, got unparseable string")
        if not isinstance(todos, list):
            return tool_error(
                f"todos must be a list, got {type(todos).__name__}"
            )
        items = store.write(todos, merge)
    else:
        items = store.read()

    # Build summary counts
    pending = sum(1 for i in items if i["status"] == "pending")
    in_progress = sum(1 for i in items if i["status"] == "in_progress")
    completed = sum(1 for i in items if i["status"] == "completed")
    cancelled = sum(1 for i in items if i["status"] == "cancelled")

    return json.dumps({
        "todos": items,
        "summary": {
            "total": len(items),
            "pending": pending,
            "in_progress": in_progress,
            "completed": completed,
            "cancelled": cancelled,
        },
    }, ensure_ascii=False)


def check_todo_requirements() -> bool:
    """Todo tool has no external requirements -- always available."""
    return True


# =============================================================================
# OpenAI Function-Calling Schema
# =============================================================================
# Behavioral guidance is baked into the description so it's part of the
# static tool schema (cached, never changes mid-conversation).

TODO_SCHEMA = {
    "name": "todo",
    "description": (
        "Manage your task list for the current session. Use for complex tasks "
        "with 3+ steps or when the user provides multiple tasks. "
        "For 'all N items' tasks, enumerate every instance as its own checklist "
        "item so none are silently dropped. "
        "Call with no parameters to read the current list.\n\n"
        "Writing:\n"
        "- Provide 'todos' array to create/update items\n"
        "- merge=false (default): replace the entire list with a fresh plan\n"
        "- merge=true: update existing items by id, add any new ones\n\n"
        "Each item: {id: string, content: string, "
        "status: pending|in_progress|completed|cancelled}\n"
        "List order is priority. Only ONE item in_progress at a time.\n"
        "Mark an item completed only after the work is verified done, never "
        "based on intent. If something fails, "
        "cancel it and add a revised item.\n\n"
        "Always returns the full current list."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "description": "Task items to write. Omit to read current list.",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": "Unique item identifier"
                        },
                        "content": {
                            "type": "string",
                            "description": "Task description"
                        },
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed", "cancelled"],
                            "description": "Current status"
                        }
                    },
                    "required": ["id", "content", "status"]
                }
            },
            "merge": {
                "type": "boolean",
                "description": (
                    "true: update existing items by id, add new ones. "
                    "false (default): replace the entire list."
                ),
                "default": False
            }
        },
        "required": []
    }
}


# --- Registry ---
from tools.registry import registry, tool_error

registry.register(
    name="todo",
    toolset="todo",
    schema=TODO_SCHEMA,
    handler=lambda args, **kw: todo_tool(
        todos=args.get("todos"), merge=args.get("merge", False), store=kw.get("store")),
    check_fn=check_todo_requirements,
    emoji="📋",
)
