"""Dedicated, read-only session broker. Never constructs an employee runtime."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path

from agent.tool_argument_projection import sanitize_tool_display_value
from hermes_cli.dashboard_auth import audit, profile_access
from hermes_cli.dashboard_auth.profile_access import ProfileAccessDenied
from hermes_cli.dashboard_auth.session_ownership import has_exact_cui_session_owner


class AdminReadUnavailable(Exception):
    """No safe public session data is available."""


@contextmanager
def _open_readonly(path):
    connection = None
    file_fd = None
    directory_fd = None
    try:
        path = Path(path).absolute()
        if any(part in {"", ".", ".."} for part in path.parts[1:]):
            raise AdminReadUnavailable("session data unavailable")
        required = ("O_CLOEXEC", "O_DIRECTORY", "O_NOFOLLOW")
        if any(not hasattr(os, name) for name in required):
            raise AdminReadUnavailable("session data unavailable")
        directory_fd = os.open(
            path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
        for component in path.parts[1:-1]:
            child_fd = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            os.close(directory_fd)
            directory_fd = child_fd
        file_fd = os.open(
            path.name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=directory_fd)
        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            raise AdminReadUnavailable("session data unavailable")
        # SQLite canonicalizes this retained capability to the opened inode, while still
        # discovering a live WAL beside it. Path replacement cannot retarget the connection.
        proc_path = f"/proc/self/fd/{file_fd}"
        if not os.path.exists(proc_path):
            raise AdminReadUnavailable("session data unavailable")
        connection = sqlite3.connect(f"file:{proc_path}?mode=ro", uri=True, timeout=2)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        # Bound hostile/corrupt SQL work without leaking SQL or storage paths.
        budget = [0]
        def progress():
            budget[0] += 1
            return budget[0] > 10000
        connection.set_progress_handler(progress, 1000)
        connection.execute("BEGIN")
        yield connection
    except (OSError, sqlite3.Error, TypeError, ValueError):
        raise AdminReadUnavailable("session data unavailable") from None
    finally:
        if connection is not None:
            connection.close()
        if file_fd is not None:
            os.close(file_fd)
        if directory_fd is not None:
            os.close(directory_fd)


def _public(value):
    value = sanitize_tool_display_value(value)
    if isinstance(value, str):
        # Preserve complete public URLs while removing local absolute paths.
        spans = re.split(r"(\b(?:https?://|shared://)[^\s<>\"']+)", value)
        return "".join(part if i % 2 else re.sub(
            r"(?<![\w:/])(?:/[\w.~/\\-]+|[A-Za-z]:[\\/][^\s]+)",
            "[REDACTED_PATH]", part) for i, part in enumerate(spans))
    if isinstance(value, dict):
        return {key: _public(item) for key, item in value.items()
                if key not in {"model_config", "system_prompt", "cwd", "host_path", "headers"}}
    if isinstance(value, list):
        return [_public(item) for item in value]
    return value


def _owner(row, decision):
    if row["profile_name"] != decision.target_profile:
        return None
    try:
        config = json.loads(row["model_config"] or "null")
        if not isinstance(config, dict):
            return None
        actor = config.get("_cui_actor_context")
        if not isinstance(actor, dict):
            actor = {"actor_id": config.get("_cui_actor_id"),
                     "tenant_id": config.get("_cui_tenant_id"),
                     "role": config.get("_cui_actor_role")}
        tenant, actor_id = actor.get("tenant_id"), actor.get("actor_id")
        if type(tenant) is not str or type(actor_id) is not str:
            return None
        if (tenant != decision.tenant_id
                or decision._policy._actors.get((tenant, actor_id)) != decision.target_profile
                or not has_exact_cui_session_owner(config, actor)):
            return None
        return tenant, actor_id
    except (ValueError, TypeError):
        return None


_COLUMNS = "id,source,title,started_at,ended_at,message_count,profile_name,model_config,parent_session_id"
_PUBLIC_COLUMNS = ("id", "source", "title", "started_at", "ended_at", "message_count")


def _visible(connection, row, decision):
    owner = _owner(row, decision)
    if owner is None:
        return False
    seen = {row["id"]}
    parent = row["parent_session_id"]
    for _ in range(100):
        if not parent:
            return True
        if parent in seen:
            return False
        seen.add(parent)
        ancestor = connection.execute(
            f"SELECT {_COLUMNS} FROM sessions WHERE id=?", (parent,)).fetchone()
        if ancestor is None or _owner(ancestor, decision) != owner:
            return False
        parent = ancestor["parent_session_id"]
    return False


def _query(connection, decision, operation, session_id, query, limit, offset):
    if operation == "read":
        row = connection.execute(
            f"SELECT {_COLUMNS} FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row is None or not _visible(connection, row, decision):
            raise AdminReadUnavailable("session data unavailable")
        messages = connection.execute(
            "SELECT id,role,content,timestamp FROM messages WHERE session_id=? ORDER BY id LIMIT ? OFFSET ?",
            (session_id, limit + 1, offset)).fetchall()
        has_more = len(messages) > limit
        messages = messages[:limit]
        return {"sessions": [_public({key: row[key] for key in _PUBLIC_COLUMNS})],
                "messages": [_public(dict(message)) for message in messages],
                "has_more": has_more,
                "next_offset": offset + len(messages) if has_more else None}
    # Page raw candidates in bounded batches; ownership/lineage precede public offset.
    result, scanned, visible = [], 0, 0
    predicate = "profile_name=?"
    params = [decision.target_profile]
    if operation == "search":
        predicate += " AND (instr(coalesce(title,''),?)>0 OR EXISTS (SELECT 1 FROM messages m WHERE m.session_id=sessions.id AND instr(coalesce(m.content,''),?)>0))"
        params.extend([query, query])
    while True:
        rows = connection.execute(
            f"SELECT {_COLUMNS} FROM sessions WHERE {predicate} ORDER BY started_at DESC,id LIMIT ? OFFSET ?",
            (*params, 100, scanned)).fetchall()
        for row in rows:
            if not _visible(connection, row, decision):
                continue
            visible += 1
            if visible <= offset:
                continue
            projected = {key: row[key] for key in _PUBLIC_COLUMNS}
            if operation == "search":
                hit = connection.execute(
                    "SELECT substr(content,1,1000) FROM messages WHERE session_id=? AND instr(coalesce(content,''),?)>0 ORDER BY id LIMIT 1",
                    (row["id"], query)).fetchone()
                projected["snippet"] = hit[0] if hit else row["title"]
            result.append(_public(projected))
            if len(result) == limit:
                return {"sessions": result}
        if len(rows) < 100:
            return {"sessions": result}
        scanned += len(rows)


def read_sessions(actor, target, operation, *, session_id=None, query=None, limit=50, offset=0):
    decision = None
    action = {"list": "session.list", "search": "session.search", "read": "session.read"}.get(
        operation if type(operation) is str else "")
    result, count = "denied", 0
    try:
        decision = profile_access.authorize(actor, action, target, delegated=True)
        if (decision is None or type(limit) is not int or not 1 <= limit <= 100
                or type(offset) is not int or not 0 <= offset <= 1000000
                or (operation == "read" and (type(session_id) is not str or
                    not session_id or len(session_id) > 256 or session_id != session_id.strip()))
                or (operation == "search" and (type(query) is not str or not query or len(query) > 4096))):
            raise ProfileAccessDenied("profile access denied")
        from hermes_cli.profiles import get_profile_dir
        home = get_profile_dir(decision.target_profile)
        with _open_readonly(home / "state.db") as connection:
            payload = _query(connection, decision, operation, session_id, query, limit, offset)
        result, count = "allowed", len(payload["sessions"])
        return payload
    finally:
        audit.delegated_session_read(actor, target, action, decision, result, count,
                                     correlation_id=uuid.uuid4().hex)
