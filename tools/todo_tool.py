#!/usr/bin/env python3
"""
Todo Tool Module - Planning & Task Management

Provides a task list the agent uses to decompose complex tasks,
track progress, and maintain focus across long conversations. The live state
lives on the AIAgent instance (one per session) and can sync to TODO.md so
CUI Aufgaben shows the same active agent work. It is also re-injected into
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
from pathlib import Path
from typing import Dict, Any, List, Optional


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
_TRUNCATION_MARKER = "… [truncated]"


def default_todo_markdown_path() -> Path:
    """Return the shared TODO.md path used by the agent and CUI Aufgaben panel."""
    raw = os.environ.get("AIWERK_CUI_TODO_PATH") or os.environ.get("HERMES_TODO_PATH")
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
        self._markdown_path = Path(markdown_path).expanduser() if markdown_path else None
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
        external_changed = self._refresh_from_markdown_if_changed()
        external_items = [item.copy() for item in self._items] if external_changed else []

        if not merge:
            # Replace mode: new list entirely. If TODO.md was edited by the
            # CUI since this store last synced, keep external-only tasks so a
            # stale agent plan cannot clobber freshly added right-rail tasks.
            self._items = [self._validate(t) for t in self._dedupe_by_id(todos)]
            if external_items:
                seen_ids = {item["id"] for item in self._items}
                seen_content = {self._content_key(item["content"]) for item in self._items}
                for item in external_items:
                    content_key = self._content_key(item["content"])
                    if item["id"] not in seen_ids and content_key not in seen_content:
                        self._items.append(item.copy())
                        seen_ids.add(item["id"])
                        seen_content.add(content_key)
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
            self._items = rebuilt
        # Bound total item count so a replayed/oversized list can't grow the
        # re-injection block without limit. Keep the highest-priority head
        # (list order is priority), then sync the bounded list to TODO.md.
        if len(self._items) > MAX_TODO_ITEMS:
            self._items = self._items[:MAX_TODO_ITEMS]
        self._sync_markdown()
        return self.read()

    def read(self) -> List[Dict[str, str]]:
        """Return a copy of the current list."""
        self._refresh_from_markdown_if_changed()
        return [item.copy() for item in self._items]

    def markdown_path(self) -> Optional[Path]:
        """Return the backing TODO.md path, if markdown sync is enabled."""
        return self._markdown_path

    def has_items(self) -> bool:
        """Check if there are any items in the list."""
        self._refresh_from_markdown_if_changed()
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

        # Status markers for compact display
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

        lines = ["[Your active task list was preserved across context compression]"]
        for item in active_items:
            marker = markers.get(item["status"], "[?]")
            content = self._sanitize_for_injection(item["content"])
            lines.append(f"- {marker} {item['id']}. {content} ({item['status']})")

        return "\n".join(lines)

    @staticmethod
    def _sanitize_for_injection(content: str) -> str:
        """Threat-scan task content before it re-enters the prompt.

        The fork syncs TODO.md bidirectionally, and the customer-facing CUI
        endpoint (/api/assistant/todos/add) lets an authenticated customer
        append arbitrary text into TODO.md, which format_for_injection then
        re-reads and appends verbatim as a user message after compression.
        That is the same trust boundary the memory tool defends when a
        file-backed entry enters the system prompt, so we reuse the exact
        same scanner (tools.threat_patterns, "strict" scope) and the same
        placeholder pattern as MemoryStore._sanitize_entries_for_snapshot.
        Clean task text flows through unchanged.
        """
        if not content:
            return content
        from tools.threat_patterns import scan_for_threats

        findings = scan_for_threats(content, scope="strict")
        if findings:
            return (
                f"[BLOCKED: TODO.md task contained threat pattern(s): "
                f"{', '.join(findings)}. Removed from compression injection.]"
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
            item_id = TodoStore._normalize_id(item.get("id", "")) or "?"
            last_index[item_id] = i
        return [todos[i] for i in sorted(last_index.values())]

    @staticmethod
    def _content_key(content: str) -> str:
        """Normalize task text for duplicate detection across file/tool ids."""
        return re.sub(r"\s+", " ", str(content or "")).strip().casefold()

    @staticmethod
    def _normalize_id(item_id: str) -> str:
        """Collapse any whitespace in an id to a single token.

        The id is persisted into the ``hermes:id=<id> status=<status>``
        metadata comment and recovered with ``(\\S+)``; internal whitespace
        would truncate recovery at the first space and silently mint a
        synthetic ``todo-N`` id (re-introducing duplicate tasks + losing the
        precise status). Replacing whitespace with ``_`` keeps the id a
        single ``\\S+`` token so the round-trip stays lossless.
        """
        return re.sub(r"\s+", "_", str(item_id or "").strip())

    @staticmethod
    def _strip_meta_comments(content: str) -> str:
        """Remove any ``<!-- ... -->`` comments embedded in task content.

        Content is written to TODO.md as ``- [m] {content} <!-- hermes:id=... -->``.
        An embedded comment in ``content`` (whether agent- or CUI-authored)
        could otherwise be mistaken for the trailing metadata on recovery
        (#27 bypass). Stripping it before persisting guarantees the only
        comment on the line is the one ``_sync_markdown`` appends.
        """
        return re.sub(r"<!--.*?-->", "", str(content or "")).strip()

    def _markdown_mtime(self) -> Optional[int]:
        if not self._markdown_path or not self._markdown_path.exists():
            return None
        try:
            return self._markdown_path.stat().st_mtime_ns
        except Exception:
            return None

    def _load_markdown_if_available(self) -> None:
        """Hydrate the in-memory list from TODO.md without failing agent startup."""
        if not self._markdown_path or not self._markdown_path.exists():
            return
        try:
            lines = self._markdown_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            return
        items: List[Dict[str, str]] = []
        for line_no, line in enumerate(lines, start=1):
            match = re.match(r"^\s*[-*]\s+\[([ xX])\]\s+(.+?)\s*$", line)
            if not match:
                continue
            raw = match.group(2)
            # Recover the stable id + precise status persisted by _sync_markdown
            # (so merge-by-id keeps working and cancelled/in_progress survive the
            # round-trip) before the comment is stripped from the visible text.
            # Anchor the metadata comment to the END of the line so a comment
            # embedded earlier in the visible content (e.g. a spoofed
            # "<!-- hermes:id=spoof status=... -->" pasted into a task) can't
            # win the recovery over the trailing one _sync_markdown appends
            # (#27 bypass). _sync_markdown also strips embedded comments before
            # persisting, but recovery must stay safe for files written by
            # other tools / older code paths too.
            meta = re.search(r"<!--\s*hermes:id=(\S+)\s+status=(\S+)\s*-->\s*$", raw)
            content = re.sub(r"<!--.*?-->", "", raw).strip()
            if not content:
                continue
            checked = match.group(1).lower() == "x"
            meta_status = meta.group(2) if meta else None
            # The checkbox is authoritative for the done/not-done axis (so a CUI
            # user toggling it is honoured); the comment disambiguates within each
            # state (completed vs cancelled, pending vs in_progress).
            if checked:
                status = meta_status if meta_status in {"completed", "cancelled"} else "completed"
            else:
                status = meta_status if meta_status in {"pending", "in_progress"} else "pending"
            item_id = meta.group(1) if meta else f"todo-{line_no}"
            items.append({
                "id": item_id,
                "content": self._cap_content(content),
                "status": status,
            })
        self._items = items[:MAX_TODO_ITEMS]
        self._markdown_mtime_ns = self._markdown_mtime()

    def _refresh_from_markdown_if_changed(self) -> bool:
        """Reload TODO.md when CUI or another process changed it externally."""
        current_mtime = self._markdown_mtime()
        if current_mtime is None or current_mtime == self._markdown_mtime_ns:
            return False
        self._load_markdown_if_available()
        return True

    def _sync_markdown(self) -> None:
        """Persist the current tool list to TODO.md for the CUI Aufgaben panel."""
        if not self._markdown_path:
            return
        try:
            self._markdown_path.parent.mkdir(parents=True, exist_ok=True)
            lines = [
                "# Agent TODO",
                "",
                "<!-- Managed by Hermes todo tool. The CUI Aufgaben panel reads open Markdown checkboxes from this file. -->",
                "",
            ]
            for item in self._items:
                marker = "x" if item["status"] in {"completed", "cancelled"} else " "
                # Strip any comment embedded in the content so the ONLY comment
                # on the persisted line is the trailing metadata one — otherwise
                # an embedded "<!-- hermes:id=... -->" could hijack recovery.
                content = self._strip_meta_comments(item["content"].replace("\n", " "))
                # Persist id + precise status in an HTML comment (invisible in the
                # rendered CUI panel, stripped from the loaded text) so the
                # round-trip preserves agent ids and all four statuses. The id is
                # already whitespace-normalized in _validate, so it stays a single
                # \S+ token that recovery can parse.
                meta = f"<!-- hermes:id={item['id']} status={item['status']} -->"
                lines.append(f"- [{marker}] {content} {meta}")
            self._markdown_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
            self._markdown_mtime_ns = self._markdown_mtime()
        except Exception:
            # Todo is a planning aid; disk sync must never break tool execution.
            return


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
        "Manage your shared task list for the current session. When the agent is "
        "initialized with a TODO.md path, writes are also synced to that Markdown "
        "file so the CUI Aufgaben panel can show the same active tasks. Use for "
        "complex tasks with 3+ steps or when the user provides multiple tasks. "
        "Call with no parameters to read the current list.\n\n"
        "Writing:\n"
        "- Provide 'todos' array to create/update items\n"
        "- merge=false (default): replace the entire list with a fresh plan\n"
        "- merge=true: update existing items by id, add any new ones\n\n"
        "Each item: {id: string, content: string, "
        "status: pending|in_progress|completed|cancelled}\n"
        "List order is priority. Only ONE item in_progress at a time.\n"
        "Mark items completed immediately when done. If something fails, "
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
