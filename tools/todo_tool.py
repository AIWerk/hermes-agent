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
import tempfile
from pathlib import Path
from typing import Dict, Any, List, Optional

from utils import atomic_replace

# fcntl is Unix-only. Hermes runs on Linux, but guard the import so the module
# still loads on Windows (sync just falls back to a non-locked atomic write).
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

# The Hermes metadata comment ``_sync_markdown`` appends to persist a task's
# stable id + precise status. Shared by the persist-side strip (so an embedded
# metadata-shaped comment can't hijack recovery) and the load-side visible-text
# extraction (so the trailing metadata is removed while legitimate ``<!-- ... -->``
# comments in a task description survive the round-trip). ReDoS-safe: the
# ``\S+`` runs are delimited by the literal ``status=`` and ``-->`` tokens — no
# nested unbounded quantifiers.
_HERMES_META_COMMENT_RE = re.compile(r"<!--\s*hermes:id=\S+\s+status=\S+\s*-->")


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
        # CUI/manual Markdown tasks must survive replace-mode agent writes. Older
        # CUI/manual checkboxes have no Hermes metadata and hydrate as synthetic
        # todo-<line> ids; newer CUI tasks carry stable cui-* metadata ids. If an
        # agent starts after a customer added one, there is no "external_changed"
        # edge left to see: the item is already loaded into this store. Preserve
        # those open items so a fresh agent plan cannot silently erase the user's
        # right-rail task.
        loaded_unmanaged_items = [] if external_changed else [
            item.copy()
            for item in self._items
            if self._is_unmanaged_markdown_item(item)
        ]

        if not merge:
            # Replace mode: new list entirely. If TODO.md was edited by the
            # CUI since this store last synced, keep external-only tasks so a
            # stale agent plan cannot clobber freshly added right-rail tasks.
            self._items = [self._validate(t) for t in self._dedupe_by_id(todos)]
            preserved_items = external_items or loaded_unmanaged_items
            if preserved_items:
                seen_ids = {item["id"] for item in self._items}
                seen_content = {self._content_key(item["content"]) for item in self._items}
                for item in preserved_items:
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
        """Return a copy of the current list.

        When TODO.md sync is enabled, the items are file-backed and an
        authenticated CUI customer (via /api/assistant/todos/add) can append
        arbitrary text into the file. ``todo_tool`` serializes this list
        straight back to the model as a tool result far more often than a
        compression event fires, so the same threat scan that guards
        ``format_for_injection`` must also run here — otherwise the documented
        trust boundary is bypassed on the primary read path. Items the agent
        authored in-session (no markdown backing) are not customer-controlled,
        so the scan is gated on markdown sync to avoid neutralizing the agent's
        own legitimate plan text.
        """
        self._refresh_from_markdown_if_changed()
        if not self._markdown_path:
            return [item.copy() for item in self._items]
        return [self._sanitize_item_for_read(item) for item in self._items]

    @classmethod
    def _sanitize_item_for_read(cls, item: Dict[str, str]) -> Dict[str, str]:
        """Return a copy of a file-backed item safe to hand back to the model.

        Both ``content`` and ``id`` are threat-scanned with the same
        ``scope="strict"`` scanner the compression path uses. ``id`` is also
        re-normalized to the ``[A-Za-z0-9_-]`` whitelist so an id smuggled
        through a hand-edited / externally-written TODO.md (bypassing
        ``_normalize_id`` on the write path) cannot carry invisible/bidi
        unicode or structural payloads into the tool result.
        """
        safe = item.copy()
        safe["content"] = cls._sanitize_for_injection(safe.get("content", ""))
        safe["id"] = cls._normalize_id(safe.get("id", "")) or "?"
        return safe

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
            # The id is emitted verbatim into the prompt, so whitelist it to the
            # safe charset before injection: a payload smuggled through the id
            # field (invisible unicode, ``evil].(SYSTEM:…)``) would otherwise
            # ride into the prompt unscanned (#27 id-bypass).
            safe_id = self._normalize_id(item["id"]) or "?"
            lines.append(f"- {marker} {safe_id}. {content} ({item['status']})")

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
    def _content_key(content: str) -> str:
        """Normalize task text for duplicate detection across file/tool ids."""
        return re.sub(r"\s+", " ", str(content or "")).strip().casefold()

    @staticmethod
    def _is_unmanaged_markdown_item(item: Dict[str, str]) -> bool:
        """Return True for open CUI/manual Markdown tasks to preserve."""
        item_id = str(item.get("id") or "")
        status = str(item.get("status") or "")
        if status not in {"pending", "in_progress"}:
            return False
        return bool(re.fullmatch(r"todo-\d+", item_id) or item_id.startswith("cui-"))

    @staticmethod
    def _normalize_id(item_id: str) -> str:
        """Normalize an id to a safe single ``[A-Za-z0-9_-]`` token.

        The id is persisted into the ``hermes:id=<id> status=<status>``
        metadata comment and recovered with ``(\\S+)``; internal whitespace
        would truncate recovery at the first space and silently mint a
        synthetic ``todo-N`` id (re-introducing duplicate tasks + losing the
        precise status). Whitespace collapses to ``_`` so the round-trip stays
        a single ``\\S+`` token.

        The id is also emitted verbatim into the post-compression injection
        block (``format_for_injection``), so it is an injection surface in its
        own right: ``(\\S+)`` matches ANY non-whitespace, including invisible /
        bidi unicode (U+200B, U+202E, …) and structural payloads
        (``evil].(SYSTEM:…)``) that the content scanner is meant to catch.
        We therefore whitelist the id to an ASCII-safe charset, dropping every
        other character (non-ASCII, invisible, bidi, punctuation), so neither a
        CUI customer nor a stale TODO.md can smuggle an unscanned payload
        through the id. An id that reduces to empty falls back to ``?`` /
        ``todo-N`` upstream, exactly as for a missing id.
        """
        collapsed = re.sub(r"\s+", "_", str(item_id or "").strip())
        return re.sub(r"[^A-Za-z0-9_-]", "", collapsed)

    @staticmethod
    def _strip_meta_comments(content: str) -> str:
        """Strip only Hermes *metadata* comments from task content.

        Content is written to TODO.md as ``- [m] {content} <!-- hermes:id=... -->``.
        An embedded ``<!-- hermes:id=... status=... -->`` comment in ``content``
        (whether agent- or CUI-authored) could otherwise be mistaken for the
        trailing metadata on recovery (#27 bypass), so it must not survive into
        the persisted line. But arbitrary ``<!-- ... -->`` comments in a task
        description are legitimate content (e.g. ``Document the
        <!-- TODO: later --> section``) and deleting them silently corrupts the
        task across the round-trip. We therefore strip ONLY the metadata-shaped
        comment and leave every other comment untouched; recovery stays
        unambiguous because the only hermes metadata comment left on the line is
        the trailing one ``_sync_markdown`` appends.
        """
        return _HERMES_META_COMMENT_RE.sub("", str(content or "")).strip()

    def _markdown_mtime(self) -> Optional[int]:
        if not self._markdown_path or not self._markdown_path.exists():
            return None
        try:
            return self._markdown_path.stat().st_mtime_ns
        except Exception:
            return None

    @classmethod
    def _parse_markdown_text(cls, text: str) -> List[Dict[str, str]]:
        """Parse TODO.md text into todo items (id, content, status).

        Pure function of the file text so both the in-memory hydrate and the
        under-lock re-read in ``_sync_markdown`` share one parser.
        """
        items: List[Dict[str, str]] = []
        for line_no, line in enumerate(text.splitlines(), start=1):
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
            # Strip ONLY hermes metadata comments from the visible text. A
            # legitimate ``<!-- ... -->`` in the task description (e.g.
            # ``Document the <!-- TODO: later --> section``) is real content and
            # must survive the round-trip; deleting every comment corrupted it
            # silently. Recovery above already takes the end-anchored metadata,
            # so a non-metadata comment can't hijack id/status.
            content = _HERMES_META_COMMENT_RE.sub("", raw).strip()
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
            # Normalize the recovered id too: the metadata comment is written by
            # whoever last touched TODO.md (agent, CUI, or a hand edit), and the
            # ``(\S+)`` capture matches invisible / bidi unicode and structural
            # payloads. Without this an attacker bypasses the id whitelist by
            # smuggling the payload through the persisted ``hermes:id=...`` field
            # rather than through a tool write. Fall back to the synthetic
            # ``todo-N`` id if the recovered id reduces to empty.
            item_id = cls._normalize_id(meta.group(1)) if meta else ""
            if not item_id:
                item_id = f"todo-{line_no}"
            items.append({
                "id": item_id,
                "content": cls._cap_content(content),
                "status": status,
            })
        return items[:MAX_TODO_ITEMS]

    def _load_markdown_if_available(self) -> None:
        """Hydrate the in-memory list from TODO.md without failing agent startup."""
        if not self._markdown_path or not self._markdown_path.exists():
            return
        try:
            text = self._markdown_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return
        self._items = self._parse_markdown_text(text)
        self._markdown_mtime_ns = self._markdown_mtime()

    def _refresh_from_markdown_if_changed(self) -> bool:
        """Reload TODO.md when CUI or another process changed it externally."""
        current_mtime = self._markdown_mtime()
        if current_mtime is None or current_mtime == self._markdown_mtime_ns:
            return False
        self._load_markdown_if_available()
        return True

    def _merge_external_open_items(self, on_disk: List[Dict[str, str]]) -> None:
        """Fold any open on-disk task not already in memory into ``self._items``.

        Called under the sync lock with the file as it is *right now*, so a CUI
        append (``/api/assistant/todos/add``) that landed in the window between
        this store's last refresh and this write is merged instead of clobbered
        by the truncating rewrite. Only open (pending/in_progress) items are
        preserved — a completed/cancelled item the agent dropped should stay
        dropped — and dedup uses the same id + normalized-content keys as the
        replace-mode preservation in ``write()``.
        """
        seen_ids = {item["id"] for item in self._items}
        seen_content = {self._content_key(item["content"]) for item in self._items}
        for item in on_disk:
            if item.get("status") not in {"pending", "in_progress"}:
                continue
            content_key = self._content_key(item["content"])
            if item["id"] in seen_ids or content_key in seen_content:
                continue
            self._items.append(item.copy())
            seen_ids.add(item["id"])
            seen_content.add(content_key)
        if len(self._items) > MAX_TODO_ITEMS:
            self._items = self._items[:MAX_TODO_ITEMS]

    def _render_markdown(self) -> str:
        """Render the current list to TODO.md text."""
        lines = [
            "# Agent TODO",
            "",
            "<!-- Managed by Hermes todo tool. The CUI Aufgaben panel reads open Markdown checkboxes from this file. -->",
            "",
        ]
        for item in self._items:
            marker = "x" if item["status"] in {"completed", "cancelled"} else " "
            # Strip any *metadata-shaped* comment embedded in the content so the
            # ONLY hermes metadata comment on the persisted line is the trailing
            # one — otherwise an embedded "<!-- hermes:id=... -->" could hijack
            # recovery. Legitimate non-metadata comments are preserved.
            content = self._strip_meta_comments(item["content"].replace("\n", " "))
            # Persist id + precise status in an HTML comment (invisible in the
            # rendered CUI panel, stripped from the loaded text) so the
            # round-trip preserves agent ids and all four statuses. The id is
            # already whitelist-normalized in _validate, so it stays a single
            # \S+ token that recovery can parse.
            meta = f"<!-- hermes:id={item['id']} status={item['status']} -->"
            lines.append(f"- [{marker}] {content} {meta}")
        return "\n".join(lines).rstrip() + "\n"

    def _sync_markdown(self) -> None:
        """Persist the current tool list to TODO.md for the CUI Aufgaben panel.

        The design has two concurrent writers — this agent and the CUI
        ``/api/assistant/todos/add`` endpoint. To avoid last-writer-wins data
        loss and partial reads we (1) take an exclusive advisory lock on a
        sibling ``.lock`` file, (2) re-read the file under the lock and merge any
        open task that appeared since our last refresh, then (3) write
        atomically via a temp file + ``os.replace`` so a concurrent CUI reader
        always sees a whole file, never a truncated one.
        """
        if not self._markdown_path:
            return
        try:
            self._markdown_path.parent.mkdir(parents=True, exist_ok=True)
            lock_path = self._markdown_path.with_name(self._markdown_path.name + ".lock")
            lock_fd = None
            try:
                if fcntl is not None:
                    lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
                    fcntl.flock(lock_fd, fcntl.LOCK_EX)
                # Under the lock, fold in any concurrent CUI append that landed
                # between our last refresh and now so the truncating rewrite
                # below does not clobber it.
                if self._markdown_path.exists():
                    try:
                        current_text = self._markdown_path.read_text(
                            encoding="utf-8", errors="replace"
                        )
                        self._merge_external_open_items(
                            self._parse_markdown_text(current_text)
                        )
                    except Exception:
                        pass
                self._atomic_write_text(self._markdown_path, self._render_markdown())
            finally:
                if lock_fd is not None:
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    finally:
                        os.close(lock_fd)
            self._markdown_mtime_ns = self._markdown_mtime()
        except Exception:
            # Todo is a planning aid; disk sync must never break tool execution.
            return

    @staticmethod
    def _atomic_write_text(path: Path, text: str) -> None:
        """Write ``text`` to ``path`` atomically (temp file + os.replace).

        A plain ``write_text`` truncates the target first, so a concurrent CUI
        reader can observe an empty/partial file. Writing to a sibling temp file
        and renaming it into place means readers always see the old complete
        file or the new one, never a torn write.
        """
        fd, tmp_path = tempfile.mkstemp(
            dir=str(path.parent), prefix=".todo_", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
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
