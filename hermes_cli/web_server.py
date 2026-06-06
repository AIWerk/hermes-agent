"""
Hermes Agent — Web UI server.

Provides a FastAPI backend serving the Vite/React frontend and REST API
endpoints for managing configuration, environment variables, and sessions.

Usage:
    python -m hermes_cli.main web          # Start on http://127.0.0.1:9119
    python -m hermes_cli.main web --port 8080
"""

from contextlib import asynccontextmanager

import asyncio
import base64
import binascii
import copy
from dataclasses import dataclass
import email.header
import email.utils
import html
import hmac
from html.parser import HTMLParser
import http.cookiejar
import importlib.util
import json
import logging
import mimetypes
import ipaddress
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

import yaml

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from hermes_cli import __version__, __release_date__
from hermes_cli.config import (
    cfg_get,
    DEFAULT_CONFIG,
    OPTIONAL_ENV_VARS,
    get_config_path,
    get_env_path,
    get_hermes_home,
    load_config,
    load_env,
    save_config,
    save_env_value,
    remove_env_value,
    check_config_version,
    detect_install_method,
    format_docker_update_message,
    recommended_update_command_for_method,
    redact_key,
)
from gateway.status import get_running_pid, read_runtime_status
from utils import env_var_enabled

try:
    from agent.redact import redact_sensitive_text as _redact_sensitive_text
except Exception:
    def _redact_sensitive_text(value: Any) -> str:
        return str(value)

try:
    from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
    from fastapi.staticfiles import StaticFiles
    from pydantic import BaseModel
except ImportError:
    # First try lazy-installing the dashboard extras. Only the user actually
    # running `hermes dashboard` needs fastapi+uvicorn; lazy install keeps
    # them out of every other install path. After install, re-import.
    try:
        from tools.lazy_deps import ensure as _lazy_ensure
        _lazy_ensure("tool.dashboard", prompt=False)
        from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
        from fastapi.middleware.cors import CORSMiddleware
        from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
        from fastapi.staticfiles import StaticFiles
        from pydantic import BaseModel
    except Exception:
        raise SystemExit(
            "Web UI requires fastapi and uvicorn.\n"
            f"Install with: {sys.executable} -m pip install 'fastapi' 'uvicorn[standard]'"
        )

WEB_DIST = Path(os.environ["HERMES_WEB_DIST"]) if "HERMES_WEB_DIST" in os.environ else Path(__file__).parent / "web_dist"
_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Per-channel subscriber registry used by /api/pub (PTY-side gateway → dashboard)
# and /api/events (dashboard → browser sidebar).  Keyed by an opaque channel id
# the chat tab generates on mount; entries auto-evict when the last subscriber
# drops AND the publisher has disconnected.
#
# State lives on app.state (not module-level globals) so that asyncio.Lock is
# created on the running event loop during lifespan startup.  A module-level
# asyncio.Lock() binds to whatever loop was active at import time, which breaks
# when the same module is used across TestClient instances or uvicorn reloads.
# ---------------------------------------------------------------------------

@asynccontextmanager
async def _lifespan(app: "FastAPI"):
    app.state.event_channels = {}  # dict[str, set]
    app.state.event_lock = asyncio.Lock()
    yield


def _get_event_state(app: "FastAPI"):
    """Return (event_channels, event_lock) from app.state.

    Lazily initialises the state if the lifespan hasn't run (e.g. when
    TestClient is constructed without a ``with`` block).  The lifespan
    path is preferred because it guarantees the Lock is created on the
    correct event loop, but the lazy path lets existing non-``with``
    TestClient usages keep working.
    """
    try:
        return app.state.event_channels, app.state.event_lock
    except AttributeError:
        app.state.event_channels = {}
        app.state.event_lock = asyncio.Lock()
        return app.state.event_channels, app.state.event_lock


app = FastAPI(title="Hermes Agent", version=__version__, lifespan=_lifespan)

# ---------------------------------------------------------------------------
# Session token for protecting sensitive endpoints (reveal).
# The desktop shell mints the token and injects it via
# HERMES_DASHBOARD_SESSION_TOKEN so its main process can authenticate the
# /api calls it makes on the user's behalf; otherwise we generate one fresh
# on every server start. Either way it dies when the process exits and is
# injected into the SPA HTML so only the legitimate web UI can use it.
# ---------------------------------------------------------------------------
_SESSION_TOKEN = os.environ.get("HERMES_DASHBOARD_SESSION_TOKEN") or secrets.token_urlsafe(32)
_SESSION_HEADER_NAME = "X-Hermes-Session-Token"

# In-browser Chat tab (/chat, /api/pty, /api/ws, …).  Always enabled: the
# desktop app and the dashboard's own Chat tab both drive the agent over the
# `/api/ws` + `/api/pty` WebSockets, so the embedded-chat surface is an
# unconditional part of the dashboard.  Kept as a module-level constant (rather
# than inlining ``True`` at every gate) so the WS endpoints and the SPA token
# injection share a single, testable seam.
_DASHBOARD_EMBEDDED_CHAT_ENABLED = True

# Dashboard surface mode. ``admin`` is the full built-in dashboard. ``assistant``
# serves the simplified AIWerk assistant surface and blocks admin APIs server-side.
_DASHBOARD_MODE = "admin"

# Simple rate limiter for the reveal endpoint
_reveal_timestamps: List[float] = []
_REVEAL_MAX_PER_WINDOW = 5
_REVEAL_WINDOW_SECONDS = 30

# CORS: restrict to localhost origins only.  The web UI is intended to run
# locally; binding to 0.0.0.0 with allow_origins=["*"] would let any website
# read/modify config and secrets.

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Endpoints that do NOT require the session token.  Everything else under
# /api/ is gated by the auth middleware below.
#
# This list is defined in ``hermes_cli.dashboard_auth.public_paths`` so the
# OAuth gate middleware can honour the same allowlist — keeping the two
# gates in lockstep avoids drift like the wildcard-subdomain regression
# where ``/api/status`` was public under the legacy gate but 401'd under
# the OAuth gate (breaking the portal's liveness probe).
#
# Keep the upstream list minimal — only truly non-sensitive, read-only
# endpoints belong there.
# ---------------------------------------------------------------------------
from hermes_cli.dashboard_auth.public_paths import (
    PUBLIC_API_PATHS as _PUBLIC_API_PATHS,
)

_ASSISTANT_ALLOWED_API_EXACT: frozenset = frozenset({
    "/api/status",
    "/api/sessions",
    "/api/model/info",
    "/api/dashboard/themes",
    "/api/assistant/resources",
    "/api/assistant/support",
    "/api/assistant/todos/add",
    "/api/assistant/todos/update",
    "/api/cui/contacts/search",
    "/api/cui/context/contacts",
    "/api/cui/contacts/frequent",
    "/api/cui/contacts",
    "/api/cui/contacts/hide",
    "/api/assistant/email/view",
    "/api/assistant/calendar/view",
    "/api/assistant/shared-folder/open",
    "/api/assistant/shared-folder/open-folder",
    "/api/assistant/attachments",
    "/api/assistant/attachments/resource",
    "/api/assistant/transcribe",
    "/api/assistant/tts",
})
_ASSISTANT_ALLOWED_API_PREFIXES: tuple[str, ...] = (
    "/api/sessions/",
)

# Customer UI attachment upload limits. Files are stored under HERMES_HOME only,
# scoped by session id, and never under arbitrary user supplied paths.
_ASSISTANT_UPLOAD_MAX_FILES = 10
_ASSISTANT_UPLOAD_MAX_FILE_BYTES = 12 * 1024 * 1024
_ASSISTANT_UPLOAD_MAX_TOTAL_BYTES = 32 * 1024 * 1024
_ASSISTANT_TEXT_EXTRACT_LIMIT = 60_000
_ASSISTANT_UPLOAD_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp",
    ".pdf", ".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".docx",
})
_ASSISTANT_AUDIO_EXTENSIONS = frozenset({".mp3", ".mp4", ".mpeg", ".mpga", ".m4a", ".wav", ".webm", ".ogg", ".aac", ".flac"})
_ASSISTANT_AUDIO_MAX_BYTES = 25 * 1024 * 1024

# Files whose content can execute script in the dashboard origin if rendered
# (directly, or via a client-created blob: URL which inherits the page origin).
# The shared-folder open endpoint serves these as a non-renderable download.
_SHARED_FOLDER_ACTIVE_CONTENT_EXTENSIONS = frozenset({
    ".html", ".htm", ".xhtml", ".shtml", ".svg", ".svgz",
    ".xml", ".xsl", ".xslt", ".mhtml", ".mht", ".htc",
})


def _safe_shared_open_disposition(name: str, media_type: str) -> tuple[str, str]:
    """Return (media_type, content_disposition) for serving a shared file.

    Active-content types are forced to application/octet-stream + attachment so
    attacker-supplied markup placed in the shared folder cannot execute in the
    dashboard origin when opened. The frontend turns the response body into a
    blob: URL (which inherits the page origin), so the Content-Type — not the
    Content-Disposition — is what actually prevents script execution; we set
    both, plus X-Content-Type-Options: nosniff at the call site.
    """
    if Path(name).suffix.lower() in _SHARED_FOLDER_ACTIVE_CONTENT_EXTENSIONS:
        return "application/octet-stream", "attachment"
    return media_type, "inline"



_ASSISTANT_RESOURCE_MAX_SHARED_ITEMS = 40
_ASSISTANT_RESOURCE_MAX_SHARED_DEPTH = 5
_ASSISTANT_RESOURCE_DEFAULT_VISIBLE_ITEMS = 12
_ASSISTANT_RESOURCE_HIDDEN_NAMES = frozenset({
    ".env", ".env.local", ".envrc", "config.yaml", "auth.json", "credentials.json",
    "id_rsa", "id_ed25519", "known_hosts",
})
_ASSISTANT_EMAIL_PREVIEW_ITEMS = 5
_ASSISTANT_EMAIL_UNREAD_SCAN_LIMIT = 50
_ASSISTANT_CONTACT_PREVIEW_ITEMS = 20
_ASSISTANT_CONTACT_SEARCH_LIMIT = 20
_ASSISTANT_CONTACT_RELEVANCE_WINDOW_DAYS = 10
_ASSISTANT_CONTACT_SAVED_TOP_UP_TARGET = 16
_ASSISTANT_EMAIL_TIMEOUT_SECONDS = 12
_ASSISTANT_MCP_BRIDGE_TIMEOUT_SECONDS = 30


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _resource_status_label(status: str) -> str:
    return {
        "connected": "Verbunden",
        "limited": "Eingeschränkt",
        "auth_required": "Anmeldung nötig",
        "not_configured": "Nicht eingerichtet",
        "error": "Fehler",
    }.get(status, "Unbekannt")


def _safe_resource_id(value: str, fallback: str = "item") -> str:
    safe = re.sub(r"[^A-Za-z0-9._:-]+", "-", value or "").strip(".-_:")
    return (safe or fallback)[:120]


def _read_optional_json(path_value: str | None) -> dict[str, Any] | None:
    if not path_value:
        return None
    try:
        path = Path(path_value).expanduser().resolve()
        if not path.is_file() or path.stat().st_size > 256_000:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _resolve_shared_folder_root(config: dict[str, Any]) -> Path | None:
    candidates: list[Any] = [
        os.environ.get("AIWERK_CUI_SHARED_FOLDER"),
        os.environ.get("AIWERK_SHARED_FOLDER"),
        os.environ.get("HERMES_SHARED_FOLDER"),
        os.environ.get("HERMES_SHARED_DIR"),
    ]
    for section_name in ("assistant", "dashboard", "shared_folder", "shared"):
        section = config.get(section_name)
        if isinstance(section, dict):
            for key in ("shared_folder", "shared_dir", "shared_path", "path", "root", "mount_path", "dav_path", "webdav_path"):
                candidates.append(section.get(key))
            cloud = section.get("shared_cloud") or section.get("cloud_share")
            if isinstance(cloud, dict):
                for key in ("mount_path", "dav_path", "webdav_path", "local_path", "local_mount"):
                    candidates.append(cloud.get(key))
    discovered = _discover_dav_shared_folder_root(config)
    if discovered:
        candidates.append(str(discovered))
    for value in candidates:
        if not isinstance(value, str) or not value.strip():
            continue
        try:
            root = Path(value).expanduser().resolve()
            if root.is_dir():
                return root
        except Exception:
            continue
    return None


def _discover_dav_shared_folder_root(config: dict[str, Any]) -> Path | None:
    """Best-effort discovery for a desktop WebDAV mount used by the CUI shared folder."""
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    gvfs_root = Path(runtime_dir) / "gvfs"
    if not gvfs_root.is_dir():
        return None

    wanted_hosts: set[str] = {"dav.aiwerk.ch"}
    cloud = _shared_cloud_config(config)
    if isinstance(cloud, dict):
        for raw in (cloud.get("dav_url"), cloud.get("webdav_url"), cloud.get("mount_url")):
            if isinstance(raw, str) and raw.strip():
                parsed = urllib.parse.urlparse(raw)
                if parsed.hostname:
                    wanted_hosts.add(parsed.hostname.lower())
    preferred_folder = "Hermes-Shared"

    try:
        for mount in sorted(gvfs_root.iterdir(), key=lambda p: p.name):
            mount_name = mount.name.lower()
            if not mount.is_dir() or not any(f"host={host}" in mount_name for host in wanted_hosts):
                continue
            direct = mount / preferred_folder
            if direct.is_dir():
                return direct
            for owner_dir in sorted([p for p in mount.iterdir() if p.is_dir()], key=lambda p: p.name.lower()):
                candidate = owner_dir / preferred_folder
                if candidate.is_dir():
                    return candidate
    except Exception:
        return None
    return None


def _is_hidden_shared_item(path: Path) -> bool:
    name = path.name
    lower = name.lower()
    if name.startswith("."):
        return True
    if lower in _ASSISTANT_RESOURCE_HIDDEN_NAMES:
        return True
    if any(part in lower for part in ("secret", "credential", "password", "token", "private-key")):
        return True
    return False


def _shared_cloud_browse_url(cloud: dict[str, Any] | None, rel_path: str | None = None) -> str | None:
    if not isinstance(cloud, dict):
        return None
    base_url = str(cloud.get("base_url") or "").rstrip("/")
    share_id = str(cloud.get("share_id") or "").strip().strip("/")
    if not base_url or not share_id:
        return None
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None

    root_path = str(cloud.get("path") or "/").strip() or "/"
    clean_root = _clean_shared_cloud_path(root_path) or "/"
    if rel_path:
        clean_rel = _clean_shared_relative_path(rel_path)
        if not clean_rel:
            return None
        path = _clean_shared_cloud_path(clean_root.rstrip("/") + "/" + clean_rel)
    else:
        path = clean_root
    if not path:
        return None
    return f"{base_url}/web/client/pubshares/{urllib.parse.quote(share_id, safe='')}/browse?path={urllib.parse.quote(path, safe='')}"


def _shared_folder_items(root: Path, *, base: Path | None = None, depth: int = _ASSISTANT_RESOURCE_MAX_SHARED_DEPTH, cloud: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    try:
        root_real = root.resolve()
        base_real = (base or root).resolve()
        for child in sorted(root_real.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if len(items) >= _ASSISTANT_RESOURCE_MAX_SHARED_ITEMS:
                break
            try:
                resolved = child.resolve()
                if base_real not in resolved.parents and resolved != base_real:
                    continue
                if _is_hidden_shared_item(resolved):
                    continue
                stat_result = resolved.stat()
                mime = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
                rel = resolved.relative_to(base_real).as_posix()
                item = {
                    "id": _safe_resource_id(rel),
                    "name": resolved.name,
                    "kind": "folder" if resolved.is_dir() else "file",
                    "mime": mime if resolved.is_file() else None,
                    "size_bytes": None if resolved.is_dir() else stat_result.st_size,
                    "modified_at": datetime.fromtimestamp(stat_result.st_mtime, timezone.utc).isoformat().replace("+00:00", "Z"),
                }
                if resolved.is_file():
                    item["open_url"] = f"/api/assistant/shared-folder/open?path={urllib.parse.quote(rel, safe='')}"
                if resolved.is_dir():
                    cloud_url = _shared_cloud_browse_url(cloud, rel)
                    if cloud_url:
                        item["cloud_url"] = cloud_url
                if resolved.is_dir() and depth > 0:
                    item["children"] = _shared_folder_items(resolved, base=base_real, depth=depth - 1, cloud=cloud)
                    item["child_count"] = len(item["children"])
                items.append(item)
            except Exception:
                continue
    except Exception:
        return []
    return items


def _shared_cloud_config(config: dict[str, Any]) -> dict[str, Any] | None:
    for section_name in ("assistant", "dashboard", "shared", "shared_folder"):
        section = config.get(section_name)
        if not isinstance(section, dict):
            continue
        cloud = section.get("shared_cloud") or section.get("cloud_share")
        if isinstance(cloud, dict):
            return cloud
    return None


def _clean_shared_relative_path(value: str) -> str | None:
    parts = [part for part in str(value or "").replace("\\", "/").split("/") if part]
    clean_parts: list[str] = []
    for part in parts:
        if part in {".", ".."} or "/" in part or _is_hidden_shared_item(Path(part)):
            return None
        clean_parts.append(part)
    return "/".join(clean_parts) if clean_parts else None


def _clean_shared_cloud_path(value: str) -> str | None:
    parts = [part for part in str(value or "").replace("\\", "/").split("/") if part]
    clean_parts: list[str] = []
    for part in parts:
        if part in {".", ".."} or "/" in part or _is_hidden_shared_item(Path(part)):
            return None
        clean_parts.append(part)
    return "/" + "/".join(clean_parts) if clean_parts else "/"


def _resolve_shared_folder_file(root: Path, rel_path: str) -> Path | None:
    clean = _clean_shared_relative_path(rel_path)
    if not clean:
        return None
    try:
        base = root.resolve()
        target = (base / clean).resolve()
        if base not in target.parents or not target.is_file() or _is_hidden_shared_item(target):
            return None
        return target
    except Exception:
        return None


def _bool_config_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _shared_folder_remote_open_allowed(config: dict[str, Any]) -> bool:
    if _bool_config_value(os.environ.get("HERMES_CUI_ALLOW_REMOTE_FILE_MANAGER_OPEN")):
        return True
    for section_name in ("assistant", "dashboard", "shared_folder", "shared"):
        section = config.get(section_name)
        if not isinstance(section, dict):
            continue
        for key in ("allow_remote_file_manager_open", "allow_remote_open_folder", "remote_file_manager_open"):
            if _bool_config_value(section.get(key)):
                return True
    return False


def _request_looks_local(request: Request | None) -> bool:
    if request is None:
        return False
    forwarded_for = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
    real_ip = request.headers.get("x-real-ip", "").strip()
    client_host = forwarded_for or real_ip or (request.client.host if request.client else "")
    try:
        if not ipaddress.ip_address(client_host).is_loopback:
            return False
    except ValueError:
        if client_host not in {"localhost", ""}:
            return False

    raw_host = request.headers.get("host", "").strip().lower()
    if raw_host.startswith("[") and "]" in raw_host:
        host = raw_host[1:raw_host.index("]")]
    else:
        host = raw_host.split(":", 1)[0]
    if host in {"", "localhost", "127.0.0.1", "::1"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _can_open_system_folder() -> bool:
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False
    if sys.platform.startswith("linux"):
        return shutil.which("xdg-open") is not None
    if sys.platform == "darwin":
        return shutil.which("open") is not None
    if os.name == "nt":
        return True
    return False


def _can_open_shared_folder_for_request(request: Request | None, config: dict[str, Any]) -> bool:
    if not _can_open_system_folder():
        return False
    if _shared_folder_remote_open_allowed(config):
        return True
    return _request_looks_local(request)


def _open_system_folder(path: Path, *, request: Request | None = None, config: dict[str, Any] | None = None) -> bool:
    try:
        effective_config = config or load_config()
        if not path.is_dir() or not _can_open_shared_folder_for_request(request, effective_config):
            return False
        if sys.platform.startswith("linux"):
            subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            return True
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            return True
        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
            return True
    except Exception:
        return False
    return False


def _pass_first_line(entry: str) -> str | None:
    if not entry or not re.match(r"^[A-Za-z0-9._/@+-]+$", entry):
        return None
    try:
        result = subprocess.run(
            ["pass", "show", entry],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return None
        return (result.stdout.splitlines() or [""])[0].strip() or None
    except Exception:
        return None


def _urlopen_text(opener: urllib.request.OpenerDirector, request: urllib.request.Request, timeout: int = 20) -> tuple[int, str]:
    with opener.open(request, timeout=timeout) as response:
        data = response.read(512_000)
        return response.status, data.decode("utf-8", errors="replace")


def _urlopen_json(opener: urllib.request.OpenerDirector, request: urllib.request.Request, timeout: int = 20) -> Any:
    with opener.open(request, timeout=timeout) as response:
        data = response.read(512_000)
        return json.loads(data.decode("utf-8", errors="replace"))


def _sftpgo_item_kind(raw: dict[str, Any]) -> str:
    raw_type = raw.get("type")
    if raw_type in (1, "1", "dir", "directory", "folder"):
        return "folder"
    return "file"


def _sftpgo_modified_at(raw: dict[str, Any]) -> str | None:
    for key in ("modified_time", "mtime", "last_modified"):
        value = raw.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, (int, float)) and value > 0:
            seconds = value / 1000 if value > 10_000_000_000 else value
            return datetime.fromtimestamp(seconds, timezone.utc).isoformat().replace("+00:00", "Z")
    return None


def _sftpgo_pubshare_items(cloud: dict[str, Any]) -> list[dict[str, Any]]:
    base_url = str(cloud.get("base_url") or "").rstrip("/")
    share_id = str(cloud.get("share_id") or "").strip().strip("/")
    pass_entry = str(cloud.get("password_pass_entry") or cloud.get("pass_entry") or "").strip()
    root_path = str(cloud.get("path") or "/").strip() or "/"
    max_depth = int(cloud.get("max_depth") or _ASSISTANT_RESOURCE_MAX_SHARED_DEPTH)
    if not base_url or not share_id or not pass_entry:
        return []
    password = _pass_first_line(pass_entry)
    if not password:
        return []

    cookie_jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))
    quoted_share_id = urllib.parse.quote(share_id, safe="")
    login_next = urllib.parse.quote(f"/web/client/pubshares/{share_id}/browse", safe="")
    login_url = f"{base_url}/web/client/pubshares/{quoted_share_id}/login?next={login_next}"
    status, login_html = _urlopen_text(opener, urllib.request.Request(login_url, headers={"User-Agent": "Hermes-CUI/1.0"}))
    if status >= 400:
        return []
    match = re.search(r'name="_form_token"\s+value="([^"]+)"', login_html)
    if not match:
        return []
    form_token = html.unescape(match.group(1))
    body = urllib.parse.urlencode({"share_password": password, "_form_token": form_token}).encode()
    status, browse_html = _urlopen_text(
        opener,
        urllib.request.Request(
            login_url,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "Hermes-CUI/1.0"},
            method="POST",
        ),
    )
    if status >= 400 or 'name="share_password"' in browse_html:
        return []
    csrf_match = re.search(r"'X-CSRF-TOKEN':\s*'([^']+)'", browse_html)
    if not csrf_match:
        return []
    headers = {"X-CSRF-TOKEN": csrf_match.group(1), "User-Agent": "Hermes-CUI/1.0"}

    def clean_path(value: str) -> str:
        parts = [part for part in value.split("/") if part and part not in {".", ".."}]
        return "/" + "/".join(parts) if parts else "/"

    def child_path(parent: str, name: str) -> str:
        return clean_path((parent.rstrip("/") + "/" + name) if parent != "/" else "/" + name)

    def list_path(path: str, depth: int) -> list[dict[str, Any]]:
        dirs_url = f"{base_url}/web/client/pubshares/{quoted_share_id}/dirs?path={urllib.parse.quote(clean_path(path), safe='')}"
        raw_items = _urlopen_json(opener, urllib.request.Request(dirs_url, headers=headers))
        if not isinstance(raw_items, list):
            return []
        items: list[dict[str, Any]] = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name") or "").strip()
            if not name or "/" in name or name in {".", ".."} or _is_hidden_shared_item(Path(name)):
                continue
            kind = _sftpgo_item_kind(raw)
            size = raw.get("size")
            size_bytes = int(size) if kind == "file" and isinstance(size, (int, float, str)) and str(size).isdigit() else None
            item_path = child_path(path, name)
            item = {
                "id": _safe_resource_id(item_path),
                "name": name,
                "kind": kind,
                "mime": (mimetypes.guess_type(name)[0] or "application/octet-stream") if kind == "file" else None,
                "size_bytes": size_bytes,
                "modified_at": _sftpgo_modified_at(raw),
            }
            if kind == "file":
                item["open_url"] = f"/api/assistant/shared-folder/open?path={urllib.parse.quote(item_path, safe='')}"
            if kind == "folder":
                clean_item_path = _clean_shared_cloud_path(item_path)
                if clean_item_path:
                    item["cloud_url"] = f"{base_url}/web/client/pubshares/{quoted_share_id}/browse?path={urllib.parse.quote(clean_item_path, safe='')}"
            if kind == "folder" and depth > 0:
                children = list_path(item_path, depth - 1)
                item["children"] = children
                item["child_count"] = len(children)
            items.append(item)
            if len(items) >= _ASSISTANT_RESOURCE_MAX_SHARED_ITEMS:
                break
        return items

    return list_path(root_path, max(0, min(max_depth, _ASSISTANT_RESOURCE_MAX_SHARED_DEPTH)))


def _download_sftpgo_pubshare_file(cloud: dict[str, Any], rel_path: str) -> tuple[bytes, str, str] | None:
    clean = _clean_shared_relative_path(rel_path)
    if not clean:
        return None
    base_url = str(cloud.get("base_url") or "").rstrip("/")
    share_id = str(cloud.get("share_id") or "").strip().strip("/")
    pass_entry = str(cloud.get("password_pass_entry") or cloud.get("pass_entry") or "").strip()
    if not base_url or not share_id or not pass_entry:
        return None
    password = _pass_first_line(pass_entry)
    if not password:
        return None

    cookie_jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))
    quoted_share_id = urllib.parse.quote(share_id, safe="")
    login_next = urllib.parse.quote(f"/web/client/pubshares/{share_id}/browse", safe="")
    login_url = f"{base_url}/web/client/pubshares/{quoted_share_id}/login?next={login_next}"
    status, login_html = _urlopen_text(opener, urllib.request.Request(login_url, headers={"User-Agent": "Hermes-CUI/1.0"}))
    if status >= 400:
        return None
    match = re.search(r'name="_form_token"\s+value="([^"]+)"', login_html)
    if not match:
        return None
    body = urllib.parse.urlencode({"share_password": password, "_form_token": html.unescape(match.group(1))}).encode()
    status, browse_html = _urlopen_text(
        opener,
        urllib.request.Request(
            login_url,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "Hermes-CUI/1.0"},
            method="POST",
        ),
    )
    if status >= 400 or 'name="share_password"' in browse_html:
        return None

    csrf_match = re.search(r"'X-CSRF-TOKEN':\s*'([^']+)'", browse_html)
    headers = {"User-Agent": "Hermes-CUI/1.0"}
    if csrf_match:
        headers["X-CSRF-TOKEN"] = csrf_match.group(1)

    filename = Path(clean).name
    file_url = f"{base_url}/web/client/pubshares/{quoted_share_id}/browse?path={urllib.parse.quote('/' + clean, safe='')}"
    try:
        with opener.open(urllib.request.Request(file_url, headers=headers), timeout=30) as response:
            content_type = response.headers.get("content-type", mimetypes.guess_type(filename)[0] or "application/octet-stream")
            data = response.read(64 * 1024 * 1024 + 1)
            if len(data) > 64 * 1024 * 1024:
                return None
            if response.status < 400 and data and "text/html" not in content_type.lower():
                return data, content_type, filename
    except Exception:
        return None
    return None


def _shared_folder_summary(config: dict[str, Any], request: Request | None = None) -> dict[str, Any]:
    cloud = _shared_cloud_config(config)
    cloud_url = _shared_cloud_browse_url(cloud)
    shared_root = _resolve_shared_folder_root(config)
    if shared_root:
        items = _shared_folder_items(shared_root, cloud=cloud)
        payload = {
            "status": "connected",
            "root_label": shared_root.name,
            "summary": f"{len(items)} Dateien" if len(items) != 1 else "1 Datei",
            "items": items[:_ASSISTANT_RESOURCE_DEFAULT_VISIBLE_ITEMS],
            "total_count": len(items),
            "source": "local",
            "can_open_folder": _can_open_shared_folder_for_request(request, config),
        }
        if cloud_url:
            payload["cloud_url"] = cloud_url
        return payload

    if isinstance(cloud, dict):
        items = _sftpgo_pubshare_items(cloud)
        root_label = str(cloud.get("root_label") or cloud.get("label") or "cloud.aiwerk.ch")
        if items:
            payload = {
                "status": "connected",
                "root_label": root_label,
                "summary": f"{len(items)} Dateien" if len(items) != 1 else "1 Datei",
                "items": items[:_ASSISTANT_RESOURCE_DEFAULT_VISIBLE_ITEMS],
                "total_count": len(items),
                "source": "cloud",
                "can_open_folder": False,
            }
            if cloud_url:
                payload["cloud_url"] = cloud_url
            return payload
        payload = {
            "status": "error",
            "root_label": root_label,
            "summary": "Cloud-Ordner konnte nicht geprüft werden",
            "items": [],
            "total_count": 0,
            "source": "cloud",
            "can_open_folder": False,
        }
        if cloud_url:
            payload["cloud_url"] = cloud_url
        return payload

    return {
        "status": "not_configured",
        "root_label": "Shared",
        "summary": "Nicht eingerichtet",
        "items": [],
        "total_count": 0,
        "source": "none",
        "can_open_folder": False,
    }


def _parse_himalaya_email_date(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        # Himalaya JSON currently returns e.g. ``2026-05-30 16:57+00:00``.
        normalized = raw.replace(" ", "T", 1) if "T" not in raw else raw
        return datetime.fromisoformat(normalized).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except Exception:
        return raw


def _format_himalaya_address(value: Any) -> str:
    if isinstance(value, dict):
        name = str(value.get("name") or "").strip()
        addr = str(value.get("addr") or value.get("email") or "").strip()
        if name and addr:
            return f"{name} <{addr}>"
        return name or addr
    if isinstance(value, str):
        return value.strip()
    return ""


def _himalaya_envelope_to_resource_item(envelope: Any) -> dict[str, Any] | None:
    if not isinstance(envelope, dict):
        return None
    envelope_id = str(envelope.get("id") or "").strip()
    subject = str(envelope.get("subject") or "").strip()
    sender = _format_himalaya_address(envelope.get("from"))
    received_at = _parse_himalaya_email_date(envelope.get("date"))
    item = {
        "id": _safe_resource_id(envelope_id or f"mail-{subject}-{sender}", "mail"),
        "sender": sender,
        "subject": subject,
        "received_at": received_at,
    }
    if envelope_id:
        item["message_id"] = envelope_id
    flags = envelope.get("flags")
    if isinstance(flags, list):
        item["unread"] = not any(str(flag).lower() == "seen" for flag in flags)
    if bool(envelope.get("has_attachment")):
        item["has_attachment"] = True
    return item


def _email_resource_config(config: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(config, dict):
        return {}
    for section_name in ("assistant", "dashboard", "email", "mailbox"):
        section = config.get(section_name)
        if not isinstance(section, dict):
            continue
        if section_name in {"email", "mailbox"}:
            return section
        nested = section.get("email") or section.get("mailbox")
        if isinstance(nested, dict):
            return nested
    return {}


def _email_backend_name(account_cfg: dict[str, Any]) -> str:
    return str(account_cfg.get("backend") or account_cfg.get("type") or account_cfg.get("provider") or "").strip().lower()


def _is_google_email_backend(backend: str) -> bool:
    return backend in {"aiwerk_bridge", "aiwerk-bridge", "google_workspace", "google-workspace", "gmail", "mcp"}


def _is_himalaya_email_backend(backend: str) -> bool:
    return backend in {"himalaya", "imap"}


def _email_account_label(account_cfg: dict[str, Any], fallback: str) -> str:
    for key in ("address", "email", "user_google_email", "google_email", "label", "name", "account"):
        value = str(account_cfg.get(key) or "").strip()
        if value and value != "me":
            return value
    return fallback


def _email_account_address(account_cfg: dict[str, Any], fallback: str = "") -> str:
    for key in ("address", "email", "user_google_email", "google_email"):
        value = str(account_cfg.get(key) or "").strip()
        if value and value != "me":
            return value
    return fallback


def _email_account_dicts(raw: Any, *, defaults: dict[str, Any] | None = None, backend: str | None = None) -> list[dict[str, Any]]:
    defaults = defaults or {}
    if isinstance(raw, list):
        source_items = [item for item in raw if isinstance(item, dict)]
    elif isinstance(raw, dict):
        nested = raw.get("accounts")
        if isinstance(nested, list):
            source_items = [item for item in nested if isinstance(item, dict)]
            defaults = {**defaults, **{k: v for k, v in raw.items() if k != "accounts"}}
        else:
            source_items = [raw]
    else:
        source_items = []
    accounts: list[dict[str, Any]] = []
    for item in source_items:
        if item.get("enabled") is False:
            continue
        merged = {**defaults, **item}
        if backend and not _email_backend_name(merged):
            merged["backend"] = backend
        accounts.append(merged)
    return accounts


def _email_account_configs(config: dict[str, Any] | None) -> list[dict[str, Any]]:
    email_cfg = _email_resource_config(config)
    if not email_cfg:
        return []

    accounts: list[dict[str, Any]] = []
    accounts.extend(_email_account_dicts(email_cfg.get("accounts")))
    accounts.extend(_email_account_dicts(email_cfg.get("google_workspace"), backend="google_workspace"))
    accounts.extend(_email_account_dicts(email_cfg.get("gmail"), backend="gmail"))
    accounts.extend(_email_account_dicts(email_cfg.get("imap"), backend="imap"))
    accounts.extend(_email_account_dicts(email_cfg.get("himalaya"), backend="himalaya"))
    if accounts:
        return accounts

    backend = _email_backend_name(email_cfg)
    enabled = email_cfg.get("enabled")
    if backend or enabled is True:
        return [email_cfg]
    return []


def _email_sort_key(item: dict[str, Any]) -> str:
    value = item.get("received_at")
    return value if isinstance(value, str) else ""


def _email_item_ref(item: dict[str, Any]) -> str:
    return str(item.get("message_id") or item.get("id") or "").strip()


def _unread_first_email_items(
    unread_items: list[dict[str, Any]],
    latest_items: list[dict[str, Any]] | None = None,
    *,
    min_items: int = _ASSISTANT_EMAIL_PREVIEW_ITEMS,
) -> list[dict[str, Any]]:
    """Show all unread items first, then fill short lists with latest read mail."""
    unread_sorted = [
        dict(item, unread=True)
        for item in unread_items
        if isinstance(item, dict) and not _is_dashboard_spam_email_item(item)
    ]
    unread_sorted.sort(key=_email_sort_key, reverse=True)
    if len(unread_sorted) >= min_items:
        return unread_sorted

    seen = {_email_item_ref(item) for item in unread_sorted if _email_item_ref(item)}
    combined = list(unread_sorted)
    for item in latest_items or []:
        if not isinstance(item, dict) or _is_dashboard_spam_email_item(item):
            continue
        ref = _email_item_ref(item)
        if ref and ref in seen:
            continue
        next_item = dict(item)
        next_item["unread"] = False
        combined.append(next_item)
        if ref:
            seen.add(ref)
        if len(combined) >= min_items:
            break
    return combined


_ASSISTANT_EMAIL_BLOCKED_SENDER_DOMAINS = {
    "attractivewedding.info",
}

_ASSISTANT_EMAIL_BRAND_DOMAINS = {
    "migros": {"migros.ch", "migros.com", "migrosbank.ch"},
}


def _email_sender_domain(sender: Any) -> str:
    _name, address = email.utils.parseaddr(str(sender or ""))
    if "@" not in address:
        return ""
    return address.rsplit("@", 1)[-1].strip().lower().rstrip(".")


def _email_sender_display(sender: Any) -> str:
    name, address = email.utils.parseaddr(str(sender or ""))
    return (name or address or str(sender or "")).strip().lower()


def _domain_matches(domain: str, allowed_domains: set[str]) -> bool:
    return any(domain == allowed or domain.endswith(f".{allowed}") for allowed in allowed_domains)


def _is_dashboard_spam_email_item(item: dict[str, Any]) -> bool:
    """Hide obvious spam from the CUI resource rail without touching the mailbox."""
    sender = item.get("sender")
    sender_domain = _email_sender_domain(sender)
    if sender_domain in _ASSISTANT_EMAIL_BLOCKED_SENDER_DOMAINS:
        return True
    sender_display = _email_sender_display(sender)
    subject = str(item.get("subject") or "").lower()
    for brand, allowed_domains in _ASSISTANT_EMAIL_BRAND_DOMAINS.items():
        if brand in sender_display and sender_domain and not _domain_matches(sender_domain, allowed_domains):
            return True
        if brand in subject and sender_domain and not _domain_matches(sender_domain, allowed_domains):
            return True
    return False


def _visible_email_items(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    visible: list[dict[str, Any]] = []
    hidden = 0
    for item in items:
        if _is_dashboard_spam_email_item(item):
            hidden += 1
            continue
        visible.append(item)
    return visible, hidden


def _email_unread_count(items: list[dict[str, Any]], fallback: int = 0) -> int:
    if any("unread" in item for item in items):
        return sum(1 for item in items if item.get("unread") is True)
    return min(fallback, len(items)) if fallback else 0


def _merge_email_summaries(summaries: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not summaries:
        return None
    items: list[dict[str, Any]] = []
    accounts: list[dict[str, Any]] = []
    connected = 0
    filtered_count = 0
    for summary in summaries:
        status = str(summary.get("status") or "not_configured")
        if status == "connected":
            connected += 1
        label = str(summary.get("account_label") or "Mailbox").strip() or "Mailbox"
        account_address = str(summary.get("account_address") or label)
        account_items: list[dict[str, Any]] = []
        for item in summary.get("items") or []:
            if isinstance(item, dict):
                item = dict(item)
                item.setdefault("account_label", label)
                item.setdefault("account_address", account_address)
                account_items.append(item)
        account_items, hidden = _visible_email_items(account_items)
        filtered_count += hidden
        account_unread = _email_unread_count(account_items, int(summary.get("unread_count") or 0))
        items.extend(account_items)
        accounts.append({
            "label": label,
            "address": account_address,
            "source": str(summary.get("source") or ""),
            "status": status,
            "unread_count": account_unread,
            "summary": str(summary.get("summary") or ""),
            "items": account_items,
            "filtered_count": hidden,
        })
    items.sort(key=_email_sort_key, reverse=True)
    account_count = len(accounts)
    unread = sum(int(account.get("unread_count") or 0) for account in accounts)
    if unread:
        suffix = f" in {account_count} Konten" if account_count > 1 else ""
        summary_text = f"{unread} neue Nachrichten{suffix}" if unread != _ASSISTANT_EMAIL_UNREAD_SCAN_LIMIT else f"{unread}+ neue Nachrichten{suffix}"
    else:
        summary_text = "Keine neuen Nachrichten" if account_count <= 1 else f"Keine neuen Nachrichten in {account_count} Konten"
    status = "connected" if connected == account_count else ("limited" if connected else "error")
    return {
        "status": status,
        "unread_count": unread,
        "summary": summary_text,
        "items": items[:_ASSISTANT_EMAIL_PREVIEW_ITEMS],
        "accounts": accounts,
        "filtered_count": filtered_count,
    }


def _run_himalaya_envelope_list(*, query: list[str] | None = None, page_size: int = _ASSISTANT_EMAIL_PREVIEW_ITEMS, account: str | None = None, folder: str | None = None) -> list[dict[str, Any]]:
    if not shutil.which("himalaya"):
        raise FileNotFoundError("himalaya not installed")
    cmd = ["himalaya", "envelope", "list"]
    account = account or os.environ.get("AIWERK_CUI_EMAIL_ACCOUNT") or os.environ.get("HIMALAYA_ACCOUNT")
    folder = folder or os.environ.get("AIWERK_CUI_EMAIL_FOLDER") or os.environ.get("HIMALAYA_FOLDER") or "INBOX"
    if account:
        cmd.extend(["--account", account])
    if folder:
        cmd.extend(["--folder", folder])
    cmd.extend(["--page-size", str(page_size), "--output", "json"])
    if query:
        cmd.extend(query)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=_ASSISTANT_EMAIL_TIMEOUT_SECONDS)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "himalaya failed").strip())
    stdout = (proc.stdout or "").strip()
    if not stdout:
        return []
    data = json.loads(stdout)
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _run_himalaya_message_read(*, message_id: str, account: str | None = None, folder: str | None = None) -> str:
    if not shutil.which("himalaya"):
        raise FileNotFoundError("himalaya not installed")
    clean_message_id = str(message_id or "").strip()
    if not clean_message_id or not re.fullmatch(r"[A-Za-z0-9._:-]{1,160}", clean_message_id):
        raise ValueError("Invalid message id")
    cmd = ["himalaya", "message", "read", "--preview", "--output", "plain"]
    account = account or os.environ.get("AIWERK_CUI_EMAIL_ACCOUNT") or os.environ.get("HIMALAYA_ACCOUNT")
    folder = folder or os.environ.get("AIWERK_CUI_EMAIL_FOLDER") or os.environ.get("HIMALAYA_FOLDER") or "INBOX"
    if account:
        cmd.extend(["--account", account])
    if folder:
        cmd.extend(["--folder", folder])
    cmd.append(clean_message_id)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=_ASSISTANT_EMAIL_TIMEOUT_SECONDS)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "himalaya message read failed").strip())
    return proc.stdout or ""


def _find_email_account_config(config: dict[str, Any] | None, account_ref: str) -> dict[str, Any] | None:
    wanted = str(account_ref or "").strip()
    if not wanted:
        return None
    for account_cfg in _email_account_configs(config):
        backend = _email_backend_name(account_cfg)
        if not (_is_himalaya_email_backend(backend) or _is_google_email_backend(backend) or account_cfg.get("enabled") is True):
            continue
        account = str(account_cfg.get("account") or account_cfg.get("name") or "").strip()
        label = _email_account_label(account_cfg, account or "Mailbox")
        address = _email_account_address(account_cfg, label)
        user_google_email = str(account_cfg.get("user_google_email") or account_cfg.get("google_email") or "").strip()
        if wanted in {account, label, address, user_google_email}:
            return account_cfg
    return None


def _run_google_workspace_message_read(config: dict[str, Any] | None, account_cfg: dict[str, Any], *, message_id: str) -> str:
    clean_message_id = str(message_id or "").strip()
    if not clean_message_id or not re.fullmatch(r"[A-Za-z0-9._:-]{1,160}", clean_message_id):
        raise ValueError("Invalid message id")
    server = str(
        os.environ.get("AIWERK_CUI_GOOGLE_WORKSPACE_SERVER")
        or account_cfg.get("server")
        or account_cfg.get("mcp_server")
        or "google-workspace-aiwerk"
    ).strip()
    user_google_email = str(
        os.environ.get("AIWERK_CUI_GOOGLE_EMAIL")
        or account_cfg.get("user_google_email")
        or account_cfg.get("google_email")
        or account_cfg.get("address")
        or account_cfg.get("email")
        or "me"
    ).strip() or "me"
    result = _call_aiwerk_bridge_tool(
        config,
        server=server,
        tool="get_gmail_messages_content_batch",
        params={"message_ids": [clean_message_id], "user_google_email": user_google_email, "format": "full"},
    )
    text = _bridge_result_text(result).strip()
    if not text:
        raise RuntimeError("Google Workspace message body is empty")
    return text


_EMAIL_READER_META_HEADER_RE = re.compile(
    r"^(?:Message ID|Message-ID|Thread ID|Subject|From|To|Cc|Bcc|Date|Reply-To|List-[A-Za-z-]+|Web Link):\s*.*$",
    re.IGNORECASE,
)
_EMAIL_READER_RETRIEVED_RE = re.compile(r"^Retrieved\s+\d+\s+messages?:\s*$", re.IGNORECASE)
_EMAIL_READER_BODY_MARKER_RE = re.compile(r"^[-\s]*BODY[-\s]*$", re.IGNORECASE)
_EMAIL_READER_ATTACHMENTS_MARKER_RE = re.compile(r"^[-\s]*ATTACHMENTS[-\s]*$", re.IGNORECASE)
_EMAIL_READER_ATTACHMENT_ITEM_RE = re.compile(r"^\s*\d+\.\s+(.+?)\s+\(([^,()]+)(?:,\s*([^()]+))?\)\s*$")
_EMAIL_READER_URL_RE = re.compile(r"(?i)\b(?:https?|ftp)://[^\s<>()\[\]{}\"']+")
_EMAIL_READER_WWW_RE = re.compile(r"(?i)(?<![@\w])www\.[^\s<>()\[\]{}\"']+")
_EMAIL_READER_INVISIBLE_RE = re.compile(r"[\u034f\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]")
_EMAIL_READER_LONG_BODY_BOUNDARY_RE = re.compile(
    r"(?<=[.!?])\s+(?=(?:[A-ZÄÖÜ][A-Za-zÄÖÜäöüß]+|N\d{1,3}|[A-Z]{2,}\b))"
)
_EMAIL_READER_LONG_BODY_HINT_RE = re.compile(
    r"\s+(?=(?:Don't forget to confirm|Confirm my details|What happens|This is an official email|Remember,|Need help\?|Chat with us|If you have|We[’']re here|N26 Bank SE|Registered in|Management Board|This email was intended)\b)",
    re.IGNORECASE,
)


def _email_reader_attachment_summaries(lines: list[str]) -> tuple[list[str], list[str]]:
    """Remove raw attachment transport blocks and return compact customer-safe summaries."""
    kept: list[str] = []
    attachment_lines: list[str] = []
    in_attachments = False
    for line in lines:
        if _EMAIL_READER_ATTACHMENTS_MARKER_RE.match(line.strip()):
            in_attachments = True
            continue
        if not in_attachments:
            kept.append(line)
            continue
        match = _EMAIL_READER_ATTACHMENT_ITEM_RE.match(line)
        if not match:
            continue
        filename = match.group(1).strip()
        mime_type = match.group(2).strip()
        size = (match.group(3) or "").strip()
        descriptor = mime_type if not size else f"{mime_type}, {size}"
        if filename:
            attachment_lines.append(f"- {filename} ({descriptor})")
        else:
            attachment_lines.append(f"- {descriptor}")
    return kept, attachment_lines


def _replace_email_reader_links(body: str) -> str:
    """Hide raw URLs in the read-only CUI viewer while preserving that a link existed."""
    text = str(body or "")
    text = _EMAIL_READER_URL_RE.sub("[LINK]", text)
    text = _EMAIL_READER_WWW_RE.sub("[LINK]", text)
    text = re.sub(r"(?:\[LINK\](?:\s*[,;|·-]\s*)?){2,}", "[LINK]", text)
    return text


def _wrap_long_email_reader_body_text(text: str) -> str:
    non_empty_lines = [line for line in str(text or "").splitlines() if line.strip()]
    if len(non_empty_lines) == 1 and len(non_empty_lines[0]) > 500:
        long_line = non_empty_lines[0]
        long_line = _EMAIL_READER_LONG_BODY_HINT_RE.sub("\n\n", long_line)
        long_line = _EMAIL_READER_LONG_BODY_BOUNDARY_RE.sub("\n\n", long_line)
        return re.sub(r"\n{3,}", "\n\n", long_line).strip()
    return str(text or "").strip()


def _normalize_email_reader_body_text(body: str) -> str:
    """Make bridge/plain email bodies readable without exposing unsafe HTML."""
    text = _EMAIL_READER_INVISIBLE_RE.sub("", str(body or "")).replace("\r\n", "\n").replace("\r", "\n")
    normalized_lines = [re.sub(r"[ \t\f\v]{2,}", " ", line).strip() for line in text.split("\n")]
    text = "\n".join(normalized_lines)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return _wrap_long_email_reader_body_text(text)


def _strip_email_reader_transport_metadata(body: str) -> str:
    """Remove bridge/Himalaya transport headers from the CUI read-only body."""
    text = _normalize_email_reader_body_text(body)
    lines = text.splitlines()
    index = 0
    while index < len(lines) and not lines[index].strip():
        index += 1
    if index < len(lines) and _EMAIL_READER_RETRIEVED_RE.match(lines[index].strip()):
        index += 1
    stripped_any_header = False
    while index < len(lines):
        line = lines[index].strip()
        if not line:
            index += 1
            if stripped_any_header:
                continue
            continue
        if _EMAIL_READER_META_HEADER_RE.match(line):
            stripped_any_header = True
            index += 1
            continue
        break
    content_lines = lines[index:]
    while content_lines and not content_lines[0].strip():
        content_lines.pop(0)
    if content_lines and _EMAIL_READER_BODY_MARKER_RE.match(content_lines[0].strip()):
        content_lines.pop(0)
        while content_lines and not content_lines[0].strip():
            content_lines.pop(0)
    content_lines, attachment_summaries = _email_reader_attachment_summaries(content_lines)
    cleaned = "\n".join(content_lines).lstrip()
    if attachment_summaries:
        cleaned = f"{cleaned.rstrip()}\n\nAnhänge:\n" + "\n".join(attachment_summaries) if cleaned else "Anhänge:\n" + "\n".join(attachment_summaries)
    cleaned = _wrap_long_email_reader_body_text(cleaned)
    if not cleaned and not stripped_any_header:
        cleaned = text
    return _replace_email_reader_links(cleaned)


def _plain_email_reader_html(*, account_label: str, sender: str, subject: str, received_at: str, body: str) -> str:
    safe_subject = html.escape(subject or "Ohne Betreff")
    safe_sender = html.escape(sender or "Unbekannt")
    safe_account = html.escape(account_label or "Mailbox")
    safe_date = html.escape(received_at or "")
    clean_body = _strip_email_reader_transport_metadata(body)
    safe_body = html.escape(clean_body or "")
    return f"""<!doctype html>
<html lang=\"de\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>{safe_subject}</title>
  <style>
    :root {{ color-scheme: light; background: #f4efe7; color: #302b24; }}
    body {{ margin: 0; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, \"Segoe UI\", sans-serif; background: #f4efe7; color: #302b24; }}
    main {{ max-width: 920px; margin: 32px auto; padding: 0 20px 40px; }}
    article {{ border: 1px solid #ded4c4; border-radius: 24px; background: #fffaf2; box-shadow: 0 18px 50px rgba(56,42,20,.08); overflow: hidden; }}
    header {{ padding: 22px 24px 18px; border-bottom: 1px solid #eadfce; background: rgba(255,250,242,.96); }}
    .eyebrow {{ margin: 0 0 8px; font-size: 11px; font-weight: 800; letter-spacing: .18em; text-transform: uppercase; color: #948873; }}
    h1 {{ margin: 0; font-size: 22px; line-height: 1.25; color: #302b24; }}
    dl {{ display: grid; grid-template-columns: 110px minmax(0,1fr); gap: 8px 14px; margin: 18px 0 0; font-size: 13px; }}
    dt {{ color: #8a7f70; font-weight: 700; }}
    dd {{ margin: 0; min-width: 0; overflow-wrap: anywhere; color: #473f34; }}
    .body {{ padding: 22px 24px 26px; }}
    .email-body {{ margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; font: 14px/1.62 Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: #342f27; }}
    .notice {{ margin: 0 0 16px; color: #7c705f; font-size: 12px; }}
  </style>
</head>
<body>
  <main>
    <article>
      <header>
        <p class=\"eyebrow\">Nur-Leseansicht</p>
        <h1>{safe_subject}</h1>
        <dl>
          <dt>Von</dt><dd>{safe_sender}</dd>
          <dt>Konto</dt><dd>{safe_account}</dd>
          <dt>Datum</dt><dd>{safe_date}</dd>
        </dl>
      </header>
      <section class=\"body\">
        <p class=\"notice\">Diese Ansicht ist bereinigt. Externe Inhalte, Skripte und Anhänge werden nicht automatisch geladen.</p>
        <div class=\"email-body\">{safe_body}</div>
      </section>
    </article>
  </main>
</body>
</html>"""


class _CalendarHtmlToTextParser(HTMLParser):
    _BLOCK_TAGS = {"address", "article", "aside", "blockquote", "div", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "li", "p", "section", "tr"}
    _LINE_TAGS = {"br", "hr"}
    _SKIP_TAGS = {"script", "style", "iframe", "object", "embed", "noscript"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def _newline(self) -> None:
        if not self.parts or self.parts[-1].endswith("\n"):
            return
        self.parts.append("\n")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        name = tag.lower()
        if name in self._SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if name in self._LINE_TAGS or name in self._BLOCK_TAGS:
            self._newline()

    def handle_endtag(self, tag: str) -> None:
        name = tag.lower()
        if name in self._SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if name in self._BLOCK_TAGS:
            self._newline()

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        self.parts.append(data)

    def text(self) -> str:
        return "".join(self.parts)


def _html_fragment_to_plain_text(value: str) -> str:
    text = html.unescape(str(value or ""))
    if not re.search(r"<\s*/?\s*[A-Za-z][^>]*>", text):
        return text
    parser = _CalendarHtmlToTextParser()
    try:
        parser.feed(text)
        parser.close()
        return parser.text()
    except Exception:
        return re.sub(r"<[^>]+>", " ", text)


def _clean_calendar_reader_body(body: str) -> str:
    text = _html_fragment_to_plain_text(body)
    text = _EMAIL_READER_INVISIBLE_RE.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t\f\v]{2,}", " ", line).strip() for line in text.split("\n")]
    text = "\n".join(line for line in lines if line)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return _replace_email_reader_links(_wrap_long_email_reader_body_text(text))


def _format_swiss_datetime(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        normalized = raw.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(ZoneInfo("Europe/Zurich"))
        return parsed.strftime("%d.%m.%Y, %H:%M Uhr")
    except Exception:
        return raw


def _plain_calendar_reader_html(*, account_label: str, title: str, starts_at: str, ends_at: str, location: str, body: str) -> str:
    safe_title = html.escape(title or "Termin")
    safe_account = html.escape(account_label or "Kalender")
    safe_starts = html.escape(_format_swiss_datetime(starts_at))
    safe_ends = html.escape(_format_swiss_datetime(ends_at))
    safe_location = html.escape(location or "")
    safe_body = html.escape(_clean_calendar_reader_body(body or ""))
    return f"""<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{safe_title}</title>
  <style>
    :root {{ color-scheme: light; background: #f4efe7; color: #302b24; }}
    body {{ margin: 0; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f4efe7; color: #302b24; }}
    main {{ max-width: 920px; margin: 32px auto; padding: 0 20px 40px; }}
    article {{ border: 1px solid #ded4c4; border-radius: 24px; background: #fffaf2; box-shadow: 0 18px 50px rgba(56,42,20,.08); overflow: hidden; }}
    header {{ padding: 22px 24px 18px; border-bottom: 1px solid #eadfce; background: rgba(255,250,242,.96); }}
    .eyebrow {{ margin: 0 0 8px; font-size: 11px; font-weight: 800; letter-spacing: .18em; text-transform: uppercase; color: #948873; }}
    h1 {{ margin: 0; font-size: 22px; line-height: 1.25; color: #302b24; }}
    dl {{ display: grid; grid-template-columns: 110px minmax(0,1fr); gap: 8px 14px; margin: 18px 0 0; font-size: 13px; }}
    dt {{ color: #8a7f70; font-weight: 700; }}
    dd {{ margin: 0; min-width: 0; overflow-wrap: anywhere; color: #473f34; }}
    .body {{ padding: 22px 24px 26px; }}
    pre {{ margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; font: 14px/1.55 ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace; color: #342f27; }}
    .notice {{ margin: 0 0 16px; color: #7c705f; font-size: 12px; }}
  </style>
</head>
<body>
  <main>
    <article>
      <header>
        <p class="eyebrow">Nur-Leseansicht</p>
        <h1>{safe_title}</h1>
        <dl>
          <dt>Kalender</dt><dd>{safe_account}</dd>
          <dt>Beginn</dt><dd>{safe_starts}</dd>
          <dt>Ende</dt><dd>{safe_ends}</dd>
          <dt>Ort</dt><dd>{safe_location}</dd>
        </dl>
      </header>
      <section class="body">
        <p class="notice">Diese Ansicht ist bereinigt. Externe Inhalte, Skripte und Rohlinks werden nicht automatisch geladen.</p>
        <pre>{safe_body}</pre>
      </section>
    </article>
  </main>
</body>
</html>"""

_CONFIG_ENV_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_config_env_refs(value: Any) -> str:
    """Resolve ${ENV_VAR} placeholders in config values from process/.env."""
    text = str(value)
    if "${" not in text:
        return text
    env_values: dict[str, str] | None = None

    def replace(match: re.Match[str]) -> str:
        nonlocal env_values
        name = match.group(1)
        if name in os.environ:
            return os.environ[name]
        if env_values is None:
            try:
                env_values = load_env()
            except Exception:
                env_values = {}
        loaded_values = env_values or {}
        return loaded_values.get(name, match.group(0))

    return _CONFIG_ENV_REF_RE.sub(replace, text)


def _mcp_bridge_config(config: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(config, dict):
        return {}
    servers = config.get("mcp_servers")
    if not isinstance(servers, dict):
        return {}
    bridge = servers.get("aiwerk_bridge")
    if not isinstance(bridge, dict) or bridge.get("enabled") is False:
        return {}
    url = str(bridge.get("url") or "").strip()
    if not url:
        return {}
    raw_headers = bridge.get("headers")
    headers = raw_headers if isinstance(raw_headers, dict) else {}
    return {"url": _expand_config_env_refs(url), "headers": {str(k): _expand_config_env_refs(v) for k, v in headers.items()}}


_MCP_BRIDGE_SESSION_LOCK = threading.Lock()
_MCP_BRIDGE_SESSIONS: dict[str, str | None] = {}
_MCP_BRIDGE_REQUEST_IDS: dict[str, int] = {}


def _mcp_bridge_session_key(config: dict[str, Any] | None) -> str:
    bridge = _mcp_bridge_config(config)
    if not bridge:
        raise RuntimeError("AIWerk Bridge MCP server not configured")
    try:
        return json.dumps(bridge, sort_keys=True, default=str)
    except TypeError:
        return repr(bridge)


def _mcp_bridge_next_request_id(session_key: str) -> int:
    with _MCP_BRIDGE_SESSION_LOCK:
        next_id = int(_MCP_BRIDGE_REQUEST_IDS.get(session_key, 0)) + 1
        _MCP_BRIDGE_REQUEST_IDS[session_key] = next_id
        return next_id


def _mcp_bridge_rpc(config: dict[str, Any] | None, method: str, params: dict[str, Any], *, session_id: str | None = None, request_id: int = 1) -> tuple[dict[str, Any], str | None]:
    bridge = _mcp_bridge_config(config)
    if not bridge:
        raise RuntimeError("AIWerk Bridge MCP server not configured")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        **bridge.get("headers", {}),
    }
    if session_id:
        headers["MCP-Session-Id"] = session_id
    payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(bridge["url"], data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=_ASSISTANT_MCP_BRIDGE_TIMEOUT_SECONDS) as resp:
        raw = resp.read().decode("utf-8")
        response = json.loads(raw) if raw else {}
        next_session_id = resp.headers.get("MCP-Session-Id") or resp.headers.get("mcp-session-id") or session_id
    if isinstance(response, dict) and response.get("error"):
        raise RuntimeError(str(response["error"]))
    return response, next_session_id


def _mcp_bridge_initialize(config: dict[str, Any] | None, *, session_key: str | None = None) -> str | None:
    session_key = session_key or _mcp_bridge_session_key(config)
    _, session_id = _mcp_bridge_rpc(
        config,
        "initialize",
        {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "aiwerk-cui", "version": str(__version__)},
        },
        request_id=_mcp_bridge_next_request_id(session_key),
    )
    return session_id


def _mcp_bridge_session(config: dict[str, Any] | None) -> tuple[str, str | None]:
    session_key = _mcp_bridge_session_key(config)
    with _MCP_BRIDGE_SESSION_LOCK:
        cached_session_id = _MCP_BRIDGE_SESSIONS.get(session_key)
    if cached_session_id:
        return session_key, cached_session_id
    session_id = _mcp_bridge_initialize(config, session_key=session_key)
    with _MCP_BRIDGE_SESSION_LOCK:
        _MCP_BRIDGE_SESSIONS[session_key] = session_id
    return session_key, session_id


def _mcp_bridge_forget_session(session_key: str) -> None:
    with _MCP_BRIDGE_SESSION_LOCK:
        _MCP_BRIDGE_SESSIONS.pop(session_key, None)


def _mcp_bridge_router_call(config: dict[str, Any] | None, arguments: dict[str, Any]) -> dict[str, Any]:
    session_key, session_id = _mcp_bridge_session(config)
    try:
        response, next_session_id = _mcp_bridge_rpc(
            config,
            "tools/call",
            {"name": "mcp", "arguments": arguments},
            session_id=session_id,
            request_id=_mcp_bridge_next_request_id(session_key),
        )
    except RuntimeError as exc:
        if "session" not in str(exc).lower():
            raise
        _mcp_bridge_forget_session(session_key)
        session_key, session_id = _mcp_bridge_session(config)
        response, next_session_id = _mcp_bridge_rpc(
            config,
            "tools/call",
            {"name": "mcp", "arguments": arguments},
            session_id=session_id,
            request_id=_mcp_bridge_next_request_id(session_key),
        )
    if next_session_id and next_session_id != session_id:
        with _MCP_BRIDGE_SESSION_LOCK:
            _MCP_BRIDGE_SESSIONS[session_key] = next_session_id
    return response


def _call_aiwerk_bridge_tool(config: dict[str, Any] | None, *, server: str, tool: str, params: dict[str, Any]) -> dict[str, Any]:
    response = _mcp_bridge_router_call(
        config,
        {"action": "call", "server": server, "tool": tool, "params": params},
    )
    result = response.get("result") if isinstance(response, dict) else None
    if not isinstance(result, dict):
        return {}
    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                text = item["text"].strip()
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, dict):
                        return parsed
                except Exception:
                    continue
    return result


def _bridge_result_text(result: dict[str, Any]) -> str:
    nested = result.get("result") if isinstance(result, dict) else None
    if isinstance(nested, dict):
        structured = nested.get("structuredContent")
        if isinstance(structured, dict) and isinstance(structured.get("result"), str):
            return structured["result"]
        content = nested.get("content")
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
            if parts:
                return "\n".join(parts)
    structured = result.get("structuredContent") if isinstance(result, dict) else None
    if isinstance(structured, dict) and isinstance(structured.get("result"), str):
        return structured["result"]
    return ""


def _gmail_search_message_ids(text: str) -> list[str]:
    return re.findall(r"Message ID:\s*([A-Za-z0-9_-]+)", text or "")


def _parse_gmail_bridge_date(value: str) -> str | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        return email.utils.parsedate_to_datetime(raw).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except Exception:
        return raw


def _gmail_bridge_metadata_to_items(text: str, *, unread: bool) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for block in re.split(r"\n(?=Message ID:\s*)", text or ""):
        if "Message ID:" not in block:
            continue
        message_id = ""
        subject = ""
        sender = ""
        date = ""
        web_link = ""
        for line in block.splitlines():
            key, _, value = line.partition(":")
            normalized = key.strip().lower()
            value = value.strip()
            if normalized == "message id":
                message_id = value
            elif normalized == "subject":
                subject = value
            elif normalized == "from":
                sender = value
            elif normalized == "date":
                date = value
            elif normalized == "web link":
                web_link = value
        if not message_id and not subject:
            continue
        item: dict[str, Any] = {
            "id": _safe_resource_id(message_id or f"gmail-{subject}-{sender}", "mail"),
            "message_id": message_id,
            "sender": sender,
            "subject": subject,
            "received_at": _parse_gmail_bridge_date(date),
            "unread": unread,
        }
        if web_link:
            item["gmail_web_url"] = web_link
        items.append(item)
    return items


def _parse_gmail_bridge_metadata_blocks(text: str) -> list[dict[str, str]]:
    blocks: list[dict[str, str]] = []
    for block in re.split(r"\n(?=Message ID:\s*)", text or ""):
        if "Message ID:" not in block:
            continue
        parsed: dict[str, str] = {}
        for line in block.splitlines():
            key, _, value = line.partition(":")
            normalized = key.strip().lower()
            if not normalized or not value:
                continue
            parsed[normalized] = value.strip()
        if parsed:
            blocks.append(parsed)
    return blocks


def _gmail_bridge_search_message_ids(config: dict[str, Any] | None, *, server: str, user_google_email: str, query: str, page_size: int) -> list[str]:
    search = _call_aiwerk_bridge_tool(
        config,
        server=server,
        tool="search_gmail_messages",
        params={"query": query, "user_google_email": user_google_email, "page_size": page_size},
    )
    return _gmail_search_message_ids(_bridge_result_text(search))[:page_size]


def _gmail_bridge_metadata_items_for_ids(config: dict[str, Any] | None, *, server: str, user_google_email: str, message_ids: list[str], unread: bool, page_size: int) -> list[dict[str, Any]]:
    if not message_ids:
        return []
    batch = _call_aiwerk_bridge_tool(
        config,
        server=server,
        tool="get_gmail_messages_content_batch",
        params={"message_ids": message_ids[:page_size], "user_google_email": user_google_email, "format": "metadata"},
    )
    items = _gmail_bridge_metadata_to_items(_bridge_result_text(batch), unread=unread)
    items.sort(key=lambda item: str(item.get("received_at") or ""), reverse=True)
    return items[:page_size]


def _gmail_bridge_message_items(config: dict[str, Any] | None, *, server: str, user_google_email: str, query: str, page_size: int, unread: bool) -> list[dict[str, Any]]:
    message_ids = _gmail_bridge_search_message_ids(
        config,
        server=server,
        user_google_email=user_google_email,
        query=query,
        page_size=page_size,
    )
    return _gmail_bridge_metadata_items_for_ids(
        config,
        server=server,
        user_google_email=user_google_email,
        message_ids=message_ids,
        unread=unread,
        page_size=page_size,
    )


def _google_workspace_email_summary(config: dict[str, Any] | None = None, account_cfg: dict[str, Any] | None = None) -> dict[str, Any] | None:
    if os.environ.get("AIWERK_CUI_EMAIL_DISABLE_AIWERK_BRIDGE", "").lower() in {"1", "true", "yes", "on"}:
        return None
    email_cfg = account_cfg or _email_resource_config(config)
    backend = str((os.environ.get("AIWERK_CUI_EMAIL_BACKEND") if account_cfg is None else "") or _email_backend_name(email_cfg))
    if not _is_google_email_backend(backend):
        return None
    server = str(os.environ.get("AIWERK_CUI_GOOGLE_WORKSPACE_SERVER") or email_cfg.get("server") or email_cfg.get("mcp_server") or "google-workspace-aiwerk").strip()
    user_google_email = str(os.environ.get("AIWERK_CUI_GOOGLE_EMAIL") or email_cfg.get("user_google_email") or email_cfg.get("google_email") or email_cfg.get("address") or email_cfg.get("email") or "me").strip() or "me"
    account_label = _email_account_label(email_cfg, "Google Workspace")
    account_address = _email_account_address(email_cfg, account_label)
    unread_query = str(email_cfg.get("unread_query") or os.environ.get("AIWERK_CUI_GMAIL_UNREAD_QUERY") or "in:inbox is:unread").strip()
    latest_query = str(email_cfg.get("latest_query") or os.environ.get("AIWERK_CUI_GMAIL_LATEST_QUERY") or "in:inbox").strip()
    try:
        unread_ids = _gmail_bridge_search_message_ids(
            config,
            server=server,
            user_google_email=user_google_email,
            query=unread_query,
            page_size=_ASSISTANT_EMAIL_UNREAD_SCAN_LIMIT,
        )
        unread = len(unread_ids)
        unread_items = _gmail_bridge_metadata_items_for_ids(
            config,
            server=server,
            user_google_email=user_google_email,
            message_ids=unread_ids,
            unread=True,
            page_size=_ASSISTANT_EMAIL_UNREAD_SCAN_LIMIT,
        )
        latest_items: list[dict[str, Any]] = []
        if len(unread_items) < _ASSISTANT_EMAIL_PREVIEW_ITEMS and latest_query != unread_query:
            latest_items = _gmail_bridge_message_items(
                config,
                server=server,
                user_google_email=user_google_email,
                query=latest_query,
                page_size=_ASSISTANT_EMAIL_PREVIEW_ITEMS + len(unread_items),
                unread=False,
            )
        preview_items = _unread_first_email_items(unread_items, latest_items)
        for item in preview_items:
            item.setdefault("account_label", account_label)
            item.setdefault("account_address", account_address)
            item.setdefault("source", "gmail")
            message_id = str(item.get("message_id") or item.get("id") or "").strip()
            if message_id:
                params = urllib.parse.urlencode({"account": account_address, "id": message_id})
                item["open_url"] = f"/api/assistant/email/view?{params}"
        if unread:
            summary = f"{unread} neue Nachrichten" if unread != _ASSISTANT_EMAIL_UNREAD_SCAN_LIMIT else f"{unread}+ neue Nachrichten"
        else:
            summary = "Keine neuen Nachrichten"
        return {
            "status": "connected",
            "unread_count": unread,
            "summary": summary,
            "items": preview_items,
            "account_label": account_label,
            "account_address": account_address,
            "source": "gmail",
        }
    except Exception as exc:
        _log.debug("CUI AIWerk Bridge Gmail summary failed for %s: %s", account_address, exc)
        return {
            "status": "error",
            "unread_count": 0,
            "summary": "E-Mail konnte nicht geprüft werden",
            "items": [],
            "account_label": account_label,
            "account_address": account_address,
            "source": "gmail",
            "error": str(exc),
        }


def _himalaya_email_summary(config: dict[str, Any] | None = None, account_cfg: dict[str, Any] | None = None) -> dict[str, Any] | None:
    if os.environ.get("AIWERK_CUI_EMAIL_DISABLE_HIMALAYA", "").lower() in {"1", "true", "yes", "on"}:
        return None
    email_cfg = account_cfg or _email_resource_config(config)
    backend = str((os.environ.get("AIWERK_CUI_EMAIL_BACKEND") if account_cfg is None else "") or _email_backend_name(email_cfg))
    enabled = email_cfg.get("enabled")
    if not _is_himalaya_email_backend(backend) and enabled is not True:
        return None
    account = str(email_cfg.get("account") or email_cfg.get("name") or "").strip() or None
    folder = str(email_cfg.get("folder") or email_cfg.get("mailbox") or "").strip() or None
    account_label = _email_account_label(email_cfg, account or "IMAP")
    account_address = _email_account_address(email_cfg, account_label)
    try:
        unread_raw = _run_himalaya_envelope_list(
            query=["not", "flag", "Seen"],
            page_size=_ASSISTANT_EMAIL_UNREAD_SCAN_LIMIT,
            account=account,
            folder=folder,
        )
        unread_items = [_himalaya_envelope_to_resource_item(item) for item in unread_raw]
        unread_items = [item for item in unread_items if item]
        unread = len(unread_items)
        latest_items: list[dict[str, Any]] = []
        if len(unread_items) < _ASSISTANT_EMAIL_PREVIEW_ITEMS:
            latest_raw = _run_himalaya_envelope_list(
                page_size=_ASSISTANT_EMAIL_PREVIEW_ITEMS + len(unread_items),
                account=account,
                folder=folder,
            )
            latest_items = [item for item in (_himalaya_envelope_to_resource_item(item) for item in latest_raw) if item]
        preview_items = _unread_first_email_items(unread_items, latest_items)
        for item in preview_items:
            item.setdefault("account_label", account_label)
            item.setdefault("account_address", account_address)
            item.setdefault("source", "imap")
            message_id = str(item.get("message_id") or item.get("id") or "").strip()
            if message_id:
                params = urllib.parse.urlencode({"account": account_address, "id": message_id})
                item.setdefault("open_url", f"/api/assistant/email/view?{params}")
        if unread:
            summary = f"{unread} neue Nachrichten" if unread != _ASSISTANT_EMAIL_UNREAD_SCAN_LIMIT else f"{unread}+ neue Nachrichten"
        else:
            summary = "Keine neuen Nachrichten"
        return {
            "status": "connected",
            "unread_count": unread,
            "summary": summary,
            "items": preview_items,
            "account_label": account_label,
            "account_address": account_address,
            "source": "imap",
        }
    except Exception as exc:
        _log.debug("CUI himalaya email summary failed: %s", exc)
        return None


def _email_summary(config: dict[str, Any] | None = None) -> dict[str, Any]:
    data = _read_optional_json(os.environ.get("AIWERK_CUI_EMAIL_SUMMARY_JSON"))
    if data:
        unread = int(data.get("unread_count") or data.get("new_count") or 0)
        raw_items = data.get("items")
        items = raw_items if isinstance(raw_items, list) else []
        return {
            "status": data.get("status") or "connected",
            "unread_count": unread,
            "summary": data.get("summary") or (f"{unread} neue Nachrichten" if unread else "Keine neuen Nachrichten"),
            "items": items[:_ASSISTANT_EMAIL_PREVIEW_ITEMS],
        }
    account_cfgs = _email_account_configs(config)
    if account_cfgs:
        summaries: list[dict[str, Any]] = []
        for account_cfg in account_cfgs:
            backend = _email_backend_name(account_cfg)
            if _is_google_email_backend(backend):
                summary = _google_workspace_email_summary(config, account_cfg)
            elif _is_himalaya_email_backend(backend) or account_cfg.get("enabled") is True:
                summary = _himalaya_email_summary(config, account_cfg)
            else:
                summary = None
            if summary:
                summaries.append(summary)
        merged = _merge_email_summaries(summaries)
        if merged:
            return merged

    maildir = os.environ.get("AIWERK_CUI_MAILDIR") or os.environ.get("MAILDIR")
    if maildir:
        try:
            new_dir = Path(maildir).expanduser() / "new"
            unread = len([p for p in new_dir.iterdir() if p.is_file()]) if new_dir.is_dir() else 0
            return {
                "status": "connected",
                "unread_count": unread,
                "summary": f"{unread} neue Nachrichten" if unread else "Keine neuen Nachrichten",
                "items": [],
            }
        except Exception:
            return {"status": "error", "unread_count": 0, "summary": "E-Mail konnte nicht geprüft werden", "items": []}
    return {"status": "not_configured", "unread_count": 0, "summary": "Nicht eingerichtet", "items": []}


def _calendar_resource_config(config: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(config, dict):
        return {}
    for section_name in ("assistant", "dashboard", "calendar", "calendars"):
        section = config.get(section_name)
        if not isinstance(section, dict):
            continue
        if section_name in {"calendar", "calendars"}:
            return section
        nested = section.get("calendar") or section.get("calendars")
        if isinstance(nested, dict):
            return nested
    return {}


def _calendar_account_configs(config: dict[str, Any] | None) -> list[dict[str, Any]]:
    calendar_cfg = _calendar_resource_config(config)
    accounts: list[dict[str, Any]] = []
    accounts.extend(_email_account_dicts(calendar_cfg.get("accounts") if calendar_cfg else None, backend="google_workspace"))
    accounts.extend(_email_account_dicts(calendar_cfg.get("google_workspace") if calendar_cfg else None, backend="google_workspace"))
    accounts.extend(_email_account_dicts(calendar_cfg.get("google") if calendar_cfg else None, backend="google_workspace"))
    if not accounts and calendar_cfg:
        backend = _email_backend_name(calendar_cfg)
        enabled = calendar_cfg.get("enabled")
        if backend or enabled is True:
            accounts = [calendar_cfg]
    if accounts:
        return accounts
    # Default: mirror configured Google Workspace mail accounts, because the
    # CUI calendar rail should follow the same tenant/account routing as mail.
    return [account for account in _email_account_configs(config) if _is_google_email_backend(_email_backend_name(account))]


def _calendar_item_with_open_url(item: dict[str, Any], *, account_address: str | None = None) -> dict[str, Any]:
    normalized = dict(item)
    event_ref = str(normalized.get("event_id") or normalized.get("id") or "").strip()
    account_ref = str(account_address or normalized.get("account_address") or normalized.get("account_label") or "Kalender").strip()
    if event_ref and not normalized.get("open_url"):
        params = urllib.parse.urlencode({"account": account_ref, "id": event_ref})
        normalized["open_url"] = f"/api/assistant/calendar/view?{params}"
    return normalized


def _parse_google_workspace_event_detail(text: str) -> dict[str, str]:
    details: dict[str, str] = {}
    key_map = {
        "Title": "title",
        "Starts": "starts_at",
        "Ends": "ends_at",
        "Description": "description",
        "Location": "location_hint",
        "Event ID": "event_id",
        "Link": "html_link",
    }
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("-") or ":" not in stripped:
            continue
        key, value = stripped[1:].split(":", 1)
        normalized_key = key_map.get(key.strip())
        if not normalized_key:
            continue
        cleaned = value.strip()
        if cleaned in {"", "No Description", "No Location", "No Link", "No ID"}:
            continue
        details[normalized_key] = cleaned
    return details


def _calendar_account_config_for_ref(config: dict[str, Any] | None, account_ref: str) -> dict[str, Any] | None:
    normalized_ref = (account_ref or "").strip()
    if not normalized_ref:
        return None
    for account_cfg in _calendar_account_configs(config):
        if not isinstance(account_cfg, dict):
            continue
        account_label = _email_account_label(account_cfg, "Google Kalender")
        account_address = _email_account_address(account_cfg, account_label)
        candidates = {
            str(account_address or "").strip(),
            str(account_label or "").strip(),
            str(account_cfg.get("address") or "").strip(),
            str(account_cfg.get("email") or "").strip(),
            str(account_cfg.get("user_google_email") or account_cfg.get("google_email") or "").strip(),
        }
        if normalized_ref in candidates:
            return account_cfg
    return None


def _fetch_google_workspace_calendar_event_detail(config: dict[str, Any] | None, account_cfg: dict[str, Any] | None, event_id: str) -> dict[str, str]:
    if not isinstance(account_cfg, dict) or not event_id:
        return {}
    backend = _email_backend_name(account_cfg)
    if backend and not _is_google_email_backend(backend):
        return {}
    server = str(account_cfg.get("server") or account_cfg.get("mcp_server") or "google-workspace-aiwerk").strip()
    account_label = _email_account_label(account_cfg, "Google Kalender")
    account_address = _email_account_address(account_cfg, account_label)
    user_google_email = str(account_cfg.get("user_google_email") or account_cfg.get("google_email") or account_cfg.get("address") or account_cfg.get("email") or "me").strip() or "me"
    calendar_id = str(account_cfg.get("calendar_id") or account_cfg.get("calendar") or account_cfg.get("calendar_email") or account_address or user_google_email or "primary").strip() or "primary"
    result = _call_aiwerk_bridge_tool(
        config,
        server=server,
        tool="get_events",
        params={
            "calendar_id": calendar_id,
            "user_google_email": user_google_email,
            "event_id": event_id,
            "max_results": 1,
            "detailed": True,
        },
    )
    return _parse_google_workspace_event_detail(_bridge_result_text(result))


def _parse_google_workspace_event_items(text: str, *, account_label: str, account_address: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("-"):
            continue
        match = re.search(
            r'^-\s+"(?P<title>.*?)"\s+\(Starts:\s+(?P<start>.*?),\s+Ends:\s+(?P<end>.*?)\)\s+ID:\s+(?P<id>\S+)(?:\s+\|\s+Link:\s+(?P<link>\S+))?',
            stripped,
        )
        if not match:
            continue
        item: dict[str, Any] = {
            "id": _safe_resource_id(match.group("id"), "event"),
            "event_id": match.group("id"),
            "title": match.group("title"),
            "starts_at": match.group("start"),
            "ends_at": match.group("end"),
            "account_label": account_label,
            "account_address": account_address,
            "source": "google_calendar",
        }
        link = match.group("link")
        if link:
            item["html_link"] = link
        event_ref = str(item.get("event_id") or item.get("id") or "").strip()
        if event_ref:
            params = urllib.parse.urlencode({"account": account_address, "id": event_ref})
            item["open_url"] = f"/api/assistant/calendar/view?{params}"
        items.append(item)
    items.sort(key=lambda item: str(item.get("starts_at") or ""))
    return items


def _google_workspace_calendar_summary(config: dict[str, Any] | None, account_cfg: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any] | None:
    backend = _email_backend_name(account_cfg)
    if backend and not _is_google_email_backend(backend):
        return None
    server = str(account_cfg.get("server") or account_cfg.get("mcp_server") or "google-workspace-aiwerk").strip()
    user_google_email = str(account_cfg.get("user_google_email") or account_cfg.get("google_email") or account_cfg.get("address") or account_cfg.get("email") or "me").strip() or "me"
    account_label = _email_account_label(account_cfg, "Google Kalender")
    account_address = _email_account_address(account_cfg, account_label)
    calendar_id = str(account_cfg.get("calendar_id") or account_cfg.get("calendar") or account_cfg.get("calendar_email") or account_address or user_google_email or "primary").strip() or "primary"
    now = now or datetime.now(timezone.utc)
    time_min = str(account_cfg.get("time_min") or now.isoformat())
    horizon_days = int(account_cfg.get("horizon_days") or os.environ.get("AIWERK_CUI_CALENDAR_HORIZON_DAYS") or 7)
    time_max = str(account_cfg.get("time_max") or (now + timedelta(days=horizon_days)).isoformat())
    max_results = int(account_cfg.get("max_results") or os.environ.get("AIWERK_CUI_CALENDAR_MAX_RESULTS") or 5)
    event_types_cfg = account_cfg.get("event_types", ["default"])
    if isinstance(event_types_cfg, str):
        event_types = [part.strip() for part in event_types_cfg.split(",") if part.strip()]
    elif isinstance(event_types_cfg, list):
        event_types = [str(part).strip() for part in event_types_cfg if str(part).strip()]
    else:
        event_types = []
    result = _call_aiwerk_bridge_tool(
        config,
        server=server,
        tool="get_events",
        params={
            "calendar_id": calendar_id,
            "user_google_email": user_google_email,
            "time_min": time_min,
            "time_max": time_max,
            "max_results": max_results,
            "event_types": event_types or ["default"],
        },
    )
    items = _parse_google_workspace_event_items(_bridge_result_text(result), account_label=account_label, account_address=account_address)[:max_results]
    return {
        "label": account_label,
        "address": account_address,
        "calendar_id": calendar_id,
        "source": "google_calendar",
        "status": "connected",
        "summary": f"{len(items)} kommende Termine" if items else "Keine kommenden Termine",
        "items": items,
    }


def _merge_calendar_summaries(accounts: list[dict[str, Any]]) -> dict[str, Any]:
    connected = [account for account in accounts if account.get("status") == "connected"]
    items: list[dict[str, Any]] = []
    for account in accounts:
        normalized_account_items: list[dict[str, Any]] = []
        for item in account.get("items") or []:
            if isinstance(item, dict):
                account_address = str(item.get("account_address") or account.get("address") or account.get("label") or "Kalender")
                normalized = _calendar_item_with_open_url(item, account_address=account_address)
                normalized_account_items.append(normalized)
                items.append(normalized)
        if normalized_account_items:
            account["items"] = normalized_account_items
    items.sort(key=lambda item: str(item.get("starts_at") or ""))
    total = len(items)
    account_count = len(accounts)
    if total:
        summary = f"{total} kommende Termine" + (f" in {account_count} Kalendern" if account_count > 1 else "")
    elif connected:
        summary = f"Keine kommenden Termine" + (f" in {account_count} Kalendern" if account_count > 1 else "")
    else:
        summary = "Kalender konnte nicht geprüft werden" if accounts else "Nicht eingerichtet"
    status = "connected" if connected else ("error" if accounts else "not_configured")
    return {"status": status, "summary": summary, "items": items[:5], "accounts": accounts}


def _calendar_summary(config: dict[str, Any] | None = None) -> dict[str, Any]:
    data = _read_optional_json(os.environ.get("AIWERK_CUI_CALENDAR_SUMMARY_JSON"))
    if data:
        raw_items = data.get("items") if isinstance(data.get("items"), list) else []
        items = [_calendar_item_with_open_url(item) for item in raw_items if isinstance(item, dict)]
        accounts = data.get("accounts") if isinstance(data.get("accounts"), list) else []
        for account in accounts:
            if isinstance(account, dict):
                account_items = account.get("items") if isinstance(account.get("items"), list) else []
                account_address = str(account.get("address") or account.get("label") or "Kalender")
                account["items"] = [_calendar_item_with_open_url(item, account_address=account_address) for item in account_items if isinstance(item, dict)]
        return {
            "status": data.get("status") or "connected",
            "summary": data.get("summary") or (f"{len(items)} kommende Termine" if items else "Keine Termine heute"),
            "items": items[:5],
            "accounts": accounts,
        }
    account_cfgs = _calendar_account_configs(config)
    if account_cfgs:
        summaries: list[dict[str, Any]] = []
        for account_cfg in account_cfgs:
            try:
                summary = _google_workspace_calendar_summary(config, account_cfg)
            except Exception as exc:
                _log.debug("CUI Google Workspace calendar summary failed: %s", exc)
                label = _email_account_label(account_cfg, "Google Kalender")
                address = _email_account_address(account_cfg, label)
                summary = {"label": label, "address": address, "source": "google_calendar", "status": "error", "summary": "Kalender konnte nicht geprüft werden", "items": []}
            if summary:
                summaries.append(summary)
        if summaries:
            return _merge_calendar_summaries(summaries)
    return {"status": "not_configured", "summary": "Nicht eingerichtet", "items": [], "accounts": []}


_AIWERK_BRIDGE_SUBSERVER_LABELS = {
    "coinmarketcap": "CoinMarketCap",
    "firecrawl": "Firecrawl",
    "google-maps": "Google Maps",
    "google-workspace-aiwerk": "Google Workspace AIWerk",
    "google-workspace-demo": "Google Workspace Demo",
    "grok": "Grok",
    "serpapi": "SerpAPI",
    "smallinvoice": "Smallinvoice",
    "vault": "Vault",
}

_AIWERK_BRIDGE_SUBSERVER_DESCRIPTIONS = {
    "coinmarketcap": "Krypto-Marktdaten",
    "firecrawl": "Webseiten auslesen",
    "google-maps": "Orte und Routen",
    "google-workspace-aiwerk": "Gmail, Kalender und Drive",
    "google-workspace-demo": "Gmail, Kalender und Drive",
    "grok": "xAI und X-Suche",
    "serpapi": "Websuche und SERP-Daten",
    "smallinvoice": "Offerten und Rechnungen",
    "vault": "Sichere Zugangsdaten",
}

_AIWERK_BRIDGE_CATALOG_SLUGS = {
    "google-workspace-aiwerk": "google-workspace",
    "google-workspace-demo": "google-workspace",
}


def _aiwerk_bridge_subserver_label(name: str, details: dict[str, Any] | None = None) -> str:
    if details and details.get("label"):
        return str(details["label"])
    clean = str(name or "").strip()
    return _AIWERK_BRIDGE_SUBSERVER_LABELS.get(clean, clean.replace("_", " ").replace("-", " ").title())


def _aiwerk_bridge_subserver_description(name: str, details: dict[str, Any] | None = None) -> str:
    if details:
        for key in ("description", "summary"):
            value = str(details.get(key) or "").strip()
            if value:
                return value
    clean = str(name or "").strip()
    if clean.startswith("google-workspace-"):
        return "Gmail, Kalender und Drive"
    return _AIWERK_BRIDGE_SUBSERVER_DESCRIPTIONS.get(clean, "MCP-Werkzeug über Bridge")


def _aiwerk_bridge_catalog_slug(name: str, details: dict[str, Any] | None = None) -> str:
    if details:
        for key in ("catalog_slug", "catalog"):
            value = str(details.get(key) or "").strip()
            if value:
                return value
    clean = str(name or "").strip()
    if clean.startswith("google-workspace-"):
        return "google-workspace"
    return _AIWERK_BRIDGE_CATALOG_SLUGS.get(clean, clean)


def _default_aiwerk_bridge_subservers() -> list[dict[str, Any]]:
    return [_aiwerk_bridge_subserver_item(name) for name in _AIWERK_BRIDGE_SUBSERVER_LABELS]


def _aiwerk_bridge_subserver_item(name: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    clean_name = str(name or "").strip()
    item = {
        "id": f"aiwerk-bridge-{_safe_resource_id(clean_name)}",
        "label": _aiwerk_bridge_subserver_label(clean_name, details),
        "status": "connected",
        "status_label": _resource_status_label("connected"),
        "capabilities": ["Bridge-Subserver"],
        "description": _aiwerk_bridge_subserver_description(clean_name, details),
    }
    if clean_name:
        catalog_slug = _aiwerk_bridge_catalog_slug(clean_name, details)
        if catalog_slug:
            item["open_url"] = f"https://aiwerkmcp.com/#/catalog/{urllib.parse.quote(catalog_slug, safe='-_.~')}"
    return item


def _aiwerk_bridge_live_subservers(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Ask the AIWerk Bridge router for its current server list.

    The bridge's configured subserver set changes independently from the local
    Hermes config, so the CUI should prefer the live router inventory and only
    fall back to static/configured names when the bridge cannot be reached.
    """
    response = _mcp_bridge_router_call(config, {"action": "status"})
    result = response.get("result") if isinstance(response, dict) else None
    content = result.get("content") if isinstance(result, dict) else None
    for item in content if isinstance(content, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            continue
        try:
            payload = json.loads(item["text"])
        except Exception:
            continue
        servers = payload.get("servers") if isinstance(payload, dict) else None
        if not isinstance(servers, list):
            continue
        children: list[dict[str, Any]] = []
        for server in servers:
            if isinstance(server, dict):
                name = str(server.get("name") or "").strip()
                enabled = server.get("enabled", True)
            else:
                name = str(server or "").strip()
                enabled = True
            if not name or enabled is False:
                continue
            children.append(_aiwerk_bridge_subserver_item(name, server if isinstance(server, dict) else None))
        if children:
            return children
    return []


def _aiwerk_bridge_subservers(config: dict[str, Any], bridge_details: dict[str, Any]) -> list[dict[str, Any]]:
    raw_servers = bridge_details.get("subservers") or bridge_details.get("servers")
    if raw_servers is None:
        dashboard = config.get("dashboard")
        if isinstance(dashboard, dict):
            bridge_cfg = dashboard.get("aiwerk_bridge") or dashboard.get("bridge")
            if isinstance(bridge_cfg, dict):
                raw_servers = bridge_cfg.get("subservers") or bridge_cfg.get("servers")

    children: list[dict[str, Any]] = []
    if isinstance(raw_servers, dict):
        iterable = raw_servers.items()
    elif isinstance(raw_servers, list):
        iterable = [(str(item.get("name") if isinstance(item, dict) else item), item) for item in raw_servers]
    else:
        try:
            live_children = _aiwerk_bridge_live_subservers(config)
            if live_children:
                return live_children
        except Exception as exc:
            _log.debug("CUI AIWerk Bridge live subserver inventory failed: %s", exc)
        return _default_aiwerk_bridge_subservers()

    for name, raw in iterable:
        details = raw if isinstance(raw, dict) else {}
        if details.get("enabled", True) is False:
            continue
        children.append(_aiwerk_bridge_subserver_item(str(name), details))
    return children


def _connector_summary(config: dict[str, Any], shared_folder: dict[str, Any], email: dict[str, Any], calendar: dict[str, Any]) -> list[dict[str, Any]]:
    del shared_folder, email, calendar
    connectors: list[dict[str, Any]] = []

    def add_connector(connector_id: str, label: str, capabilities: list[str] | None = None, children: list[dict[str, Any]] | None = None) -> None:
        item = {
            "id": connector_id,
            "label": label,
            "status": "connected",
            "status_label": _resource_status_label("connected"),
            "capabilities": capabilities or ["MCP"],
        }
        if children:
            item["children"] = children
        connectors.append(item)

    mcp_servers = config.get("mcp_servers")
    label_map = {
        "aiwerk_bridge": "AIWerk Bridge",
        "elevenlabs": "ElevenLabs",
        "hermes_neo4j": "Wissensbasis",
    }
    if isinstance(mcp_servers, dict):
        for name, raw in sorted(mcp_servers.items()):
            details = raw if isinstance(raw, dict) else {}
            if details.get("enabled", True) is False:
                continue
            label = label_map.get(str(name), str(name).replace("_", " ").title())
            capabilities = ["MCP"]
            if details.get("url"):
                capabilities.append("Remote")
            if details.get("command"):
                capabilities.append("Lokal")
            children = _aiwerk_bridge_subservers(config, details) if str(name) == "aiwerk_bridge" else None
            add_connector(f"mcp-{_safe_resource_id(str(name))}", label, capabilities, children)

    return connectors


_ASSISTANT_RESOURCE_CACHE_TTLS = {
    "email": 60 * 60,
    "calendar": 30 * 60,
    "shared_folder": 60 * 60,
    "vault": 15 * 60,
    "todos": 60,
    "contacts": 30 * 60,
    "connectors": 60 * 60,
}
_ASSISTANT_RESOURCE_CACHE: dict[str, dict[str, Any]] = {}
_ASSISTANT_RESOURCE_CACHE_LOCK = threading.Lock()
_ASSISTANT_RESOURCE_REFRESHING: set[str] = set()
_ASSISTANT_RESOURCE_REFRESHING_LOCK = threading.Lock()
_ASSISTANT_RESOURCE_CACHE_ENV_KEYS = (
    "AIWERK_CUI_SHARED_FOLDER",
    "AIWERK_CUI_EMAIL_SUMMARY_JSON",
    "AIWERK_CUI_CALENDAR_SUMMARY_JSON",
    "AIWERK_CUI_VAULT_SUMMARY_JSON",
    "AIWERK_CUI_VAULT_URL",
    "AIWERK_CUI_TODO_PATH",
    "AIWERK_CUI_CONTACTS_JSON",
    "AIWERK_CUI_CONTACTS_DISABLE_AIWERK_BRIDGE",
    "AIWERK_CUI_CONTACTS_PAGE_SIZE",
    "HERMES_CUI_ALLOW_REMOTE_FILE_MANAGER_OPEN",
)


def _assistant_resource_config_signature(config: dict[str, Any], request: Request | None) -> str:
    request_scope = "local" if _request_looks_local(request) else "remote"
    env_scope = {key: os.environ.get(key, "") for key in _ASSISTANT_RESOURCE_CACHE_ENV_KEYS}
    raw = {"config": config, "env": env_scope, "request_scope": request_scope}
    try:
        return json.dumps(raw, sort_keys=True, default=str)
    except TypeError:
        return repr(raw)


def _assistant_write_resource_cache(full_key: str, payload: Any, ttl_seconds: int) -> dict[str, Any]:
    updated_at_ts = time.time()
    expires_at_ts = updated_at_ts + ttl_seconds
    meta = {
        "cached": False,
        "updated_at": datetime.fromtimestamp(updated_at_ts, timezone.utc).isoformat().replace("+00:00", "Z"),
        "expires_at": datetime.fromtimestamp(expires_at_ts, timezone.utc).isoformat().replace("+00:00", "Z"),
        "ttl_seconds": ttl_seconds,
    }
    with _ASSISTANT_RESOURCE_CACHE_LOCK:
        _ASSISTANT_RESOURCE_CACHE[full_key] = {
            "payload": copy.deepcopy(payload),
            "updated_at": meta["updated_at"],
            "expires_at": meta["expires_at"],
            "expires_at_ts": expires_at_ts,
        }
    return meta


def _assistant_schedule_resource_refresh(full_key: str, builder, ttl_seconds: int) -> bool:
    with _ASSISTANT_RESOURCE_REFRESHING_LOCK:
        if full_key in _ASSISTANT_RESOURCE_REFRESHING:
            return False
        _ASSISTANT_RESOURCE_REFRESHING.add(full_key)

    def run() -> None:
        try:
            payload = builder()
            _assistant_write_resource_cache(full_key, payload, ttl_seconds)
        except Exception:
            _log.exception("Background assistant resource refresh failed for %s", full_key.split(":", 1)[0])
        finally:
            with _ASSISTANT_RESOURCE_REFRESHING_LOCK:
                _ASSISTANT_RESOURCE_REFRESHING.discard(full_key)

    threading.Thread(target=run, name=f"assistant-resource-refresh-{full_key.split(':', 1)[0]}", daemon=True).start()
    return True


def _assistant_cached_resource(
    name: str,
    ttl_seconds: int,
    cache_key: str,
    builder,
    *,
    force_refresh: bool = False,
    stale_while_revalidate: bool = False,
    initial_payload: Any | None = None,
) -> tuple[Any, dict[str, Any]]:
    now = time.time()
    full_key = f"{name}:{cache_key}"
    with _ASSISTANT_RESOURCE_CACHE_LOCK:
        entry = _ASSISTANT_RESOURCE_CACHE.get(full_key)
        is_fresh = bool(entry and now < float(entry.get("expires_at_ts", 0)))
        if entry and not force_refresh and is_fresh:
            payload = copy.deepcopy(entry["payload"])
            return payload, {
                "cached": True,
                "updated_at": entry["updated_at"],
                "expires_at": entry["expires_at"],
                "ttl_seconds": ttl_seconds,
            }
        if entry and stale_while_revalidate:
            payload = copy.deepcopy(entry["payload"])
            scheduled = _assistant_schedule_resource_refresh(full_key, builder, ttl_seconds)
            return payload, {
                "cached": True,
                "stale": True,
                "refreshing": scheduled,
                "updated_at": entry["updated_at"],
                "expires_at": entry["expires_at"],
                "ttl_seconds": ttl_seconds,
            }
        if not entry and stale_while_revalidate and initial_payload is not None:
            scheduled = _assistant_schedule_resource_refresh(full_key, builder, ttl_seconds)
            return copy.deepcopy(initial_payload), {
                "cached": False,
                "stale": True,
                "refreshing": scheduled,
                "updated_at": None,
                "expires_at": None,
                "ttl_seconds": ttl_seconds,
            }

    try:
        payload = builder()
    except Exception as exc:
        with _ASSISTANT_RESOURCE_CACHE_LOCK:
            entry = _ASSISTANT_RESOURCE_CACHE.get(full_key)
        if entry:
            payload = copy.deepcopy(entry["payload"])
            if isinstance(payload, dict):
                payload["last_error"] = "Aktualisierung fehlgeschlagen"
            return payload, {
                "cached": True,
                "stale": True,
                "updated_at": entry["updated_at"],
                "expires_at": entry["expires_at"],
                "ttl_seconds": ttl_seconds,
                "last_error": "Aktualisierung fehlgeschlagen",
            }
        raise exc

    meta = _assistant_write_resource_cache(full_key, payload, ttl_seconds)
    return payload, meta


def _assistant_invalidate_resource_cache(name: str) -> None:
    prefix = f"{name}:"
    with _ASSISTANT_RESOURCE_CACHE_LOCK:
        for key in list(_ASSISTANT_RESOURCE_CACHE.keys()):
            if key.startswith(prefix):
                _ASSISTANT_RESOURCE_CACHE.pop(key, None)


def _dashboard_section(config: dict[str, Any], key: str) -> dict[str, Any]:
    dashboard = config.get("dashboard")
    if isinstance(dashboard, dict):
        value = dashboard.get(key)
        if isinstance(value, dict):
            return value
    assistant = config.get("assistant")
    if isinstance(assistant, dict):
        value = assistant.get(key)
        if isinstance(value, dict):
            return value
    value = config.get(key)
    return value if isinstance(value, dict) else {}


def _vault_url_from_config(config: dict[str, Any]) -> str:
    vault_cfg = _dashboard_section(config, "vault")
    raw = os.environ.get("AIWERK_CUI_VAULT_URL") or vault_cfg.get("url") or vault_cfg.get("vault_url")
    url = str(raw or "https://pass.aiwerk.ch").strip()
    return url if url.startswith(("https://", "http://")) else "https://pass.aiwerk.ch"


def _run_json_command(command: list[str], *, timeout: int = 8) -> Any:
    env = os.environ.copy()
    env["BW_NOINTERACTION"] = "true"
    completed = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        env=env,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("command failed")
    return json.loads(completed.stdout or "null")


def _password_looks_weak(password: str) -> bool:
    if len(password) < 12:
        return True
    classes = 0
    classes += any(ch.islower() for ch in password)
    classes += any(ch.isupper() for ch in password)
    classes += any(ch.isdigit() for ch in password)
    classes += any(not ch.isalnum() for ch in password)
    return classes < 3


def _parse_bridge_json_payload(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    text = _bridge_result_text(value)
    if text:
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    nested = value.get("result")
    if isinstance(nested, dict):
        content = nested.get("content")
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    try:
                        parsed = json.loads(item["text"])
                        if isinstance(parsed, dict):
                            return parsed
                    except Exception:
                        continue
    return value


def _vault_base_summary(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "not_configured",
        "vault_url": _vault_url_from_config(config),
        "summary": "Tresor noch nicht eingerichtet",
        "item_count": None,
        "weak_count": None,
        "reused_count": None,
        "compromised_count": None,
        "compromised_supported": False,
        "two_factor_status": "unknown",
        "checked_at": _utc_now_iso(),
        "source": "none",
    }


def _vault_web_url(value: Any, fallback: str = "https://pass.aiwerk.ch") -> str:
    raw = str(value or fallback).strip()
    if not raw.startswith(("https://", "http://")):
        return fallback
    parsed = urllib.parse.urlparse(raw)
    path = re.sub(r"/api/?$", "", parsed.path or "")
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, path or "", "", "", "")) or fallback


def _vault_bridge_summary(config: dict[str, Any], base: dict[str, Any]) -> dict[str, Any] | None:
    if not _mcp_bridge_config(config):
        return None
    try:
        raw_health = _call_aiwerk_bridge_tool(config, server="vault", tool="health_check", params={})
    except Exception:
        return None
    health = _parse_bridge_json_payload(raw_health)
    if not isinstance(health, dict) or not health:
        return None

    vault_url = _vault_web_url(health.get("vault_url"), str(base.get("vault_url") or "https://pass.aiwerk.ch"))
    authenticated = bool(health.get("authenticated"))
    status_text = str(health.get("status") or "").lower()
    exposed_visible = bool(health.get("exposed_collection_visible"))
    agent_visible = bool(health.get("agent_created_collection_visible"))
    exposed_count = int(health.get("items_in_exposed") or 0)
    agent_count = int(health.get("items_in_agent_created") or 0)
    item_count = exposed_count + agent_count

    if status_text != "ok" or not authenticated:
        return {**base, "status": "auth_required", "vault_url": vault_url, "summary": "Tresor-Anmeldung über Bridge nötig", "source": "aiwerk_bridge"}
    if not exposed_visible or not agent_visible:
        missing = []
        if not exposed_visible:
            missing.append("mcp-exposed")
        if not agent_visible:
            missing.append("mcp-agent-created")
        return {
            **base,
            "status": "limited",
            "vault_url": vault_url,
            "summary": f"Tresor verbunden · Collection fehlt: {', '.join(missing)}",
            "item_count": item_count,
            "agent_created_count": agent_count,
            "source": "aiwerk_bridge",
        }

    summary = f"{exposed_count} freigegebene Zugangsdaten"
    if agent_count:
        summary += f" · {agent_count} von Agent erstellt"
    elif exposed_count == 0:
        summary = "Tresor verbunden · keine freigegebenen Einträge"
    return {
        **base,
        "status": "connected",
        "vault_url": vault_url,
        "summary": summary,
        "item_count": item_count,
        "exposed_count": exposed_count,
        "agent_created_count": agent_count,
        "weak_count": None,
        "reused_count": None,
        "compromised_count": None,
        "compromised_supported": False,
        "source": "aiwerk_bridge",
    }


def _vault_local_bw_summary(config: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
    vault_url = str(base.get("vault_url") or _vault_url_from_config(config))
    bw = shutil.which("bw")
    if not bw:
        return {**base, "status": "limited", "summary": "Tresor-Link verfügbar", "source": "link"}

    try:
        status = _run_json_command([bw, "status"], timeout=5)
    except Exception:
        return {**base, "status": "error", "summary": "Tresor konnte nicht geprüft werden", "source": "bw"}

    if isinstance(status, dict):
        server_url = status.get("serverUrl")
        if isinstance(server_url, str) and server_url.startswith(("https://", "http://")):
            vault_url = _vault_web_url(server_url, vault_url)
        bw_status = str(status.get("status") or "").lower()
    else:
        bw_status = ""

    if bw_status != "unlocked":
        label = "Tresor gesperrt" if bw_status == "locked" else "Anmeldung im Tresor nötig"
        return {**base, "status": "auth_required", "vault_url": vault_url, "summary": label, "source": "bw"}

    try:
        items = _run_json_command([bw, "list", "items"], timeout=20)
    except Exception:
        return {**base, "status": "limited", "vault_url": vault_url, "summary": "Tresor verbunden · Statistik nicht verfügbar", "source": "bw"}
    if not isinstance(items, list):
        items = []

    passwords: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        login = item.get("login")
        if isinstance(login, dict):
            password = login.get("password")
            if isinstance(password, str) and password:
                passwords.append(password)
    weak_count = sum(1 for password in passwords if _password_looks_weak(password))
    password_counts: dict[str, int] = {}
    for password in passwords:
        password_counts[password] = password_counts.get(password, 0) + 1
    reused_count = sum(count for count in password_counts.values() if count > 1)
    hint_count = weak_count + reused_count
    summary = f"{len(items)} Zugangsdaten"
    summary += f" · {hint_count} Hinweise" if hint_count else " · Alles in Ordnung"
    return {
        **base,
        "status": "limited" if hint_count else "connected",
        "vault_url": vault_url,
        "summary": summary,
        "item_count": len(items),
        "weak_count": weak_count,
        "reused_count": reused_count,
        "source": "bw",
    }


def _vaultwarden_summary(config: dict[str, Any]) -> dict[str, Any]:
    override = os.environ.get("AIWERK_CUI_VAULT_SUMMARY_JSON")
    if override:
        try:
            data = json.loads(override)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass

    base = _vault_base_summary(config)
    bridge = _vault_bridge_summary(config, base)
    if bridge is not None:
        return bridge
    return _vault_local_bw_summary(config, base)


def _todo_path_from_config(config: dict[str, Any]) -> Path:
    todos_cfg = _dashboard_section(config, "todos")
    raw = os.environ.get("AIWERK_CUI_TODO_PATH") or todos_cfg.get("path") or todos_cfg.get("todo_path")
    if raw:
        return Path(str(raw)).expanduser()
    return get_hermes_home() / "TODO.md"


def _clean_todo_text(text: str) -> str:
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()[:180]


def _todo_summary(config: dict[str, Any]) -> dict[str, Any]:
    path = _todo_path_from_config(config)
    base = {
        "status": "not_configured",
        "summary": "TODO.md noch nicht angelegt",
        "path": str(path),
        "items": [],
        "open_count": 0,
        "done_count": 0,
        "total_count": 0,
        "checked_at": _utc_now_iso(),
    }
    try:
        if not path.exists() or not path.is_file():
            return base
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return {**base, "status": "error", "summary": "TODO.md konnte nicht gelesen werden"}

    items: list[dict[str, Any]] = []
    open_count = 0
    done_count = 0
    for index, line in enumerate(lines, start=1):
        match = re.match(r"^\s*[-*]\s+\[([ xX])\]\s+(.+?)\s*$", line)
        if not match:
            continue
        done = match.group(1).lower() == "x"
        text = _clean_todo_text(match.group(2))
        if not text:
            continue
        if done:
            done_count += 1
        else:
            open_count += 1
        if not done and len(items) < 8:
            items.append({"id": f"todo-{index}", "text": text, "line": index, "done": False})
    total_count = open_count + done_count
    if total_count == 0:
        summary = "Keine Aufgaben in TODO.md"
    elif open_count == 0:
        summary = f"{done_count} erledigt · nichts offen"
    else:
        summary = f"{open_count} offen"
        if done_count:
            summary += f" · {done_count} erledigt"
    return {
        **base,
        "status": "connected",
        "summary": summary,
        "items": items,
        "open_count": open_count,
        "done_count": done_count,
        "total_count": total_count,
    }




def _assistant_display_name_from_config(config: dict[str, Any]) -> str:
    """Resolve the customer-facing assistant name for the AIWerk CUI."""
    candidates: list[Any] = [
        os.environ.get("AIWERK_CUI_AGENT_NAME"),
        os.environ.get("HERMES_AGENT_NAME"),
    ]
    for section_name in ("assistant", "dashboard", "aiwerk", "branding"):
        section = config.get(section_name)
        if isinstance(section, dict):
            for key in ("agent_name", "assistant_name", "display_name", "name"):
                candidates.append(section.get(key))
    display = config.get("display")
    if isinstance(display, dict):
        for key in ("agent_name", "assistant_name"):
            candidates.append(display.get(key))
        skin_name = display.get("skin")
        if isinstance(skin_name, str) and skin_name.strip():
            try:
                from hermes_cli.skin_engine import load_skin
                candidates.append(load_skin(skin_name.strip()).get_branding("agent_name", ""))
            except Exception:
                pass
    for raw in candidates:
        if isinstance(raw, str):
            value = re.sub(r"\s+", " ", raw).strip()
            if value and "{{" not in value and "}}" not in value:
                return value[:80]
    return "Agent"


def _assistant_support_section(config: dict[str, Any]) -> dict[str, Any]:
    section = _dashboard_section(config, "support")
    return section if isinstance(section, dict) else {}


def _support_log_path(config: dict[str, Any], support_cfg: dict[str, Any]) -> Path:
    raw = support_cfg.get("local_log") or support_cfg.get("log_path") or os.environ.get("AIWERK_CUI_SUPPORT_LOG")
    if raw:
        return Path(str(raw)).expanduser()
    return get_hermes_home() / "aiwerk-support" / "inbox.jsonl"


def _safe_support_text(value: Any, *, max_len: int = 2000) -> str:
    text = _redact_sensitive_text(str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_len:
        text = text[:max_len].rstrip() + "…"
    return text


def _safe_support_multiline(value: Any, *, max_len: int = 4000) -> str:
    text = _redact_sensitive_text(str(value or ""))
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    if len(text) > max_len:
        text = text[:max_len].rstrip() + "…"
    return text


def _safe_support_diagnostics(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    allowed_scalar = (str, int, float, bool, type(None))
    result: dict[str, Any] = {}
    for key, raw in value.items():
        safe_key = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(key or "")).strip("_")[:60]
        if not safe_key:
            continue
        if isinstance(raw, allowed_scalar):
            result[safe_key] = _safe_support_text(raw, max_len=500) if isinstance(raw, str) else raw
        elif isinstance(raw, dict):
            nested: dict[str, Any] = {}
            for nkey, nraw in list(raw.items())[:20]:
                nested_key = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(nkey or "")).strip("_")[:60]
                if nested_key and isinstance(nraw, allowed_scalar):
                    nested[nested_key] = _safe_support_text(nraw, max_len=300) if isinstance(nraw, str) else nraw
            result[safe_key] = nested
        elif isinstance(raw, list):
            safe_list: list[Any] = []
            for item in raw[:20]:
                if isinstance(item, allowed_scalar):
                    safe_list.append(_safe_support_text(item, max_len=300) if isinstance(item, str) else item)
                elif isinstance(item, dict):
                    safe_list.append(_safe_support_diagnostics(item))
            result[safe_key] = safe_list
    encoded = json.dumps(result, ensure_ascii=False)
    if len(encoded) > 6000:
        return {"summary": encoded[:6000] + "…"}
    return result


def _telegram_target_from_chat_id(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.startswith("telegram:"):
        return raw
    return f"telegram:{raw}"


def _explicit_delivery_targets(raw: Any) -> list[str]:
    if isinstance(raw, str):
        candidates = [part.strip() for part in raw.split(",") if part.strip()]
    elif isinstance(raw, list):
        candidates = [str(part).strip() for part in raw if str(part).strip()]
    else:
        candidates = []
    # Do not fall back to the Hermes gateway home channel. A bare "telegram"
    # target would send to the normal chat, so it is deliberately ignored here.
    return [target for target in candidates if target != "telegram"]


def _support_delivery_targets(support_cfg: dict[str, Any]) -> list[str]:
    dedicated_chat_id = (
        support_cfg.get("telegram_chat_id")
        or support_cfg.get("support_telegram_chat_id")
        or os.environ.get("AIWERK_SUPPORT_TELEGRAM_CHAT_ID")
        or os.environ.get("AIWERK_CUI_SUPPORT_TELEGRAM_CHAT_ID")
    )
    dedicated_target = _telegram_target_from_chat_id(dedicated_chat_id)
    if dedicated_target:
        return [dedicated_target]
    return _explicit_delivery_targets(
        support_cfg.get("delivery_targets")
        or support_cfg.get("delivery_target")
        or os.environ.get("AIWERK_CUI_SUPPORT_TARGET")
    )


def _system_delivery_targets(config: dict[str, Any]) -> list[str]:
    dashboard = config.get("dashboard") if isinstance(config, dict) else {}
    notifications = dashboard.get("notifications") if isinstance(dashboard, dict) else {}
    notifications = notifications if isinstance(notifications, dict) else {}
    dedicated_chat_id = (
        notifications.get("telegram_chat_id")
        or notifications.get("system_telegram_chat_id")
        or os.environ.get("AIWERK_SYSTEM_TELEGRAM_CHAT_ID")
    )
    dedicated_target = _telegram_target_from_chat_id(dedicated_chat_id)
    if dedicated_target:
        return [dedicated_target]
    return _explicit_delivery_targets(
        notifications.get("delivery_targets")
        or notifications.get("delivery_target")
        or os.environ.get("AIWERK_SYSTEM_TARGET")
    )


def _format_support_message(record: dict[str, Any]) -> str:
    diagnostics = record.get("diagnostics") if isinstance(record.get("diagnostics"), dict) else {}
    lines = [
        "AIWerk Supportmeldung",
        "",
        f"Support-ID: {record.get('support_id')}",
        f"Kategorie: {record.get('category') or 'Sonstiges'}",
        f"Agent: {record.get('agent_name') or 'Agent'}",
        f"Session: {record.get('session_title') or 'Aktuelle Sitzung'}",
        f"Zeitpunkt: {record.get('created_at')}",
        "",
        "Nachricht:",
        str(record.get("message") or ""),
    ]
    if diagnostics:
        lines.extend(["", "Status:"])
        for key in ("connection", "email", "calendar", "shared_folder", "vault", "todos", "connectors"):
            if key in diagnostics:
                value = diagnostics[key]
                if isinstance(value, dict):
                    compact = ", ".join(f"{k}: {v}" for k, v in value.items() if v not in (None, ""))
                    lines.append(f"- {key}: {compact or '—'}")
                else:
                    lines.append(f"- {key}: {value}")
    return "\n".join(lines)


def _deliver_support_message(targets: list[str], text: str) -> tuple[bool, list[str]]:
    errors: list[str] = []
    delivered = False
    try:
        from tools.send_message_tool import send_message_tool
    except Exception as exc:
        return False, [f"send_message unavailable: {_safe_support_text(exc, max_len=200)}"]
    for target in targets:
        try:
            raw = send_message_tool({"action": "send", "target": target, "message": text})
            parsed = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(parsed, dict) and parsed.get("error"):
                errors.append(f"{target}: {_safe_support_text(parsed.get('error'), max_len=300)}")
            else:
                delivered = True
        except Exception as exc:
            errors.append(f"{target}: {_safe_support_text(exc, max_len=300)}")
    return delivered, errors


def _handle_assistant_support(payload: Any, request: Request | None = None) -> dict[str, Any]:
    config = load_config()
    support_cfg = _assistant_support_section(config)
    if support_cfg.get("enabled") is False:
        raise HTTPException(status_code=404, detail="Support is not enabled")
    message = _safe_support_multiline(payload.message)
    if not message:
        raise HTTPException(status_code=400, detail="Support message is required")
    now = _utc_now_iso()
    support_id = "sup_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_") + secrets.token_hex(3)
    diagnostics = _safe_support_diagnostics(payload.diagnostics if payload.include_diagnostics else {})
    if payload.connection:
        diagnostics.setdefault("connection", _safe_support_text(payload.connection, max_len=80))
    agent_name = _safe_support_text(payload.agent_name, max_len=80) or _assistant_display_name_from_config(config)
    record = {
        "support_id": support_id,
        "created_at": now,
        "category": _safe_support_text(payload.category or "Sonstiges", max_len=120),
        "agent_name": agent_name,
        "message": message,
        "session_id": _safe_support_text(payload.session_id, max_len=160),
        "session_title": _safe_support_text(payload.session_title, max_len=200),
        "page_url": _safe_support_text(payload.page_url, max_len=500),
        "user_agent": _safe_support_text(payload.user_agent, max_len=500),
        "diagnostics": diagnostics,
    }
    log_path = _support_log_path(config, support_cfg)
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception:
        _log.exception("Could not write AIWerk support log")
        raise HTTPException(status_code=500, detail="Support message could not be saved")
    targets = _support_delivery_targets(support_cfg)
    delivered, errors = _deliver_support_message(targets, _format_support_message(record))
    if errors:
        _log.warning("AIWerk support delivery issues for %s: %s", support_id, "; ".join(errors))
    return {"ok": True, "support_id": support_id, "delivered": delivered, "queued": not delivered, "errors": errors[:3]}


def _update_todo_item_done(config: dict[str, Any], item_id: str, done: bool) -> dict[str, Any]:
    match = re.fullmatch(r"todo-(\d+)", str(item_id or "").strip())
    if not match:
        raise HTTPException(status_code=400, detail="Invalid todo id")
    line_number = int(match.group(1))
    path = _todo_path_from_config(config)
    try:
        if not path.exists() or not path.is_file():
            raise HTTPException(status_code=404, detail="TODO.md not found")
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="TODO.md could not be read")
    if line_number < 1 or line_number > len(lines):
        raise HTTPException(status_code=404, detail="Todo item not found")
    line = lines[line_number - 1]
    checkbox = re.match(r"^(\s*[-*]\s+\[)([ xX])(\]\s+.+?\s*)$", line)
    if not checkbox:
        raise HTTPException(status_code=400, detail="Todo line is not a markdown checkbox")
    marker = "x" if done else " "
    lines[line_number - 1] = f"{checkbox.group(1)}{marker}{checkbox.group(3)}"
    try:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        raise HTTPException(status_code=500, detail="TODO.md could not be updated")
    _assistant_invalidate_resource_cache("todos")
    return _todo_summary(config)


def _add_todo_item(config: dict[str, Any], text: str) -> dict[str, Any]:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if not text:
        raise HTTPException(status_code=400, detail="Todo text is required")
    if len(text) > 240:
        text = text[:240].rstrip()
    path = _todo_path_from_config(config)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        separator = "" if not existing or existing.endswith("\n") else "\n"
        path.write_text(f"{existing}{separator}- [ ] {text}\n", encoding="utf-8")
    except Exception:
        raise HTTPException(status_code=500, detail="TODO.md could not be updated")
    _assistant_invalidate_resource_cache("todos")
    return _todo_summary(config)



def _assistant_contacts_store_path() -> Path:
    return get_hermes_home() / "cui_contacts.json"


def _safe_contact_text(value: Any, limit: int = 160) -> str:
    text = str(value or "").strip()
    if "=?" in text and "?=" in text:
        try:
            text = str(email.header.make_header(email.header.decode_header(text))).strip()
        except Exception:
            pass
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _safe_contact_email(value: Any) -> str:
    text = _safe_contact_text(value, 254)
    match = re.search(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", text, re.I)
    return match.group(0).lower() if match else ""


def _safe_contact_phone(value: Any) -> str:
    text = _safe_contact_text(value, 80)
    return text if re.search(r"\d", text) else ""


def _contact_id_for(contact: dict[str, Any]) -> str:
    seed = "|".join([
        str(contact.get("email") or ""),
        str(contact.get("phone") or ""),
        str(contact.get("display_name") or contact.get("name") or ""),
    ])
    return _safe_resource_id(seed or secrets.token_hex(6), "contact")


def _dedupe_contact_badges(values: Iterable[Any], *, limit: int = 4) -> list[str]:
    badges: list[str] = []
    seen: set[str] = set()
    for value in values:
        badge = _safe_contact_text(value, 40)
        if not badge:
            continue
        key = badge.casefold()
        if key in seen:
            continue
        seen.add(key)
        badges.append(badge)
        if len(badges) >= limit:
            break
    return badges


def _normalize_contact(raw: dict[str, Any], *, source: str = "Manuell", relevance: str = "frequent") -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    emails = raw.get("emails") if isinstance(raw.get("emails"), list) else []
    phones = raw.get("phones") if isinstance(raw.get("phones"), list) else []
    display_name = _safe_contact_text(raw.get("display_name") or raw.get("name") or raw.get("label"), 120)
    email_addr = _safe_contact_email(raw.get("email") or (emails[0] if emails else ""))
    phone = _safe_contact_phone(raw.get("phone") or (phones[0] if phones else ""))
    if not display_name and email_addr:
        display_name = email_addr.split("@", 1)[0].replace(".", " ").title()
    if not display_name and not email_addr and not phone:
        return None
    raw_source_badges = raw.get("source_badges")
    source_badges = raw_source_badges if isinstance(raw_source_badges, list) else []
    badge = _safe_contact_text(raw.get("source") or source, 40)
    badges = _dedupe_contact_badges([*source_badges, badge])
    contact = {
        "id": _safe_contact_text(raw.get("id"), 80),
        "display_name": display_name,
        "organization": _safe_contact_text(raw.get("organization") or raw.get("company"), 120),
        "role": _safe_contact_text(raw.get("role") or raw.get("title"), 120),
        "email": email_addr,
        "phone": phone,
        "note": _safe_contact_text(raw.get("note"), 240),
        "source_badges": badges[:4],
        "relevance": _safe_contact_text(raw.get("relevance") or relevance, 40),
    }
    try:
        interaction_count = int(raw.get("interaction_count") or 0)
    except Exception:
        interaction_count = 0
    try:
        interaction_score = float(raw.get("interaction_score") or 0)
    except Exception:
        interaction_score = 0.0
    if interaction_count > 0:
        contact["interaction_count"] = interaction_count
    if interaction_score > 0:
        contact["interaction_score"] = round(interaction_score, 2)
    last_interaction_at = _safe_contact_text(raw.get("last_interaction_at"), 80)
    if last_interaction_at:
        contact["last_interaction_at"] = last_interaction_at
    if not contact["id"]:
        contact["id"] = _contact_id_for(contact)
    return contact


def _dedupe_contacts(contacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for contact in contacts:
        key = (contact.get("email") or contact.get("phone") or contact.get("display_name") or contact.get("id") or "").lower()
        if not key:
            continue
        if key not in seen:
            seen[key] = contact
            order.append(key)
            continue
        existing = seen[key]
        for field in ("organization", "role", "email", "phone", "note", "last_interaction_at"):
            if not existing.get(field) and contact.get(field):
                existing[field] = contact[field]
        existing["interaction_count"] = int(existing.get("interaction_count") or 0) + int(contact.get("interaction_count") or 0)
        existing["interaction_score"] = round(float(existing.get("interaction_score") or 0) + float(contact.get("interaction_score") or 0), 2)
        existing["source_badges"] = _dedupe_contact_badges([*(existing.get("source_badges") or []), *(contact.get("source_badges") or [])])
    return [seen[key] for key in order]


def _read_contacts_store_payload() -> dict[str, Any]:
    path = _assistant_contacts_store_path()
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, list):
        return {"contacts": payload}
    return {}


def _write_contacts_store_payload(payload: dict[str, Any]) -> None:
    path = _assistant_contacts_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        path.chmod(0o600)
    except Exception:
        pass


def _read_manual_contacts() -> list[dict[str, Any]]:
    payload = _read_contacts_store_payload()
    raw_contacts = payload.get("contacts")
    if not isinstance(raw_contacts, list):
        return []
    return [contact for contact in (_normalize_contact(item, source="Manuell", relevance="frequent") for item in raw_contacts if isinstance(item, dict)) if contact]


def _write_manual_contacts(contacts: list[dict[str, Any]]) -> None:
    payload = _read_contacts_store_payload()
    payload["contacts"] = contacts
    _write_contacts_store_payload(payload)


def _contact_hide_keys(contact: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    email_addr = _safe_contact_email(contact.get("email"))
    if email_addr:
        keys.add(f"email:{email_addr}")
    contact_id = str(contact.get("id") or "").strip().lower()
    if contact_id:
        keys.add(f"id:{contact_id}")
    phone = _safe_contact_text(contact.get("phone"), 80).lower()
    if phone:
        keys.add(f"phone:{phone}")
    display_name = _contact_search_normalize(contact.get("display_name"))
    if display_name:
        keys.add(f"name:{display_name}")
    return keys


def _read_hidden_contact_keys() -> set[str]:
    payload = _read_contacts_store_payload()
    raw = payload.get("hidden") or payload.get("dismissed") or []
    if not isinstance(raw, list):
        return set()
    return {str(item).strip().lower() for item in raw if str(item).strip()}


def _write_hidden_contact_keys(keys: set[str]) -> None:
    payload = _read_contacts_store_payload()
    payload["hidden"] = sorted({key for key in keys if key})
    _write_contacts_store_payload(payload)


def _filter_hidden_contacts(contacts: list[dict[str, Any]], *, hidden_keys: set[str] | None = None) -> list[dict[str, Any]]:
    hidden = hidden_keys if hidden_keys is not None else _read_hidden_contact_keys()
    if not hidden:
        return contacts
    return [contact for contact in contacts if not (_contact_hide_keys(contact) & hidden)]


def _contacts_from_email_resource(email_resource: dict[str, Any]) -> list[dict[str, Any]]:
    contacts: list[dict[str, Any]] = []
    accounts = email_resource.get("accounts") if isinstance(email_resource, dict) else []
    if not isinstance(accounts, list):
        accounts = []
    for account in accounts:
        if not isinstance(account, dict):
            continue
        for item in account.get("items") or []:
            if not isinstance(item, dict):
                continue
            sender = str(item.get("sender") or "")
            contact = _contact_from_address(sender, source="Aus E-Mail", score=4.0, last_interaction_at=item.get("received_at"), relevance="relevant")
            if contact:
                contacts.append(contact)
    return contacts


def _contacts_from_calendar_resource(calendar_resource: dict[str, Any]) -> list[dict[str, Any]]:
    contacts: list[dict[str, Any]] = []
    accounts = calendar_resource.get("accounts") if isinstance(calendar_resource, dict) else []
    if not isinstance(accounts, list):
        accounts = []
    for account in accounts:
        if not isinstance(account, dict):
            continue
        for item in account.get("items") or []:
            if not isinstance(item, dict):
                continue
            for key in ("organizer", "creator", "email"):
                contact = _contact_from_address(item.get(key), source="Aus Kalender", score=2.0, relevance="related")
                if contact:
                    contacts.append(contact)
            attendees = item.get("attendees")
            if isinstance(attendees, list):
                for attendee in attendees:
                    if isinstance(attendee, dict):
                        contact = _contact_from_address(attendee.get("email") or attendee.get("address") or attendee.get("display_name"), source="Aus Kalender", score=2.0, relevance="related")
                    else:
                        contact = _contact_from_address(attendee, source="Aus Kalender", score=2.0, relevance="related")
                    if contact:
                        contacts.append(contact)
    return contacts


_OWN_CONTACT_EMAILS = {
    "kontakt@aiwerk.ch",
    "a.bergsmann@aiwerk.ch",
    "bergsmann@gmail.com",
    "attila@bergsmann.ch",
    "attila.bergsmann@agbergsmann.ch",
    "office@agbergsmann.ch",
}
_SYSTEM_CONTACT_LOCALPARTS = {
    "root", "postmaster", "mailer-daemon", "daemon", "noreply", "no-reply", "donotreply", "do-not-reply",
    "notifications", "notification", "newsletter", "news", "support", "info", "admin", "administrator",
    "wordpress", "bounce", "bounces", "mailing", "nonrispondere", "noresponder", "rechnungen", "rechnung",
    "account", "billing", "notice", "notices", "announcement",
}
_SYSTEM_CONTACT_TEXT_PATTERNS = (
    "google analytics", "google ads", "google search console", "google workspace", "mailer-daemon",
    "no reply", "noreply", "newsletter", "notification", "notifications", "notice", "notices", "announcement", "rechnungssystem",
    "site audit", "coinmarketcap", "kozponti rendszer", "központi rendszer", "cf-test", "testkontakt",
)


def _contact_from_address(value: Any, *, source: str, score: float = 1.0, last_interaction_at: Any = None, relevance: str = "frequent") -> dict[str, Any] | None:
    text = str(value or "").strip()
    if not text:
        return None
    name, address = email.utils.parseaddr(text)
    email_addr = _safe_contact_email(address or text)
    display_name = _safe_contact_text(name or re.sub(r"<[^>]+>", "", text).strip().strip('"') or email_addr, 120)
    if display_name == email_addr and email_addr:
        display_name = email_addr.split("@", 1)[0].replace(".", " ").replace("_", " ").title()
    badge_kind = "Relevant" if relevance == "relevant" else "Häufig"
    return _normalize_contact({
        "display_name": display_name,
        "email": email_addr,
        "source_badges": [source, badge_kind],
        "interaction_count": 1,
        "interaction_score": score,
        "last_interaction_at": last_interaction_at,
    }, source=source, relevance=relevance)


def _contacts_own_email_set(config: dict[str, Any] | None, email_resource: dict[str, Any] | None, calendar_resource: dict[str, Any] | None) -> set[str]:
    own = set(_OWN_CONTACT_EMAILS)
    env_own = os.environ.get("AIWERK_CUI_CONTACTS_OWN_EMAILS") or ""
    own.update(_safe_contact_email(part) for part in env_own.split(",") if _safe_contact_email(part))
    for account in [*_email_account_configs(config), *_calendar_account_configs(config), *_contact_account_configs(config)]:
        if not isinstance(account, dict):
            continue
        for key in ("address", "email", "user_google_email", "google_email"):
            email_addr = _safe_contact_email(account.get(key))
            if email_addr and email_addr != "me":
                own.add(email_addr)
    for resource in (email_resource, calendar_resource):
        if not isinstance(resource, dict):
            continue
        for account in resource.get("accounts") or []:
            if not isinstance(account, dict):
                continue
            for key in ("address", "email", "account_address"):
                email_addr = _safe_contact_email(account.get(key))
                if email_addr:
                    own.add(email_addr)
    return {item for item in own if item}


def _is_probably_system_contact(contact: dict[str, Any], *, own_emails: set[str]) -> bool:
    email_addr = str(contact.get("email") or "").lower().strip()
    display = str(contact.get("display_name") or "").lower().strip()
    organization = str(contact.get("organization") or "").lower().strip()
    if email_addr and email_addr in own_emails:
        return True
    local = email_addr.split("@", 1)[0].lower() if "@" in email_addr else ""
    domain = email_addr.rsplit("@", 1)[-1].lower() if "@" in email_addr else ""
    compact_local = re.sub(r"[^a-z0-9]", "", local)
    if local in _SYSTEM_CONTACT_LOCALPARTS or compact_local in {"noreply", "donotreply", "donotreply"}:
        return True
    if local in {"ertesites", "értesítés"} and domain in {"kozpontirendszer.gov.hu"}:
        return True
    if any(local.startswith(f"{prefix}-") or local.startswith(f"{prefix}+") for prefix in _SYSTEM_CONTACT_LOCALPARTS):
        return True
    if display in _SYSTEM_CONTACT_LOCALPARTS or re.sub(r"[^a-z0-9]", "", display) in {"root", "noreply", "donotreply"}:
        return True
    haystack = f"{display} {organization} {email_addr}"
    if any(pattern in haystack for pattern in _SYSTEM_CONTACT_TEXT_PATTERNS):
        return True
    if not email_addr and not contact.get("phone"):
        return True
    return False


def _filter_human_contacts(contacts: list[dict[str, Any]], *, own_emails: set[str]) -> list[dict[str, Any]]:
    return [contact for contact in contacts if not _is_probably_system_contact(contact, own_emails=own_emails)]


def _contact_search_normalize(value: Any) -> str:
    text = _safe_contact_text(value, 500).casefold()
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _contact_search_haystack(contact: dict[str, Any]) -> str:
    emails: list[Any] = list(contact.get("emails") or []) if isinstance(contact.get("emails"), list) else []
    phones: list[Any] = list(contact.get("phones") or []) if isinstance(contact.get("phones"), list) else []
    parts: list[Any] = [
        contact.get("display_name"),
        contact.get("organization"),
        contact.get("role"),
        contact.get("email"),
        contact.get("phone"),
        contact.get("note"),
        *emails,
        *phones,
    ]
    return _contact_search_normalize(" ".join(str(part or "") for part in parts))


def _contact_matches_query(contact: dict[str, Any], query: str) -> bool:
    needle = _contact_search_normalize(query)
    if not needle:
        return True
    haystack = _contact_search_haystack(contact)
    if needle in haystack:
        return True
    terms = [term for term in re.split(r"\s+", needle) if term]
    return bool(terms) and all(term in haystack for term in terms)


def _sanitize_contact_for_cui(contact: dict[str, Any], *, own_emails: set[str]) -> dict[str, Any]:
    sanitized = dict(contact)
    raw_badges = sanitized.get("source_badges")
    badges = raw_badges if isinstance(raw_badges, list) else []
    sanitized["source_badges"] = [
        badge for badge in _dedupe_contact_badges(badges)
        if not (_safe_contact_email(badge) and _safe_contact_email(badge) in own_emails)
    ]
    return sanitized


def _filter_contacts_payload(payload: dict[str, Any], *, own_emails: set[str]) -> dict[str, Any]:
    """Apply the own/system contact filter to every list exposed to the CUI.

    This is intentionally a final response-level guard as well as a source-level
    filter: contact summaries can be served from the in-process resource cache,
    and search can merge cached/manual/bridge contacts.  The UI should never see
    configured own addresses such as kontakt@aiwerk.ch or bergsmann@gmail.com —
    neither as contact email nor as source badge/account label.
    """
    if not isinstance(payload, dict):
        return payload
    filtered = dict(payload)
    for key in ("items", "frequent", "relevant"):
        value = filtered.get(key)
        if isinstance(value, list):
            filtered[key] = [
                _sanitize_contact_for_cui(contact, own_emails=own_emails)
                for contact in _filter_human_contacts([item for item in value if isinstance(item, dict)], own_emails=own_emails)
            ]
    if not filtered.get("items"):
        if filtered.get("status") == "loading":
            return filtered
        filtered["total_count"] = 0
        filtered["manual_count"] = 0
        filtered["connected_count"] = 0
        filtered["google_count"] = 0
        filtered["saved_count"] = 0
        filtered["interaction_count"] = 0
        filtered["status"] = "not_configured"
        filtered["summary"] = "Keine relevanten Kontakte"
        filtered["source_label"] = "Keine relevanten Kontakte"
    return filtered


def _sort_interaction_contacts(contacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        contacts,
        key=lambda contact: (
            float(contact.get("interaction_score") or 0),
            int(contact.get("interaction_count") or 0),
            str(contact.get("last_interaction_at") or ""),
            str(contact.get("display_name") or "").lower(),
        ),
        reverse=True,
    )


def _contact_relevance_window_days() -> int:
    return max(1, min(int(os.environ.get("AIWERK_CUI_CONTACTS_RELEVANCE_WINDOW_DAYS") or _ASSISTANT_CONTACT_RELEVANCE_WINDOW_DAYS), 90))


def _contact_saved_top_up_target() -> int:
    return max(
        _ASSISTANT_CONTACT_PREVIEW_ITEMS,
        min(int(os.environ.get("AIWERK_CUI_CONTACTS_SAVED_TOP_UP_TARGET") or _ASSISTANT_CONTACT_SAVED_TOP_UP_TARGET), 50),
    )


def _gmail_metadata_has_bulk_signal(block: dict[str, str]) -> bool:
    precedence = str(block.get("precedence") or "").lower()
    auto_submitted = str(block.get("auto-submitted") or "").lower()
    list_unsubscribe = str(block.get("list-unsubscribe") or "")
    return precedence in {"bulk", "junk", "list"} or bool(list_unsubscribe) or bool(auto_submitted and auto_submitted != "no")


def _contacts_from_gmail_metadata_blocks(blocks: list[dict[str, str]], *, sent: bool, own_emails: set[str]) -> list[dict[str, Any]]:
    contacts: list[dict[str, Any]] = []
    for block in blocks:
        if not sent and _gmail_metadata_has_bulk_signal(block):
            continue
        last_interaction_at = _parse_gmail_bridge_date(block.get("date") or "")
        if sent:
            values = [block.get("to") or "", block.get("cc") or "", block.get("bcc") or ""]
            source = "Gesendet"
            score = 5.0
        else:
            values = [block.get("from") or ""]
            source = "Aus E-Mail"
            score = 4.0
        for name, address in email.utils.getaddresses([value for value in values if value]):
            email_addr = _safe_contact_email(address)
            if not email_addr or email_addr in own_emails:
                continue
            contact = _contact_from_address(email.utils.formataddr((name, email_addr)), source=source, score=score, last_interaction_at=last_interaction_at, relevance="relevant")
            if contact:
                contacts.append(contact)
    return contacts


def _contacts_from_google_workspace_interactions(config: dict[str, Any] | None, *, own_emails: set[str]) -> list[dict[str, Any]]:
    if os.environ.get("AIWERK_CUI_CONTACTS_DISABLE_GMAIL_INTERACTIONS", "").lower() in {"1", "true", "yes", "on"}:
        return []
    if os.environ.get("AIWERK_CUI_EMAIL_DISABLE_AIWERK_BRIDGE", "").lower() in {"1", "true", "yes", "on"}:
        return []
    limit = max(1, min(int(os.environ.get("AIWERK_CUI_CONTACTS_INTERACTION_SCAN_LIMIT") or 40), 100))
    window_days = _contact_relevance_window_days()
    queries = [
        (str(os.environ.get("AIWERK_CUI_CONTACTS_SENT_QUERY") or f"in:sent newer_than:{window_days}d"), True),
        (str(os.environ.get("AIWERK_CUI_CONTACTS_INBOX_QUERY") or f"newer_than:{window_days}d -in:sent"), False),
    ]
    contacts: list[dict[str, Any]] = []
    for account in _contact_account_configs(config):
        server = str(account.get("mcp_server") or "google-workspace-aiwerk")
        user_google_email = str(account.get("user_google_email") or "me")
        for query, sent in queries:
            try:
                ids = _gmail_bridge_search_message_ids(config, server=server, user_google_email=user_google_email, query=query, page_size=limit)
                batch = _call_aiwerk_bridge_tool(
                    config,
                    server=server,
                    tool="get_gmail_messages_content_batch",
                    params={"message_ids": ids[:limit], "user_google_email": user_google_email, "format": "metadata"},
                ) if ids else {}
                blocks = _parse_gmail_bridge_metadata_blocks(_bridge_result_text(batch))
                contacts.extend(_contacts_from_gmail_metadata_blocks(blocks, sent=sent, own_emails=own_emails))
            except Exception as exc:
                _log.debug("CUI Gmail interaction contact scan failed for %s/%s/%s: %s", server, user_google_email, query, exc)
    return _sort_interaction_contacts(_dedupe_contacts(contacts))


def _contacts_from_gmail_query_blocks(blocks: list[dict[str, str]], *, own_emails: set[str]) -> list[dict[str, Any]]:
    contacts: list[dict[str, Any]] = []
    for block in blocks:
        last_interaction_at = _parse_gmail_bridge_date(block.get("date") or "")
        for key, source, score in (("from", "Aus E-Mail", 4.0), ("to", "Gesendet", 5.0), ("cc", "Gesendet", 3.0), ("bcc", "Gesendet", 3.0)):
            for name, address in email.utils.getaddresses([block.get(key) or ""]):
                email_addr = _safe_contact_email(address)
                if not email_addr or email_addr in own_emails:
                    continue
                contact = _contact_from_address(email.utils.formataddr((name, email_addr)), source=source, score=score, last_interaction_at=last_interaction_at, relevance="relevant")
                if contact:
                    contacts.append(contact)
    return contacts


def _contacts_from_google_workspace_query_interactions(config: dict[str, Any] | None, *, query: str, own_emails: set[str], limit: int) -> list[dict[str, Any]]:
    if not query.strip():
        return []
    if os.environ.get("AIWERK_CUI_CONTACTS_DISABLE_GMAIL_INTERACTIONS", "").lower() in {"1", "true", "yes", "on"}:
        return []
    if os.environ.get("AIWERK_CUI_EMAIL_DISABLE_AIWERK_BRIDGE", "").lower() in {"1", "true", "yes", "on"}:
        return []
    page_size = max(1, min(limit, 200))
    contacts: list[dict[str, Any]] = []
    for account in _contact_account_configs(config):
        server = str(account.get("mcp_server") or "google-workspace-aiwerk")
        user_google_email = str(account.get("user_google_email") or "me")
        try:
            ids = _gmail_bridge_search_message_ids(config, server=server, user_google_email=user_google_email, query=query, page_size=page_size)
            batch = _call_aiwerk_bridge_tool(
                config,
                server=server,
                tool="get_gmail_messages_content_batch",
                params={"message_ids": ids[:page_size], "user_google_email": user_google_email, "format": "metadata"},
            ) if ids else {}
            blocks = _parse_gmail_bridge_metadata_blocks(_bridge_result_text(batch))
            contacts.extend(_contacts_from_gmail_query_blocks(blocks, own_emails=own_emails))
        except Exception as exc:
            _log.debug("CUI Gmail contact query scan failed for %s/%s/%s: %s", server, user_google_email, query, exc)
    return _sort_interaction_contacts(_dedupe_contacts(contacts))


def _himalaya_contact_folder(account: dict[str, Any], *, sent: bool) -> str:
    if sent:
        return str(
            account.get("sent_folder")
            or account.get("sent_mailbox")
            or account.get("sent")
            or os.environ.get("AIWERK_CUI_CONTACTS_HIMALAYA_SENT_FOLDER")
            or "Sent"
        ).strip() or "Sent"
    return str(
        account.get("inbox_folder")
        or account.get("folder")
        or os.environ.get("AIWERK_CUI_CONTACTS_HIMALAYA_INBOX_FOLDER")
        or "INBOX"
    ).strip() or "INBOX"


def _himalaya_address_values(value: Any) -> list[str]:
    if isinstance(value, list):
        return [formatted for formatted in (_format_himalaya_address(item) for item in value) if formatted]
    formatted = _format_himalaya_address(value)
    return [formatted] if formatted else []


def _himalaya_envelope_in_relevance_window(envelope: dict[str, Any], *, window_days: int) -> bool:
    parsed = _parse_himalaya_email_date(envelope.get("date"))
    if not parsed:
        return True
    try:
        dt = datetime.fromisoformat(str(parsed).replace("Z", "+00:00"))
    except Exception:
        return True
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    return dt.astimezone(timezone.utc) >= cutoff


def _contacts_from_himalaya_envelopes(envelopes: list[dict[str, Any]], *, sent: bool, own_emails: set[str], window_days: int) -> list[dict[str, Any]]:
    contacts: list[dict[str, Any]] = []
    for envelope in envelopes:
        if not isinstance(envelope, dict) or not _himalaya_envelope_in_relevance_window(envelope, window_days=window_days):
            continue
        last_interaction_at = _parse_himalaya_email_date(envelope.get("date"))
        if sent:
            values: list[str] = []
            for key in ("to", "cc", "bcc"):
                values.extend(_himalaya_address_values(envelope.get(key)))
            source = "Gesendet"
            score = 5.0
        else:
            values = _himalaya_address_values(envelope.get("from"))
            source = "Aus E-Mail"
            score = 4.0
        for name, address in email.utils.getaddresses(values):
            email_addr = _safe_contact_email(address)
            if not email_addr or email_addr in own_emails:
                continue
            contact = _contact_from_address(email.utils.formataddr((name, email_addr)), source=source, score=score, last_interaction_at=last_interaction_at, relevance="relevant")
            if contact:
                contacts.append(contact)
    return contacts


def _contacts_from_himalaya_interactions(config: dict[str, Any] | None, *, own_emails: set[str]) -> list[dict[str, Any]]:
    if os.environ.get("AIWERK_CUI_CONTACTS_DISABLE_HIMALAYA_INTERACTIONS", "").lower() in {"1", "true", "yes", "on"}:
        return []
    if os.environ.get("AIWERK_CUI_EMAIL_DISABLE_HIMALAYA", "").lower() in {"1", "true", "yes", "on"}:
        return []
    limit = max(1, min(int(os.environ.get("AIWERK_CUI_CONTACTS_INTERACTION_SCAN_LIMIT") or 40), 100))
    window_days = _contact_relevance_window_days()
    contacts: list[dict[str, Any]] = []
    for account in _email_account_configs(config):
        if not _is_himalaya_email_backend(_email_backend_name(account)):
            continue
        account_name = str(account.get("account") or account.get("name") or "").strip() or None
        for sent in (True, False):
            folder = _himalaya_contact_folder(account, sent=sent)
            try:
                envelopes = _run_himalaya_envelope_list(page_size=limit, account=account_name, folder=folder)
                contacts.extend(_contacts_from_himalaya_envelopes(envelopes, sent=sent, own_emails=own_emails, window_days=window_days))
            except Exception as exc:
                _log.debug("CUI Himalaya interaction contact scan failed for %s/%s: %s", account_name or account.get("address") or "mailbox", folder, exc)
    return _sort_interaction_contacts(_dedupe_contacts(contacts))


def _contacts_from_env_json() -> list[dict[str, Any]]:
    data = _read_optional_json(os.environ.get("AIWERK_CUI_CONTACTS_JSON"))
    if not data:
        return []
    raw_contacts = data.get("items") or data.get("contacts") if isinstance(data, dict) else data
    if not isinstance(raw_contacts, list):
        return []
    return [contact for contact in (_normalize_contact(item, source="Manuell", relevance="frequent") for item in raw_contacts if isinstance(item, dict)) if contact]


def _contact_account_configs(config: dict[str, Any] | None) -> list[dict[str, Any]]:
    raw_accounts: list[dict[str, Any]] = []
    if isinstance(config, dict):
        contacts_cfg = config.get("contacts")
        if isinstance(contacts_cfg, dict):
            raw_accounts.extend(_email_account_dicts(contacts_cfg.get("accounts"), backend="google_workspace"))
            raw_accounts.extend(_email_account_dicts(contacts_cfg.get("google_workspace"), backend="google_workspace"))
            if not raw_accounts and (_email_backend_name(contacts_cfg) or contacts_cfg.get("enabled") is True):
                raw_accounts.append(contacts_cfg)
    if not raw_accounts:
        raw_accounts = [account for account in _email_account_configs(config) if _is_google_email_backend(_email_backend_name(account))]

    accounts: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for account in raw_accounts:
        server = str(account.get("server") or account.get("mcp_server") or os.environ.get("AIWERK_CUI_GOOGLE_WORKSPACE_SERVER") or "google-workspace-aiwerk").strip()
        user_google_email = str(account.get("user_google_email") or account.get("google_email") or account.get("address") or account.get("email") or os.environ.get("AIWERK_CUI_GOOGLE_EMAIL") or "me").strip() or "me"
        key = (server, user_google_email.lower())
        if key in seen:
            continue
        seen.add(key)
        merged = dict(account)
        merged["mcp_server"] = server
        merged["user_google_email"] = user_google_email
        accounts.append(merged)
    return accounts


def _parse_google_contacts_text(text: str, *, source: str, account_label: str, relevance: str = "frequent") -> list[dict[str, Any]]:
    contacts: list[dict[str, Any]] = []
    for block in re.split(r"\n\s*\n(?=Contact ID:\s*)", text or ""):
        if "Contact ID:" not in block:
            continue
        raw: dict[str, Any] = {"source_badges": ["Google Contacts", account_label, source], "source": "Google Contacts", "relevance": relevance}
        emails: list[str] = []
        phones: list[str] = []
        organizations: list[str] = []
        roles: list[str] = []
        for line in block.splitlines():
            key, sep, value = line.partition(":")
            if not sep:
                continue
            key = key.strip().lower()
            value = value.strip()
            if key == "contact id":
                raw["id"] = _safe_resource_id(value, "contact")
            elif key == "name":
                raw["display_name"] = value
            elif key == "email":
                email_addr = _safe_contact_email(value)
                if email_addr:
                    emails.append(email_addr)
            elif key == "phone":
                phone = _safe_contact_phone(re.sub(r"\s*\([^)]*\)\s*$", "", value).strip() or value)
                if phone:
                    phones.append(phone)
            elif key == "organization":
                cleaned = _safe_contact_text(value, 180)
                if " at " in cleaned:
                    role, org = cleaned.split(" at ", 1)
                    if role.strip():
                        roles.append(role.strip())
                    if org.strip():
                        organizations.append(org.strip())
                elif cleaned:
                    organizations.append(cleaned)
        if emails:
            raw["email"] = emails[0]
            raw["emails"] = emails
        if phones:
            raw["phone"] = phones[0]
            raw["phones"] = phones
        if organizations:
            raw["organization"] = organizations[0]
        if roles:
            raw["role"] = roles[0]
        contact = _normalize_contact(raw, source="Google Contacts", relevance=relevance)
        if contact:
            if emails:
                contact["emails"] = emails[:5]
            if phones:
                contact["phones"] = phones[:5]
            contacts.append(contact)
    return contacts


def _contacts_from_google_workspace(config: dict[str, Any] | None, *, query: str = "", limit: int | None = None, sort_order: str | None = None) -> list[dict[str, Any]]:
    if os.environ.get("AIWERK_CUI_CONTACTS_DISABLE_AIWERK_BRIDGE", "").lower() in {"1", "true", "yes", "on"}:
        return []
    contacts: list[dict[str, Any]] = []
    default_limit = int(os.environ.get("AIWERK_CUI_CONTACTS_PAGE_SIZE") or 100)
    page_size = max(1, min(limit or default_limit, 1000))
    query = (query or "").strip()
    for account in _contact_account_configs(config):
        server = str(account.get("mcp_server") or "google-workspace-aiwerk")
        user_google_email = str(account.get("user_google_email") or "me")
        account_label = _email_account_label(account, user_google_email)
        tool = "search_contacts" if query else "list_contacts"
        params: dict[str, Any] = {"user_google_email": user_google_email, "page_size": min(page_size, 30) if query else page_size}
        if query:
            params["query"] = query
        else:
            params["sort_order"] = str(sort_order or account.get("contacts_sort_order") or "LAST_MODIFIED_DESCENDING")
        try:
            result = _call_aiwerk_bridge_tool(config, server=server, tool=tool, params=params)
            text = _bridge_result_text(result)
            contacts.extend(_parse_google_contacts_text(text, source=account_label, account_label=account_label, relevance="frequent"))
        except Exception as exc:
            _log.debug("CUI Google Contacts bridge lookup failed for %s/%s: %s", server, user_google_email, exc)
    return _dedupe_contacts(contacts)


def _contact_signal_emails(email_resource: dict[str, Any] | None, calendar_resource: dict[str, Any] | None) -> set[str]:
    signals: set[str] = set()
    for resource in (email_resource, calendar_resource):
        if not isinstance(resource, dict):
            continue
        for account in resource.get("accounts") or []:
            if not isinstance(account, dict):
                continue
            for key in ("address", "email", "account_address"):
                email_addr = _safe_contact_email(account.get(key))
                if email_addr:
                    signals.add(email_addr)
            for item in account.get("items") or []:
                if not isinstance(item, dict):
                    continue
                for key in ("sender", "organizer", "creator", "email", "account_address"):
                    email_addr = _safe_contact_email(item.get(key))
                    if email_addr:
                        signals.add(email_addr)
    return signals


def _related_contacts(contacts: list[dict[str, Any]], *, email_resource: dict[str, Any] | None, calendar_resource: dict[str, Any] | None) -> list[dict[str, Any]]:
    signals = _contact_signal_emails(email_resource, calendar_resource)
    related: list[dict[str, Any]] = []
    for contact in contacts:
        badges = contact.get("source_badges") or []
        if contact.get("relevance") in {"relevant", "related"} or contact.get("interaction_count") or ("Manuell" in badges and contact.get("note")):
            related.append(dict(contact, relevance="relevant"))
            continue
        email_addr = str(contact.get("email") or "").lower()
        if email_addr and email_addr in signals:
            related.append(dict(contact, relevance="related"))
    return _dedupe_contacts(related)


def _contacts_summary(config: dict[str, Any] | None = None, email_resource: dict[str, Any] | None = None, calendar_resource: dict[str, Any] | None = None) -> dict[str, Any]:
    own_emails = _contacts_own_email_set(config, email_resource, calendar_resource)
    manual_contacts = _filter_human_contacts(_dedupe_contacts([*_contacts_from_env_json(), *_read_manual_contacts()]), own_emails=own_emails)
    resource_contacts = _filter_human_contacts(_dedupe_contacts([
        *(_contacts_from_email_resource(email_resource or {}) if isinstance(email_resource, dict) else []),
        *(_contacts_from_calendar_resource(calendar_resource or {}) if isinstance(calendar_resource, dict) else []),
    ]), own_emails=own_emails)
    interaction_contacts = _filter_human_contacts(_dedupe_contacts([
        *_contacts_from_google_workspace_interactions(config, own_emails=own_emails),
        *_contacts_from_himalaya_interactions(config, own_emails=own_emails),
    ]), own_emails=own_emails)
    signal_contacts = _sort_interaction_contacts(_dedupe_contacts([*interaction_contacts, *resource_contacts]))

    # Google Contacts is used for enrichment and controlled top-up. It must not define an unbounded
    # frequent list by itself, because Google's plain contact list contains self accounts, service
    # senders, and stale auto contacts.
    top_up_target = _contact_saved_top_up_target()
    google_contacts = _filter_human_contacts(_contacts_from_google_workspace(config, limit=top_up_target), own_emails=own_emails)
    signal_emails = {str(contact.get("email") or "").lower() for contact in signal_contacts if contact.get("email")}
    google_enrichment = [contact for contact in google_contacts if str(contact.get("email") or "").lower() in signal_emails]

    base_contacts = _sort_interaction_contacts(_dedupe_contacts([*manual_contacts, *signal_contacts, *google_enrichment]))
    seen_emails = {str(contact.get("email") or "").lower() for contact in base_contacts if contact.get("email")}
    saved_top_up: list[dict[str, Any]] = []
    if len(base_contacts) < top_up_target:
        for contact in google_contacts:
            email_addr = str(contact.get("email") or "").lower()
            if email_addr and email_addr in seen_emails:
                continue
            saved_top_up.append(contact)
            if email_addr:
                seen_emails.add(email_addr)
            if len(base_contacts) + len(saved_top_up) >= top_up_target:
                break

    contacts = _sort_interaction_contacts(_dedupe_contacts([*base_contacts, *saved_top_up]))
    contacts = _filter_hidden_contacts(contacts)
    relevant = _related_contacts(contacts, email_resource=email_resource, calendar_resource=calendar_resource)
    frequent = [contact for contact in contacts if contact not in relevant]
    manual_count = len([c for c in contacts if "Manuell" in (c.get("source_badges") or [])])
    google_count = len([c for c in contacts if "Google Contacts" in (c.get("source_badges") or [])])
    saved_count = len([c for c in saved_top_up if "Google Contacts" in (c.get("source_badges") or [])])
    interaction_count = len([c for c in contacts if c.get("interaction_count") or "Gesendet" in (c.get("source_badges") or []) or "Aus E-Mail" in (c.get("source_badges") or [])])
    connected_count = max(interaction_count, google_count, max(0, len(contacts) - manual_count))
    if contacts:
        source_label = "Relevante Kontakte"
        summary = f"{len(contacts)} relevante Kontakte"
        status = "connected"
    else:
        source_label = "Keine relevanten Kontakte"
        summary = "Keine relevanten Kontakte"
        status = "not_configured"
    payload = {
        "status": status,
        "summary": summary,
        "items": (relevant + frequent)[:_ASSISTANT_CONTACT_PREVIEW_ITEMS],
        "frequent": frequent[:_ASSISTANT_CONTACT_PREVIEW_ITEMS],
        "relevant": relevant[:_ASSISTANT_CONTACT_PREVIEW_ITEMS],
        "total_count": len(contacts),
        "manual_count": manual_count,
        "connected_count": connected_count,
        "google_count": google_count,
        "saved_count": saved_count,
        "interaction_count": interaction_count,
        "relevance_window_days": _contact_relevance_window_days(),
        "saved_top_up_target": top_up_target,
        "source_label": source_label,
        "checked_at": _utc_now_iso(),
    }
    return _filter_contacts_payload(payload, own_emails=own_emails)


def _search_contacts_payload(q: str = "", *, limit: int = _ASSISTANT_CONTACT_SEARCH_LIMIT) -> dict[str, Any]:
    config = load_config()
    resources = _assistant_resources_payload(force_refresh=False)
    email_resource = resources.get("email") if isinstance(resources.get("email"), dict) else {}
    calendar_resource = resources.get("calendar") if isinstance(resources.get("calendar"), dict) else {}
    own_emails = _contacts_own_email_set(config, email_resource, calendar_resource)
    contacts = resources.get("contacts", {}).get("items") or []
    all_contacts = _filter_human_contacts(_dedupe_contacts([*_read_manual_contacts(), *contacts]), own_emails=own_emails)
    needle = (q or "").strip()
    if needle:
        query_variants = [needle]
        normalized_needle = _contact_search_normalize(needle)
        if normalized_needle and normalized_needle != needle.casefold():
            query_variants.append(normalized_needle)
        query_variants.extend(term for term in re.split(r"\s+", needle) if len(term.strip()) >= 3)
        if " " not in needle and len(needle) >= 4:
            # Some connector/contact search backends miss surname-only matches that
            # are returned for a full-name query.  Add a tiny local first-name
            # expansion for common normalized/accented variants instead of making
            # the UI look broken for a known human contact.
            for first_name in ("Adam", "Ádám"):
                query_variants.extend((f"{first_name} {needle}", f"{needle} {first_name}"))
        bridge_contacts: list[dict[str, Any]] = []
        seen_queries: set[str] = set()
        for contact_query in query_variants:
            contact_query = contact_query.strip()
            query_key = contact_query.casefold()
            if not contact_query or query_key in seen_queries:
                continue
            seen_queries.add(query_key)
            bridge_contacts.extend(_contacts_from_google_workspace(config, query=contact_query, limit=max(limit, 50)))
        # Explicit search is a user-driven lookup, so use a deeper saved-contact
        # fallback than the default right-rail top-up. People API search can miss
        # surname-only matches that are found when listing the address book.
        saved_lookup_limit = max(limit * 50, 1000)
        saved_contacts = _dedupe_contacts([
            *_contacts_from_google_workspace(config, limit=saved_lookup_limit),
            *_contacts_from_google_workspace(config, limit=saved_lookup_limit, sort_order="FIRST_NAME_ASCENDING"),
        ])
        interaction_contacts: list[dict[str, Any]] = []
        for contact_query in seen_queries:
            interaction_contacts.extend(_contacts_from_google_workspace_query_interactions(config, query=contact_query, own_emails=own_emails, limit=max(limit * 10, 200)))
        interaction_contacts = _dedupe_contacts(interaction_contacts)
        all_contacts = _filter_human_contacts(_dedupe_contacts([*bridge_contacts, *interaction_contacts, *saved_contacts, *all_contacts]), own_emails=own_emails)
        all_contacts = [contact for contact in all_contacts if _contact_matches_query(contact, needle)]
    all_contacts = _filter_hidden_contacts(all_contacts)
    payload = {"items": all_contacts[:limit], "total_count": len(all_contacts), "query": q or ""}
    return _filter_contacts_payload(payload, own_emails=own_emails)

def _shared_folder_refreshing_placeholder() -> dict[str, Any]:
    return {
        "status": "loading",
        "root_label": "Shared",
        "summary": "Shared Ordner wird aktualisiert…",
        "items": [],
        "total_count": 0,
        "refreshing": True,
    }


def _vault_refreshing_placeholder(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "loading",
        "vault_url": _vault_url_from_config(config),
        "summary": "Passwort-Tresor wird aktualisiert…",
        "item_count": 0,
        "weak_count": 0,
        "reused_count": 0,
        "compromised_count": None,
        "refreshing": True,
    }


def _email_refreshing_placeholder() -> dict[str, Any]:
    return {
        "status": "loading",
        "unread_count": 0,
        "summary": "E-Mail wird aktualisiert…",
        "items": [],
        "accounts": [],
        "refreshing": True,
    }


def _calendar_refreshing_placeholder() -> dict[str, Any]:
    return {
        "status": "loading",
        "summary": "Kalender wird aktualisiert…",
        "items": [],
        "accounts": [],
        "refreshing": True,
    }


def _contacts_refreshing_placeholder() -> dict[str, Any]:
    return {
        "status": "loading",
        "summary": "Kontakte werden aktualisiert…",
        "source_label": "Relevante Kontakte",
        "items": [],
        "relevant": [],
        "frequent": [],
        "manual": [],
        "total_count": 0,
        "manual_count": 0,
        "connected_count": 0,
        "interaction_count": 0,
        "saved_count": 0,
        "saved_top_up_target": 0,
        "relevance_window_days": 10,
        "refreshing": True,
    }


def _assistant_resources_payload(
    request: Request | None = None,
    *,
    force_refresh: bool = False,
    refresh_resource: str | None = None,
) -> dict[str, Any]:
    config = load_config()
    cache_key = _assistant_resource_config_signature(config, request)
    refresh_resource = str(refresh_resource or "").strip().lower() or None

    def should_refresh(name: str) -> bool:
        return bool(force_refresh and (refresh_resource is None or refresh_resource == name))

    email, email_cache = _assistant_cached_resource(
        "email",
        _ASSISTANT_RESOURCE_CACHE_TTLS["email"],
        cache_key,
        lambda: _email_summary(config),
        force_refresh=should_refresh("email"),
        stale_while_revalidate=bool(refresh_resource == "email" or (refresh_resource is None and not force_refresh)),
        initial_payload=_email_refreshing_placeholder() if refresh_resource is None and not force_refresh else None,
    )
    calendar, calendar_cache = _assistant_cached_resource(
        "calendar",
        _ASSISTANT_RESOURCE_CACHE_TTLS["calendar"],
        cache_key,
        lambda: _calendar_summary(config),
        force_refresh=should_refresh("calendar"),
        stale_while_revalidate=bool(refresh_resource == "calendar" or (refresh_resource is None and not force_refresh)),
        initial_payload=_calendar_refreshing_placeholder() if refresh_resource is None and not force_refresh else None,
    )
    shared_folder, shared_cache = _assistant_cached_resource(
        "shared_folder",
        _ASSISTANT_RESOURCE_CACHE_TTLS["shared_folder"],
        cache_key,
        lambda: _shared_folder_summary(config, request),
        force_refresh=should_refresh("shared_folder"),
        stale_while_revalidate=bool(refresh_resource == "shared_folder" or (refresh_resource is None and not force_refresh)),
        initial_payload=_shared_folder_refreshing_placeholder() if refresh_resource is None and not force_refresh else None,
    )
    vault, vault_cache = _assistant_cached_resource(
        "vault",
        _ASSISTANT_RESOURCE_CACHE_TTLS["vault"],
        cache_key,
        lambda: _vaultwarden_summary(config),
        force_refresh=should_refresh("vault"),
        stale_while_revalidate=bool(refresh_resource == "vault" or (refresh_resource is None and not force_refresh)),
        initial_payload=_vault_refreshing_placeholder(config) if refresh_resource is None and not force_refresh else None,
    )
    todos, todos_cache = _assistant_cached_resource(
        "todos",
        _ASSISTANT_RESOURCE_CACHE_TTLS["todos"],
        cache_key,
        lambda: _todo_summary(config),
        force_refresh=should_refresh("todos"),
    )
    contacts, contacts_cache = _assistant_cached_resource(
        "contacts",
        _ASSISTANT_RESOURCE_CACHE_TTLS["contacts"],
        cache_key,
        lambda: _contacts_summary(config, email, calendar),
        force_refresh=should_refresh("contacts"),
        stale_while_revalidate=bool(refresh_resource == "contacts" or (refresh_resource is None and not force_refresh)),
        initial_payload=_contacts_refreshing_placeholder() if refresh_resource is None and not force_refresh else None,
    )
    contacts = _filter_contacts_payload(
        contacts,
        own_emails=_contacts_own_email_set(config, email if isinstance(email, dict) else {}, calendar if isinstance(calendar, dict) else {}),
    )
    connectors, connectors_cache = _assistant_cached_resource(
        "connectors",
        _ASSISTANT_RESOURCE_CACHE_TTLS["connectors"],
        cache_key,
        lambda: _connector_summary(config, shared_folder, email, calendar),
        force_refresh=should_refresh("connectors"),
        stale_while_revalidate=bool(refresh_resource == "connectors" or (refresh_resource is None and not force_refresh)),
        initial_payload=[] if refresh_resource is None and not force_refresh else None,
    )
    warnings = []
    if shared_folder.get("status") == "error":
        warnings.append("Shared Ordner konnte nicht geprüft werden")
    return {
        "checked_at": _utc_now_iso(),
        "email": email,
        "calendar": calendar,
        "shared_folder": shared_folder,
        "vault": vault,
        "todos": todos,
        "contacts": contacts,
        "connectors": connectors,
        "warnings": warnings,
        "cache": {
            "cached": any(meta.get("cached") for meta in (email_cache, calendar_cache, shared_cache, vault_cache, todos_cache, contacts_cache, connectors_cache)),
            "resources": {
                "email": email_cache,
                "calendar": calendar_cache,
                "shared_folder": shared_cache,
                "vault": vault_cache,
                "todos": todos_cache,
                "contacts": contacts_cache,
                "connectors": connectors_cache,
            },
        },
    }

def _safe_upload_component(value: str, fallback: str = "upload") -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", value or "").strip(".-_")
    return (safe or fallback)[:80]


def _assistant_upload_root() -> Path:
    root = get_hermes_home() / "dashboard_uploads"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _assistant_attachment_target_dir(session_id: str, prefix: str = "resource") -> Path:
    session_part = _safe_upload_component(session_id or "session", "session")
    batch_part = f"{prefix}-{int(time.time() * 1000)}-{secrets.token_hex(4)}"
    target_dir = _assistant_upload_root() / session_part / batch_part
    target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir


def _assistant_uploaded_attachment_payload(path: Path, *, name: str | None = None, content_type: str | None = None) -> dict[str, Any]:
    resolved = path.resolve()
    media_type = content_type or mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
    extracted_text, extraction = _extract_uploaded_text(resolved, media_type)
    return {
        "name": name or resolved.name,
        "path": str(resolved),
        "type": media_type,
        "size": resolved.stat().st_size,
        "is_image": media_type.startswith("image/"),
        "extracted_text": extracted_text,
        "extraction": extraction,
    }


def _write_assistant_text_attachment(*, session_id: str, filename: str, text: str) -> dict[str, Any]:
    target_dir = _assistant_attachment_target_dir(session_id, "resource")
    safe_name = _safe_upload_component(filename, "context.txt")
    if Path(safe_name).suffix.lower() not in {".txt", ".md"}:
        safe_name = f"{safe_name}.txt"
    dest = target_dir / safe_name
    dest.write_text(text[:_ASSISTANT_TEXT_EXTRACT_LIMIT], encoding="utf-8")
    try:
        dest.chmod(0o600)
    except Exception:
        pass
    return _assistant_uploaded_attachment_payload(dest, name=filename, content_type="text/plain")


def _decode_text_bytes(data: bytes) -> str:
    for enc in ("utf-8", "utf-16", "latin-1"):
        try:
            return data.decode(enc).replace("\x00", "")
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace").replace("\x00", "")


def _extract_uploaded_text(path: Path, content_type: str = "") -> tuple[str, str]:
    """Best-effort text extraction for customer UI attachments.

    Returns ``(text, note)``. Empty text is valid for binary/image files; the note
    tells the prompt layer what happened without leaking raw bytes.
    """
    ext = path.suffix.lower()
    if content_type.startswith("image/") or ext in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
        return "", "image"
    if ext in {".txt", ".md", ".csv", ".json", ".yaml", ".yml"} or content_type.startswith("text/"):
        data = path.read_bytes()[: _ASSISTANT_TEXT_EXTRACT_LIMIT + 1]
        text = _decode_text_bytes(data)[:_ASSISTANT_TEXT_EXTRACT_LIMIT]
        return text, "text" if text else "empty"
    if ext == ".docx":
        try:
            import zipfile
            import xml.etree.ElementTree as ET

            with zipfile.ZipFile(path) as zf:
                xml = zf.read("word/document.xml")
            root = ET.fromstring(xml)
            parts = [node.text for node in root.iter() if node.text]
            text = " ".join(parts).strip()[:_ASSISTANT_TEXT_EXTRACT_LIMIT]
            return text, "docx" if text else "empty-docx"
        except Exception:
            return "", "docx-extraction-failed"
    if ext == ".pdf":
        try:
            proc = subprocess.run(
                ["pdftotext", "-layout", str(path), "-"],
                check=False,
                capture_output=True,
                timeout=10,
            )
            if proc.returncode == 0 and proc.stdout:
                text = _decode_text_bytes(proc.stdout)[:_ASSISTANT_TEXT_EXTRACT_LIMIT]
                return text, "pdf" if text else "empty-pdf"
        except Exception:
            pass
        return "", "pdf-text-extraction-unavailable"
    return "", "binary"


class AssistantResourceAttachmentRequest(BaseModel):
    kind: str
    item: Dict[str, Any]
    session_id: str = ""


class AssistantSupportRequest(BaseModel):
    category: str = ""
    message: str
    include_diagnostics: bool = True
    session_id: str = ""
    session_title: str = ""
    agent_name: str = ""
    connection: str = ""
    page_url: str = ""
    user_agent: str = ""
    diagnostics: dict[str, Any] | None = None


class AssistantTodoAddRequest(BaseModel):
    text: str


class AssistantTodoUpdateRequest(BaseModel):
    id: str
    done: bool


class CuiContactCreateRequest(BaseModel):
    name: str = ""
    organization: str = ""
    role: str = ""
    email: str = ""
    phone: str = ""
    note: str = ""
    link_current_context: bool = False


class CuiContactHideRequest(BaseModel):
    id: str = ""
    email: str = ""
    phone: str = ""
    display_name: str = ""


def _shared_attachment_rel_path(item: dict[str, Any]) -> str | None:
    open_url = str(item.get("open_url") or "")
    if not open_url:
        return None
    parsed = urllib.parse.urlparse(open_url)
    query = urllib.parse.parse_qs(parsed.query)
    path_values = query.get("path") or []
    if not path_values:
        return None
    return _clean_shared_relative_path(path_values[0])


def _create_shared_file_attachment(config: dict[str, Any], item: dict[str, Any], session_id: str) -> dict[str, Any]:
    rel_path = _shared_attachment_rel_path(item)
    if not rel_path:
        raise HTTPException(status_code=400, detail="Shared file path missing")
    filename = Path(rel_path).name
    safe_name = _safe_upload_component(filename, "shared-file")
    ext = Path(safe_name).suffix.lower()
    if ext not in _ASSISTANT_UPLOAD_EXTENSIONS:
        raise HTTPException(status_code=415, detail=f"Unsupported file type: {filename}")

    target_dir = _assistant_attachment_target_dir(session_id, "shared")
    dest = target_dir / safe_name
    shared_root = _resolve_shared_folder_root(config)
    if shared_root:
        source = _resolve_shared_folder_file(shared_root, rel_path)
        if not source:
            raise HTTPException(status_code=404, detail="Shared file not found")
        size = source.stat().st_size
        if size > _ASSISTANT_UPLOAD_MAX_FILE_BYTES:
            raise HTTPException(status_code=413, detail=f"File too large: {filename}")
        shutil.copyfile(source, dest)
        content_type = mimetypes.guess_type(source.name)[0] or str(item.get("mime") or "application/octet-stream")
    else:
        cloud = _shared_cloud_config(config)
        downloaded = _download_sftpgo_pubshare_file(cloud, rel_path) if isinstance(cloud, dict) else None
        if not downloaded:
            raise HTTPException(status_code=404, detail="Shared file not found")
        data, content_type, downloaded_name = downloaded
        if len(data) > _ASSISTANT_UPLOAD_MAX_FILE_BYTES:
            raise HTTPException(status_code=413, detail=f"File too large: {downloaded_name or filename}")
        dest.write_bytes(data)
        filename = downloaded_name or filename
    try:
        dest.chmod(0o600)
    except Exception:
        pass
    return _assistant_uploaded_attachment_payload(dest, name=filename, content_type=content_type)


def _find_resource_email_metadata(resources: dict[str, Any], account_ref: str, message_id: str) -> dict[str, Any]:
    email_resource = resources.get("email") if isinstance(resources, dict) else None
    accounts = email_resource.get("accounts") if isinstance(email_resource, dict) else None
    if not isinstance(accounts, list):
        return {}
    for account_entry in accounts:
        if not isinstance(account_entry, dict):
            continue
        if str(account_entry.get("address") or account_entry.get("label") or "") != account_ref:
            continue
        for item in account_entry.get("items") or []:
            if not isinstance(item, dict):
                continue
            if message_id in {str(item.get("message_id") or ""), str(item.get("id") or "")}:
                return item
    return {}


def _create_email_context_attachment(request: Request, config: dict[str, Any], item: dict[str, Any], session_id: str) -> dict[str, Any]:
    account_ref = str(item.get("account_address") or item.get("account_label") or item.get("account") or "").strip()
    message_id = str(item.get("message_id") or item.get("id") or "").strip()
    if not account_ref or not message_id:
        raise HTTPException(status_code=400, detail="Missing email account or message id")
    account_cfg = _find_email_account_config(config, account_ref)
    if not account_cfg:
        raise HTTPException(status_code=404, detail="Email account not configured")
    account = str(account_cfg.get("account") or account_cfg.get("name") or "").strip() or None
    folder = str(account_cfg.get("folder") or account_cfg.get("mailbox") or "").strip() or None
    backend = _email_backend_name(account_cfg)
    metadata = dict(item)
    try:
        resources = _assistant_resources_payload(request, force_refresh=False)
        metadata = {**metadata, **_find_resource_email_metadata(resources, account_ref, message_id)}
    except Exception:
        pass
    try:
        if _is_google_email_backend(backend):
            body = _run_google_workspace_message_read(config, account_cfg, message_id=message_id)
        else:
            body = _run_himalaya_message_read(message_id=message_id, account=account, folder=folder)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid message id")
    except FileNotFoundError:
        raise HTTPException(status_code=503, detail="Himalaya is not installed")
    except Exception as exc:
        _log.debug("CUI email attachment failed: %s", exc)
        raise HTTPException(status_code=502, detail="Email could not be loaded") from exc

    subject = str(metadata.get("subject") or "Ohne Betreff")
    clean_body = _strip_email_reader_transport_metadata(body)
    text = "\n".join([
        "Attached email context",
        f"Account: {account_ref}",
        f"From: {metadata.get('sender') or 'Unbekannt'}",
        f"Subject: {subject}",
        f"Date: {metadata.get('received_at') or ''}",
        f"Source: {metadata.get('source') or backend or 'email'}",
        "",
        clean_body,
    ]).strip()
    return _write_assistant_text_attachment(
        session_id=session_id,
        filename=f"email-{_safe_upload_component(subject, 'message')}.txt",
        text=text,
    )


def _create_calendar_context_attachment(item: dict[str, Any], session_id: str) -> dict[str, Any]:
    title = str(item.get("title") or "Termin")
    lines = [
        "Attached calendar event context",
        f"Title: {title}",
        f"Starts: {item.get('starts_at') or ''}",
        f"Ends: {item.get('ends_at') or ''}",
        f"Location: {item.get('location_hint') or ''}",
        f"Account: {item.get('account_address') or item.get('account_label') or ''}",
        f"Source: {item.get('source') or 'calendar'}",
    ]
    html_link = str(item.get("html_link") or "").strip()
    if html_link:
        lines.append("Link: [LINK]")
    return _write_assistant_text_attachment(
        session_id=session_id,
        filename=f"event-{_safe_upload_component(title, 'termin')}.txt",
        text="\n".join(lines).strip(),
    )


def _create_contact_context_attachment(item: dict[str, Any], session_id: str) -> dict[str, Any]:
    display_name = str(item.get("display_name") or item.get("name") or item.get("email") or item.get("phone") or "Kontakt").strip()
    raw_source_badges = item.get("source_badges")
    source_badges = raw_source_badges if isinstance(raw_source_badges, list) else []
    source = ", ".join(str(badge) for badge in source_badges if str(badge).strip()) or str(item.get("source") or "contacts")
    lines = [
        "Attached contact context",
        f"Name: {display_name}",
        f"Organization: {item.get('organization') or ''}",
        f"Role: {item.get('role') or ''}",
        f"Email: {item.get('email') or ''}",
        f"Phone: {item.get('phone') or ''}",
        f"Source: {source}",
    ]
    return _write_assistant_text_attachment(
        session_id=session_id,
        filename=f"contact-{_safe_upload_component(display_name, 'kontakt')}.txt",
        text="\n".join(lines).strip(),
    )


def _create_resource_attachment(request: Request, payload: AssistantResourceAttachmentRequest) -> dict[str, Any]:
    kind = payload.kind.strip().lower()
    item = payload.item if isinstance(payload.item, dict) else {}
    config = load_config()
    if kind == "shared_file":
        return _create_shared_file_attachment(config, item, payload.session_id)
    if kind == "email":
        return _create_email_context_attachment(request, config, item, payload.session_id)
    if kind == "calendar_event":
        return _create_calendar_context_attachment(item, payload.session_id)
    if kind == "contact":
        return _create_contact_context_attachment(item, payload.session_id)
    raise HTTPException(status_code=400, detail="Unsupported resource attachment type")


def _assistant_api_allowed(path: str) -> bool:
    """Return True for HTTP API paths exposed in assistant mode."""
    if path in _ASSISTANT_ALLOWED_API_EXACT:
        return True
    return any(path.startswith(prefix) for prefix in _ASSISTANT_ALLOWED_API_PREFIXES)


def _assistant_mode_enabled() -> bool:
    return _DASHBOARD_MODE == "assistant"


def _assistant_user_display_name() -> Optional[str]:
    """Return a sanitized display name for the customer UI bootstrap.

    Never expose the full USER.md/profile content to the browser. The CUI only
    needs a short first-name style label for local personalization.
    """
    user_path = get_hermes_home() / "memories" / "USER.md"
    try:
        text = user_path.read_text(encoding="utf-8", errors="ignore")[:16_384]
    except OSError:
        return None

    explicit_patterns = (
        r"\bUser['’]s\s+name\s+is\s+([A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’-]{1,39})\b",
        r"\bname\s+is\s+([A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’-]{1,39})\b",
    )
    generic_patterns = (
        r"^\s*([A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’-]{1,39})\s+is\b",
    )
    blocked_names = {"assistant", "bot", "golem", "cody", "hermes"}
    for patterns, allow_blocked in ((explicit_patterns, True), (generic_patterns, False)):
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.MULTILINE | re.IGNORECASE)
            if not match:
                continue
            name = match.group(1).strip(" '’-\t\r\n")
            if (
                1 < len(name) <= 40
                and re.fullmatch(r"[A-Za-zÀ-ÖØ-öø-ÿ'’-]+", name)
                and (allow_blocked or name.casefold() not in blocked_names)
            ):
                return name
    return None


def _has_valid_session_token(request: Request) -> bool:
    """True if the request carries a valid dashboard session token.

    The dedicated session header avoids collisions with reverse proxies that
    already use ``Authorization`` (for example Caddy ``basic_auth``). We still
    accept the legacy Bearer path for backward compatibility with older
    dashboard bundles.
    """
    session_header = request.headers.get(_SESSION_HEADER_NAME, "")
    if session_header and hmac.compare_digest(
        session_header.encode(),
        _SESSION_TOKEN.encode(),
    ):
        return True

    auth = request.headers.get("authorization", "")
    expected = f"Bearer {_SESSION_TOKEN}"
    return hmac.compare_digest(auth.encode(), expected.encode())


def _require_token(request: Request) -> None:
    """Validate the ephemeral session token.  Raises 401 on mismatch."""
    if not _has_valid_session_token(request):
        raise HTTPException(status_code=401, detail="Unauthorized")


# Accepted Host header values for loopback binds. DNS rebinding attacks
# point a victim browser at an attacker-controlled hostname (evil.test)
# which resolves to 127.0.0.1 after a TTL flip — bypassing same-origin
# checks because the browser now considers evil.test and our dashboard
# "same origin". Validating the Host header at the app layer rejects any
# request whose Host isn't one we bound for. See GHSA-ppp5-vxwm-4cf7.
_LOOPBACK_HOST_VALUES: frozenset = frozenset({
    "localhost", "127.0.0.1", "::1",
})


def should_require_auth(host: str, allow_public: bool) -> bool:
    """Return True iff the dashboard OAuth auth gate must be active.

    Truth table:
      host == loopback                              → False (no auth)
      host != loopback AND allow_public (--insecure)→ False (legacy escape hatch)
      host != loopback AND NOT allow_public         → True  (gate engages)

    "Loopback" matches the same set used by ``--insecure`` enforcement in
    ``start_server``: 127.0.0.1, localhost, ::1. RFC1918 / CGNAT / link-local
    are deliberately treated as PUBLIC — a hostile device on the same LAN is
    exactly the threat model the gate is designed for.
    """
    return (host not in _LOOPBACK_HOST_VALUES) and (not allow_public)


def _is_accepted_host(host_header: str, bound_host: str) -> bool:
    """True if the Host header targets the interface we bound to.

    Accepts:
    - Exact bound host (with or without port suffix)
    - Loopback aliases when bound to loopback
    - Any host when bound to 0.0.0.0 (explicit opt-in to non-loopback,
      no protection possible at this layer)
    """
    if not host_header:
        return False
    # Strip port suffix. IPv6 addresses use bracket notation:
    #   [::1]         — no port
    #   [::1]:9119    — with port
    # Plain hosts/v4:
    #   localhost:9119
    #   127.0.0.1:9119
    h = host_header.strip()
    if h.startswith("["):
        # IPv6 bracketed — port (if any) follows "]:"
        close = h.find("]")
        if close != -1:
            host_only = h[1:close]  # strip brackets
        else:
            host_only = h.strip("[]")
    else:
        host_only = h.rsplit(":", 1)[0] if ":" in h else h
    host_only = host_only.lower()

    # 0.0.0.0 bind means operator explicitly opted into all-interfaces
    # (requires --insecure per web_server.start_server). No Host-layer
    # defence can protect that mode; rely on operator network controls.
    if bound_host in {"0.0.0.0", "::"}:
        return True

    # Loopback bind: accept the loopback names
    bound_lc = bound_host.lower()
    if bound_lc in _LOOPBACK_HOST_VALUES:
        return host_only in _LOOPBACK_HOST_VALUES

    # Explicit non-loopback bind: require exact host match
    return host_only == bound_lc


@app.middleware("http")
async def host_header_middleware(request: Request, call_next):
    """Reject requests whose Host header doesn't match the bound interface.

    Defends against DNS rebinding: a victim browser on a localhost
    dashboard is tricked into fetching from an attacker hostname that
    TTL-flips to 127.0.0.1. CORS and same-origin checks don't help —
    the browser now treats the attacker origin as same-origin with the
    dashboard. Host-header validation at the app layer catches it.

    See GHSA-ppp5-vxwm-4cf7.
    """
    # Store the bound host on app.state so this middleware can read it —
    # set by start_server() at listen time.
    bound_host = getattr(app.state, "bound_host", None)
    if bound_host:
        host_header = request.headers.get("host", "")
        if not _is_accepted_host(host_header, bound_host):
            return JSONResponse(
                status_code=400,
                content={
                    "detail": (
                        "Invalid Host header. Dashboard requests must use "
                        "the hostname the server was bound to."
                    ),
                },
            )
    return await call_next(request)


# ---------------------------------------------------------------------------
# Dashboard OAuth auth gate — engaged only when start_server flags the
# bind as non-loopback-without-insecure.  No-op pass-through in loopback
# mode so the legacy auth_middleware (below) handles those binds via
# the injected ``_SESSION_TOKEN``.  Registered between host_header and
# auth_middleware so the order is: host check → cookie auth → token auth.
# ---------------------------------------------------------------------------


@app.middleware("http")
async def _dashboard_auth_gate(request: Request, call_next):
    from hermes_cli.dashboard_auth.middleware import gated_auth_middleware
    return await gated_auth_middleware(request, call_next)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Require the session token on /api/ routes and gate assistant-mode APIs."""
    path = request.url.path
    if path.startswith("/api/") and _assistant_mode_enabled() and not _assistant_api_allowed(path):
        return JSONResponse(
            status_code=404,
            content={"detail": "Not found"},
        )

    # When the OAuth gate is active, cookie-based auth (gated_auth_middleware
    # above) is authoritative.  The legacy _SESSION_TOKEN path is loopback-only
    # and is skipped here so the gate's session attachment isn't overridden.
    if getattr(request.app.state, "auth_required", False):
        return await call_next(request)

    if path.startswith("/api/"):
        if path not in _PUBLIC_API_PATHS and not _has_valid_session_token(request):
            return JSONResponse(
                status_code=401,
                content={"detail": "Unauthorized"},
            )
    return await call_next(request)


# ---------------------------------------------------------------------------
# Config schema — auto-generated from DEFAULT_CONFIG
# ---------------------------------------------------------------------------

# Manual overrides for fields that need select options or custom types
_SCHEMA_OVERRIDES: Dict[str, Dict[str, Any]] = {
    "model": {
        "type": "string",
        "description": "Default model (e.g. anthropic/claude-sonnet-4.6)",
        "category": "general",
    },
    "model_context_length": {
        "type": "number",
        "description": "Context window override (0 = auto-detect from model metadata)",
        "category": "general",
    },
    "terminal.backend": {
        "type": "select",
        "description": "Terminal execution backend",
        "options": ["local", "docker", "ssh", "modal", "daytona", "singularity"],
    },
    "terminal.modal_mode": {
        "type": "select",
        "description": "Modal sandbox mode",
        "options": ["sandbox", "function"],
    },
    "tts.provider": {
        "type": "select",
        "description": "Text-to-speech provider",
        "options": ["edge", "elevenlabs", "openai", "neutts"],
    },
    "stt.provider": {
        "type": "select",
        "description": "Speech-to-text provider",
        # "mistral" temporarily removed — mistralai PyPI package quarantined
        # (malicious 2.4.6 release on 2026-05-12). Restore once available.
        "options": ["local", "groq", "openai", "xai", "elevenlabs"],
    },
    "stt.elevenlabs.model_id": {
        "type": "select",
        "description": "ElevenLabs Scribe model",
        "options": ["scribe_v2", "scribe_v1"],
    },
    "display.skin": {
        "type": "select",
        "description": "CLI visual theme",
        "options": ["default", "ares", "mono", "slate"],
    },
    "dashboard.theme": {
        "type": "select",
        "description": "Web dashboard visual theme",
        "options": ["default", "midnight", "ember", "mono", "cyberpunk", "rose"],
    },
    "display.resume_display": {
        "type": "select",
        "description": "How resumed sessions display history",
        "options": ["minimal", "full", "off"],
    },
    "display.busy_input_mode": {
        "type": "select",
        "description": "Input behavior while agent is running",
        "options": ["interrupt", "queue", "steer"],
    },
    "memory.provider": {
        "type": "select",
        "description": "Memory provider plugin",
        "options": ["builtin", "honcho"],
    },
    "approvals.mode": {
        "type": "select",
        "description": "Dangerous command approval mode",
        "options": ["ask", "yolo", "deny"],
    },
    "context.engine": {
        "type": "select",
        "description": "Context management engine",
        "options": ["default", "custom"],
    },
    "human_delay.mode": {
        "type": "select",
        "description": "Simulated typing delay mode",
        "options": ["off", "typing", "fixed"],
    },
    "logging.level": {
        "type": "select",
        "description": "Log level for agent.log",
        "options": ["DEBUG", "INFO", "WARNING", "ERROR"],
    },
    "agent.service_tier": {
        "type": "select",
        "description": "API service tier (OpenAI/Anthropic)",
        "options": ["", "auto", "default", "flex"],
    },
    "delegation.reasoning_effort": {
        "type": "select",
        "description": "Reasoning effort for delegated subagents",
        "options": ["", "low", "medium", "high"],
    },
}

# Categories with fewer fields get merged into "general" to avoid tab sprawl.
_CATEGORY_MERGE: Dict[str, str] = {
    "privacy": "security",
    "context": "agent",
    "skills": "agent",
    "cron": "agent",
    "network": "agent",
    "checkpoints": "agent",
    "approvals": "security",
    "human_delay": "display",
    "dashboard": "display",
    "code_execution": "agent",
    "prompt_caching": "agent",
    "goals": "agent",
    # Only `telegram.reactions` currently lives under telegram — fold it in
    # with the other messaging-platform config (discord) so it isn't an
    # orphan tab of one field.
    "telegram": "discord",
}

# Display order for tabs — unlisted categories sort alphabetically after these.
_CATEGORY_ORDER = [
    "general", "agent", "terminal", "display", "delegation",
    "memory", "compression", "security", "browser", "voice",
    "tts", "stt", "logging", "discord", "auxiliary",
]


def _infer_type(value: Any) -> str:
    """Infer a UI field type from a Python value."""
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "number"
    if isinstance(value, float):
        return "number"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "object"
    return "string"


def _build_schema_from_config(
    config: Dict[str, Any],
    prefix: str = "",
) -> Dict[str, Dict[str, Any]]:
    """Walk DEFAULT_CONFIG and produce a flat dot-path → field schema dict."""
    schema: Dict[str, Dict[str, Any]] = {}
    for key, value in config.items():
        full_key = f"{prefix}.{key}" if prefix else key

        # Skip internal / version keys
        if full_key in {"_config_version",}:
            continue

        # Category is the first path component for nested keys, or "general"
        # for top-level scalar fields (model, toolsets, timezone, etc.).
        if prefix:
            category = prefix.split(".")[0]
        elif isinstance(value, dict):
            category = key
        else:
            category = "general"

        if isinstance(value, dict):
            # Recurse into nested dicts
            schema.update(_build_schema_from_config(value, full_key))
        else:
            entry: Dict[str, Any] = {
                "type": _infer_type(value),
                "description": full_key.replace(".", " → ").replace("_", " ").title(),
                "category": category,
            }
            # Apply manual overrides
            if full_key in _SCHEMA_OVERRIDES:
                entry.update(_SCHEMA_OVERRIDES[full_key])
            # Merge small categories
            entry["category"] = _CATEGORY_MERGE.get(entry["category"], entry["category"])
            schema[full_key] = entry
    return schema


CONFIG_SCHEMA = _build_schema_from_config(DEFAULT_CONFIG)

# Inject virtual fields that don't live in DEFAULT_CONFIG but are surfaced
# by the normalize/denormalize cycle.  Insert model_context_length right after
# the "model" key so it renders adjacent in the frontend.
_mcl_entry = _SCHEMA_OVERRIDES["model_context_length"]
_ordered_schema: Dict[str, Dict[str, Any]] = {}
for _k, _v in CONFIG_SCHEMA.items():
    _ordered_schema[_k] = _v
    if _k == "model":
        _ordered_schema["model_context_length"] = _mcl_entry
CONFIG_SCHEMA = _ordered_schema


class ConfigUpdate(BaseModel):
    config: dict


class EnvVarUpdate(BaseModel):
    key: str
    value: str


class EnvVarDelete(BaseModel):
    key: str


class EnvVarReveal(BaseModel):
    key: str


class MessagingPlatformUpdate(BaseModel):
    enabled: Optional[bool] = None
    env: Dict[str, str] = {}
    clear_env: List[str] = []


class TelegramOnboardingStart(BaseModel):
    bot_name: Optional[str] = None


class TelegramOnboardingApply(BaseModel):
    allowed_user_ids: List[str]


class AudioTranscriptionRequest(BaseModel):
    data_url: str
    mime_type: Optional[str] = None


class ModelAssignment(BaseModel):
    """Payload for POST /api/model/set — assign a provider/model to a slot.

    scope="main"        → writes model.provider + model.default
    scope="auxiliary"   → writes auxiliary.<task>.provider + auxiliary.<task>.model
    scope="auxiliary" with task=""  → applied to every auxiliary.* slot
    scope="auxiliary" with task="__reset__"  → resets every slot to provider="auto"
    """

    scope: str
    provider: str
    model: str
    task: str = ""


_AUDIO_MIME_EXTENSIONS: Dict[str, str] = {
    "audio/aac": ".aac",
    "audio/flac": ".flac",
    "audio/m4a": ".m4a",
    "audio/mp3": ".mp3",
    "audio/mp4": ".mp4",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
    "audio/wav": ".wav",
    "audio/wave": ".wav",
    "audio/webm": ".webm",
    "audio/x-m4a": ".m4a",
    "audio/x-wav": ".wav",
    "video/webm": ".webm",
}
_MAX_TRANSCRIPTION_UPLOAD_BYTES = 25 * 1024 * 1024


def _audio_extension_for_mime(mime_type: str) -> str:
    normalized = (mime_type or "").split(";", 1)[0].strip().lower()
    return _AUDIO_MIME_EXTENSIONS.get(normalized, ".webm")


class ModelAssignment(BaseModel):
    """Payload for POST /api/model/set — assign a provider/model to a slot.

    scope="main"        → writes model.provider + model.default
    scope="auxiliary"   → writes auxiliary.<task>.provider + auxiliary.<task>.model
    scope="auxiliary" with task=""  → applied to every auxiliary.* slot
    scope="auxiliary" with task="__reset__"  → resets every slot to provider="auto"
    """
    scope: str
    provider: str
    model: str
    task: str = ""
    # Optional OpenAI-compatible endpoint URL. Only honored for custom/local
    # providers on the main slot — lets the GUI configure a self-hosted endpoint
    # (vLLM, llama.cpp, Ollama, …) that needs no API key. The runtime resolver
    # reads model.base_url from config (it ignores OPENAI_BASE_URL), so this is
    # the path that actually wires a local endpoint into resolution.
    base_url: str = ""


def _apply_main_model_assignment(
    model_cfg: "Any", provider: str, model: str, base_url: str = ""
) -> dict:
    """Apply a main-slot model assignment to a ``model`` config dict in place.

    Sets ``provider``/``default``, then reconciles ``base_url``: custom/local
    providers persist the supplied endpoint URL (the runtime resolver reads
    ``model.base_url`` from config and ignores ``OPENAI_BASE_URL``), while every
    other provider clears any stale URL so the resolver picks that provider's
    own default endpoint. The hardcoded ``context_length`` override is always
    dropped since the new model may have a different context window.

    Returns the same dict (coerced to a fresh dict if the input wasn't one) so
    callers can assign it straight back onto ``cfg["model"]``.
    """
    if not isinstance(model_cfg, dict):
        model_cfg = {}
    model_cfg["provider"] = provider
    model_cfg["default"] = model
    if provider.strip().lower() == "custom" and base_url.strip():
        model_cfg["base_url"] = base_url.strip()
    elif model_cfg.get("base_url"):
        model_cfg["base_url"] = ""
    model_cfg.pop("context_length", None)
    return model_cfg


_GATEWAY_HEALTH_URL = os.getenv("GATEWAY_HEALTH_URL")
try:
    _GATEWAY_HEALTH_TIMEOUT = float(os.getenv("GATEWAY_HEALTH_TIMEOUT", "3"))
except (ValueError, TypeError):
    _log.warning(
        "Invalid GATEWAY_HEALTH_TIMEOUT value %r — using default 3.0s",
        os.getenv("GATEWAY_HEALTH_TIMEOUT"),
    )
    _GATEWAY_HEALTH_TIMEOUT = 3.0

# DEPRECATED (scheduled for removal): GATEWAY_HEALTH_URL / GATEWAY_HEALTH_TIMEOUT.
# Cross-container / cross-host gateway liveness detection will be folded into a
# first-class dashboard config key so it's no longer Docker-adjacent lore buried
# in env vars.  The env vars still work for now so existing Compose deployments
# don't break.  Do not add new callers — wire new uses through the planned
# config surface.


def _probe_gateway_health() -> tuple[bool, dict | None]:
    """Probe the gateway via its HTTP health endpoint (cross-container).

    .. deprecated::
        Driven by the deprecated ``GATEWAY_HEALTH_URL`` /
        ``GATEWAY_HEALTH_TIMEOUT`` env vars.  Scheduled for removal alongside
        a move to a first-class dashboard config key.  See
        :data:`_GATEWAY_HEALTH_URL` for context.

    Uses ``/health/detailed`` first (returns full state), falling back to
    the simpler ``/health`` endpoint.  Returns ``(is_alive, body_dict)``.

    Accepts any of these as ``GATEWAY_HEALTH_URL``:
    - ``http://gateway:8642``                (base URL — recommended)
    - ``http://gateway:8642/health``         (explicit health path)
    - ``http://gateway:8642/health/detailed`` (explicit detailed path)

    This is a **blocking** call — run via ``run_in_executor`` from async code.
    """
    if not _GATEWAY_HEALTH_URL:
        return False, None

    # Normalise to base URL so we always probe the right paths regardless of
    # whether the user included /health or /health/detailed in the env var.
    base = _GATEWAY_HEALTH_URL.rstrip("/")
    if base.endswith("/health/detailed"):
        base = base[: -len("/health/detailed")]
    elif base.endswith("/health"):
        base = base[: -len("/health")]

    for path in (f"{base}/health/detailed", f"{base}/health"):
        try:
            req = urllib.request.Request(path, method="GET")
            with urllib.request.urlopen(req, timeout=_GATEWAY_HEALTH_TIMEOUT) as resp:
                if resp.status == 200:
                    body = json.loads(resp.read())
                    return True, body
        except Exception:
            continue
    return False, None


@app.get("/api/status")
async def get_status():
    current_ver, latest_ver = check_config_version()

    # --- Gateway liveness detection ---
    # Try local PID check first (same-host).  If that fails and a remote
    # GATEWAY_HEALTH_URL is configured, probe the gateway over HTTP so the
    # dashboard works when the gateway runs in a separate container.
    gateway_pid = get_running_pid()
    gateway_running = gateway_pid is not None
    remote_health_body: dict | None = None

    if not gateway_running and _GATEWAY_HEALTH_URL:
        loop = asyncio.get_running_loop()
        alive, remote_health_body = await loop.run_in_executor(
            None, _probe_gateway_health
        )
        if alive:
            gateway_running = True
            # PID from the remote container (display only — not locally valid)
            if remote_health_body:
                gateway_pid = remote_health_body.get("pid")

    gateway_state = None
    gateway_platforms: dict = {}
    gateway_exit_reason = None
    gateway_updated_at = None
    configured_gateway_platforms: set[str] | None = None
    try:
        from gateway.config import load_gateway_config

        gateway_config = load_gateway_config()
        configured_gateway_platforms = {
            platform.value for platform in gateway_config.get_connected_platforms()
        }
    except Exception:
        configured_gateway_platforms = None

    # Prefer the detailed health endpoint response (has full state) when the
    # local runtime status file is absent or stale (cross-container).
    runtime = read_runtime_status()
    if runtime is None and remote_health_body and remote_health_body.get("gateway_state"):
        runtime = remote_health_body

    if runtime:
        gateway_state = runtime.get("gateway_state")
        gateway_platforms = runtime.get("platforms") or {}
        if configured_gateway_platforms is not None:
            gateway_platforms = {
                key: value
                for key, value in gateway_platforms.items()
                if key in configured_gateway_platforms
            }
        gateway_exit_reason = runtime.get("exit_reason")
        gateway_updated_at = runtime.get("updated_at")
        if not gateway_running:
            gateway_state = gateway_state if gateway_state in {"stopped", "startup_failed"} else "stopped"
            gateway_platforms = {}
        elif gateway_running and remote_health_body is not None:
            # The health probe confirmed the gateway is alive, but the local
            # runtime status file may be stale (cross-container).  Override
            # stopped/None state so the dashboard shows the correct badge.
            if gateway_state in {None, "stopped"}:
                gateway_state = "running"

    # If there was no runtime info at all but the health probe confirmed alive,
    # ensure we still report the gateway as running (no shared volume scenario).
    if gateway_running and gateway_state is None and remote_health_body is not None:
        gateway_state = "running"

    active_sessions = 0
    try:
        from hermes_state import SessionDB
        db = SessionDB()
        try:
            sessions = db.list_sessions_rich(limit=50)
            now = time.time()
            active_sessions = sum(
                1 for s in sessions
                if s.get("ended_at") is None
                and (now - s.get("last_active", s.get("started_at", 0))) < 300
            )
        finally:
            db.close()
    except Exception:
        pass

    # Dashboard auth gate (Phase 7): surface whether the gate is engaged
    # and which providers are registered so ``hermes status`` and the
    # SPA's StatusPage can show "OAuth gate ON via Nous Research" or
    # "loopback only — no auth gate" with no extra round trips.
    auth_required = bool(getattr(app.state, "auth_required", False))
    auth_providers: list[str] = []
    try:
        from hermes_cli.dashboard_auth import list_providers as _list_providers
        auth_providers = [p.name for p in _list_providers()]
    except Exception:
        # Module not importable yet (early startup) — leave as [].
        pass

    return {
        "version": __version__,
        "release_date": __release_date__,
        "hermes_home": str(get_hermes_home()),
        "config_path": str(get_config_path()),
        "env_path": str(get_env_path()),
        "config_version": current_ver,
        "latest_config_version": latest_ver,
        "gateway_running": gateway_running,
        "gateway_pid": gateway_pid,
        "gateway_health_url": _GATEWAY_HEALTH_URL,
        "gateway_state": gateway_state,
        "gateway_platforms": gateway_platforms,
        "gateway_exit_reason": gateway_exit_reason,
        "gateway_updated_at": gateway_updated_at,
        "active_sessions": active_sessions,
        "auth_required": auth_required,
        "auth_providers": auth_providers,
    }



@app.get("/api/assistant/resources")
async def get_assistant_resources(request: Request, refresh: str | None = None, resource: str | None = None):
    """Return sanitized AIWerk CUI resource summaries for the right rail."""
    _require_token(request)
    force_refresh = _bool_config_value(refresh)
    refresh_resource = str(resource or "").strip().lower() or None
    if refresh_resource and refresh_resource not in {"email", "calendar", "shared_folder", "vault", "todos", "contacts", "connectors"}:
        raise HTTPException(status_code=400, detail="Unknown resource")
    try:
        return await asyncio.to_thread(
            _assistant_resources_payload,
            request,
            force_refresh=force_refresh,
            refresh_resource=refresh_resource,
        )
    except Exception:
        _log.exception("GET /api/assistant/resources failed")
        raise HTTPException(status_code=500, detail="Resource summary failed")




@app.get("/api/cui/contacts/search")
async def search_cui_contacts(request: Request, q: str = ""):
    """Search sanitized CUI contacts without exposing raw connector metadata."""
    _require_token(request)
    try:
        return _search_contacts_payload(q)
    except Exception:
        _log.exception("GET /api/cui/contacts/search failed")
        raise HTTPException(status_code=500, detail="Contact search failed")


@app.get("/api/cui/context/contacts")
async def get_cui_context_contacts(request: Request, session_id: str = ""):
    """Return deterministic context contact suggestions for the current session."""
    _require_token(request)
    try:
        resources = _assistant_resources_payload(request, force_refresh=False)
        contacts = resources.get("contacts", {}) if isinstance(resources, dict) else {}
        return {"items": contacts.get("relevant") or [], "session_id": session_id}
    except Exception:
        _log.exception("GET /api/cui/context/contacts failed")
        raise HTTPException(status_code=500, detail="Context contacts failed")


@app.get("/api/cui/contacts/frequent")
async def get_cui_frequent_contacts(request: Request):
    """Return frequent/safe fallback contacts for the CUI right rail."""
    _require_token(request)
    try:
        resources = _assistant_resources_payload(request, force_refresh=False)
        contacts = resources.get("contacts", {}) if isinstance(resources, dict) else {}
        return {"items": contacts.get("frequent") or [], "total_count": contacts.get("total_count") or 0}
    except Exception:
        _log.exception("GET /api/cui/contacts/frequent failed")
        raise HTTPException(status_code=500, detail="Frequent contacts failed")


@app.post("/api/cui/contacts")
async def create_cui_contact(request: Request, payload: CuiContactCreateRequest):
    """Create one manual CUI contact in a tenant-local sanitized JSON store."""
    _require_token(request)
    raw = {
        "display_name": payload.name,
        "organization": payload.organization,
        "role": payload.role,
        "email": payload.email,
        "phone": payload.phone,
        "note": payload.note,
        "source_badges": ["Manuell"],
        "relevance": "relevant" if payload.link_current_context else "frequent",
    }
    contact = _normalize_contact(raw, source="Manuell", relevance=raw["relevance"])
    if not contact:
        raise HTTPException(status_code=400, detail="Contact name, email or phone required")
    try:
        contacts = _dedupe_contacts([contact, *_read_manual_contacts()])
        _write_manual_contacts(contacts)
        with _ASSISTANT_RESOURCE_CACHE_LOCK:
            for key in list(_ASSISTANT_RESOURCE_CACHE):
                if key.startswith("contacts:"):
                    _ASSISTANT_RESOURCE_CACHE.pop(key, None)
        return {"ok": True, "contact": contact}
    except Exception:
        _log.exception("POST /api/cui/contacts failed")
        raise HTTPException(status_code=500, detail="Contact create failed")


@app.post("/api/cui/contacts/hide")
async def hide_cui_contact(request: Request, payload: CuiContactHideRequest):
    """Hide one generated/connected contact from default and search contact lists."""
    _require_token(request)
    raw_contact = {"id": payload.id, "email": payload.email, "phone": payload.phone, "display_name": payload.display_name}
    keys = _contact_hide_keys(raw_contact)
    if not keys:
        raise HTTPException(status_code=400, detail="Contact identity required")
    try:
        hidden = _read_hidden_contact_keys()
        hidden.update(keys)
        _write_hidden_contact_keys(hidden)
        with _ASSISTANT_RESOURCE_CACHE_LOCK:
            for key in list(_ASSISTANT_RESOURCE_CACHE):
                if key.startswith("contacts:"):
                    _ASSISTANT_RESOURCE_CACHE.pop(key, None)
        return {"ok": True, "hidden": sorted(hidden)}
    except Exception:
        _log.exception("POST /api/cui/contacts/hide failed")
        raise HTTPException(status_code=500, detail="Contact hide failed")


@app.post("/api/assistant/support")
async def submit_assistant_support(request: Request, payload: AssistantSupportRequest):
    """Save and deliver a sanitized AIWerk CUI support message to the admin channel."""
    _require_token(request)
    try:
        return _handle_assistant_support(payload, request)
    except HTTPException:
        raise
    except Exception:
        _log.exception("POST /api/assistant/support failed")
        raise HTTPException(status_code=500, detail="Support message failed")


@app.post("/api/assistant/todos/add")
async def add_assistant_todo(request: Request, payload: AssistantTodoAddRequest):
    """Append one Markdown TODO item for the AIWerk CUI right rail."""
    _require_token(request)
    try:
        todos = _add_todo_item(load_config(), payload.text)
        return {"ok": True, "todos": todos}
    except HTTPException:
        raise
    except Exception:
        _log.exception("POST /api/assistant/todos/add failed")
        raise HTTPException(status_code=500, detail="TODO add failed")


@app.post("/api/assistant/todos/update")
async def update_assistant_todo(request: Request, payload: AssistantTodoUpdateRequest):
    """Mark one Markdown TODO item done/undone for the AIWerk CUI right rail."""
    _require_token(request)
    try:
        todos = _update_todo_item_done(load_config(), payload.id, payload.done)
        return {"ok": True, "todos": todos}
    except HTTPException:
        raise
    except Exception:
        _log.exception("POST /api/assistant/todos/update failed")
        raise HTTPException(status_code=500, detail="TODO update failed")


@app.get("/api/assistant/email/view")
async def view_assistant_email(request: Request):
    """Open a read-only sanitized CUI email view for an IMAP/Himalaya message."""
    _require_token(request)
    account_ref = (request.query_params.get("account") or "").strip()
    message_id = (request.query_params.get("id") or "").strip()
    if not account_ref or not message_id:
        raise HTTPException(status_code=400, detail="Missing email account or message id")
    config = load_config()
    account_cfg = _find_email_account_config(config, account_ref)
    if not account_cfg:
        raise HTTPException(status_code=404, detail="Email account not configured")
    account = str(account_cfg.get("account") or account_cfg.get("name") or "").strip() or None
    folder = str(account_cfg.get("folder") or account_cfg.get("mailbox") or "").strip() or None
    backend = _email_backend_name(account_cfg)
    account_label = _email_account_address(account_cfg, _email_account_label(account_cfg, account or "Mailbox"))
    sender = ""
    subject = "Ohne Betreff"
    received_at = ""
    try:
        resources = _assistant_resources_payload(request, force_refresh=False)
        email_resource = resources.get("email") if isinstance(resources, dict) else None
        accounts = email_resource.get("accounts") if isinstance(email_resource, dict) else None
        if isinstance(accounts, list):
            for account_entry in accounts:
                if not isinstance(account_entry, dict):
                    continue
                if str(account_entry.get("address") or account_entry.get("label") or "") != account_ref:
                    continue
                for item in account_entry.get("items") or []:
                    if not isinstance(item, dict):
                        continue
                    if message_id in {str(item.get("message_id") or ""), str(item.get("id") or "")}:
                        sender = str(item.get("sender") or "")
                        subject = str(item.get("subject") or subject)
                        received_at = str(item.get("received_at") or "")
                        break
    except Exception:
        _log.debug("Could not hydrate email metadata for reader", exc_info=True)
    try:
        if _is_google_email_backend(backend):
            body = _run_google_workspace_message_read(config, account_cfg, message_id=message_id)
        else:
            body = _run_himalaya_message_read(message_id=message_id, account=account, folder=folder)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid message id")
    except FileNotFoundError:
        raise HTTPException(status_code=503, detail="Himalaya is not installed")
    except Exception as exc:
        _log.debug("CUI email reader failed: %s", exc)
        raise HTTPException(status_code=502, detail="Email could not be loaded")
    page = _plain_email_reader_html(
        account_label=account_label,
        sender=sender,
        subject=subject,
        received_at=received_at,
        body=body,
    )
    return HTMLResponse(
        page,
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src 'none'; frame-ancestors 'none'; base-uri 'none'",
            "Content-Disposition": "inline",
        },
    )


@app.get("/api/assistant/calendar/view")
async def view_assistant_calendar_event(request: Request):
    """Open a read-only sanitized CUI calendar event view."""
    _require_token(request)
    account_ref = (request.query_params.get("account") or "").strip()
    event_id = (request.query_params.get("id") or "").strip()
    if not account_ref or not event_id:
        raise HTTPException(status_code=400, detail="Missing calendar account or event id")
    try:
        resources = _assistant_resources_payload(request, force_refresh=False)
        calendar_resource = resources.get("calendar") if isinstance(resources, dict) else None
        accounts = calendar_resource.get("accounts") if isinstance(calendar_resource, dict) else None
        found: dict[str, Any] | None = None
        account_label = account_ref
        if isinstance(accounts, list):
            for account_entry in accounts:
                if not isinstance(account_entry, dict):
                    continue
                candidate_account = str(account_entry.get("address") or account_entry.get("label") or "")
                if candidate_account != account_ref:
                    continue
                account_label = candidate_account or account_ref
                for item in account_entry.get("items") or []:
                    if not isinstance(item, dict):
                        continue
                    if event_id in {str(item.get("event_id") or ""), str(item.get("id") or "")}:
                        found = item
                        break
                if found:
                    break
        if not found and isinstance(calendar_resource, dict):
            for item in calendar_resource.get("items") or []:
                if not isinstance(item, dict):
                    continue
                if event_id in {str(item.get("event_id") or ""), str(item.get("id") or "")}:
                    found = item
                    account_label = str(item.get("account_address") or item.get("account_label") or account_ref)
                    break
        if not found:
            raise HTTPException(status_code=404, detail="Calendar event not found")
        config = load_config()
        detail = _fetch_google_workspace_calendar_event_detail(
            config,
            _calendar_account_config_for_ref(config, account_label or account_ref),
            event_id,
        )
        if detail:
            found = {**found, **detail}
        title = str(found.get("title") or "Termin")
        starts_at = str(found.get("starts_at") or "")
        ends_at = str(found.get("ends_at") or "")
        location = str(found.get("location_hint") or "")
        body_lines: list[str] = []
        description = str(found.get("description") or "").strip()
        if description:
            body_lines.append(description)
        if found.get("html_link"):
            body_lines.append("Link: [LINK]")
        page = _plain_calendar_reader_html(
            account_label=account_label,
            title=title,
            starts_at=starts_at,
            ends_at=ends_at,
            location=location,
            body="\n".join(body_lines),
        )
    except HTTPException:
        raise
    except Exception as exc:
        _log.debug("CUI calendar reader failed: %s", exc)
        raise HTTPException(status_code=502, detail="Calendar event could not be loaded")
    return HTMLResponse(
        page,
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src 'none'; frame-ancestors 'none'; base-uri 'none'",
            "Content-Disposition": "inline",
        },
    )


@app.post("/api/assistant/shared-folder/open-folder")
async def open_assistant_shared_folder_root(request: Request):
    """Open the local shared-folder root in the server machine's file manager."""
    _require_token(request)
    config = load_config()
    shared_root = _resolve_shared_folder_root(config)
    if not shared_root:
        raise HTTPException(status_code=409, detail="Shared folder is not locally mounted")
    if not _open_system_folder(shared_root, request=request, config=config):
        raise HTTPException(status_code=409, detail="File manager is not available")
    return {"ok": True}


@app.get("/api/assistant/shared-folder/open")
async def open_assistant_shared_folder_file(request: Request):
    """Open a sanitized shared-folder file through the CUI backend."""
    _require_token(request)
    rel_path = request.query_params.get("path") or ""
    config = load_config()

    shared_root = _resolve_shared_folder_root(config)
    if shared_root:
        target = _resolve_shared_folder_file(shared_root, rel_path)
        if not target:
            raise HTTPException(status_code=404, detail="File not found")
        media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        media_type, disposition = _safe_shared_open_disposition(target.name, media_type)
        return FileResponse(
            target,
            media_type=media_type,
            filename=target.name,
            headers={
                "Content-Disposition": f"{disposition}; filename*=UTF-8''{urllib.parse.quote(target.name)}",
                "X-Content-Type-Options": "nosniff",
            },
        )

    cloud = _shared_cloud_config(config)
    if isinstance(cloud, dict):
        downloaded = _download_sftpgo_pubshare_file(cloud, rel_path)
        if not downloaded:
            raise HTTPException(status_code=404, detail="File not found")
        data, media_type, filename = downloaded
        media_type, disposition = _safe_shared_open_disposition(filename, media_type)
        return Response(
            data,
            media_type=media_type,
            headers={
                "Content-Disposition": f"{disposition}; filename*=UTF-8''{urllib.parse.quote(filename)}",
                "X-Content-Type-Options": "nosniff",
            },
        )

    raise HTTPException(status_code=404, detail="Shared folder not configured")


@app.post("/api/assistant/attachments")
async def upload_assistant_attachments(
    request: Request,
    files: List[UploadFile] = File(...),
    session_id: str = Form(""),
):
    """Store AIWerk Customer UI attachments in a session-scoped temp area."""
    _require_token(request)
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded")
    if len(files) > _ASSISTANT_UPLOAD_MAX_FILES:
        raise HTTPException(status_code=413, detail="Too many files")

    session_part = _safe_upload_component(session_id or "session", "session")
    batch_part = f"{int(time.time() * 1000)}-{secrets.token_hex(4)}"
    target_dir = _assistant_upload_root() / session_part / batch_part
    target_dir.mkdir(parents=True, exist_ok=True)

    total = 0
    uploaded: List[Dict[str, Any]] = []
    for idx, upload in enumerate(files, start=1):
        original = Path(upload.filename or f"attachment-{idx}").name
        safe_name = _safe_upload_component(original, f"attachment-{idx}")
        ext = Path(safe_name).suffix.lower()
        if ext not in _ASSISTANT_UPLOAD_EXTENSIONS:
            raise HTTPException(status_code=415, detail=f"Unsupported file type: {original}")

        data = await upload.read(_ASSISTANT_UPLOAD_MAX_FILE_BYTES + 1)
        if len(data) > _ASSISTANT_UPLOAD_MAX_FILE_BYTES:
            raise HTTPException(status_code=413, detail=f"File too large: {original}")
        total += len(data)
        if total > _ASSISTANT_UPLOAD_MAX_TOTAL_BYTES:
            raise HTTPException(status_code=413, detail="Attachment batch too large")

        dest = target_dir / f"{idx:02d}-{safe_name}"
        dest.write_bytes(data)
        try:
            dest.chmod(0o600)
        except Exception:
            pass

        content_type = upload.content_type or mimetypes.guess_type(dest.name)[0] or "application/octet-stream"
        extracted_text, extraction = _extract_uploaded_text(dest, content_type)
        uploaded.append({
            "name": original,
            "path": str(dest),
            "type": content_type,
            "size": len(data),
            "is_image": content_type.startswith("image/"),
            "extracted_text": extracted_text,
            "extraction": extraction,
        })

    return {"attachments": uploaded}


@app.post("/api/assistant/attachments/resource")
async def attach_assistant_resource(
    request: Request,
    payload: AssistantResourceAttachmentRequest,
):
    """Attach one right-rail resource to the current CUI session.

    Shared-folder files are copied as real file artifacts so images can reach
    multimodal models. Email and calendar items are converted into sanitized
    text context attachments.
    """
    _require_token(request)
    try:
        attachment = _create_resource_attachment(request, payload)
    except HTTPException:
        raise
    except Exception as exc:
        _log.exception("Assistant resource attachment failed")
        raise HTTPException(status_code=500, detail="Resource could not be attached") from exc
    return {"attachments": [attachment]}


@app.post("/api/assistant/transcribe")
async def transcribe_assistant_audio(
    request: Request,
    file: UploadFile = File(...),
    session_id: str = Form(""),
):
    """Transcribe one browser-recorded audio clip for the AIWerk Customer UI."""
    _require_token(request)
    original = Path(file.filename or "sprache.webm").name
    safe_name = _safe_upload_component(original, "sprache.webm")
    ext = Path(safe_name).suffix.lower()
    if ext not in _ASSISTANT_AUDIO_EXTENSIONS:
        raise HTTPException(status_code=415, detail=f"Unsupported audio type: {original}")

    data = await file.read(_ASSISTANT_AUDIO_MAX_BYTES + 1)
    if len(data) > _ASSISTANT_AUDIO_MAX_BYTES:
        raise HTTPException(status_code=413, detail=f"Audio file too large: {original}")
    if not data:
        raise HTTPException(status_code=400, detail="Empty audio file")

    session_part = _safe_upload_component(session_id or "session", "session")
    batch_part = f"voice-{int(time.time() * 1000)}-{secrets.token_hex(4)}"
    target_dir = _assistant_upload_root() / session_part / batch_part
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / safe_name
    dest.write_bytes(data)
    try:
        dest.chmod(0o600)
    except Exception:
        pass

    try:
        from tools.transcription_tools import transcribe_audio
        result = await asyncio.to_thread(transcribe_audio, str(dest))
    except Exception as exc:
        _log.exception("Assistant audio transcription failed")
        raise HTTPException(status_code=500, detail="Transcription failed") from exc

    if not result.get("success"):
        raise HTTPException(status_code=500, detail=result.get("error") or "Transcription failed")
    return {
        "text": (result.get("transcript") or "").strip(),
        "provider": result.get("provider"),
    }


class AssistantTTSRequest(BaseModel):
    text: str
    session_id: Optional[str] = None


@app.post("/api/assistant/tts")
async def synthesize_assistant_speech(request: Request, body: AssistantTTSRequest):
    """Generate high-quality TTS audio for one AIWerk Customer UI answer."""
    _require_token(request)
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Text is required")
    if len(text) > 4_000:
        text = text[:4_000]

    session_part = _safe_upload_component(body.session_id or "session", "session")
    batch_part = f"tts-{int(time.time() * 1000)}-{secrets.token_hex(4)}"
    target_dir = _assistant_upload_root() / session_part / batch_part
    target_dir.mkdir(parents=True, exist_ok=True)
    output_path = target_dir / "answer.mp3"

    try:
        from tools.tts_tool import text_to_speech_tool
        raw = await asyncio.to_thread(text_to_speech_tool, text, str(output_path))
        result = json.loads(raw)
    except Exception as exc:
        _log.exception("Assistant speech synthesis failed")
        raise HTTPException(status_code=500, detail="Speech synthesis failed") from exc

    if not result.get("success"):
        detail = result.get("error") or "Speech synthesis failed"
        raise HTTPException(status_code=500, detail=detail)

    file_path = Path(str(result.get("file_path") or output_path)).expanduser().resolve()
    upload_root = _assistant_upload_root().resolve()
    if not str(file_path).startswith(str(upload_root) + os.sep) or not file_path.exists():
        raise HTTPException(status_code=500, detail="Speech synthesis produced no playable audio")

    media_type = mimetypes.guess_type(str(file_path))[0] or "audio/mpeg"
    return FileResponse(
        str(file_path),
        media_type=media_type,
        filename=file_path.name,
        headers={"Cache-Control": "no-store"},
    )
@app.get("/api/system/stats")
async def get_system_stats():
    """Host + process system stats for the System page.

    OS / Python / host identity from stdlib; CPU / memory / disk / uptime from
    psutil when available, with graceful degradation when it isn't.  Read-only
    and non-sensitive (no env values, no paths beyond the hermes home root).
    """
    import platform as _platform

    info: Dict[str, Any] = {
        "os": _platform.system(),
        "os_release": _platform.release(),
        "os_version": _platform.version(),
        "platform": _platform.platform(),
        "arch": _platform.machine(),
        "hostname": _platform.node(),
        "python_version": _platform.python_version(),
        "python_impl": _platform.python_implementation(),
        "hermes_version": __version__,
        "cpu_count": os.cpu_count(),
    }

    # psutil enriches the picture when present; everything below is optional.
    try:
        import psutil  # type: ignore

        vm = psutil.virtual_memory()
        info["memory"] = {
            "total": vm.total,
            "available": vm.available,
            "used": vm.used,
            "percent": vm.percent,
        }
        try:
            du = psutil.disk_usage(str(get_hermes_home()))
            info["disk"] = {
                "total": du.total,
                "used": du.used,
                "free": du.free,
                "percent": du.percent,
            }
        except Exception:
            pass
        try:
            info["cpu_percent"] = psutil.cpu_percent(interval=0.1)
            la = getattr(psutil, "getloadavg", None)
            if la:
                info["load_avg"] = list(la())
        except Exception:
            pass
        try:
            boot = psutil.boot_time()
            info["uptime_seconds"] = int(time.time() - boot)
        except Exception:
            pass
        try:
            proc = psutil.Process()
            info["process"] = {
                "pid": proc.pid,
                "rss": proc.memory_info().rss,
                "create_time": int(proc.create_time()),
                "num_threads": proc.num_threads(),
            }
        except Exception:
            pass
        info["psutil"] = True
    except Exception:
        info["psutil"] = False
        # stdlib-only fallbacks for load average + uptime where the kernel
        # exposes them.
        try:
            info["load_avg"] = list(os.getloadavg())
        except (OSError, AttributeError):
            pass

    return info


# ---------------------------------------------------------------------------
# Curator endpoints — background skill-maintenance status + controls.
#
# The curator periodically reviews skills (archive stale, prune, pin).  The
# dashboard surfaces its state and the pause/resume/run-now controls that
# `hermes curator` exposes.
# ---------------------------------------------------------------------------


@app.get("/api/curator")
async def get_curator_status():
    try:
        from agent import curator
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Curator unavailable: {exc}")
    try:
        state = curator.load_state()
    except Exception:
        state = {}
    return {
        "enabled": _safe_call(curator, "is_enabled", True),
        "paused": _safe_call(curator, "is_paused", False),
        "interval_hours": _safe_call(curator, "get_interval_hours", None),
        "last_run_at": state.get("last_run_at"),
        "min_idle_hours": _safe_call(curator, "get_min_idle_hours", None),
        "stale_after_days": _safe_call(curator, "get_stale_after_days", None),
        "archive_after_days": _safe_call(curator, "get_archive_after_days", None),
    }


class CuratorPause(BaseModel):
    paused: bool


@app.put("/api/curator/paused")
async def set_curator_paused(body: CuratorPause):
    from agent import curator

    curator.set_paused(bool(body.paused))
    return {"ok": True, "paused": bool(body.paused)}


@app.post("/api/curator/run")
async def run_curator():
    """Trigger a curator review now (backgrounded; tail via action status)."""
    try:
        proc = _spawn_hermes_action(["curator", "run"], "curator-run")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to run curator: {exc}")
    return {"ok": True, "pid": proc.pid, "name": "curator-run"}


def _safe_call(mod, fn_name: str, default):
    try:
        fn = getattr(mod, fn_name, None)
        return fn() if callable(fn) else default
    except Exception:
        return default


# ---------------------------------------------------------------------------
# Portal endpoint — Nous Portal auth + Tool Gateway routing status (read-only).
# ---------------------------------------------------------------------------


@app.get("/api/portal")
async def get_portal_status():
    cfg = load_config() or {}
    auth: Dict[str, Any] = {}
    try:
        from hermes_cli.auth import get_nous_auth_status

        auth = get_nous_auth_status() or {}
    except Exception:
        auth = {}

    features = []
    try:
        from hermes_cli.nous_subscription import get_nous_subscription_features

        feats = get_nous_subscription_features(cfg)
        if feats is not None:
            for feat in feats.items():
                if getattr(feat, "managed_by_nous", False):
                    state = "via Nous Portal"
                elif getattr(feat, "active", False) and getattr(feat, "current_provider", None):
                    state = feat.current_provider
                elif getattr(feat, "active", False):
                    state = "active"
                else:
                    state = "not configured"
                features.append({"label": getattr(feat, "label", ""), "state": state})
    except Exception:
        _log.exception("portal features failed")

    model_cfg = cfg.get("model") if isinstance(cfg.get("model"), dict) else {}
    return {
        "logged_in": bool(auth.get("logged_in")),
        "portal_url": auth.get("portal_base_url"),
        "inference_url": auth.get("inference_base_url"),
        "provider": str((model_cfg or {}).get("provider") or ""),
        "subscription_url": "https://portal.nousresearch.com/manage-subscription",
        "features": features,
    }


# ---------------------------------------------------------------------------
# Diagnostics: prompt-size, support dump, debug upload, config migrate.
# All produce text output, so they spawn background actions tailed via
# /api/actions/<name>/status.
# ---------------------------------------------------------------------------


@app.post("/api/ops/prompt-size")
async def run_prompt_size():
    try:
        proc = _spawn_hermes_action(["prompt-size"], "prompt-size")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed: {exc}")
    return {"ok": True, "pid": proc.pid, "name": "prompt-size"}


@app.post("/api/ops/dump")
async def run_dump():
    try:
        proc = _spawn_hermes_action(["dump"], "dump")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed: {exc}")
    return {"ok": True, "pid": proc.pid, "name": "dump"}


@app.post("/api/ops/config-migrate")
async def run_config_migrate():
    try:
        proc = _spawn_hermes_action(["config", "migrate"], "config-migrate")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed: {exc}")
    return {"ok": True, "pid": proc.pid, "name": "config-migrate"}


class DebugShareRequest(BaseModel):
    # Redaction is ON by default — force-mode scrubs credential-shaped tokens
    # out of log content before it leaves the machine. The toggle exists so an
    # operator who knows the logs are clean can opt out for fuller fidelity.
    redact: bool = True
    # Recent log lines included in the summary tail (full logs are separate).
    lines: int = 200


@app.post("/api/ops/debug-share")
async def run_debug_share_endpoint(body: DebugShareRequest | None = None):
    """Upload a redacted debug report + full logs and return the paste URLs.

    Unlike the other diagnostics actions (doctor, dump, prompt-size) this is
    *synchronous*: the whole point of ``debug share`` is the set of shareable
    URLs it produces, so we run the upload in a worker thread and return the
    structured ``{urls, failures, redacted, ...}`` payload directly. The
    dashboard renders those as real, copyable links instead of scraping a log
    tail. Pastes auto-delete after 6 hours (handled inside the share core).
    """
    from hermes_cli.debug import build_debug_share

    req = body or DebugShareRequest()
    try:
        result = await asyncio.to_thread(
            build_debug_share,
            log_lines=max(1, min(int(req.lines), 5000)),
            redact=bool(req.redact),
        )
    except RuntimeError as exc:
        # Required summary-report upload failed (offline / paste service down).
        raise HTTPException(status_code=502, detail=f"Upload failed: {exc}")
    except Exception as exc:
        _log.exception("debug share failed")
        raise HTTPException(status_code=500, detail=f"Failed: {exc}")

    return {
        "ok": True,
        "urls": result.urls,
        "failures": result.failures,
        "redacted": result.redacted,
        "auto_delete_seconds": result.auto_delete_seconds,
    }


# ---------------------------------------------------------------------------
# Gateway + update actions (invoked from the Status page).
#
# Both commands are spawned as detached subprocesses so the HTTP request
# returns immediately.  stdin is closed (``DEVNULL``) so any stray ``input()``
# calls fail fast with EOF rather than hanging forever.  stdout/stderr are
# streamed to a per-action log file under ``~/.hermes/logs/<action>.log`` so
# the dashboard can tail them back to the user.
# ---------------------------------------------------------------------------

_ACTION_LOG_DIR: Path = get_hermes_home() / "logs"

# Short ``name`` (from the URL) → absolute log file path.
_ACTION_LOG_FILES: Dict[str, str] = {
    "gateway-restart": "gateway-restart.log",
    "gateway-start": "gateway-start.log",
    "gateway-stop": "gateway-stop.log",
    "hermes-update": "hermes-update.log",
    "doctor": "action-doctor.log",
    "security-audit": "action-security-audit.log",
    "backup": "action-backup.log",
    "import": "action-import.log",
    "checkpoints-prune": "action-checkpoints-prune.log",
    "skills-install": "action-skills-install.log",
    "skills-uninstall": "action-skills-uninstall.log",
    "skills-update": "action-skills-update.log",
    "curator-run": "action-curator-run.log",
    "prompt-size": "action-prompt-size.log",
    "dump": "action-dump.log",
    "config-migrate": "action-config-migrate.log",
}

# ``name`` → most recently spawned Popen handle.  Used so ``status`` can
# report liveness and exit code without shelling out to ``ps``.
_ACTION_PROCS: Dict[str, subprocess.Popen] = {}

# ``name`` → completed synthetic action result for actions the server handled
# without spawning a subprocess (for example, unsupported Docker updates).
_ACTION_RESULTS: Dict[str, Dict[str, Any]] = {}


def _record_completed_action(name: str, message: str, exit_code: int = 1) -> None:
    """Record a non-spawned action result and write it to the action log."""
    log_file_name = _ACTION_LOG_FILES[name]
    _ACTION_LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = _ACTION_LOG_DIR / log_file_name
    with open(log_path, "ab", buffering=0) as log_file:
        log_file.write(
            f"\n=== {name} completed {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n".encode()
        )
        log_file.write(message.encode("utf-8", errors="replace"))
        if not message.endswith("\n"):
            log_file.write(b"\n")
    _ACTION_PROCS.pop(name, None)
    _ACTION_RESULTS[name] = {"exit_code": exit_code, "pid": None}


def _spawn_hermes_action(subcommand: List[str], name: str) -> subprocess.Popen:
    """Spawn ``hermes <subcommand>`` detached and record the Popen handle.

    Uses the running interpreter's ``hermes_cli.main`` module so the action
    inherits the same venv/PYTHONPATH the web server is using.
    """
    log_file_name = _ACTION_LOG_FILES[name]
    _ACTION_LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = _ACTION_LOG_DIR / log_file_name
    log_file = open(log_path, "ab", buffering=0)
    log_file.write(
        f"\n=== {name} started {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n".encode()
    )

    cmd = [sys.executable, "-m", "hermes_cli.main", *subcommand]

    popen_kwargs: Dict[str, Any] = {
        "cwd": str(PROJECT_ROOT),
        "stdin": subprocess.DEVNULL,
        "stdout": log_file,
        "stderr": subprocess.STDOUT,
        "env": {**os.environ, "HERMES_NONINTERACTIVE": "1"},
    }
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
            | getattr(subprocess, "DETACHED_PROCESS", 0)
        )
    else:
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(cmd, **popen_kwargs)
    # The child inherits its own duplicated fd for stdout/stderr, so the
    # parent's handle can be released immediately — otherwise we leak one
    # fd per spawned action.
    log_file.close()
    _ACTION_RESULTS.pop(name, None)
    _ACTION_PROCS[name] = proc
    return proc


def _tail_lines(path: Path, n: int) -> List[str]:
    """Return the last ``n`` lines of ``path``.  Reads the whole file — fine
    for our small per-action logs.  Binary-decoded with ``errors='replace'``
    so log corruption doesn't 500 the endpoint."""
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines = text.splitlines()
    return lines[-n:] if n > 0 else lines


@app.post("/api/gateway/restart")
async def restart_gateway():
    """Kick off a ``hermes gateway restart`` in the background."""
    try:
        proc = _spawn_hermes_action(["gateway", "restart"], "gateway-restart")
    except Exception as exc:
        _log.exception("Failed to spawn gateway restart")
        raise HTTPException(status_code=500, detail=f"Failed to restart gateway: {exc}")
    return {
        "ok": True,
        "pid": proc.pid,
        "name": "gateway-restart",
    }


@app.post("/api/hermes/update")
async def update_hermes():
    """Kick off ``hermes update`` in the background."""
    install_method = detect_install_method(PROJECT_ROOT)
    if install_method == "docker":
        message = format_docker_update_message()
        _record_completed_action("hermes-update", message, exit_code=1)
        return {
            "ok": False,
            "pid": None,
            "name": "hermes-update",
            "error": "docker_update_unsupported",
            "message": message,
            "update_command": recommended_update_command_for_method(install_method),
        }

    try:
        proc = _spawn_hermes_action(["update"], "hermes-update")
    except Exception as exc:
        _log.exception("Failed to spawn hermes update")
        raise HTTPException(status_code=500, detail=f"Failed to start update: {exc}")
    return {
        "ok": True,
        "pid": proc.pid,
        "name": "hermes-update",
    }


@app.get("/api/hermes/update/check")
async def check_hermes_update(force: bool = False):
    """Report whether a Hermes update is available, without applying it.

    Powers the dashboard's "check before you update" flow: the System page
    shows the commit-behind count and asks the user to confirm before
    ``POST /api/hermes/update`` actually runs ``hermes update``.

    Returns:
        install_method: 'git' | 'pip' | 'docker' | 'nixos' | 'homebrew' | ...
        current_version: installed Hermes version string
        behind: commits behind upstream (>=1), 0 if up to date,
                -1 if behind by an unknown count (nix/pypi), or null if the
                check could not run (offline, no remote, etc.)
        update_available: convenience bool (behind is non-zero and not null)
        can_apply: True when the dashboard's update button can apply it
                   in place (git/pip); False for docker/nix/homebrew where the
                   user must update out-of-band
        update_command: the recommended command for this install method
        message: human-readable guidance for non-applyable methods
    """
    install_method = detect_install_method(PROJECT_ROOT)
    update_command = recommended_update_command_for_method(install_method)

    payload: Dict[str, Any] = {
        "install_method": install_method,
        "current_version": __version__,
        "behind": None,
        "update_available": False,
        "can_apply": install_method in ("git", "pip"),
        "update_command": update_command,
        "message": None,
    }

    if install_method == "docker":
        payload["message"] = format_docker_update_message()
        return payload

    # banner.check_for_updates() handles git / pypi / nix-revision paths and
    # caches the result for 6h. ``force`` busts the cache so the "Check now"
    # button reflects reality immediately.
    try:
        from hermes_cli.banner import check_for_updates

        if force:
            try:
                (get_hermes_home() / ".update_check").unlink()
            except OSError:
                pass

        behind = await asyncio.to_thread(check_for_updates)
    except Exception:
        _log.exception("Update check failed")
        behind = None

    payload["behind"] = behind
    if behind is None:
        payload["message"] = "Couldn't reach the update source — try again later."
    elif behind == 0:
        payload["message"] = "You're on the latest version."
    else:
        payload["update_available"] = True

    return payload


@app.post("/api/audio/transcribe")
async def transcribe_audio_upload(payload: AudioTranscriptionRequest):
    data_url = (payload.data_url or "").strip()
    if not data_url.startswith("data:") or "," not in data_url:
        raise HTTPException(status_code=400, detail="Invalid audio payload")

    header, encoded = data_url.split(",", 1)
    if ";base64" not in header:
        raise HTTPException(
            status_code=400, detail="Audio payload must be base64 encoded"
        )

    mime_type = (
        payload.mime_type or header[5:].split(";", 1)[0] or "audio/webm"
    ).strip()
    normalized_mime_type = mime_type.split(";", 1)[0].lower()
    if not (
        normalized_mime_type.startswith("audio/")
        or normalized_mime_type == "video/webm"
    ):
        raise HTTPException(
            status_code=400, detail="Payload must be an audio recording"
        )

    try:
        audio_bytes = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=400, detail="Audio payload is not valid base64")

    if not audio_bytes:
        raise HTTPException(status_code=400, detail="Audio recording is empty")
    if len(audio_bytes) > _MAX_TRANSCRIPTION_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Audio recording is too large")

    temp_path = ""
    try:
        suffix = _audio_extension_for_mime(mime_type)
        with tempfile.NamedTemporaryFile(
            prefix="hermes-desktop-voice-",
            suffix=suffix,
            delete=False,
        ) as tmp:
            tmp.write(audio_bytes)
            temp_path = tmp.name

        from tools.transcription_tools import transcribe_audio

        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, transcribe_audio, temp_path)
    except HTTPException:
        raise
    except Exception as exc:
        _log.exception("Desktop voice transcription failed")
        raise HTTPException(status_code=500, detail=f"Transcription failed: {exc}")
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass

    if not result.get("success"):
        raise HTTPException(
            status_code=400,
            detail=result.get("error") or "Transcription failed",
        )

    return {
        "ok": True,
        "transcript": str(result.get("transcript") or "").strip(),
        "provider": result.get("provider"),
    }


class TTSSpeakRequest(BaseModel):
    text: str


def _elevenlabs_voice_label(voice: Dict[str, Any]) -> str:
    name = str(voice.get("name") or voice.get("voice_id") or "Voice").strip()
    category = str(voice.get("category") or "").strip()

    return f"{name} ({category})" if category else name


@app.get("/api/audio/elevenlabs/voices")
async def get_elevenlabs_voices():
    """Return ElevenLabs voices when an API key is configured.

    The desktop UI uses this for the ``tts.elevenlabs.voice_id`` dropdown.
    Only non-secret voice metadata is returned; the API key stays server-side.
    """
    api_key = (load_env().get("ELEVENLABS_API_KEY") or os.environ.get("ELEVENLABS_API_KEY") or "").strip()
    if not api_key:
        return {"available": False, "voices": []}

    request = urllib.request.Request(
        "https://api.elevenlabs.io/v1/voices",
        headers={
            "Accept": "application/json",
            "xi-api-key": api_key,
        },
    )

    try:
        loop = asyncio.get_running_loop()

        def _fetch() -> Dict[str, Any]:
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.loads(response.read().decode("utf-8"))

        payload = await loop.run_in_executor(None, _fetch)
    except Exception as exc:
        _log.warning("ElevenLabs voice list failed: %s", exc)
        raise HTTPException(status_code=502, detail="Could not load ElevenLabs voices")

    voices = []
    for voice in payload.get("voices") or []:
        if not isinstance(voice, dict):
            continue

        voice_id = str(voice.get("voice_id") or "").strip()
        if not voice_id:
            continue

        voices.append({
            "voice_id": voice_id,
            "name": str(voice.get("name") or voice_id),
            "label": _elevenlabs_voice_label(voice),
        })

    voices.sort(key=lambda item: str(item.get("label") or "").lower())
    return {"available": True, "voices": voices}


@app.post("/api/audio/speak")
async def speak_text(payload: TTSSpeakRequest):
    """Synthesize speech and return audio as base64 data URL.

    Used by the desktop voice-conversation mode to play back assistant
    responses without exposing the on-disk file path. Reuses the
    existing TTS provider chain (Edge / OpenAI / ElevenLabs / etc.)
    configured in ``~/.hermes/config.yaml`` under ``tts.``.
    """
    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Text is required")

    try:
        from tools.tts_tool import text_to_speech_tool
        loop = asyncio.get_running_loop()
        result_json = await loop.run_in_executor(None, text_to_speech_tool, text)
    except Exception as exc:
        _log.exception("Desktop voice TTS failed")
        raise HTTPException(status_code=500, detail=f"Speech synthesis failed: {exc}")

    try:
        result = json.loads(result_json) if isinstance(result_json, str) else result_json
    except Exception:
        raise HTTPException(status_code=500, detail="Invalid TTS response")

    if not result.get("success"):
        raise HTTPException(
            status_code=400,
            detail=result.get("error") or "Speech synthesis failed",
        )

    file_path = result.get("file_path")
    if not file_path or not os.path.isfile(file_path):
        raise HTTPException(status_code=500, detail="Audio file missing")

    ext = os.path.splitext(file_path)[1].lower()
    mime_type = {
        ".mp3": "audio/mpeg",
        ".ogg": "audio/ogg",
        ".opus": "audio/ogg",
        ".wav": "audio/wav",
        ".flac": "audio/flac",
    }.get(ext, "audio/mpeg")

    try:
        with open(file_path, "rb") as fh:
            audio_bytes = fh.read()
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Could not read audio: {exc}")
    finally:
        try:
            os.unlink(file_path)
        except OSError:
            pass

    encoded = base64.b64encode(audio_bytes).decode("ascii")
    return {
        "ok": True,
        "data_url": f"data:{mime_type};base64,{encoded}",
        "mime_type": mime_type,
        "provider": result.get("provider"),
    }


@app.get("/api/actions/{name}/status")
async def get_action_status(name: str, lines: int = 200):
    """Tail an action log and report whether the process is still running."""
    log_file_name = _ACTION_LOG_FILES.get(name)
    if log_file_name is None:
        raise HTTPException(status_code=404, detail=f"Unknown action: {name}")

    log_path = _ACTION_LOG_DIR / log_file_name
    tail = _tail_lines(log_path, min(max(lines, 1), 2000))

    proc = _ACTION_PROCS.get(name)
    if proc is None:
        result = _ACTION_RESULTS.get(name)
        running = False
        exit_code = result.get("exit_code") if result else None
        pid = result.get("pid") if result else None
    else:
        exit_code = proc.poll()
        running = exit_code is None
        pid = proc.pid

    return {
        "name": name,
        "running": running,
        "exit_code": exit_code,
        "pid": pid,
        "lines": tail,
    }


@app.get("/api/sessions")
async def get_sessions(
    limit: int = 20,
    offset: int = 0,
    exclude_sources: str = "",
    min_messages: int = 0,
    archived: str = "exclude",
    order: str = "created",
):
    """List sessions with AIWerk source filtering and upstream archive/order controls."""
    excluded = [source.strip() for source in exclude_sources.split(",") if source.strip()]
    if archived not in ("exclude", "only", "include"):
        raise HTTPException(
            status_code=400,
            detail="archived must be one of: exclude, only, include",
        )
    if order not in ("created", "recent"):
        raise HTTPException(
            status_code=400,
            detail="order must be one of: created, recent",
        )
    try:
        from hermes_state import SessionDB
        db = SessionDB()
        try:
            min_message_count = max(0, min_messages)
            archived_only = archived == "only"
            include_archived = archived == "include"
            sessions = db.list_sessions_rich(
                limit=limit,
                offset=offset,
                exclude_sources=excluded or None,
                min_message_count=min_message_count,
                include_archived=include_archived,
                archived_only=archived_only,
                order_by_last_active=order == "recent",
            )
            total = db.session_count(
                exclude_sources=excluded or None,
                min_message_count=min_message_count,
                include_archived=include_archived,
                archived_only=archived_only,
                exclude_children=True,
            )
            now = time.time()
            for s in sessions:
                s["is_active"] = (
                    s.get("ended_at") is None
                    and (now - s.get("last_active", s.get("started_at", 0))) < 300
                )
                # SQLite stores the flag as 0/1; expose a real JSON boolean.
                s["archived"] = bool(s.get("archived"))
            return {"sessions": sessions, "total": total, "limit": limit, "offset": offset}
        finally:
            db.close()
    except Exception:
        _log.exception("GET /api/sessions failed")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/profiles/sessions")
async def get_profiles_sessions(
    limit: int = 20,
    offset: int = 0,
    min_messages: int = 0,
    archived: str = "exclude",
    order: str = "recent",
    profile: str = "all",
):
    """Unified, read-only session list aggregated across ALL profiles.

    Intentionally process-light: this opens each profile's ``state.db`` directly
    from disk — it does NOT spawn a dashboard backend per profile. Each returned
    session is tagged with its owning ``profile`` so the desktop renders one
    browsable list and only spins up a profile's backend when the user actually
    interacts (sends a message). A user with a single (default) profile gets the
    same rows as ``/api/sessions``, just tagged ``profile="default"``.
    """
    if archived not in ("exclude", "only", "include"):
        raise HTTPException(status_code=400, detail="archived must be one of: exclude, only, include")
    if order not in ("created", "recent"):
        raise HTTPException(status_code=400, detail="order must be one of: created, recent")

    from hermes_state import SessionDB
    from hermes_cli import profiles as profiles_mod

    targets: List[Tuple[str, Path]] = []
    if profile and profile != "all":
        name, home = _cron_profile_home(profile)
        targets.append((name, home))
    else:
        try:
            infos = profiles_mod.list_profiles()
            targets = [(info.name, info.path) for info in infos]
        except Exception:
            _log.exception("GET /api/profiles/sessions: list_profiles failed")
            targets = []
        if not targets:
            targets.append(("default", profiles_mod.get_profile_dir("default")))

    min_message_count = max(0, min_messages)
    archived_only = archived == "only"
    include_archived = archived == "include"
    # Over-fetch per profile so the merged+sorted window is correct for the
    # requested page. Capped so a huge profile can't blow up the response.
    per_profile = min(max(limit + offset, limit), 500)

    merged: List[Dict[str, Any]] = []
    total = 0
    profile_totals: Dict[str, int] = {}
    errors: List[Dict[str, str]] = []
    now = time.time()
    for name, home in targets:
        db_path = Path(home) / "state.db"
        if not db_path.exists():
            continue
        try:
            # Read-only: this loop runs on every sidebar refresh, so it must
            # never DDL/write-lock another profile's live DB (see SessionDB
            # read_only docstring).
            db = SessionDB(db_path=db_path, read_only=True)
        except Exception as exc:
            errors.append({"profile": name, "error": str(exc)})
            continue
        try:
            rows = db.list_sessions_rich(
                limit=per_profile,
                offset=0,
                min_message_count=min_message_count,
                include_archived=include_archived,
                archived_only=archived_only,
                order_by_last_active=order == "recent",
            )
            profile_total = db.session_count(
                min_message_count=min_message_count,
                include_archived=include_archived,
                archived_only=archived_only,
                exclude_children=True,
            )
            total += profile_total
            profile_totals[name] = profile_total
            for s in rows:
                s["profile"] = name
                s["is_default_profile"] = name == "default"
                s["is_active"] = (
                    s.get("ended_at") is None
                    and (now - s.get("last_active", s.get("started_at", 0))) < 300
                )
                s["archived"] = bool(s.get("archived"))
                merged.append(s)
        except Exception as exc:
            errors.append({"profile": name, "error": str(exc)})
        finally:
            db.close()

    sort_key = "last_active" if order == "recent" else "started_at"
    merged.sort(key=lambda s: s.get(sort_key) or s.get("started_at") or 0, reverse=True)
    window = merged[offset:offset + limit]
    return {
        "sessions": window,
        "total": total,
        "profile_totals": profile_totals,
        "limit": limit,
        "offset": offset,
        "errors": errors,
    }


@app.get("/api/sessions/search")
async def search_sessions(q: str = "", limit: int = 20):
    """Search sessions by ID plus full-text message content using FTS5.

    Direct session-id matches are surfaced first, then FTS message-content
    matches. Results are deduped by compression lineage, not by raw
    ``session_id``. Auto-compression rotates a conversation onto a fresh
    session id (and leaves the old segment's messages in the FTS index), so one
    logical chat can own many ``sessions`` rows that all match the same query.
    Branches also use ``parent_session_id``, but they are real alternate
    conversations; don't collapse branch-specific hits back into the parent.
    """
    if not q or not q.strip():
        return {"results": []}
    try:
        from hermes_state import SessionDB
        db = SessionDB()
        try:
            safe_limit = max(1, min(int(limit or 20), 100))

            # Walk parent_session_id to the compression root, memoized so a
            # chain of compression segments only costs one walk. We deliberately
            # stop at branch/delegate edges: those sessions may diverge from the
            # parent and should remain searchable on their own.
            root_cache: dict = {}

            def compression_root(session_id: str) -> str:
                if not session_id:
                    return session_id
                if session_id in root_cache:
                    return root_cache[session_id]
                chain = []
                cur = session_id
                visited = set()
                root = session_id
                while cur and cur not in visited:
                    visited.add(cur)
                    chain.append(cur)
                    if cur in root_cache:
                        root = root_cache[cur]
                        break
                    try:
                        s = db.get_session(cur)
                    except Exception:
                        s = None
                    if not s:
                        root = cur
                        break
                    parent = s.get("parent_session_id") if isinstance(s, dict) else None
                    if not parent:
                        root = cur
                        break
                    try:
                        parent_session = db.get_session(parent)
                    except Exception:
                        parent_session = None
                    if not parent_session:
                        root = cur
                        break
                    parent_ended_at = parent_session.get("ended_at")
                    started_at = s.get("started_at")
                    is_compression_edge = (
                        parent_session.get("end_reason") == "compression"
                        and parent_ended_at is not None
                        and started_at is not None
                        and started_at >= parent_ended_at
                    )
                    if not is_compression_edge:
                        root = cur
                        break
                    cur = parent
                for node in chain:
                    root_cache[node] = root
                return root

            tip_cache: dict = {}

            def lineage_tip(root_id: str) -> str:
                if root_id in tip_cache:
                    return tip_cache[root_id]
                tip = root_id
                try:
                    resolved = db.get_compression_tip(root_id)
                    if resolved:
                        tip = resolved
                except Exception:
                    pass
                tip_cache[root_id] = tip
                return tip

            # Both ID matches and content matches share one keyspace, keyed by
            # compression lineage root, so an id-hit and a content-hit on the
            # same logical conversation collapse to a single result. The first
            # hit for a lineage wins; ID matches run first and take priority.
            seen: dict = {}

            def add_lineage_result(raw_sid: str, payload: dict) -> None:
                if not raw_sid:
                    return
                root = compression_root(raw_sid)
                if root in seen or len(seen) >= safe_limit:
                    return
                payload = dict(payload)
                payload["session_id"] = lineage_tip(root)
                payload["lineage_root"] = root
                seen[root] = payload

            # Direct ID matches first: users often paste a session id from CLI,
            # logs, or another Hermes surface. FTS can't find those unless the
            # id happens to appear in message text. search_sessions_by_id is
            # SQL-bounded, so this stays cheap even with thousands of sessions.
            for row in db.search_sessions_by_id(q, limit=safe_limit, include_archived=True):
                sid = row.get("id")
                preview = (row.get("preview") or "").strip()
                snippet = preview or f"Session ID: {sid}"
                add_lineage_result(
                    sid,
                    {
                        "snippet": snippet,
                        "role": None,
                        "source": row.get("source"),
                        "model": row.get("model"),
                        "session_started": row.get("started_at"),
                    },
                )

            # Auto-add prefix wildcards so partial words match
            # e.g. "nimb" → "nimb*" matches "nimby"
            # Preserve quoted phrases and existing wildcards as-is
            import re
            terms = []
            for token in re.findall(r'"[^"]*"|\S+', q.strip()):
                if token.startswith('"') or token.endswith("*"):
                    terms.append(token)
                else:
                    terms.append(token + "*")
            prefix_query = " ".join(terms)
            # Over-fetch so lineage dedup can still surface `limit` distinct
            # conversations even when several hits collapse onto one root.
            fetch_limit = max(safe_limit * 5, 50)
            matches = db.search_messages(query=prefix_query, limit=fetch_limit)

            for m in matches:
                if len(seen) >= safe_limit:
                    break
                add_lineage_result(
                    m["session_id"],
                    {
                        "snippet": m.get("snippet", ""),
                        "role": m.get("role"),
                        "source": m.get("source"),
                        "model": m.get("model"),
                        "session_started": m.get("session_started"),
                    },
                )
            return {"results": list(seen.values())}
        finally:
            db.close()
    except Exception:
        _log.exception("GET /api/sessions/search failed")
        raise HTTPException(status_code=500, detail="Search failed")


def _normalize_config_for_web(config: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize config for the web UI.

    Hermes supports ``model`` as either a bare string (``"anthropic/claude-sonnet-4"``)
    or a dict (``{default: ..., provider: ..., base_url: ...}``).  The schema is built
    from DEFAULT_CONFIG where ``model`` is a string, but user configs often have the
    dict form.  Normalize to the string form so the frontend schema matches.

    Also surfaces ``model_context_length`` as a top-level field so the web UI can
    display and edit it.  A value of 0 means "auto-detect".
    """
    config = dict(config)  # shallow copy
    model_val = config.get("model")
    if isinstance(model_val, dict):
        # Extract context_length before flattening the dict
        ctx_len = model_val.get("context_length", 0)
        config["model"] = model_val.get("default", model_val.get("name", ""))
        config["model_context_length"] = ctx_len if isinstance(ctx_len, int) else 0
    else:
        config["model_context_length"] = 0
    return config


@app.get("/api/config")
async def get_config():
    config = _normalize_config_for_web(load_config())
    # Strip internal keys that the frontend shouldn't see or send back
    return {k: v for k, v in config.items() if not k.startswith("_")}


@app.get("/api/config/defaults")
async def get_defaults():
    return DEFAULT_CONFIG


@app.get("/api/config/schema")
async def get_schema():
    return {"fields": CONFIG_SCHEMA, "category_order": _CATEGORY_ORDER}


_EMPTY_MODEL_INFO: dict = {
    "model": "",
    "provider": "",
    "auto_context_length": 0,
    "config_context_length": 0,
    "effective_context_length": 0,
    "capabilities": {},
    "agent_name": "Agent",
}


@app.get("/api/model/info")
def get_model_info():
    """Return resolved model metadata for the currently configured model.

    Calls the same context-length resolution chain the agent uses, so the
    frontend can display "Auto-detected: 200K" alongside the override field.
    Also returns model capabilities (vision, reasoning, tools) when available.
    """
    try:
        cfg = load_config()
        model_cfg = cfg.get("model", "")

        # Extract model name and provider from the config
        if isinstance(model_cfg, dict):
            model_name = model_cfg.get("default", model_cfg.get("name", ""))
            provider = model_cfg.get("provider", "")
            base_url = model_cfg.get("base_url", "")
            config_ctx = model_cfg.get("context_length")
        else:
            model_name = str(model_cfg) if model_cfg else ""
            provider = ""
            base_url = ""
            config_ctx = None

        if not model_name:
            return dict(_EMPTY_MODEL_INFO, provider=provider, agent_name=_assistant_display_name_from_config(cfg))

        # Resolve auto-detected context length (pass config_ctx=None to get
        # purely auto-detected value, then separately report the override)
        try:
            from agent.model_metadata import get_model_context_length
            auto_ctx = get_model_context_length(
                model=model_name,
                base_url=base_url,
                provider=provider,
                config_context_length=None,  # ignore override — we want auto value
            )
        except Exception:
            auto_ctx = 0

        config_ctx_int = 0
        if isinstance(config_ctx, int) and config_ctx > 0:
            config_ctx_int = config_ctx

        # Effective is what the agent actually uses
        effective_ctx = config_ctx_int if config_ctx_int > 0 else auto_ctx

        # Try to get model capabilities from models.dev
        caps = {}
        try:
            from agent.models_dev import get_model_capabilities
            mc = get_model_capabilities(provider=provider, model=model_name)
            if mc is not None:
                caps = {
                    "supports_tools": mc.supports_tools,
                    "supports_vision": mc.supports_vision,
                    "supports_reasoning": mc.supports_reasoning,
                    "context_window": mc.context_window,
                    "max_output_tokens": mc.max_output_tokens,
                    "model_family": mc.model_family,
                }
        except Exception:
            pass

        return {
            "model": model_name,
            "provider": provider,
            "auto_context_length": auto_ctx,
            "config_context_length": config_ctx_int,
            "effective_context_length": effective_ctx,
            "capabilities": caps,
            "agent_name": _assistant_display_name_from_config(cfg),
        }
    except Exception:
        _log.exception("GET /api/model/info failed")
        return dict(_EMPTY_MODEL_INFO)


# ---------------------------------------------------------------------------
# Model assignment — pick provider+model for main slot or auxiliary slots.
# Mirrors the model.options JSON-RPC from tui_gateway but uses REST so the
# Models page (which has no chat PTY open) can drive it.
# ---------------------------------------------------------------------------

# Canonical auxiliary task slots. Keep in sync with DEFAULT_CONFIG["auxiliary"]
# in hermes_cli/config.py — listed here for deterministic ordering in the UI.
_AUX_TASK_SLOTS: Tuple[str, ...] = (
    "vision",
    "web_extract",
    "compression",
    "skills_hub",
    "approval",
    "mcp",
    "title_generation",
    "triage_specifier",
    "kanban_decomposer",
    "profile_describer",
    "curator",
)


@app.get("/api/model/options")
def get_model_options():
    """Return authenticated providers + their curated model lists.

    REST equivalent of the ``model.options`` JSON-RPC on tui_gateway, so the
    dashboard Models page can render the picker without a live chat session.
    The response shape matches ``model.options`` 1:1 so ``ModelPickerDialog``
    can share the same types.
    """
    try:
        from hermes_cli.inventory import build_models_payload, load_picker_context

        return build_models_payload(
            load_picker_context(), max_models=50, pricing=True, capabilities=True
        )
    except Exception:
        _log.exception("GET /api/model/options failed")
        raise HTTPException(status_code=500, detail="Failed to list model options")


@app.get("/api/model/recommended-default")
def get_recommended_default_model(provider: str = ""):
    """Return the recommended default model for a freshly-authenticated provider.

    Mirrors the model-curation `hermes model` does so GUI onboarding lands on a
    sensible default instead of blindly taking the first curated entry. For
    Nous this honors the user's free/paid tier: free users get a free model,
    paid users get the full curated default. For any other provider it falls
    back to the first curated model (same as before).

    Response: {"provider": str, "model": str, "free_tier": bool | None}
    where free_tier is True/False for Nous and None otherwise. `model` may be
    empty if nothing could be resolved (caller degrades gracefully).
    """
    slug = (provider or "").strip().lower()

    if slug == "nous":
        try:
            from hermes_cli.models import (
                get_curated_nous_model_ids,
                get_pricing_for_provider,
                check_nous_free_tier,
                partition_nous_models_by_tier,
                union_with_portal_free_recommendations,
                union_with_portal_paid_recommendations,
            )
            from hermes_cli.auth import get_provider_auth_state

            model_ids = get_curated_nous_model_ids()
            pricing = get_pricing_for_provider("nous") or {}
            free_tier = check_nous_free_tier(force_fresh=True)

            portal_url = ""
            try:
                state = get_provider_auth_state("nous") or {}
                portal_url = state.get("portal_base_url", "") or ""
            except Exception:
                portal_url = ""

            if free_tier:
                model_ids, pricing = union_with_portal_free_recommendations(
                    model_ids, pricing, portal_url
                )
                model_ids, _unavailable = partition_nous_models_by_tier(
                    model_ids, pricing, free_tier=True
                )
            else:
                model_ids, pricing = union_with_portal_paid_recommendations(
                    model_ids, pricing, portal_url
                )

            model = model_ids[0] if model_ids else ""
            return {"provider": "nous", "model": model, "free_tier": bool(free_tier)}
        except Exception:
            _log.exception("GET /api/model/recommended-default (nous) failed")
            return {"provider": "nous", "model": "", "free_tier": None}

    # Non-Nous: first curated model for the provider, matching prior behaviour.
    try:
        from hermes_cli.inventory import build_models_payload, load_picker_context

        payload = build_models_payload(load_picker_context(), max_models=50)
        for row in payload.get("providers", []):
            if str(row.get("slug", "")).lower() == slug:
                models = row.get("models") or []
                return {"provider": slug, "model": models[0] if models else "", "free_tier": None}
        return {"provider": slug, "model": "", "free_tier": None}
    except Exception:
        _log.exception("GET /api/model/recommended-default failed")
        return {"provider": slug, "model": "", "free_tier": None}


@app.get("/api/model/auxiliary")
def get_auxiliary_models():
    """Return current auxiliary task assignments.

    Shape:
      {
        "tasks": [
          {"task": "vision", "provider": "auto", "model": "", "base_url": ""},
          ...
        ],
        "main": {"provider": "openrouter", "model": "anthropic/claude-opus-4.7"},
      }
    """
    try:
        cfg = load_config()
        aux_cfg = cfg.get("auxiliary", {})
        if not isinstance(aux_cfg, dict):
            aux_cfg = {}

        tasks = []
        for slot in _AUX_TASK_SLOTS:
            slot_cfg = aux_cfg.get(slot, {}) if isinstance(aux_cfg.get(slot), dict) else {}
            tasks.append({
                "task": slot,
                "provider": str(slot_cfg.get("provider", "auto") or "auto"),
                "model": str(slot_cfg.get("model", "") or ""),
                "base_url": str(slot_cfg.get("base_url", "") or ""),
            })

        model_cfg = cfg.get("model", {})
        if isinstance(model_cfg, dict):
            main = {
                "provider": str(model_cfg.get("provider", "") or ""),
                "model": str(model_cfg.get("default", model_cfg.get("name", "")) or ""),
            }
        else:
            main = {"provider": "", "model": str(model_cfg) if model_cfg else ""}

        return {"tasks": tasks, "main": main}
    except Exception:
        _log.exception("GET /api/model/auxiliary failed")
        raise HTTPException(status_code=500, detail="Failed to read auxiliary config")


@app.post("/api/model/set")
async def set_model_assignment(body: ModelAssignment):
    """Assign a model to the main slot or an auxiliary task slot.

    Writes to ``~/.hermes/config.yaml`` — applies to **new** sessions only.
    The currently running chat PTY (if any) is not affected; use the
    ``/model`` slash command inside a chat to hot-swap that specific session.
    """
    scope = (body.scope or "").strip().lower()
    provider = (body.provider or "").strip()
    model = (body.model or "").strip()
    task = (body.task or "").strip().lower()
    base_url = (body.base_url or "").strip()

    if scope not in {"main", "auxiliary"}:
        raise HTTPException(status_code=400, detail="scope must be 'main' or 'auxiliary'")

    try:
        cfg = load_config()

        if scope == "main":
            if not provider or not model:
                raise HTTPException(status_code=400, detail="provider and model required for main")
            model_cfg = _apply_main_model_assignment(
                cfg.get("model", {}), provider, model, base_url
            )
            cfg["model"] = model_cfg

            # When switching the main provider to Nous, mirror the CLI's
            # post-model-selection behaviour (hermes_cli/main.py
            # prompt_enable_tool_gateway / tools_config apply_nous_managed_defaults):
            # auto-route any *unconfigured* tools through the Nous Tool Gateway.
            # This is purely additive — apply_nous_managed_defaults skips every
            # tool where the user already has a direct key (FIRECRAWL_API_KEY,
            # FAL_KEY, etc.) or an explicit backend/provider in config, so it
            # never overwrites a user's own setup. GUI users thus land on the
            # gateway the same way CLI users do, without a separate prompt.
            gateway_tools: list[str] = []
            if provider.strip().lower() == "nous":
                try:
                    from hermes_cli.nous_subscription import apply_nous_managed_defaults
                    from hermes_cli.tools_config import _get_platform_tools

                    enabled = _get_platform_tools(
                        cfg, "cli", include_default_mcp_servers=False
                    )
                    changed = apply_nous_managed_defaults(
                        cfg,
                        enabled_toolsets=enabled,
                        force_fresh=True,
                    )
                    gateway_tools = sorted(changed)
                except Exception:
                    # Portal lookup hiccups / non-subscriber / non-nous gating
                    # must never block saving the model assignment.
                    _log.debug("apply_nous_managed_defaults skipped", exc_info=True)

            save_config(cfg)
            return {
                "ok": True,
                "scope": "main",
                "provider": provider,
                "model": model,
                "base_url": model_cfg.get("base_url", ""),
                "gateway_tools": gateway_tools,
            }

        # scope == "auxiliary"
        aux = cfg.get("auxiliary")
        if not isinstance(aux, dict):
            aux = {}

        if task == "__reset__":
            # Reset every slot to provider="auto", model="" — keeps other fields intact.
            for slot in _AUX_TASK_SLOTS:
                slot_cfg = aux.get(slot)
                if not isinstance(slot_cfg, dict):
                    slot_cfg = {}
                slot_cfg["provider"] = "auto"
                slot_cfg["model"] = ""
                aux[slot] = slot_cfg
            cfg["auxiliary"] = aux
            save_config(cfg)
            return {"ok": True, "scope": "auxiliary", "reset": True}

        if not provider:
            raise HTTPException(status_code=400, detail="provider required for auxiliary")

        targets = [task] if task else list(_AUX_TASK_SLOTS)
        for slot in targets:
            if slot not in _AUX_TASK_SLOTS:
                raise HTTPException(status_code=400, detail=f"unknown auxiliary task: {slot}")
            slot_cfg = aux.get(slot)
            if not isinstance(slot_cfg, dict):
                slot_cfg = {}
            slot_cfg["provider"] = provider
            slot_cfg["model"] = model
            aux[slot] = slot_cfg

        cfg["auxiliary"] = aux
        save_config(cfg)
        return {
            "ok": True,
            "scope": "auxiliary",
            "tasks": targets,
            "provider": provider,
            "model": model,
        }
    except HTTPException:
        raise
    except Exception:
        _log.exception("POST /api/model/set failed")
        raise HTTPException(status_code=500, detail="Failed to save model assignment")




def _denormalize_config_from_web(config: Dict[str, Any]) -> Dict[str, Any]:
    """Reverse _normalize_config_for_web before saving.

    Reconstructs ``model`` as a dict by reading the current on-disk config
    to recover model subkeys (provider, base_url, api_mode, etc.) that were
    stripped from the GET response.  The frontend only sees model as a flat
    string; the rest is preserved transparently.

    Also handles ``model_context_length`` — writes it back into the model dict
    as ``context_length``.  A value of 0 or absent means "auto-detect" (omitted
    from the dict so get_model_context_length() uses its normal resolution).
    """
    config = dict(config)
    # Remove any _model_meta that might have leaked in (shouldn't happen
    # with the stripped GET response, but be defensive)
    config.pop("_model_meta", None)

    # Extract and remove model_context_length before processing model
    ctx_override = config.pop("model_context_length", 0)
    if not isinstance(ctx_override, int):
        try:
            ctx_override = int(ctx_override)
        except (TypeError, ValueError):
            ctx_override = 0

    model_val = config.get("model")
    if isinstance(model_val, str) and model_val:
        # Read the current disk config to recover model subkeys
        try:
            disk_config = load_config()
            disk_model = disk_config.get("model")
            if isinstance(disk_model, dict):
                # Preserve all subkeys, update default with the new value
                disk_model["default"] = model_val
                # Write context_length into the model dict (0 = remove/auto)
                if ctx_override > 0:
                    disk_model["context_length"] = ctx_override
                else:
                    disk_model.pop("context_length", None)
                config["model"] = disk_model
            # Model was previously a bare string — upgrade to dict if
            # user is setting a context_length override
            elif ctx_override > 0:
                config["model"] = {
                    "default": model_val,
                    "context_length": ctx_override,
                }
        except Exception:
            pass  # can't read disk config — just use the string form
    return config


@app.put("/api/config")
async def update_config(body: ConfigUpdate):
    try:
        save_config(_denormalize_config_from_web(body.config))
        return {"ok": True}
    except Exception:
        _log.exception("PUT /api/config failed")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/env")
async def get_env_vars():
    env_on_disk = load_env()
    channel_keys = _channel_managed_env_keys()
    result = {}
    for var_name, info in OPTIONAL_ENV_VARS.items():
        value = env_on_disk.get(var_name)
        result[var_name] = {
            "is_set": bool(value),
            "redacted_value": redact_key(value) if value else None,
            "description": info.get("description", ""),
            "url": info.get("url"),
            "category": info.get("category", ""),
            "is_password": info.get("password", False),
            "tools": info.get("tools", []),
            "advanced": info.get("advanced", False),
            # True when this var is a messaging-platform credential owned by a
            # Channels page card. The Keys/Env page uses this to hide it and
            # avoid duplicating the (richer) Channels configuration UI.
            "channel_managed": var_name in channel_keys,
        }
    return result


@app.put("/api/env")
async def set_env_var(body: EnvVarUpdate):
    try:
        save_env_value(body.key, body.value)
        return {"ok": True, "key": body.key}
    except ValueError as exc:
        # save_env_value raises ValueError for invalid names and for keys
        # on the denylist (LD_PRELOAD, PATH, PYTHONPATH, …). Surface the
        # message to the SPA so the user understands why the write was
        # refused instead of seeing an opaque 500.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        _log.exception("PUT /api/env failed")
        raise HTTPException(status_code=500, detail="Internal server error")


# Live credential probes keyed by env var. Each entry is (method, url, auth)
# where auth is "bearer" (Authorization header) or "query" (?key=). A cheap
# read-only models/key call that 401s on a bad token — enough to catch a
# mistyped key before it's persisted. Providers absent from this map (or local
# endpoints) are not network-validated; the client treats those as "unknown".
_CREDENTIAL_PROBES: dict[str, tuple[str, str]] = {
    "OPENROUTER_API_KEY": ("https://openrouter.ai/api/v1/key", "bearer"),
    "OPENAI_API_KEY": ("https://api.openai.com/v1/models", "bearer"),
    "XAI_API_KEY": ("https://api.x.ai/v1/models", "bearer"),
    "GEMINI_API_KEY": ("https://generativelanguage.googleapis.com/v1beta/models", "query"),
}


def _parse_model_ids(resp: "Any") -> List[str]:
    """Extract model ids from an OpenAI-compatible ``/v1/models`` response.

    Tolerant of the common shapes: ``{"data": [{"id": ...}]}`` (OpenAI / vLLM /
    llama.cpp) and a bare ``{"data": ["id", ...]}``. Returns ``[]`` on any
    parse/HTTP error so a slightly non-standard endpoint never hard-blocks.
    """
    try:
        if not resp.is_success:
            return []
        payload = resp.json()
    except Exception:
        return []
    data = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(data, list):
        return []
    ids: List[str] = []
    for item in data:
        if isinstance(item, dict):
            mid = str(item.get("id") or "").strip()
        else:
            mid = str(item or "").strip()
        if mid:
            ids.append(mid)
    return ids


@app.post("/api/providers/validate")
async def validate_provider_credential(body: EnvVarUpdate, request: Request):
    """Live-probe a provider credential before it's saved.

    Returns {ok, reachable, message}. ok=True means the provider accepted the
    key; ok=False + reachable=True means the key is bad (caller should block);
    reachable=False means the network probe couldn't run (caller may save with
    a warning rather than hard-blocking offline users).
    """
    _require_token(request)
    import httpx

    key = (body.key or "").strip()
    value = (body.value or "").strip()
    if not value:
        return {"ok": False, "reachable": True, "message": "Enter a value first."}

    # Local / custom endpoint: validate connectivity, not auth — any HTTP
    # response (even 401) proves the endpoint is up. Also surface the model
    # ids the endpoint advertises (OpenAI ``/v1/models`` shape) so the GUI can
    # auto-pick a default without asking the user to type a model name.
    if key == "OPENAI_BASE_URL":
        url = value.rstrip("/") + "/models"
        try:
            with httpx.Client(timeout=httpx.Timeout(8.0)) as client:
                resp = client.get(url)
            return {"ok": True, "reachable": True, "message": "", "models": _parse_model_ids(resp)}
        except Exception:
            return {"ok": False, "reachable": False, "message": f"Could not reach {url}."}

    probe = _CREDENTIAL_PROBES.get(key)
    if not probe:
        # No probe for this provider — can't validate, don't block.
        return {"ok": True, "reachable": False, "message": ""}

    url, auth = probe
    headers = {"Accept": "application/json"}
    params = {}
    if auth == "bearer":
        headers["Authorization"] = f"Bearer {value}"
    else:
        params["key"] = value

    try:
        with httpx.Client(timeout=httpx.Timeout(10.0)) as client:
            resp = client.get(url, headers=headers, params=params)
    except Exception:
        return {"ok": False, "reachable": False, "message": "Could not reach the provider to verify the key."}

    if resp.status_code in (401, 403):
        return {"ok": False, "reachable": True, "message": "That API key was rejected. Double-check it and try again."}
    if resp.status_code == 429 or resp.is_success:
        # 429 = key is valid but rate-limited; success = valid.
        return {"ok": True, "reachable": True, "message": ""}
    return {"ok": False, "reachable": True, "message": f"Provider returned HTTP {resp.status_code} for this key."}


@app.delete("/api/env")
async def remove_env_var(body: EnvVarDelete):
    try:
        removed = remove_env_value(body.key)
        if not removed:
            raise HTTPException(status_code=404, detail=f"{body.key} not found in .env")
        return {"ok": True, "key": body.key}
    except HTTPException:
        raise
    except ValueError as exc:
        # remove_env_value raises ValueError for invalid key names. Surface
        # the message to the SPA so the user understands why the delete was
        # refused instead of seeing an opaque 500. Mirrors PUT /api/env.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        _log.exception("DELETE /api/env failed")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/api/env/reveal")
async def reveal_env_var(body: EnvVarReveal, request: Request):
    """Return the real (unredacted) value of a single env var.

    Protected by:
    - Ephemeral session token (generated per server start, injected into SPA)
    - Rate limiting (max 5 reveals per 30s window)
    - Audit logging
    """
    # --- Token check ---
    _require_token(request)

    # --- Rate limit ---
    now = time.time()
    cutoff = now - _REVEAL_WINDOW_SECONDS
    _reveal_timestamps[:] = [t for t in _reveal_timestamps if t > cutoff]
    if len(_reveal_timestamps) >= _REVEAL_MAX_PER_WINDOW:
        raise HTTPException(status_code=429, detail="Too many reveal requests. Try again shortly.")
    _reveal_timestamps.append(now)

    # --- Reveal ---
    env_on_disk = load_env()
    value = env_on_disk.get(body.key)
    if value is None:
        raise HTTPException(status_code=404, detail=f"{body.key} not found in .env")

    _log.info("env/reveal: %s", body.key)
    return {"key": body.key, "value": value}


# Entries omit fields they don't need to override; the catalog builder fills
# in env_vars from OPTIONAL_ENV_VARS via prefix matching when not specified,
# and pulls required_env from a plugin's PlatformEntry when available.
_PLATFORM_OVERRIDES: dict[str, dict[str, Any]] = {
    "telegram": {
        "name": "Telegram",
        "description": "Run Hermes from Telegram DMs, groups, and topics.",
        "docs_url": "https://core.telegram.org/bots/features#botfather",
        "env_vars": ("TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_USERS", "TELEGRAM_PROXY"),
        "required_env": ("TELEGRAM_BOT_TOKEN",),
    },
    "discord": {
        "name": "Discord",
        "description": "Connect Hermes to Discord DMs, channels, and threads.",
        "docs_url": "https://discord.com/developers/applications",
        "env_vars": (
            "DISCORD_BOT_TOKEN",
            "DISCORD_ALLOWED_USERS",
            "DISCORD_REPLY_TO_MODE",
        ),
        "required_env": ("DISCORD_BOT_TOKEN",),
    },
    "slack": {
        "name": "Slack",
        "description": "Use Hermes from Slack via Socket Mode.",
        "docs_url": "https://api.slack.com/apps",
        "env_vars": ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"),
        "required_env": ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"),
    },
    "mattermost": {
        "name": "Mattermost",
        "description": "Connect Hermes to Mattermost channels and direct messages.",
        "docs_url": "https://mattermost.com/deploy/",
        "env_vars": ("MATTERMOST_URL", "MATTERMOST_TOKEN", "MATTERMOST_ALLOWED_USERS"),
        "required_env": ("MATTERMOST_URL", "MATTERMOST_TOKEN"),
    },
    "matrix": {
        "name": "Matrix",
        "description": "Use Hermes in Matrix rooms and direct messages.",
        "docs_url": "https://matrix.org/ecosystem/servers/",
        "env_vars": (
            "MATRIX_HOMESERVER",
            "MATRIX_ACCESS_TOKEN",
            "MATRIX_USER_ID",
            "MATRIX_ALLOWED_USERS",
        ),
        "required_env": ("MATRIX_HOMESERVER", "MATRIX_ACCESS_TOKEN", "MATRIX_USER_ID"),
    },
    "signal": {
        "name": "Signal",
        "description": "Connect through a signal-cli REST bridge.",
        "docs_url": "https://github.com/bbernhard/signal-cli-rest-api",
        "env_vars": ("SIGNAL_HTTP_URL", "SIGNAL_ACCOUNT", "SIGNAL_ALLOWED_USERS"),
        "required_env": ("SIGNAL_HTTP_URL", "SIGNAL_ACCOUNT"),
    },
    "whatsapp": {
        "name": "WhatsApp",
        "description": "Use Hermes through the bundled WhatsApp bridge with QR-based auth.",
        "docs_url": "https://github.com/tulir/whatsmeow",
        "env_vars": ("WHATSAPP_ENABLED", "WHATSAPP_MODE", "WHATSAPP_ALLOWED_USERS"),
        "required_env": (),
    },
    "homeassistant": {
        "name": "Home Assistant",
        "description": "Control your smart home from Hermes via Home Assistant.",
        "docs_url": "https://www.home-assistant.io/docs/authentication/",
        "env_vars": ("HASS_URL", "HASS_TOKEN"),
        "required_env": ("HASS_URL", "HASS_TOKEN"),
    },
    "email": {
        "name": "Email",
        "description": "Talk to Hermes through an IMAP/SMTP mailbox.",
        "docs_url": "https://hermes-agent.nousresearch.com/docs/user-guide/messaging/",
        "env_vars": (
            "EMAIL_ADDRESS",
            "EMAIL_PASSWORD",
            "EMAIL_IMAP_HOST",
            "EMAIL_SMTP_HOST",
        ),
        "required_env": (
            "EMAIL_ADDRESS",
            "EMAIL_PASSWORD",
            "EMAIL_IMAP_HOST",
            "EMAIL_SMTP_HOST",
        ),
    },
    "sms": {
        "name": "SMS (Twilio)",
        "description": "Send and receive text messages via Twilio.",
        "docs_url": "https://www.twilio.com/console",
        "env_vars": ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN"),
        "required_env": ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN"),
    },
    "dingtalk": {
        "name": "DingTalk",
        "description": "Connect Hermes to DingTalk groups (钉钉).",
        "docs_url": "https://open.dingtalk.com/document/orgapp/the-robot-development-process",
        "env_vars": ("DINGTALK_CLIENT_ID", "DINGTALK_CLIENT_SECRET"),
        "required_env": ("DINGTALK_CLIENT_ID", "DINGTALK_CLIENT_SECRET"),
    },
    "feishu": {
        "name": "Feishu / Lark",
        "description": "Use Hermes inside Feishu / Lark.",
        "docs_url": "https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/intro",
        "env_vars": (
            "FEISHU_APP_ID",
            "FEISHU_APP_SECRET",
            "FEISHU_ENCRYPT_KEY",
            "FEISHU_VERIFICATION_TOKEN",
        ),
        "required_env": ("FEISHU_APP_ID", "FEISHU_APP_SECRET"),
    },
    "wecom": {
        "name": "WeCom (group bot)",
        "description": "Send-only WeCom group bot via webhook.",
        "docs_url": "https://developer.work.weixin.qq.com/document/path/91770",
        "env_vars": ("WECOM_BOT_ID", "WECOM_SECRET"),
        "required_env": ("WECOM_BOT_ID",),
    },
    "wecom_callback": {
        "name": "WeCom (app)",
        "description": "Two-way WeCom integration via callback app.",
        "docs_url": "https://developer.work.weixin.qq.com/document/path/90930",
        "env_vars": (
            "WECOM_CALLBACK_CORP_ID",
            "WECOM_CALLBACK_CORP_SECRET",
            "WECOM_CALLBACK_AGENT_ID",
            "WECOM_CALLBACK_TOKEN",
            "WECOM_CALLBACK_ENCODING_AES_KEY",
        ),
        "required_env": (
            "WECOM_CALLBACK_CORP_ID",
            "WECOM_CALLBACK_CORP_SECRET",
            "WECOM_CALLBACK_AGENT_ID",
        ),
    },
    "weixin": {
        "name": "WeChat (Official Account)",
        "description": "Connect a WeChat Official Account.",
        "docs_url": "https://developers.weixin.qq.com/doc/offiaccount/Getting_Started/Overview.html",
        "env_vars": ("WEIXIN_ACCOUNT_ID", "WEIXIN_TOKEN", "WEIXIN_BASE_URL"),
        "required_env": ("WEIXIN_ACCOUNT_ID", "WEIXIN_TOKEN"),
    },
    "bluebubbles": {
        "name": "BlueBubbles (iMessage)",
        "description": "Use Hermes through iMessage via a BlueBubbles server.",
        "docs_url": "https://bluebubbles.app/",
        "env_vars": (
            "BLUEBUBBLES_SERVER_URL",
            "BLUEBUBBLES_PASSWORD",
            "BLUEBUBBLES_ALLOWED_USERS",
        ),
        "required_env": ("BLUEBUBBLES_SERVER_URL", "BLUEBUBBLES_PASSWORD"),
    },
    "qqbot": {
        "name": "QQ Bot",
        "description": "Connect Hermes to a QQ Bot from the QQ Open Platform.",
        "docs_url": "https://q.qq.com",
        "env_vars": ("QQ_APP_ID", "QQ_CLIENT_SECRET", "QQ_ALLOWED_USERS"),
        "required_env": ("QQ_APP_ID", "QQ_CLIENT_SECRET"),
    },
    "yuanbao": {
        "name": "Yuanbao (元宝)",
        "description": "Connect Hermes to Tencent Yuanbao.",
        "docs_url": "",
        "required_env": (),
    },
    "api_server": {
        "name": "API server",
        "description": "Expose Hermes as an OpenAI-compatible HTTP API for tools like Open WebUI.",
        "docs_url": "https://hermes-agent.nousresearch.com/docs/user-guide/messaging/",
        "env_vars": (
            "API_SERVER_ENABLED",
            "API_SERVER_KEY",
            "API_SERVER_PORT",
            "API_SERVER_HOST",
            "API_SERVER_MODEL_NAME",
        ),
        "required_env": (),
    },
    "webhook": {
        "name": "Webhooks",
        "description": "Receive events from GitHub, GitLab, and other webhook sources.",
        "docs_url": "https://hermes-agent.nousresearch.com/docs/user-guide/messaging/webhooks/",
        "env_vars": ("WEBHOOK_ENABLED", "WEBHOOK_PORT", "WEBHOOK_SECRET"),
        "required_env": (),
    },
}

# Display order: well-known platforms surface first; unknown plugins fall to
# the end alphabetically.
_PLATFORM_ORDER: tuple[str, ...] = (
    "telegram",
    "discord",
    "slack",
    "mattermost",
    "matrix",
    "whatsapp",
    "signal",
    "bluebubbles",
    "homeassistant",
    "email",
    "sms",
    "dingtalk",
    "feishu",
    "wecom",
    "wecom_callback",
    "weixin",
    "qqbot",
    "yuanbao",
    "api_server",
    "webhook",
)

# Display labels for env vars not in OPTIONAL_ENV_VARS (HOME_CHANNEL_*, bridge
# toggles, Twilio, HASS, Email, etc.). Anything missing from OPTIONAL_ENV_VARS
# falls back here so the UI can still render a friendly label.
_MESSAGING_ENV_FALLBACKS: dict[str, dict[str, Any]] = {
    "SIGNAL_HTTP_URL": {
        "description": "signal-cli REST API base URL, e.g. http://127.0.0.1:8080",
        "prompt": "Signal bridge URL",
        "url": "https://github.com/bbernhard/signal-cli-rest-api",
    },
    "SIGNAL_ACCOUNT": {
        "description": "Signal account phone number registered with the bridge",
        "prompt": "Signal account",
    },
    "SIGNAL_ALLOWED_USERS": {
        "description": "Comma-separated Signal users allowed to use the bot",
        "prompt": "Allowed Signal users",
    },
    "WHATSAPP_ENABLED": {
        "description": "Enable the WhatsApp gateway adapter",
        "prompt": "Enable WhatsApp",
        "advanced": True,
    },
    "WHATSAPP_MODE": {
        "description": "WhatsApp bridge mode",
        "prompt": "WhatsApp mode",
        "advanced": True,
    },
    "WHATSAPP_ALLOWED_USERS": {
        "description": "Comma-separated WhatsApp users allowed to use the bot",
        "prompt": "Allowed WhatsApp users",
    },
    "HASS_URL": {
        "description": "Home Assistant base URL, e.g. https://homeassistant.local:8123",
        "prompt": "Home Assistant URL",
    },
    "HASS_TOKEN": {
        "description": "Long-lived access token from Home Assistant (Profile → Security)",
        "prompt": "Home Assistant access token",
        "password": True,
    },
    "EMAIL_ADDRESS": {
        "description": "Email address to send and receive from",
        "prompt": "Email address",
    },
    "EMAIL_PASSWORD": {
        "description": "Email account password or app password",
        "prompt": "Email password",
        "password": True,
    },
    "EMAIL_IMAP_HOST": {
        "description": "IMAP server host (e.g. imap.gmail.com)",
        "prompt": "IMAP host",
    },
    "EMAIL_SMTP_HOST": {
        "description": "SMTP server host (e.g. smtp.gmail.com)",
        "prompt": "SMTP host",
    },
    "TWILIO_ACCOUNT_SID": {
        "description": "Twilio Account SID",
        "prompt": "Twilio Account SID",
        "url": "https://www.twilio.com/console",
    },
    "TWILIO_AUTH_TOKEN": {
        "description": "Twilio Auth Token",
        "prompt": "Twilio Auth Token",
        "password": True,
    },
    "WECOM_BOT_ID": {"description": "WeCom group bot ID", "prompt": "WeCom Bot ID"},
    "WECOM_SECRET": {
        "description": "WeCom group bot secret",
        "prompt": "WeCom Secret",
        "password": True,
    },
    "WECOM_CALLBACK_CORP_ID": {
        "description": "WeCom corp ID",
        "prompt": "WeCom Corp ID",
    },
    "WECOM_CALLBACK_CORP_SECRET": {
        "description": "WeCom app corp secret",
        "prompt": "WeCom Corp Secret",
        "password": True,
    },
    "WECOM_CALLBACK_AGENT_ID": {
        "description": "WeCom app agent ID",
        "prompt": "WeCom Agent ID",
    },
    "WECOM_CALLBACK_TOKEN": {
        "description": "WeCom callback verification token",
        "prompt": "WeCom Token",
    },
    "WECOM_CALLBACK_ENCODING_AES_KEY": {
        "description": "WeCom callback AES encoding key",
        "prompt": "WeCom AES Key",
        "password": True,
    },
    "WEIXIN_ACCOUNT_ID": {
        "description": "WeChat Official Account ID",
        "prompt": "Account ID",
    },
    "WEIXIN_TOKEN": {
        "description": "WeChat callback token",
        "prompt": "Token",
        "password": True,
    },
    "WEIXIN_BASE_URL": {
        "description": "WeChat platform base URL",
        "prompt": "Base URL",
    },
    "FEISHU_APP_ID": {"description": "Feishu / Lark app ID", "prompt": "App ID"},
    "FEISHU_APP_SECRET": {
        "description": "Feishu / Lark app secret",
        "prompt": "App secret",
        "password": True,
    },
    "FEISHU_ENCRYPT_KEY": {
        "description": "Feishu / Lark encrypt key",
        "prompt": "Encrypt key",
        "password": True,
    },
    "FEISHU_VERIFICATION_TOKEN": {
        "description": "Feishu / Lark verification token",
        "prompt": "Verification token",
        "password": True,
    },
    "DINGTALK_CLIENT_ID": {
        "description": "DingTalk client ID (App key)",
        "prompt": "Client ID",
    },
    "DINGTALK_CLIENT_SECRET": {
        "description": "DingTalk client secret (App secret)",
        "prompt": "Client secret",
        "password": True,
    },
}


def _messaging_platform_catalog() -> tuple[dict[str, Any], ...]:
    """Build the messaging catalog from the gateway's Platform enum + plugin registry.

    Built-in platforms come from ``gateway.config.Platform`` (LOCAL is excluded).
    Plugin platforms come from ``gateway.platform_registry.plugin_entries()``,
    which lets newly installed adapters (e.g. IRC) appear without a code change
    here. Per-platform UI metadata (description, docs URL, env-var picks) lives
    in :data:`_PLATFORM_OVERRIDES`; anything not overridden gets reasonable
    defaults derived from the platform id and required_env.
    """
    from gateway.config import Platform

    seen: set[str] = set()
    entries: list[dict[str, Any]] = []

    for member in Platform.__members__.values():
        if member.value == "local":
            continue
        if member.value in seen:
            continue
        seen.add(member.value)
        entries.append(_build_catalog_entry(member.value))

    try:
        from gateway.platform_registry import platform_registry

        for plugin_entry in platform_registry.plugin_entries():
            if plugin_entry.name in seen:
                continue
            seen.add(plugin_entry.name)
            entries.append(_build_catalog_entry(plugin_entry.name, plugin_entry))
    except Exception:
        _log.debug("plugin platform registry unavailable", exc_info=True)

    order = {pid: idx for idx, pid in enumerate(_PLATFORM_ORDER)}
    entries.sort(
        key=lambda e: (order.get(e["id"], len(_PLATFORM_ORDER)), e["name"].lower())
    )
    return tuple(entries)


def _channel_managed_env_keys() -> frozenset[str]:
    """Env-var keys owned by a Channels page platform card.

    The Channels page is the canonical surface for configuring messaging
    platform credentials (with connection status, test, enable toggle and
    gateway restart). The Keys/Env page consults this set to hide those vars
    so the same fields aren't duplicated in a plainer UI. Best-effort: if the
    gateway catalog can't be built, nothing is flagged and Keys shows it all.
    """
    try:
        keys: set[str] = set()
        for entry in _messaging_platform_catalog():
            keys.update(entry.get("env_vars", ()))
        return frozenset(keys)
    except Exception:
        _log.debug("could not build channel-managed env key set", exc_info=True)
        return frozenset()


# Cross-cutting gateway / relay knobs stay on the Keys → Settings tab even though
# they use the ``messaging`` category in OPTIONAL_ENV_VARS. Platform-scoped vars
# (``DISCORD_*``, ``MATRIX_*``, …) are owned by the Messaging UI instead.
_MESSAGING_KEYS_PAGE_KEYS = frozenset({
    "GATEWAY_ALLOW_ALL_USERS",
    "GATEWAY_PROXY_KEY",
    "GATEWAY_PROXY_URL",
})


def _platform_env_prefixes(platform_id: str) -> tuple[str, ...]:
    """Env-var prefixes owned by a messaging platform card."""
    aliases: dict[str, tuple[str, ...]] = {
        "email": ("EMAIL_",),
        "homeassistant": ("HASS_",),
        "qqbot": ("QQ_", "QQBOT_"),
        "sms": ("TWILIO_",),
        "wecom": ("WECOM_BOT_", "WECOM_SECRET"),
        "wecom_callback": ("WECOM_CALLBACK_",),
    }
    if platform_id in aliases:
        return aliases[platform_id]
    return (platform_id.upper().replace("-", "_") + "_",)


def _discover_platform_env_vars(platform_id: str) -> tuple[str, ...]:
    """All messaging-category env vars for a platform (override + plugin + prefix)."""
    prefixes = _platform_env_prefixes(platform_id)
    keys: list[str] = []
    for name, info in OPTIONAL_ENV_VARS.items():
        if info.get("category") != "messaging":
            continue
        if name in _MESSAGING_KEYS_PAGE_KEYS:
            continue
        if not any(name.startswith(prefix) for prefix in prefixes):
            continue
        keys.append(name)
    return tuple(sorted(set(keys)))


def _merge_platform_env_vars(
    platform_id: str,
    override: dict[str, Any],
    plugin_entry: Any | None,
) -> tuple[str, ...]:
    """Canonical env-var list for a messaging platform card."""
    discovered = _discover_platform_env_vars(platform_id)
    if "env_vars" in override:
        return tuple(dict.fromkeys((*override["env_vars"], *discovered)))
    if plugin_entry is not None and plugin_entry.required_env:
        return tuple(dict.fromkeys((*tuple(plugin_entry.required_env), *discovered)))
    return discovered


def _build_catalog_entry(
    platform_id: str, plugin_entry: Any | None = None
) -> dict[str, Any]:
    override = _PLATFORM_OVERRIDES.get(platform_id, {})

    env_vars = _merge_platform_env_vars(platform_id, override, plugin_entry)

    if "required_env" in override:
        required_env = tuple(override["required_env"])
    elif plugin_entry is not None:
        required_env = tuple(plugin_entry.required_env or ())
    else:
        required_env = ()

    if override.get("name"):
        name = override["name"]
    elif plugin_entry is not None and plugin_entry.label:
        name = plugin_entry.label
    else:
        name = platform_id.replace("_", " ").title()

    description = override.get("description")
    if not description and plugin_entry is not None:
        description = plugin_entry.install_hint or ""

    return {
        "id": platform_id,
        "name": name,
        "description": description or "",
        "docs_url": override.get("docs_url", ""),
        "env_vars": env_vars,
        "required_env": required_env,
    }


def _catalog_lookup(platform_id: str) -> dict[str, Any] | None:
    for entry in _messaging_platform_catalog():
        if entry["id"] == platform_id:
            return entry
    return None


def _messaging_env_info(key: str) -> dict[str, Any]:
    info = OPTIONAL_ENV_VARS.get(key) or _MESSAGING_ENV_FALLBACKS.get(key) or {}
    return {
        "description": info.get("description", ""),
        "prompt": info.get("prompt", key),
        "url": info.get("url"),
        "is_password": info.get("password", False),
        "advanced": info.get("advanced", False),
    }


def _gateway_platform_config(platform_id: str):
    from gateway.config import Platform, load_gateway_config

    config = load_gateway_config()
    platform = Platform(platform_id)
    platform_config = config.platforms.get(platform)
    return config, platform, platform_config


def _messaging_platform_payload(
    entry: dict[str, Any], env_on_disk: dict[str, str], runtime: dict | None
) -> dict[str, Any]:
    platform_id = entry["id"]
    gateway_running = get_running_pid() is not None
    runtime_platforms = runtime.get("platforms") if runtime else {}
    runtime_platform = (
        runtime_platforms.get(platform_id, {})
        if isinstance(runtime_platforms, dict)
        else {}
    )
    env_vars = []

    for key in entry["env_vars"]:
        value = env_on_disk.get(key) or os.getenv(key, "")
        env_vars.append(
            {
                "key": key,
                "required": key in entry["required_env"],
                "is_set": bool(value),
                "redacted_value": redact_key(value) if value else None,
                **_messaging_env_info(key),
            }
        )

    try:
        gateway_config, platform, platform_config = _gateway_platform_config(
            platform_id
        )
        enabled = bool(platform_config and platform_config.enabled)
        configured = bool(
            platform_config
            and gateway_config._is_platform_connected(platform, platform_config)
        )
        home_channel = (
            platform_config.home_channel.to_dict()
            if platform_config and platform_config.home_channel
            else None
        )
    except Exception:
        enabled = False
        configured = all(
            env_on_disk.get(key) or os.getenv(key, "") for key in entry["required_env"]
        )
        home_channel = None

    state = (
        runtime_platform.get("state") if isinstance(runtime_platform, dict) else None
    )
    if not enabled:
        state = "disabled"
    elif not configured:
        state = "not_configured"
    elif gateway_running and not state:
        state = "pending_restart"
    elif not gateway_running and not state:
        state = "gateway_stopped"

    return {
        "id": platform_id,
        "name": entry["name"],
        "description": entry["description"],
        "docs_url": entry["docs_url"],
        "enabled": enabled,
        "configured": configured,
        "gateway_running": gateway_running,
        "state": state,
        "error_code": (
            runtime_platform.get("error_code")
            if isinstance(runtime_platform, dict)
            else None
        ),
        "error_message": (
            runtime_platform.get("error_message")
            if isinstance(runtime_platform, dict)
            else None
        ),
        "updated_at": (
            runtime_platform.get("updated_at")
            if isinstance(runtime_platform, dict)
            else None
        ),
        "home_channel": home_channel,
        "env_vars": env_vars,
    }


def _write_platform_enabled(platform_id: str, enabled: bool) -> None:
    config = load_config()
    platforms = config.setdefault("platforms", {})
    if not isinstance(platforms, dict):
        platforms = {}
        config["platforms"] = platforms
    platform_config = platforms.setdefault(platform_id, {})
    if not isinstance(platform_config, dict):
        platform_config = {}
        platforms[platform_id] = platform_config
    platform_config["enabled"] = enabled
    save_config(config)


_TELEGRAM_ONBOARDING_DEFAULT_URL = "https://setup.hermes-agent.nousresearch.com"
_TELEGRAM_USER_ID_RE = re.compile(r"^\d+$")


@dataclass
class _TelegramOnboardingPairing:
    poll_token: str
    expires_at: str
    expires_at_ts: float
    bot_token: str | None = None
    bot_username: str | None = None
    owner_user_id: str | None = None


_telegram_onboarding_pairings: dict[str, _TelegramOnboardingPairing] = {}
_telegram_onboarding_lock = threading.RLock()


def _telegram_onboarding_base_url() -> str:
    return (
        os.getenv("TELEGRAM_ONBOARDING_URL", _TELEGRAM_ONBOARDING_DEFAULT_URL)
        .strip()
        .rstrip("/")
    )


def _parse_expiry_ts(value: str) -> float:
    try:
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except Exception:
        return time.time() + 600


def _prune_telegram_onboarding_pairings() -> None:
    now = time.time()
    expired = [
        pairing_id
        for pairing_id, record in _telegram_onboarding_pairings.items()
        if record.expires_at_ts <= now
    ]
    for pairing_id in expired:
        _telegram_onboarding_pairings.pop(pairing_id, None)


def _normalize_telegram_user_id(value: Any) -> str | None:
    normalized = str(value or "").strip()
    if _TELEGRAM_USER_ID_RE.fullmatch(normalized):
        return normalized
    return None


def _telegram_onboarding_error_message(error: str, fallback: str) -> str:
    return {
        "not_found": "Telegram pairing was not found. Start a new setup.",
        "expired": "Telegram setup expired. Start a new setup.",
        "claimed": "Telegram setup was already claimed. Start a new setup.",
        "unauthorized": "Telegram setup service rejected this request.",
        "telegram_manager_bot_token_not_configured": "Telegram setup service is not configured.",
        "telegram_token_fetch_failed": "Telegram could not finish bot setup. Try again.",
    }.get(error, fallback)


def _telegram_onboarding_request_sync(
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    bearer_token: str | None = None,
) -> dict[str, Any]:
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"

    request = urllib.request.Request(
        f"{_telegram_onboarding_base_url()}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        try:
            parsed = json.loads(payload.decode("utf-8"))
        except Exception:
            parsed = {}
        error = str(parsed.get("error") or parsed.get("status") or "")
        detail = _telegram_onboarding_error_message(
            error,
            "Telegram setup service returned an error.",
        )
        status_code = 404 if exc.code == 404 else 502
        if error in {"expired", "claimed"}:
            status_code = 410
        raise HTTPException(status_code=status_code, detail=detail) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail="Telegram setup service is unavailable. Try again shortly.",
        ) from exc

    try:
        parsed = json.loads(payload.decode("utf-8"))
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail="Telegram setup service returned an invalid response.",
        ) from exc
    if not isinstance(parsed, dict):
        raise HTTPException(
            status_code=502,
            detail="Telegram setup service returned an invalid response.",
        )
    return parsed


async def _telegram_onboarding_request(
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    bearer_token: str | None = None,
) -> dict[str, Any]:
    return await asyncio.to_thread(
        _telegram_onboarding_request_sync,
        method,
        path,
        body=body,
        bearer_token=bearer_token,
    )


@app.post("/api/messaging/telegram/onboarding/start")
async def start_telegram_onboarding(body: TelegramOnboardingStart):
    bot_name = (body.bot_name or "Hermes Agent").strip() or "Hermes Agent"
    payload = await _telegram_onboarding_request(
        "POST",
        "/v1/telegram/pairings",
        body={"bot_name": bot_name},
    )

    pairing_id = str(payload.get("pairing_id") or "").strip()
    poll_token = str(payload.get("poll_token") or "").strip()
    expires_at = str(payload.get("expires_at") or "").strip()
    deep_link = str(payload.get("deep_link") or "").strip()
    qr_payload = str(payload.get("qr_payload") or deep_link).strip()
    suggested_username = str(payload.get("suggested_username") or "").strip()
    if not pairing_id or not poll_token or not expires_at or not deep_link:
        raise HTTPException(
            status_code=502,
            detail="Telegram setup service returned an incomplete response.",
        )

    with _telegram_onboarding_lock:
        _prune_telegram_onboarding_pairings()
        _telegram_onboarding_pairings[pairing_id] = _TelegramOnboardingPairing(
            poll_token=poll_token,
            expires_at=expires_at,
            expires_at_ts=_parse_expiry_ts(expires_at),
        )

    return {
        "pairing_id": pairing_id,
        "suggested_username": suggested_username,
        "deep_link": deep_link,
        "qr_payload": qr_payload,
        "expires_at": expires_at,
    }


@app.get("/api/messaging/telegram/onboarding/{pairing_id}")
async def get_telegram_onboarding_status(pairing_id: str):
    with _telegram_onboarding_lock:
        _prune_telegram_onboarding_pairings()
        record = _telegram_onboarding_pairings.get(pairing_id)
        if not record:
            raise HTTPException(
                status_code=404,
                detail="Telegram setup session was not found. Start a new setup.",
            )
        if record.bot_token:
            return {
                "status": "ready",
                "bot_username": record.bot_username,
                "owner_user_id": record.owner_user_id,
                "expires_at": record.expires_at,
            }
        poll_token = record.poll_token

    payload = await _telegram_onboarding_request(
        "GET",
        f"/v1/telegram/pairings/{urllib.parse.quote(pairing_id, safe='')}",
        bearer_token=poll_token,
    )
    status = str(payload.get("status") or "").strip()
    if status == "waiting":
        with _telegram_onboarding_lock:
            current = _telegram_onboarding_pairings.get(pairing_id)
            expires_at = current.expires_at if current else ""
        return {"status": "waiting", "expires_at": expires_at}

    if status == "ready":
        bot_token = str(payload.get("token") or "").strip()
        bot_username = str(payload.get("bot_username") or "").strip()
        if not bot_token:
            raise HTTPException(
                status_code=502,
                detail="Telegram setup service returned an incomplete response.",
            )
        owner_user_id = _normalize_telegram_user_id(payload.get("owner_user_id"))
        with _telegram_onboarding_lock:
            record = _telegram_onboarding_pairings.get(pairing_id)
            if not record:
                raise HTTPException(
                    status_code=404,
                    detail="Telegram setup session was not found. Start a new setup.",
                )
            record.bot_token = bot_token
            record.bot_username = bot_username or None
            record.owner_user_id = owner_user_id
            return {
                "status": "ready",
                "bot_username": record.bot_username,
                "owner_user_id": record.owner_user_id,
                "expires_at": record.expires_at,
            }

    if status in {"expired", "claimed"}:
        with _telegram_onboarding_lock:
            _telegram_onboarding_pairings.pop(pairing_id, None)
        raise HTTPException(
            status_code=410,
            detail=_telegram_onboarding_error_message(
                status,
                "Telegram setup is no longer available. Start a new setup.",
            ),
        )

    raise HTTPException(
        status_code=502,
        detail="Telegram setup service returned an unknown status.",
    )


@app.post("/api/messaging/telegram/onboarding/{pairing_id}/apply")
async def apply_telegram_onboarding(
    pairing_id: str, body: TelegramOnboardingApply
):
    allowed_user_ids = []
    seen = set()
    for raw_id in body.allowed_user_ids:
        normalized = _normalize_telegram_user_id(raw_id)
        if not normalized:
            raise HTTPException(
                status_code=400,
                detail="Allowed Telegram user IDs must be numeric.",
            )
        if normalized not in seen:
            seen.add(normalized)
            allowed_user_ids.append(normalized)
    if not allowed_user_ids:
        raise HTTPException(
            status_code=400,
            detail="Add at least one allowed Telegram user ID.",
        )

    with _telegram_onboarding_lock:
        _prune_telegram_onboarding_pairings()
        record = _telegram_onboarding_pairings.get(pairing_id)
        if not record:
            raise HTTPException(
                status_code=404,
                detail="Telegram setup session was not found. Start a new setup.",
            )
        bot_token = record.bot_token
        bot_username = record.bot_username
        if not bot_token:
            raise HTTPException(
                status_code=409,
                detail="Telegram setup is not ready yet.",
            )

    try:
        save_env_value("TELEGRAM_BOT_TOKEN", bot_token)
        save_env_value("TELEGRAM_ALLOWED_USERS", ",".join(allowed_user_ids))
        _write_platform_enabled("telegram", True)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        _log.exception("Telegram onboarding apply failed")
        raise HTTPException(
            status_code=500,
            detail="Failed to save Telegram setup.",
        ) from exc

    with _telegram_onboarding_lock:
        _telegram_onboarding_pairings.pop(pairing_id, None)

    return {
        "ok": True,
        "platform": "telegram",
        "bot_username": bot_username,
        "needs_restart": True,
    }


@app.delete("/api/messaging/telegram/onboarding/{pairing_id}")
async def cancel_telegram_onboarding(pairing_id: str):
    with _telegram_onboarding_lock:
        _telegram_onboarding_pairings.pop(pairing_id, None)
    return {"ok": True}


@app.get("/api/messaging/platforms")
async def get_messaging_platforms():
    env_on_disk = load_env()
    runtime = read_runtime_status()
    return {
        "platforms": [
            _messaging_platform_payload(entry, env_on_disk, runtime)
            for entry in _messaging_platform_catalog()
        ]
    }


@app.put("/api/messaging/platforms/{platform_id}")
async def update_messaging_platform(platform_id: str, body: MessagingPlatformUpdate):
    entry = _catalog_lookup(platform_id)
    if not entry:
        raise HTTPException(
            status_code=404, detail=f"Unknown messaging platform: {platform_id}"
        )

    allowed_env = set(entry["env_vars"])
    try:
        for key in body.clear_env:
            if key not in allowed_env:
                raise HTTPException(
                    status_code=400,
                    detail=f"{key} is not configurable for {entry['name']}",
                )
            remove_env_value(key)

        for key, value in body.env.items():
            if key not in allowed_env:
                raise HTTPException(
                    status_code=400,
                    detail=f"{key} is not configurable for {entry['name']}",
                )
            trimmed = value.strip()
            if trimmed:
                save_env_value(key, trimmed)

        if body.enabled is not None:
            _write_platform_enabled(platform_id, body.enabled)

        return {"ok": True, "platform": platform_id}
    except HTTPException:
        raise
    except Exception:
        _log.exception("PUT /api/messaging/platforms/%s failed", platform_id)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/api/messaging/platforms/{platform_id}/test")
async def test_messaging_platform(platform_id: str):
    entry = _catalog_lookup(platform_id)
    if not entry:
        raise HTTPException(
            status_code=404, detail=f"Unknown messaging platform: {platform_id}"
        )

    env_on_disk = load_env()
    payload = _messaging_platform_payload(entry, env_on_disk, read_runtime_status())
    if not payload["enabled"]:
        message = f"{entry['name']} is disabled. Enable it, then restart the gateway."
        return {"ok": False, "state": payload["state"], "message": message}
    if not payload["configured"]:
        missing = [
            field["key"]
            for field in payload["env_vars"]
            if field["required"] and not field["is_set"]
        ]
        message = (
            f"Missing required setup: {', '.join(missing)}"
            if missing
            else "Platform setup is incomplete."
        )
        return {"ok": False, "state": payload["state"], "message": message}
    if not payload["gateway_running"]:
        return {
            "ok": False,
            "state": payload["state"],
            "message": "Gateway is not running. Restart the gateway to connect this platform.",
        }
    if payload["state"] == "connected":
        return {
            "ok": True,
            "state": payload["state"],
            "message": f"{entry['name']} is connected.",
        }
    if payload.get("error_message"):
        return {
            "ok": False,
            "state": payload["state"],
            "message": payload["error_message"],
        }
    return {
        "ok": False,
        "state": payload["state"],
        "message": "Setup looks complete, but the gateway has not reported a connection yet. Restart the gateway.",
    }


# ---------------------------------------------------------------------------
# OAuth provider endpoints — status + disconnect (Phase 1)
# ---------------------------------------------------------------------------
#
# Phase 1 surfaces *which OAuth providers exist* and whether each is
# connected, plus a disconnect button. The actual login flow (PKCE for
# Anthropic, device-code for Nous/Codex) still runs in the CLI for now;
# Phase 2 will add in-browser flows. For unconnected providers we return
# the canonical ``hermes auth add <provider>`` command so the dashboard
# can surface a one-click copy.


def _truncate_token(value: Optional[str], visible: int = 6) -> str:
    """Return ``...XXXXXX`` (last N chars) for safe display in the UI.

    We never expose more than the trailing ``visible`` characters of an
    OAuth access token. JWT prefixes (the part before the first dot) are
    stripped first when present so the visible suffix is always part of
    the signing region rather than a meaningless header chunk.

    Returns the Entra-ID placeholder when handed a callable (Azure Foundry
    bearer provider) — the callable is NEVER invoked here.
    """
    if not value:
        return ""
    if callable(value) and not isinstance(value, str):
        # Entra ID bearer provider — never reveal a minted token in the UI.
        return "<entra-id-bearer>"
    s = str(value)
    if "." in s and s.count(".") >= 2:
        # Looks like a JWT — show the trailing piece of the signature only.
        s = s.rsplit(".", 1)[-1]
    if len(s) <= visible:
        return s
    return f"…{s[-visible:]}"


def _anthropic_oauth_status() -> Dict[str, Any]:
    """Combined status across the three Anthropic credential sources we read.

    Hermes resolves Anthropic creds in this order at runtime:
    1. ``~/.hermes/.anthropic_oauth.json`` — Hermes-managed PKCE flow
    2. ``~/.claude/.credentials.json`` — Claude Code CLI credentials (auto)
    3. ``ANTHROPIC_TOKEN`` / ``ANTHROPIC_API_KEY`` env vars
    The dashboard reports the highest-priority source that's actually present.
    """
    try:
        from agent.anthropic_adapter import (
            read_hermes_oauth_credentials,
            read_claude_code_credentials,
            _HERMES_OAUTH_FILE,
        )
    except ImportError:
        read_claude_code_credentials = None  # type: ignore
        read_hermes_oauth_credentials = None  # type: ignore
        _HERMES_OAUTH_FILE = None  # type: ignore

    hermes_creds = None
    if read_hermes_oauth_credentials:
        try:
            hermes_creds = read_hermes_oauth_credentials()
        except Exception:
            hermes_creds = None
    if hermes_creds and hermes_creds.get("accessToken"):
        return {
            "logged_in": True,
            "source": "hermes_pkce",
            "source_label": f"Hermes PKCE ({_HERMES_OAUTH_FILE})",
            "token_preview": _truncate_token(hermes_creds.get("accessToken")),
            "expires_at": hermes_creds.get("expiresAt"),
            "has_refresh_token": bool(hermes_creds.get("refreshToken")),
        }

    cc_creds = None
    if read_claude_code_credentials:
        try:
            cc_creds = read_claude_code_credentials()
        except Exception:
            cc_creds = None
    if cc_creds and cc_creds.get("accessToken"):
        return {
            "logged_in": True,
            "source": "claude_code",
            "source_label": "Claude Code (~/.claude/.credentials.json)",
            "token_preview": _truncate_token(cc_creds.get("accessToken")),
            "expires_at": cc_creds.get("expiresAt"),
            "has_refresh_token": bool(cc_creds.get("refreshToken")),
        }

    env_token = os.getenv("ANTHROPIC_TOKEN") or os.getenv("CLAUDE_CODE_OAUTH_TOKEN")
    if env_token:
        return {
            "logged_in": True,
            "source": "env_var",
            "source_label": "ANTHROPIC_TOKEN environment variable",
            "token_preview": _truncate_token(env_token),
            "expires_at": None,
            "has_refresh_token": False,
        }
    return {"logged_in": False, "source": None}


def _claude_code_only_status() -> Dict[str, Any]:
    """Surface Claude Code CLI credentials as their own provider entry.

    Independent of the Anthropic entry above so users can see whether their
    Claude Code subscription tokens are actively flowing into Hermes even
    when they also have a separate Hermes-managed PKCE login.
    """
    try:
        from agent.anthropic_adapter import read_claude_code_credentials
        creds = read_claude_code_credentials()
    except Exception:
        creds = None
    if creds and creds.get("accessToken"):
        return {
            "logged_in": True,
            "source": "claude_code_cli",
            "source_label": "~/.claude/.credentials.json",
            "token_preview": _truncate_token(creds.get("accessToken")),
            "expires_at": creds.get("expiresAt"),
            "has_refresh_token": bool(creds.get("refreshToken")),
        }
    return {"logged_in": False, "source": None}


# Provider catalog. The order matters — it's how we render the UI list.
# ``cli_command`` is what the dashboard surfaces as the copy-to-clipboard
# fallback while Phase 2 (in-browser flows) isn't built yet.
# ``flow`` describes the OAuth shape so the future modal can pick the
# right UI: ``pkce`` = open URL + paste callback code, ``device_code`` =
# show code + verification URL + poll, ``external`` = read-only (delegated
# to a third-party CLI like Claude Code or Qwen).
_OAUTH_PROVIDER_CATALOG: tuple[Dict[str, Any], ...] = (
    {
        "id": "nous",
        "name": "Nous Portal",
        "flow": "device_code",
        "cli_command": "hermes auth add nous",
        "docs_url": "https://portal.nousresearch.com",
        "status_fn": None,  # dispatched via auth.get_nous_auth_status
    },
    {
        "id": "openai-codex",
        "name": "OpenAI OAuth (ChatGPT)",
        "flow": "device_code",
        "cli_command": "hermes auth add openai-codex",
        "docs_url": "https://platform.openai.com/docs",
        "status_fn": None,  # dispatched via auth.get_codex_auth_status
    },
    {
        "id": "qwen-oauth",
        "name": "Qwen (via Qwen CLI)",
        "flow": "external",
        "cli_command": "hermes auth add qwen-oauth",
        "docs_url": "https://github.com/QwenLM/qwen-code",
        "status_fn": None,  # dispatched via auth.get_qwen_auth_status
    },
    {
        "id": "minimax-oauth",
        "name": "MiniMax (OAuth)",
        # MiniMax's flow is structurally device-code (verification URI +
        # user code, backend polls the token endpoint) with a PKCE
        # extension for code-binding. The dashboard renders the same UX
        # as Nous's device-code flow; the PKCE bit is a security
        # extension that doesn't change the operator experience.
        "flow": "device_code",
        "cli_command": "hermes auth add minimax-oauth",
        "docs_url": "https://www.minimax.io",
        "status_fn": None,  # dispatched via auth.get_minimax_oauth_auth_status
    },
    {
        "id": "xai-oauth",
        "name": "xAI Grok OAuth (SuperGrok / Premium+)",
        # Loopback PKCE: the desktop's local backend binds a 127.0.0.1
        # callback server, the client opens the browser, and the redirect
        # lands back on the loopback listener — no code to copy/paste.
        "flow": "loopback",
        "cli_command": "hermes auth add xai-oauth",
        "docs_url": "https://hermes-agent.nousresearch.com/docs/guides/xai-grok-oauth",
        "status_fn": None,  # dispatched via auth.get_xai_oauth_auth_status
    },
    # ── Anthropic / Claude entries sit at the bottom: the API-key path
    # first, then the subscription OAuth path (which only works with extra
    # usage credits on top of a Claude Max plan — see disclaimer in name).
    {
        "id": "anthropic",
        "name": "Anthropic API Key",
        "flow": "pkce",
        "cli_command": "hermes auth add anthropic",
        "docs_url": "https://docs.claude.com/en/api/getting-started",
        "status_fn": _anthropic_oauth_status,
    },
    {
        "id": "claude-code",
        "name": "Anthropic OAuth: Required Extra Usage Credits to Use Subscription",
        "flow": "external",
        "cli_command": "claude setup-token",
        "docs_url": "https://docs.claude.com/en/docs/claude-code",
        "status_fn": _claude_code_only_status,
    },
)


def _resolve_provider_status(provider_id: str, status_fn) -> Dict[str, Any]:
    """Dispatch to the right status helper for an OAuth provider entry."""
    if status_fn is not None:
        try:
            return status_fn()
        except Exception as e:
            return {"logged_in": False, "error": str(e)}
    try:
        from hermes_cli import auth as hauth
        if provider_id == "nous":
            raw = hauth.get_nous_auth_status()
            return {
                "logged_in": bool(raw.get("logged_in")),
                "source": "nous_portal",
                "source_label": raw.get("portal_base_url") or "Nous Portal",
                "token_preview": _truncate_token(raw.get("access_token")),
                "expires_at": raw.get("access_expires_at"),
                "has_refresh_token": bool(raw.get("has_refresh_token")),
            }
        if provider_id == "openai-codex":
            raw = hauth.get_codex_auth_status()
            return {
                "logged_in": bool(raw.get("logged_in")),
                "source": raw.get("source") or "openai_codex",
                "source_label": raw.get("auth_mode") or "OpenAI Codex",
                "token_preview": _truncate_token(raw.get("api_key")),
                "expires_at": None,
                "has_refresh_token": False,
                "last_refresh": raw.get("last_refresh"),
            }
        if provider_id == "qwen-oauth":
            raw = hauth.get_qwen_auth_status()
            return {
                "logged_in": bool(raw.get("logged_in")),
                "source": "qwen_cli",
                "source_label": raw.get("auth_store_path") or "Qwen CLI",
                "token_preview": _truncate_token(raw.get("access_token")),
                "expires_at": raw.get("expires_at"),
                "has_refresh_token": bool(raw.get("has_refresh_token")),
            }
        if provider_id == "minimax-oauth":
            raw = hauth.get_minimax_oauth_auth_status()
            return {
                "logged_in": bool(raw.get("logged_in")),
                "source": "minimax_oauth",
                "source_label": f"MiniMax ({raw.get('region', 'global')})",
                "token_preview": None,
                "expires_at": raw.get("expires_at"),
                "has_refresh_token": True,
            }
        if provider_id == "xai-oauth":
            raw = hauth.get_xai_oauth_auth_status()
            # source_label is meant to be a human-readable origin (auth-store
            # path / credential source), not the internal auth_mode string
            # ("oauth_pkce"). Prefer the store path, then the source slug.
            return {
                "logged_in": bool(raw.get("logged_in")),
                "source": raw.get("source") or "xai_oauth",
                "source_label": raw.get("auth_store") or raw.get("source") or "xAI Grok OAuth",
                "token_preview": _truncate_token(raw.get("api_key")),
                "expires_at": None,
                "has_refresh_token": True,
                "last_refresh": raw.get("last_refresh"),
            }
    except Exception as e:
        return {"logged_in": False, "error": str(e)}
    return {"logged_in": False}


@app.get("/api/providers/oauth")
async def list_oauth_providers():
    """Enumerate every OAuth-capable LLM provider with current status.

    Response shape (per provider):
        id              stable identifier (used in DELETE path)
        name            human label
        flow            "pkce" | "device_code" | "external" | "loopback"
        cli_command     fallback CLI command for users to run manually
        docs_url        external docs/portal link for the "Learn more" link
        status:
          logged_in        bool — currently has usable creds
          source           short slug ("hermes_pkce", "claude_code", ...)
          source_label     human-readable origin (file path, env var name)
          token_preview    last N chars of the token, never the full token
          expires_at       ISO timestamp string or null
          has_refresh_token bool
    """
    providers = []
    for p in _OAUTH_PROVIDER_CATALOG:
        status = _resolve_provider_status(p["id"], p.get("status_fn"))
        providers.append({
            "id": p["id"],
            "name": p["name"],
            "flow": p["flow"],
            "cli_command": p["cli_command"],
            "docs_url": p["docs_url"],
            "status": status,
        })
    return {"providers": providers}


@app.delete("/api/providers/oauth/{provider_id}")
async def disconnect_oauth_provider(provider_id: str, request: Request):
    """Disconnect an OAuth provider. Token-protected (matches /env/reveal)."""
    _require_token(request)

    valid_ids = {p["id"] for p in _OAUTH_PROVIDER_CATALOG}
    if provider_id not in valid_ids:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown provider: {provider_id}. "
                   f"Available: {', '.join(sorted(valid_ids))}",
        )

    # Anthropic and claude-code clear the same Hermes-managed PKCE file
    # AND forget the Claude Code import. We don't touch ~/.claude/* directly
    # — that's owned by the Claude Code CLI; users can re-auth there if they
    # want to undo a disconnect.
    if provider_id in {"anthropic", "claude-code"}:
        try:
            from agent.anthropic_adapter import _HERMES_OAUTH_FILE
            if _HERMES_OAUTH_FILE.exists():
                _HERMES_OAUTH_FILE.unlink()
        except Exception:
            pass
        # Also clear the credential pool entry if present.
        try:
            from hermes_cli.auth import clear_provider_auth
            clear_provider_auth("anthropic")
        except Exception:
            pass
        _log.info("oauth/disconnect: %s", provider_id)
        return {"ok": True, "provider": provider_id}

    try:
        from hermes_cli.auth import clear_provider_auth
        cleared = clear_provider_auth(provider_id)
        _log.info("oauth/disconnect: %s (cleared=%s)", provider_id, cleared)
        return {"ok": bool(cleared), "provider": provider_id}
    except Exception as e:
        _log.exception("disconnect %s failed", provider_id)
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# OAuth Phase 2 — in-browser PKCE & device-code flows
# ---------------------------------------------------------------------------
#
# Two flow shapes are supported:
#
#   PKCE (Anthropic):
#     1. POST /api/providers/oauth/anthropic/start
#          → server generates code_verifier + challenge, builds claude.ai
#            authorize URL, stashes verifier in _oauth_sessions[session_id]
#          → returns { session_id, flow: "pkce", auth_url }
#     2. UI opens auth_url in a new tab. User authorizes, copies code.
#     3. POST /api/providers/oauth/anthropic/submit { session_id, code }
#          → server exchanges (code + verifier) → tokens at console.anthropic.com
#          → persists to ~/.hermes/.anthropic_oauth.json AND credential pool
#          → returns { ok: true, status: "approved" }
#
#   Device code (Nous, OpenAI Codex):
#     1. POST /api/providers/oauth/{nous|openai-codex}/start
#          → server hits provider's device-auth endpoint
#          → gets { user_code, verification_url, device_code, interval, expires_in }
#          → spawns background poller thread that polls the token endpoint
#            every `interval` seconds until approved/expired
#          → stores poll status in _oauth_sessions[session_id]
#          → returns { session_id, flow: "device_code", user_code,
#                      verification_url, expires_in, poll_interval }
#     2. UI opens verification_url in a new tab and shows user_code.
#     3. UI polls GET /api/providers/oauth/{provider}/poll/{session_id}
#          every 2s until status != "pending".
#     4. On "approved" the background thread has already saved creds; UI
#        refreshes the providers list.
#
#   Loopback PKCE (xAI Grok):
#     1. POST /api/providers/oauth/xai-oauth/start
#          → server binds a 127.0.0.1 callback listener, builds the xAI
#            authorize URL, spawns a background worker waiting on the redirect
#          → returns { session_id, flow: "loopback", auth_url, expires_in }
#     2. UI opens auth_url in the browser. There is NO user_code/code to
#        paste — the redirect lands back on the loopback listener.
#     3. UI polls GET /api/providers/oauth/{provider}/poll/{session_id}
#          (same endpoint as device_code) until status != "pending".
#     4. The worker exchanges the code, persists creds, sets "approved".
#        DELETE /sessions/{id} cancels: the worker bails before persisting
#        and the callback server is shut down to free the port immediately.
#
# Sessions are kept in-memory only (single-process FastAPI) and time out
# after 15 minutes. A periodic cleanup runs on each /start call to GC
# expired sessions so the dict doesn't grow without bound.

_OAUTH_SESSION_TTL_SECONDS = 15 * 60
_oauth_sessions: Dict[str, Dict[str, Any]] = {}
_oauth_sessions_lock = threading.Lock()

# Import OAuth constants from canonical source instead of duplicating.
# Guarded so hermes web still starts if anthropic_adapter is unavailable;
# Phase 2 endpoints will return 501 in that case.
try:
    from agent.anthropic_adapter import (
        _OAUTH_CLIENT_ID as _ANTHROPIC_OAUTH_CLIENT_ID,
        _OAUTH_TOKEN_URL as _ANTHROPIC_OAUTH_TOKEN_URL,
        _OAUTH_REDIRECT_URI as _ANTHROPIC_OAUTH_REDIRECT_URI,
        _OAUTH_SCOPES as _ANTHROPIC_OAUTH_SCOPES,
        _generate_pkce as _generate_pkce_pair,
    )
    _ANTHROPIC_OAUTH_AVAILABLE = True
except ImportError:
    _ANTHROPIC_OAUTH_AVAILABLE = False
_ANTHROPIC_OAUTH_AUTHORIZE_URL = "https://claude.ai/oauth/authorize"


def _gc_oauth_sessions() -> None:
    """Drop expired sessions. Called opportunistically on /start."""
    cutoff = time.time() - _OAUTH_SESSION_TTL_SECONDS
    with _oauth_sessions_lock:
        stale = [sid for sid, sess in _oauth_sessions.items() if sess["created_at"] < cutoff]
        for sid in stale:
            _oauth_sessions.pop(sid, None)


def _new_oauth_session(provider_id: str, flow: str) -> tuple[str, Dict[str, Any]]:
    """Create + register a new OAuth session, return (session_id, session_dict)."""
    sid = secrets.token_urlsafe(16)
    sess = {
        "session_id": sid,
        "provider": provider_id,
        "flow": flow,
        "created_at": time.time(),
        "status": "pending",  # pending | approved | denied | expired | error
        "error_message": None,
    }
    with _oauth_sessions_lock:
        _oauth_sessions[sid] = sess
    return sid, sess


def _save_anthropic_oauth_creds(access_token: str, refresh_token: str, expires_at_ms: int) -> None:
    """Persist Anthropic PKCE creds to both Hermes file AND credential pool.

    Mirrors what auth_commands.add_command does so the dashboard flow leaves
    the system in the same state as ``hermes auth add anthropic``.
    """
    from agent.anthropic_adapter import _HERMES_OAUTH_FILE
    payload = {
        "accessToken": access_token,
        "refreshToken": refresh_token,
        "expiresAt": expires_at_ms,
    }
    _HERMES_OAUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = _HERMES_OAUTH_FILE.with_name(
        f"{_HERMES_OAUTH_FILE.name}.tmp.{os.getpid()}.{secrets.token_hex(8)}"
    )
    try:
        with tmp_path.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, _HERMES_OAUTH_FILE)
        try:
            _HERMES_OAUTH_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
    # Best-effort credential-pool insert. Failure here doesn't invalidate
    # the file write — pool registration only matters for the rotation
    # strategy, not for runtime credential resolution.
    try:
        from agent.credential_pool import (
            PooledCredential,
            load_pool,
            AUTH_TYPE_OAUTH,
            SOURCE_MANUAL,
        )
        import uuid
        pool = load_pool("anthropic")
        # Avoid duplicate entries: delete any prior dashboard-issued OAuth entry
        existing = [e for e in pool.entries() if getattr(e, "source", "").startswith(f"{SOURCE_MANUAL}:dashboard_pkce")]
        for e in existing:
            try:
                pool.remove_entry(getattr(e, "id", ""))
            except Exception:
                pass
        entry = PooledCredential(
            provider="anthropic",
            id=uuid.uuid4().hex[:6],
            label="dashboard PKCE",
            auth_type=AUTH_TYPE_OAUTH,
            priority=0,
            source=f"{SOURCE_MANUAL}:dashboard_pkce",
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at_ms=expires_at_ms,
        )
        pool.add_entry(entry)
    except Exception as e:
        _log.warning("anthropic pool add (dashboard) failed: %s", e)


def _start_anthropic_pkce() -> Dict[str, Any]:
    """Begin PKCE flow. Returns the auth URL the UI should open."""
    if not _ANTHROPIC_OAUTH_AVAILABLE:
        raise HTTPException(status_code=501, detail="Anthropic OAuth not available (missing adapter)")
    verifier, challenge = _generate_pkce_pair()
    sid, sess = _new_oauth_session("anthropic", "pkce")
    sess["verifier"] = verifier
    sess["state"] = verifier  # Anthropic round-trips verifier as state
    params = {
        "code": "true",
        "client_id": _ANTHROPIC_OAUTH_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": _ANTHROPIC_OAUTH_REDIRECT_URI,
        "scope": _ANTHROPIC_OAUTH_SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": verifier,
    }
    auth_url = f"{_ANTHROPIC_OAUTH_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"
    return {
        "session_id": sid,
        "flow": "pkce",
        "auth_url": auth_url,
        "expires_in": _OAUTH_SESSION_TTL_SECONDS,
    }


def _submit_anthropic_pkce(session_id: str, code_input: str) -> Dict[str, Any]:
    """Exchange authorization code for tokens. Persists on success."""
    with _oauth_sessions_lock:
        sess = _oauth_sessions.get(session_id)
    if not sess or sess["provider"] != "anthropic" or sess["flow"] != "pkce":
        raise HTTPException(status_code=404, detail="Unknown or expired session")
    if sess["status"] != "pending":
        return {"ok": False, "status": sess["status"], "message": sess.get("error_message")}

    # Anthropic's redirect callback page formats the code as `<code>#<state>`.
    # Strip the state suffix if present (we already have the verifier server-side).
    parts = code_input.strip().split("#", 1)
    code = parts[0].strip()
    if not code:
        return {"ok": False, "status": "error", "message": "No code provided"}
    state_from_callback = parts[1] if len(parts) > 1 else ""

    exchange_data = json.dumps({
        "grant_type": "authorization_code",
        "client_id": _ANTHROPIC_OAUTH_CLIENT_ID,
        "code": code,
        "state": state_from_callback or sess["state"],
        "redirect_uri": _ANTHROPIC_OAUTH_REDIRECT_URI,
        "code_verifier": sess["verifier"],
    }).encode()
    req = urllib.request.Request(
        _ANTHROPIC_OAUTH_TOKEN_URL,
        data=exchange_data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "hermes-dashboard/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            result = json.loads(resp.read().decode())
    except Exception as e:
        with _oauth_sessions_lock:
            sess["status"] = "error"
            sess["error_message"] = f"Token exchange failed: {e}"
        return {"ok": False, "status": "error", "message": sess["error_message"]}

    access_token = result.get("access_token", "")
    refresh_token = result.get("refresh_token", "")
    expires_in = int(result.get("expires_in") or 3600)
    if not access_token:
        with _oauth_sessions_lock:
            sess["status"] = "error"
            sess["error_message"] = "No access token returned"
        return {"ok": False, "status": "error", "message": sess["error_message"]}

    expires_at_ms = int(time.time() * 1000) + (expires_in * 1000)
    try:
        _save_anthropic_oauth_creds(access_token, refresh_token, expires_at_ms)
    except Exception as e:
        with _oauth_sessions_lock:
            sess["status"] = "error"
            sess["error_message"] = f"Save failed: {e}"
        return {"ok": False, "status": "error", "message": sess["error_message"]}
    with _oauth_sessions_lock:
        sess["status"] = "approved"
    _log.info("oauth/pkce: anthropic login completed (session=%s)", session_id)
    return {"ok": True, "status": "approved"}


async def _start_device_code_flow(provider_id: str) -> Dict[str, Any]:
    """Initiate a device-code flow (Nous, OpenAI Codex, or MiniMax).

    Calls the provider's device-auth endpoint via the existing CLI helpers,
    then spawns a background poller. Returns the user-facing display fields
    so the UI can render the verification page link + user code.
    """
    if provider_id == "nous":
        from hermes_cli.auth import (
            _request_device_code,
            PROVIDER_REGISTRY,
        )
        import httpx
        pconfig = PROVIDER_REGISTRY["nous"]
        portal_base_url = (
            os.getenv("HERMES_PORTAL_BASE_URL")
            or os.getenv("NOUS_PORTAL_BASE_URL")
            or pconfig.portal_base_url
        ).rstrip("/")
        client_id = pconfig.client_id
        scope = pconfig.scope

        def _do_nous_device_request():
            with httpx.Client(
                timeout=httpx.Timeout(15.0),
                headers={"Accept": "application/json"},
            ) as client:
                return (
                    _request_device_code(
                        client=client,
                        portal_base_url=portal_base_url,
                        client_id=client_id,
                        scope=scope,
                    ),
                    scope,
                )

        device_data, effective_scope = await asyncio.get_running_loop().run_in_executor(
            None, _do_nous_device_request
        )
        sid, sess = _new_oauth_session("nous", "device_code")
        sess["device_code"] = str(device_data["device_code"])
        sess["interval"] = int(device_data["interval"])
        sess["expires_at"] = time.time() + int(device_data["expires_in"])
        sess["portal_base_url"] = portal_base_url
        sess["client_id"] = client_id
        sess["scope"] = effective_scope
        threading.Thread(
            target=_nous_poller, args=(sid,), daemon=True, name=f"oauth-poll-{sid[:6]}"
        ).start()
        return {
            "session_id": sid,
            "flow": "device_code",
            "user_code": str(device_data["user_code"]),
            "verification_url": str(device_data["verification_uri_complete"]),
            "expires_in": int(device_data["expires_in"]),
            "poll_interval": int(device_data["interval"]),
        }

    if provider_id == "openai-codex":
        # Codex uses fixed OpenAI device-auth endpoints; reuse the helper.
        sid, _ = _new_oauth_session("openai-codex", "device_code")
        # Use the helper but in a thread because it polls inline.
        # We can't extract just the start step without refactoring auth.py,
        # so we run the full helper in a worker and proxy the user_code +
        # verification_url back via the session dict. The helper prints
        # to stdout — we capture nothing here, just status.
        threading.Thread(
            target=_codex_full_login_worker, args=(sid,), daemon=True,
            name=f"oauth-codex-{sid[:6]}",
        ).start()
        # Block briefly until the worker has populated the user_code, OR error.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with _oauth_sessions_lock:
                s = _oauth_sessions.get(sid)
            if s and (s.get("user_code") or s["status"] != "pending"):
                break
            await asyncio.sleep(0.1)
        with _oauth_sessions_lock:
            s = _oauth_sessions.get(sid, {})
        if s.get("status") == "error":
            raise HTTPException(status_code=500, detail=s.get("error_message") or "device-auth failed")
        if not s.get("user_code"):
            raise HTTPException(status_code=504, detail="device-auth timed out before returning a user code")
        return {
            "session_id": sid,
            "flow": "device_code",
            "user_code": s["user_code"],
            "verification_url": s["verification_url"],
            "expires_in": int(s.get("expires_in") or 900),
            "poll_interval": int(s.get("interval") or 5),
        }

    if provider_id == "minimax-oauth":
        # MiniMax uses a device-code-style flow (verification URI + user
        # code + background poll) with a PKCE extension on top. From the
        # operator's perspective it's identical to Nous's device-code
        # flow; the PKCE bit (verifier + challenge from
        # _minimax_pkce_pair) is a security extension that binds the
        # token exchange to the original session.
        from hermes_cli.auth import (
            _minimax_pkce_pair,
            _minimax_request_user_code,
            MINIMAX_OAUTH_CLIENT_ID,
            MINIMAX_OAUTH_GLOBAL_BASE,
        )
        import httpx
        verifier, challenge, state = _minimax_pkce_pair()
        portal_base_url = (
            os.getenv("MINIMAX_PORTAL_BASE_URL") or MINIMAX_OAUTH_GLOBAL_BASE
        ).rstrip("/")
        def _do_minimax_request():
            with httpx.Client(
                timeout=httpx.Timeout(15.0),
                headers={"Accept": "application/json"},
                follow_redirects=True,
            ) as client:
                return _minimax_request_user_code(
                    client=client,
                    portal_base_url=portal_base_url,
                    client_id=MINIMAX_OAUTH_CLIENT_ID,
                    code_challenge=challenge,
                    state=state,
                )
        device_data = await asyncio.get_event_loop().run_in_executor(
            None, _do_minimax_request
        )
        sid, sess = _new_oauth_session("minimax-oauth", "device_code")
        # The CLI flow names this `interval_ms` because MiniMax's
        # `interval` field is in milliseconds (defensive default 2000ms
        # in _minimax_poll_token).
        interval_raw = device_data.get("interval")
        sess["interval_ms"] = (
            int(interval_raw) if interval_raw is not None else None
        )
        sess["user_code"] = str(device_data["user_code"])
        sess["code_verifier"] = verifier
        sess["state"] = state
        sess["portal_base_url"] = portal_base_url
        sess["client_id"] = MINIMAX_OAUTH_CLIENT_ID
        sess["region"] = "global"
        # `expired_in` from MiniMax is overloaded — could be a unix-ms
        # timestamp OR a seconds-from-now duration. Mirror the heuristic
        # in _minimax_poll_token. Stash the raw value for the poller;
        # compute a derived expires_at + UI-friendly expires_in seconds.
        expired_in_raw = int(device_data["expired_in"])
        sess["expired_in_raw"] = expired_in_raw
        if expired_in_raw > 1_000_000_000_000:  # likely unix-ms
            expires_at_ts = expired_in_raw / 1000.0
            expires_in_seconds = max(0, int(expires_at_ts - time.time()))
        else:
            expires_at_ts = time.time() + expired_in_raw
            expires_in_seconds = expired_in_raw
        sess["expires_at"] = expires_at_ts
        threading.Thread(
            target=_minimax_poller,
            args=(sid,),
            daemon=True,
            name=f"oauth-poll-{sid[:6]}",
        ).start()
        return {
            "session_id": sid,
            "flow": "device_code",
            "user_code": str(device_data["user_code"]),
            "verification_url": str(device_data["verification_uri"]),
            "expires_in": expires_in_seconds,
            "poll_interval": max(2, (sess["interval_ms"] or 2000) // 1000),
        }

    raise HTTPException(status_code=400, detail=f"Provider {provider_id} does not support device-code flow")


# xAI Grok OAuth uses a loopback-redirect PKCE flow (RFC 8252). Unlike the
# device-code providers there is no user_code to display: the local backend
# binds a 127.0.0.1 callback server, the client opens the authorize URL in
# the browser, and the redirect lands back on the loopback listener. The
# background worker waits for that callback, exchanges the code, and persists
# the tokens exactly like `hermes auth add xai-oauth`.
_XAI_LOOPBACK_TIMEOUT_SECONDS = 300.0


def _start_xai_loopback_flow() -> Dict[str, Any]:
    """Begin the xAI loopback PKCE flow.

    Binds the local callback server, builds the authorize URL, and spawns a
    background worker that waits for the redirect and finishes the exchange.
    Returns the authorize URL for the client to open in the browser.
    """
    from hermes_cli import auth as hauth

    discovery = hauth._xai_oauth_discovery()
    server, thread, callback_result, redirect_uri = hauth._xai_start_callback_server()
    try:
        hauth._xai_validate_loopback_redirect_uri(redirect_uri)
        verifier = hauth._oauth_pkce_code_verifier()
        challenge = hauth._oauth_pkce_code_challenge(verifier)
        state = secrets.token_hex(16)
        nonce = secrets.token_hex(16)
        authorize_url = hauth._xai_oauth_build_authorize_url(
            authorization_endpoint=discovery["authorization_endpoint"],
            redirect_uri=redirect_uri,
            code_challenge=challenge,
            state=state,
            nonce=nonce,
        )
    except Exception:
        # Binding succeeded but URL construction failed — release the socket
        # and join the serving thread so we don't leak a listener (or a
        # lingering daemon thread) on the loopback port.
        try:
            server.shutdown()
            server.server_close()
        except Exception:
            pass
        try:
            thread.join(timeout=1.0)
        except Exception:
            pass
        raise

    sid, sess = _new_oauth_session("xai-oauth", "loopback")
    sess["server"] = server
    sess["thread"] = thread
    sess["callback_result"] = callback_result
    sess["redirect_uri"] = redirect_uri
    sess["verifier"] = verifier
    sess["challenge"] = challenge
    sess["state"] = state
    sess["token_endpoint"] = discovery["token_endpoint"]
    sess["discovery"] = discovery
    sess["expires_at"] = time.time() + _XAI_LOOPBACK_TIMEOUT_SECONDS
    threading.Thread(
        target=_xai_loopback_worker, args=(sid,), daemon=True,
        name=f"oauth-xai-{sid[:6]}",
    ).start()
    return {
        "session_id": sid,
        "flow": "loopback",
        "auth_url": authorize_url,
        "expires_in": int(_XAI_LOOPBACK_TIMEOUT_SECONDS),
    }


def _xai_loopback_worker(session_id: str) -> None:
    """Wait for the xAI loopback callback, exchange the code, persist tokens."""
    from datetime import datetime, timezone

    from hermes_cli import auth as hauth

    with _oauth_sessions_lock:
        sess = _oauth_sessions.get(session_id)
    if not sess:
        return

    def _fail(message: str) -> None:
        with _oauth_sessions_lock:
            s = _oauth_sessions.get(session_id)
            if s is not None:
                s["status"] = "error"
                s["error_message"] = message

    def _cancelled() -> bool:
        # The session is removed from the registry when the user cancels
        # (DELETE /sessions/{id}). If that happened while we were blocked on
        # the callback or token exchange, abort instead of persisting tokens
        # the user no longer wants.
        with _oauth_sessions_lock:
            return session_id not in _oauth_sessions

    try:
        callback = hauth._xai_wait_for_callback(
            sess["server"],
            sess["thread"],
            sess["callback_result"],
            timeout_seconds=_XAI_LOOPBACK_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        _fail(f"xAI authorization timed out: {exc}")
        return

    if _cancelled():
        return

    if callback.get("error"):
        detail = callback.get("error_description") or callback["error"]
        _fail(f"xAI authorization failed: {detail}")
        return
    if callback.get("state") != sess["state"]:
        _fail("xAI authorization failed: state mismatch.")
        return
    code = str(callback.get("code") or "").strip()
    if not code:
        _fail("xAI authorization failed: missing authorization code.")
        return

    try:
        payload = hauth._xai_oauth_exchange_code_for_tokens(
            token_endpoint=sess["token_endpoint"],
            code=code,
            redirect_uri=sess["redirect_uri"],
            code_verifier=sess["verifier"],
            code_challenge=sess["challenge"],
        )
        access_token = str(payload.get("access_token", "") or "").strip()
        refresh_token = str(payload.get("refresh_token", "") or "").strip()
        if not access_token or not refresh_token:
            _fail("xAI token exchange did not return the expected tokens.")
            return
        base_url = hauth._xai_validate_inference_base_url(
            os.getenv("HERMES_XAI_BASE_URL", "").strip().rstrip("/")
            or os.getenv("XAI_BASE_URL", "").strip().rstrip("/"),
            fallback=hauth.DEFAULT_XAI_OAUTH_BASE_URL,
        )
        last_refresh = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        tokens = {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "id_token": str(payload.get("id_token", "") or "").strip(),
            "expires_in": payload.get("expires_in"),
            "token_type": str(payload.get("token_type") or "Bearer").strip() or "Bearer",
        }
        if _cancelled():
            return
        hauth._save_xai_oauth_tokens(
            tokens,
            discovery=sess.get("discovery"),
            redirect_uri=sess["redirect_uri"],
            last_refresh=last_refresh,
        )
        _add_xai_oauth_pool_entry(access_token, refresh_token, base_url, last_refresh)
    except Exception as exc:
        _fail(f"xAI token exchange failed: {exc}")
        return

    with _oauth_sessions_lock:
        s = _oauth_sessions.get(session_id)
        if s is not None:
            s["status"] = "approved"
    _log.info("oauth/loopback: xai-oauth login completed (session=%s)", session_id)


def _add_xai_oauth_pool_entry(
    access_token: str, refresh_token: str, base_url: str, last_refresh: str
) -> None:
    """Mirror `hermes auth add xai-oauth`'s credential-pool insert.

    Best-effort: the auth-store write in _save_xai_oauth_tokens is the source
    of truth for runtime resolution; the pool entry only matters for the
    rotation strategy.
    """
    try:
        import uuid

        from agent.credential_pool import (
            PooledCredential,
            load_pool,
            AUTH_TYPE_OAUTH,
            SOURCE_MANUAL,
        )
        pool = load_pool("xai-oauth")
        existing = [
            e for e in pool.entries()
            if getattr(e, "source", "").startswith(f"{SOURCE_MANUAL}:dashboard_xai_pkce")
        ]
        for e in existing:
            try:
                pool.remove_entry(getattr(e, "id", ""))
            except Exception:
                pass
        entry = PooledCredential(
            provider="xai-oauth",
            id=uuid.uuid4().hex[:6],
            label="dashboard PKCE",
            auth_type=AUTH_TYPE_OAUTH,
            priority=0,
            source=f"{SOURCE_MANUAL}:dashboard_xai_pkce",
            access_token=access_token,
            refresh_token=refresh_token,
            base_url=base_url,
            last_refresh=last_refresh,
        )
        pool.add_entry(entry)
    except Exception as e:
        _log.warning("xai-oauth pool add (dashboard) failed: %s", e)


def _nous_poller(session_id: str) -> None:
    """Background poller that drives a Nous device-code flow to completion."""
    from hermes_cli.auth import (
        _poll_for_token,
        refresh_nous_oauth_from_state,
    )
    from datetime import datetime, timezone
    import httpx
    with _oauth_sessions_lock:
        sess = _oauth_sessions.get(session_id)
    if not sess:
        return
    portal_base_url = sess["portal_base_url"]
    client_id = sess["client_id"]
    device_code = sess["device_code"]
    interval = sess["interval"]
    scope = sess.get("scope")
    expires_in = max(60, int(sess["expires_at"] - time.time()))
    try:
        with httpx.Client(timeout=httpx.Timeout(15.0), headers={"Accept": "application/json"}) as client:
            token_data = _poll_for_token(
                client=client,
                portal_base_url=portal_base_url,
                client_id=client_id,
                device_code=device_code,
                expires_in=expires_in,
                poll_interval=interval,
            )
        # Same post-processing as _nous_device_code_login (validate/refresh JWT)
        now = datetime.now(timezone.utc)
        token_ttl = int(token_data.get("expires_in") or 0)
        auth_state = {
            "portal_base_url": portal_base_url,
            "inference_base_url": token_data.get("inference_base_url"),
            "client_id": client_id,
            "scope": token_data.get("scope") or scope,
            "token_type": token_data.get("token_type", "Bearer"),
            "access_token": token_data["access_token"],
            "refresh_token": token_data.get("refresh_token"),
            "obtained_at": now.isoformat(),
            "expires_at": (
                datetime.fromtimestamp(now.timestamp() + token_ttl, tz=timezone.utc).isoformat()
                if token_ttl else None
            ),
            "expires_in": token_ttl,
        }
        full_state = refresh_nous_oauth_from_state(
            auth_state,
            timeout_seconds=15.0,
            force_refresh=False,
        )
        from hermes_cli.auth import persist_nous_credentials
        persist_nous_credentials(full_state)
        with _oauth_sessions_lock:
            sess["status"] = "approved"
        _log.info("oauth/device: nous login completed (session=%s)", session_id)
    except Exception as e:
        _log.warning("nous device-code poll failed (session=%s): %s", session_id, e)
        with _oauth_sessions_lock:
            sess["status"] = "error"
            sess["error_message"] = str(e)


def _minimax_poller(session_id: str) -> None:
    """Background poller that drives a MiniMax OAuth flow to completion.

    Mirrors `_nous_poller` but calls the MiniMax-specific token endpoint,
    which uses a PKCE-style ``code_verifier`` + ``user_code`` rather than
    the ``device_code`` field used by Nous. On success, builds the same
    auth_state dict that ``_minimax_oauth_login`` (the CLI flow) builds
    and persists via ``_minimax_save_auth_state`` — so the dashboard
    path leaves the system in the same state as
    ``hermes auth add minimax-oauth``.
    """
    from hermes_cli.auth import (
        _minimax_poll_token,
        _minimax_resolve_token_expiry_unix,
        _minimax_save_auth_state,
        MINIMAX_OAUTH_GLOBAL_INFERENCE,
        MINIMAX_OAUTH_SCOPE,
    )
    from datetime import datetime, timezone
    import httpx
    with _oauth_sessions_lock:
        sess = _oauth_sessions.get(session_id)
    if not sess:
        return
    portal_base_url = sess["portal_base_url"]
    client_id = sess["client_id"]
    user_code = sess["user_code"]
    code_verifier = sess["code_verifier"]
    interval_ms = sess.get("interval_ms")
    expired_in_raw = sess["expired_in_raw"]
    try:
        with httpx.Client(
            timeout=httpx.Timeout(15.0),
            headers={"Accept": "application/json"},
            follow_redirects=True,
        ) as client:
            token_data = _minimax_poll_token(
                client=client,
                portal_base_url=portal_base_url,
                client_id=client_id,
                user_code=user_code,
                code_verifier=code_verifier,
                expired_in=expired_in_raw,
                interval_ms=interval_ms,
            )
        # Build the auth_state dict in the same shape as the CLI flow's
        # `_minimax_oauth_login` so `_minimax_save_auth_state` writes
        # the canonical record. Region is fixed to "global" for the
        # dashboard path; cn-region operators can still use the CLI
        # flow which supports `--region cn`.
        now = datetime.now(timezone.utc)
        expires_at_ts = _minimax_resolve_token_expiry_unix(
            int(token_data["expired_in"]), now=now,
        )
        expires_in_s = max(0, int(expires_at_ts - now.timestamp()))
        auth_state = {
            "provider": "minimax-oauth",
            "region": sess.get("region", "global"),
            "portal_base_url": portal_base_url,
            "inference_base_url": MINIMAX_OAUTH_GLOBAL_INFERENCE,
            "client_id": client_id,
            "scope": MINIMAX_OAUTH_SCOPE,
            "token_type": token_data.get("token_type", "Bearer"),
            "access_token": token_data["access_token"],
            "refresh_token": token_data["refresh_token"],
            "resource_url": token_data.get("resource_url"),
            "obtained_at": now.isoformat(),
            "expires_at": datetime.fromtimestamp(
                expires_at_ts, tz=timezone.utc
            ).isoformat(),
            "expires_in": expires_in_s,
        }
        _minimax_save_auth_state(auth_state)
        with _oauth_sessions_lock:
            sess["status"] = "approved"
        _log.info("oauth/device: minimax login completed (session=%s)", session_id)
    except Exception as e:
        _log.warning("minimax device-code poll failed (session=%s): %s", session_id, e)
        with _oauth_sessions_lock:
            sess["status"] = "error"
            sess["error_message"] = str(e)


def _codex_full_login_worker(session_id: str) -> None:
    """Run the complete OpenAI Codex device-code flow.

    Codex doesn't use the standard OAuth device-code endpoints; it has its
    own ``/api/accounts/deviceauth/usercode`` (JSON body, returns
    ``device_auth_id``) and ``/api/accounts/deviceauth/token`` (JSON body
    polled until 200). On success the response carries an
    ``authorization_code`` + ``code_verifier`` that get exchanged at
    CODEX_OAUTH_TOKEN_URL with grant_type=authorization_code.

    The flow is replicated inline (rather than calling
    _codex_device_code_login) because that helper prints/blocks/polls in a
    single function — we need to surface the user_code to the dashboard the
    moment we receive it, well before polling completes.
    """
    try:
        import httpx
        from hermes_cli.auth import (
            CODEX_OAUTH_CLIENT_ID,
            CODEX_OAUTH_TOKEN_URL,
            DEFAULT_CODEX_BASE_URL,
        )
        issuer = "https://auth.openai.com"

        # Step 1: request device code
        with httpx.Client(timeout=httpx.Timeout(15.0)) as client:
            resp = client.post(
                f"{issuer}/api/accounts/deviceauth/usercode",
                json={"client_id": CODEX_OAUTH_CLIENT_ID},
                headers={"Content-Type": "application/json"},
            )
        if resp.status_code != 200:
            raise RuntimeError(f"deviceauth/usercode returned {resp.status_code}")
        device_data = resp.json()
        user_code = device_data.get("user_code", "")
        device_auth_id = device_data.get("device_auth_id", "")
        poll_interval = max(3, int(device_data.get("interval", "5")))
        if not user_code or not device_auth_id:
            raise RuntimeError("device-code response missing user_code or device_auth_id")
        verification_url = f"{issuer}/codex/device"
        with _oauth_sessions_lock:
            sess = _oauth_sessions.get(session_id)
            if not sess:
                return
            sess["user_code"] = user_code
            sess["verification_url"] = verification_url
            sess["device_auth_id"] = device_auth_id
            sess["interval"] = poll_interval
            sess["expires_in"] = 15 * 60  # OpenAI's effective limit
            sess["expires_at"] = time.time() + sess["expires_in"]

        # Step 2: poll until authorized
        deadline = time.monotonic() + sess["expires_in"]
        code_resp = None
        with httpx.Client(timeout=httpx.Timeout(15.0)) as client:
            while time.monotonic() < deadline:
                time.sleep(poll_interval)
                poll = client.post(
                    f"{issuer}/api/accounts/deviceauth/token",
                    json={"device_auth_id": device_auth_id, "user_code": user_code},
                    headers={"Content-Type": "application/json"},
                )
                if poll.status_code == 200:
                    code_resp = poll.json()
                    break
                if poll.status_code in {403, 404}:
                    continue  # user hasn't authorized yet
                raise RuntimeError(f"deviceauth/token poll returned {poll.status_code}")

        if code_resp is None:
            with _oauth_sessions_lock:
                sess["status"] = "expired"
                sess["error_message"] = "Device code expired before approval"
            return

        # Step 3: exchange authorization_code for tokens
        authorization_code = code_resp.get("authorization_code", "")
        code_verifier = code_resp.get("code_verifier", "")
        if not authorization_code or not code_verifier:
            raise RuntimeError("device-auth response missing authorization_code/code_verifier")
        with httpx.Client(timeout=httpx.Timeout(15.0)) as client:
            token_resp = client.post(
                CODEX_OAUTH_TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": authorization_code,
                    "redirect_uri": f"{issuer}/deviceauth/callback",
                    "client_id": CODEX_OAUTH_CLIENT_ID,
                    "code_verifier": code_verifier,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        if token_resp.status_code != 200:
            raise RuntimeError(f"token exchange returned {token_resp.status_code}")
        tokens = token_resp.json()
        access_token = tokens.get("access_token", "")
        refresh_token = tokens.get("refresh_token", "")
        if not access_token:
            raise RuntimeError("token exchange did not return access_token")

        from hermes_cli.auth import _save_codex_tokens

        _save_codex_tokens({
            "access_token": access_token,
            "refresh_token": refresh_token,
        })
        with _oauth_sessions_lock:
            sess["status"] = "approved"
        _log.info("oauth/device: openai-codex login completed (session=%s)", session_id)
    except Exception as e:
        _log.warning("codex device-code worker failed (session=%s): %s", session_id, e)
        with _oauth_sessions_lock:
            s = _oauth_sessions.get(session_id)
            if s:
                s["status"] = "error"
                s["error_message"] = str(e)


@app.post("/api/providers/oauth/{provider_id}/start")
async def start_oauth_login(provider_id: str, request: Request):
    """Initiate an OAuth login flow. Token-protected."""
    _require_token(request)
    _gc_oauth_sessions()
    valid = {p["id"] for p in _OAUTH_PROVIDER_CATALOG}
    if provider_id not in valid:
        raise HTTPException(status_code=400, detail=f"Unknown provider {provider_id}")
    catalog_entry = next(p for p in _OAUTH_PROVIDER_CATALOG if p["id"] == provider_id)
    if catalog_entry["flow"] == "external":
        raise HTTPException(
            status_code=400,
            detail=f"{provider_id} uses an external CLI; run `{catalog_entry['cli_command']}` manually",
        )
    try:
        # The pkce branch is gated on provider_id == "anthropic" because
        # `_start_anthropic_pkce()` is hardcoded to the Anthropic flow.
        # Routing any other future pkce-flagged provider through it would
        # silently launch the Anthropic OAuth flow (the bug fixed in this
        # change for MiniMax). New PKCE providers must add their own
        # start function and an explicit branch here.
        if catalog_entry["flow"] == "pkce" and provider_id == "anthropic":
            return _start_anthropic_pkce()
        if catalog_entry["flow"] == "device_code":
            return await _start_device_code_flow(provider_id)
        if catalog_entry["flow"] == "loopback" and provider_id == "xai-oauth":
            return await asyncio.get_running_loop().run_in_executor(
                None, _start_xai_loopback_flow
            )
    except HTTPException:
        raise
    except Exception as e:
        _log.exception("oauth/start %s failed", provider_id)
        raise HTTPException(status_code=500, detail=str(e))
    raise HTTPException(status_code=400, detail="Unsupported flow")


class OAuthSubmitBody(BaseModel):
    session_id: str
    code: str


@app.post("/api/providers/oauth/{provider_id}/submit")
async def submit_oauth_code(provider_id: str, body: OAuthSubmitBody, request: Request):
    """Submit the auth code for PKCE flows. Token-protected."""
    _require_token(request)
    if provider_id == "anthropic":
        return await asyncio.get_running_loop().run_in_executor(
            None, _submit_anthropic_pkce, body.session_id, body.code,
        )
    raise HTTPException(status_code=400, detail=f"submit not supported for {provider_id}")


@app.get("/api/providers/oauth/{provider_id}/poll/{session_id}")
async def poll_oauth_session(provider_id: str, session_id: str):
    """Poll a session's status (no auth — read-only state).

    Shared by the device-code flows (Nous, OpenAI Codex, MiniMax) and the
    loopback flow (xAI Grok). Both surface progress through the same
    background-worker-updated ``status`` field, so a single poll endpoint
    serves them all.
    """
    with _oauth_sessions_lock:
        sess = _oauth_sessions.get(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found or expired")
    if sess["provider"] != provider_id:
        raise HTTPException(status_code=400, detail="Provider mismatch for session")
    return {
        "session_id": session_id,
        "status": sess["status"],
        "error_message": sess.get("error_message"),
        "expires_at": sess.get("expires_at"),
    }


@app.delete("/api/providers/oauth/sessions/{session_id}")
async def cancel_oauth_session(session_id: str, request: Request):
    """Cancel a pending OAuth session. Token-protected."""
    _require_token(request)
    with _oauth_sessions_lock:
        sess = _oauth_sessions.pop(session_id, None)
    if sess is None:
        return {"ok": False, "message": "session not found"}
    # Loopback sessions own a bound 127.0.0.1 callback server. Without an
    # explicit shutdown the worker would keep that port held until
    # _xai_wait_for_callback times out (up to 5 min). Free it immediately so
    # an orphaned listener can't block a subsequent sign-in attempt.
    if sess.get("flow") == "loopback":
        # The worker is blocked in _xai_wait_for_callback, which polls
        # callback_result rather than the server state. Flag the result as
        # cancelled so that loop returns on its next tick instead of spinning
        # until the timeout — otherwise repeated cancel/retry piles up daemon
        # threads. (_cancelled() in the worker then short-circuits before any
        # persist.)
        result = sess.get("callback_result")
        if isinstance(result, dict):
            result["error"] = result.get("error") or "cancelled"
        server = sess.get("server")
        thread = sess.get("thread")
        try:
            if server is not None:
                server.shutdown()
                server.server_close()
        except Exception:
            pass
        try:
            if thread is not None:
                thread.join(timeout=1.0)
        except Exception:
            pass
    return {"ok": True, "session_id": session_id}


# ---------------------------------------------------------------------------
# Session detail endpoints
# ---------------------------------------------------------------------------



def _session_latest_descendant(session_id: str):
    """Resolve a session id to the newest child leaf session.

    /model may create child sessions. Dashboard refresh should continue the
    newest child instead of reopening the old parent.
    """
    from hermes_state import SessionDB

    def row_get(row, key, index):
        if isinstance(row, dict):
            return row.get(key)
        try:
            return row[key]
        except Exception:
            try:
                return row[index]
            except Exception:
                return None

    db = SessionDB()
    try:
        sid = db.resolve_session_id(session_id)
        if not sid or not db.get_session(sid):
            return None, []

        conn = (
            getattr(db, "conn", None)
            or getattr(db, "_conn", None)
            or getattr(db, "connection", None)
            or getattr(db, "_connection", None)
        )

        rows = []
        if conn is not None:
            raw_rows = conn.execute(
                "SELECT id, parent_session_id, started_at FROM sessions"
            ).fetchall()
            for row in raw_rows:
                rows.append({
                    "id": row_get(row, "id", 0),
                    "parent_session_id": row_get(row, "parent_session_id", 1),
                    "started_at": row_get(row, "started_at", 2),
                })
        else:
            rows = db.list_sessions_rich(limit=10000, offset=0)

        children = {}
        for row in rows:
            rid = row.get("id")
            parent = row.get("parent_session_id")
            if rid and parent:
                children.setdefault(parent, []).append(row)

        def started(row):
            try:
                return float(row.get("started_at") or 0)
            except Exception:
                return 0.0

        current = sid
        path = [sid]
        seen = {sid}

        while children.get(current):
            candidates = [r for r in children[current] if r.get("id") not in seen]
            if not candidates:
                break
            candidates.sort(key=started, reverse=True)
            current = candidates[0]["id"]
            path.append(current)
            seen.add(current)

        return current, path
    finally:
        db.close()


# CRITICAL — every literal-path route below MUST be declared BEFORE the
# templated ``/api/sessions/{session_id}`` family that follows. FastAPI/
# Starlette match routes in registration order, and the ``{session_id}``
# pattern is unconstrained — it would otherwise swallow e.g.
# ``DELETE /api/sessions/empty``, ``POST /api/sessions/bulk-delete``, or
# ``GET /api/sessions/stats`` as "operate on the session with id
# 'empty'" / "'bulk-delete'" / "'stats'", which would 404 (or worse,
# succeed and delete the wrong row). Same story as the older
# ``/api/sessions/search`` endpoint up at line ~1191. If you split or
# reorder this block, move every route in it together.
class BulkDeleteSessions(BaseModel):
    ids: List[str]


@app.post("/api/sessions/bulk-delete")
async def bulk_delete_sessions_endpoint(body: BulkDeleteSessions):
    """Delete every session in ``body.ids`` in a single DB transaction.

    Backs the dashboard's bulk-select-and-delete flow on the sessions
    page. POST (not DELETE) because most HTTP clients refuse to send a
    request body on DELETE and a body is the natural shape for a list
    of IDs — Starlette accepts both, but POSTing a list keeps proxies,
    curl, and the browser ``fetch`` API consistent.

    Per-row contract matches :meth:`SessionDB.delete_sessions`:

    * Unknown IDs are silently skipped (the response ``deleted`` count
      reflects what really happened, not the input length). This is
      deliberate — UI selection state can race against another tab's
      delete, and we'd rather succeed-on-the-rest than fail-the-whole-
      batch.
    * Children of every deleted parent are orphaned, not cascade-
      deleted.
    * Active and archived sessions ARE deleted when explicitly
      selected — unlike ``DELETE /api/sessions/empty``, the user
      hand-picked the rows so we trust the selection.
    * Like the other session-delete endpoints, this does NOT pass a
      ``sessions_dir`` through; on-disk transcript / request-dump
      cleanup runs at the CLI/agent layer on the next prune pass.

    The response carries the actual deleted count, so the dashboard
    can surface it in a toast. The IDs that were removed are not
    echoed back because the client already knows what it asked to
    delete (unknown IDs are silently skipped — see contract above)
    and can prune its in-memory list directly from the request.
    """
    # Enforce a hard cap so a runaway/typo'd selection can't lock the
    # DB writer for an extended window. The dashboard pages 20 rows
    # at a time; 500 covers a "select all on every page in a
    # reasonable scrollback" worst case without opening the door to
    # multi-thousand-row transactions.
    if len(body.ids) > 500:
        raise HTTPException(
            status_code=400,
            detail="ids must contain at most 500 entries",
        )
    from hermes_state import SessionDB
    db = SessionDB()
    try:
        deleted = db.delete_sessions(body.ids)
        return {"ok": True, "deleted": deleted}
    finally:
        db.close()


@app.get("/api/sessions/empty/count")
async def count_empty_sessions_endpoint():
    """Return the number of empty, ended, non-archived sessions.

    Drives the dashboard's "Delete empty (N)" button — when N is 0 the
    UI hides the affordance so users aren't presented with a button
    that does nothing. Cheap, single-COUNT query.
    """
    from hermes_state import SessionDB
    db = SessionDB()
    try:
        return {"count": db.count_empty_sessions()}
    finally:
        db.close()


@app.delete("/api/sessions/empty")
async def delete_empty_sessions_endpoint():
    """Delete every empty (``message_count == 0``), ended,
    non-archived session in a single transaction.

    Safety contract mirrors :meth:`SessionDB.delete_empty_sessions`:

    * Active sessions are skipped (``ended_at IS NULL``) so a live
      agent isn't yanked mid-handshake.
    * Archived sessions are skipped — the user explicitly chose to
      keep those rows.
    * Children of deleted parents are orphaned, not cascade-deleted.

    Like the single-session ``DELETE /api/sessions/{id}`` endpoint
    below, this doesn't pass a ``sessions_dir`` through — the on-disk
    transcript / request-dump cleanup is wired at the CLI/agent layer
    but the web server historically leaves file cleanup to the next
    prune-on-startup pass. Matching that pre-existing trade-off keeps
    the two delete endpoints' DB-vs-disk behaviour consistent.
    """
    from hermes_state import SessionDB
    db = SessionDB()
    try:
        deleted = db.delete_empty_sessions()
        return {"ok": True, "deleted": deleted}
    finally:
        db.close()


@app.get("/api/sessions/stats")
async def get_session_stats():
    """Session-store statistics for the Sessions page (mirrors `hermes sessions stats`).

    Registered before ``/api/sessions/{session_id}`` so the literal ``stats``
    path isn't captured as a session id by the parameterized route.
    """
    from hermes_state import SessionDB

    db = SessionDB()
    try:
        total = db.session_count(include_archived=True)
        active_store = db.session_count(include_archived=False)
        archived = db.session_count(archived_only=True)
        messages = db.message_count()
        by_source: Dict[str, int] = {}
        try:
            for s in db.list_sessions_rich(limit=10000, include_archived=True):
                src = str(s.get("source") or "cli")
                by_source[src] = by_source.get(src, 0) + 1
        except Exception:
            pass
        return {
            "total": total,
            "active_store": active_store,
            "archived": archived,
            "messages": messages,
            "by_source": by_source,
        }
    finally:
        db.close()


def _open_session_db_for_profile(profile: Optional[str]):
    """Open a SessionDB for read paths, optionally for another profile.

    ``profile`` None/empty → this process's own ``state.db`` (the common,
    single-profile case). A named profile opens that profile's on-disk
    ``state.db`` directly so the primary backend can serve cross-profile reads
    (transcripts, detail) without spawning that profile's backend.
    """
    from hermes_state import SessionDB
    if not profile:
        return SessionDB()
    _name, home = _cron_profile_home(profile)
    return SessionDB(db_path=Path(home) / "state.db")


@app.get("/api/sessions/{session_id}")
async def get_session_detail(session_id: str, profile: Optional[str] = None):
    db = _open_session_db_for_profile(profile)
    try:
        sid = db.resolve_session_id(session_id)
        session = db.get_session(sid) if sid else None
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        if profile:
            session["profile"] = _cron_profile_home(profile)[0]
        return session
    finally:
        db.close()



@app.get("/api/sessions/{session_id}/latest-descendant")
async def get_session_latest_descendant(session_id: str):
    latest, path = _session_latest_descendant(session_id)
    if not latest:
        raise HTTPException(status_code=404, detail="Session not found")
    return {
        "requested_session_id": path[0] if path else session_id,
        "session_id": latest,
        "path": path,
        "changed": bool(path and latest != path[0]),
    }

@app.get("/api/sessions/{session_id}/messages")
async def get_session_messages(session_id: str, profile: Optional[str] = None):
    db = _open_session_db_for_profile(profile)
    try:
        sid = db.resolve_session_id(session_id)
        if not sid:
            raise HTTPException(status_code=404, detail="Session not found")
        messages = db.get_messages(sid)
        return {"session_id": sid, "messages": messages}
    finally:
        db.close()


@app.delete("/api/sessions/{session_id}")
async def delete_session_endpoint(session_id: str):
    from hermes_state import SessionDB
    db = SessionDB()
    try:
        if not db.delete_session(session_id):
            raise HTTPException(status_code=404, detail="Session not found")
        return {"ok": True}
    finally:
        db.close()


class SessionRename(BaseModel):
    title: Optional[str] = None
    archived: Optional[bool] = None
    # Mutate a session belonging to another profile (opens its state.db). Omit
    # for the current/default profile.
    profile: Optional[str] = None


@app.patch("/api/sessions/{session_id}")
async def rename_session_endpoint(session_id: str, body: SessionRename):
    """Update a session: rename (or clear its title) and/or archive it.

    ``title`` renames (empty/null clears the title); ``archived`` soft-hides or
    restores the session. Either field may be omitted. ``profile`` targets
    another profile's session.
    """
    db = _open_session_db_for_profile(body.profile)
    try:
        sid = db.resolve_session_id(session_id)
        if not sid:
            raise HTTPException(status_code=404, detail="Session not found")
        if body.title is None and body.archived is None:
            raise HTTPException(
                status_code=400,
                detail="Nothing to update; provide 'title' and/or 'archived'.",
            )
        if body.title is not None:
            try:
                db.set_session_title(sid, body.title or "")
            except ValueError as e:
                # Title too long, invalid characters, or already in use.
                raise HTTPException(status_code=400, detail=str(e))
        if body.archived is not None:
            db.set_session_archived(sid, body.archived)
        result = {"ok": True, "title": db.get_session_title(sid) or ""}
        if body.archived is not None:
            result["archived"] = bool(body.archived)
        return result
    finally:
        db.close()


@app.get("/api/sessions/{session_id}/export")
async def export_session_endpoint(session_id: str):
    """Export a single session (metadata + messages) as JSON."""
    from hermes_state import SessionDB

    db = SessionDB()
    try:
        sid = db.resolve_session_id(session_id)
        if not sid:
            raise HTTPException(status_code=404, detail="Session not found")
        data = db.export_session(sid)
        if data is None:
            raise HTTPException(status_code=404, detail="Session not found")
        return data
    finally:
        db.close()


class SessionPrune(BaseModel):
    older_than_days: int = 90
    source: Optional[str] = None


@app.post("/api/sessions/prune")
async def prune_sessions_endpoint(body: SessionPrune):
    """Delete ended sessions older than N days (mirrors `hermes sessions prune`)."""
    if body.older_than_days < 1:
        raise HTTPException(status_code=400, detail="older_than_days must be >= 1")
    from hermes_state import SessionDB

    db = SessionDB()
    try:
        sessions_dir = get_hermes_home() / "sessions"
        removed = db.prune_sessions(
            older_than_days=body.older_than_days,
            source=(body.source or None),
            sessions_dir=sessions_dir if sessions_dir.exists() else None,
        )
        return {"ok": True, "removed": removed}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Log viewer endpoint
# ---------------------------------------------------------------------------


@app.get("/api/logs")
async def get_logs(
    file: str = "agent",
    lines: int = 100,
    level: Optional[str] = None,
    component: Optional[str] = None,
    search: Optional[str] = None,
):
    from hermes_cli.logs import _read_tail, LOG_FILES

    log_name = LOG_FILES.get(file)
    if not log_name:
        raise HTTPException(status_code=400, detail=f"Unknown log file: {file}")
    log_path = get_hermes_home() / "logs" / log_name
    if not log_path.exists():
        return {"file": file, "lines": []}

    try:
        from hermes_logging import COMPONENT_PREFIXES
    except ImportError:
        COMPONENT_PREFIXES = {}

    # Normalize "ALL" / "all" / empty → no filter. _matches_filters treats an
    # empty tuple as "must match a prefix" (startswith(()) is always False),
    # so passing () instead of None silently drops every line.
    min_level = level if level and level.upper() != "ALL" else None
    if component and component.lower() != "all":
        comp_prefixes = COMPONENT_PREFIXES.get(component)
        if comp_prefixes is None:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown component: {component}. "
                       f"Available: {', '.join(sorted(COMPONENT_PREFIXES))}",
            )
    else:
        comp_prefixes = None

    has_filters = bool(min_level or comp_prefixes or search)
    result = _read_tail(
        log_path, min(lines, 500) if not search else 2000,
        has_filters=has_filters,
        min_level=min_level,
        component_prefixes=comp_prefixes,
    )
    # Post-filter by search term (case-insensitive substring match).
    # _read_tail doesn't support free-text search, so we filter here and
    # trim to the requested line count afterward.
    if search:
        needle = search.lower()
        result = [l for l in result if needle in l.lower()][-min(lines, 500):]
    return {"file": file, "lines": result}


# ---------------------------------------------------------------------------
# Cron job management endpoints
# ---------------------------------------------------------------------------


class CronJobCreate(BaseModel):
    prompt: str
    schedule: str
    name: str = ""
    deliver: str = "local"


class CronJobUpdate(BaseModel):
    updates: dict


_CRON_PROFILE_LOCK = threading.RLock()


def _cron_profile_dicts() -> List[Dict[str, Any]]:
    """Return dashboard profile records, falling back to a directory scan."""
    from hermes_cli import profiles as profiles_mod
    try:
        return [_profile_to_dict(p) for p in profiles_mod.list_profiles()]
    except Exception:
        _log.exception("Failed to list profiles for cron dashboard; falling back to directory scan")
        return _fallback_profile_dicts(profiles_mod)


def _cron_profile_home(profile: Optional[str]) -> Tuple[str, Path]:
    """Resolve a profile query value to (profile_name, HERMES_HOME)."""
    from hermes_cli import profiles as profiles_mod

    raw = (profile or "default").strip() or "default"
    try:
        canon = profiles_mod.normalize_profile_name(raw)
        profiles_mod.validate_profile_name(canon)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not profiles_mod.profile_exists(canon):
        raise HTTPException(status_code=404, detail=f"Profile '{canon}' does not exist.")
    return canon, profiles_mod.get_profile_dir(canon)


def _annotate_cron_job(job: Dict[str, Any], profile: str, home: Path) -> Dict[str, Any]:
    annotated = dict(job)
    annotated["profile"] = profile
    annotated["profile_name"] = profile
    annotated["hermes_home"] = str(home)
    annotated["is_default_profile"] = profile == "default"
    return annotated


def _call_cron_for_profile(profile: Optional[str], func_name: str, *args, **kwargs):
    """Run cron.jobs helpers against the selected profile's cron directory.

    cron.jobs keeps CRON_DIR/JOBS_FILE/OUTPUT_DIR as module globals resolved
    from the process HERMES_HOME at import time. The dashboard is a single
    process that can inspect many profiles, so temporarily retarget those
    globals while holding a lock and restore them immediately after the call.
    """
    profile_name, home = _cron_profile_home(profile)
    with _CRON_PROFILE_LOCK:
        from cron import jobs as cron_jobs

        old_cron_dir = cron_jobs.CRON_DIR
        old_jobs_file = cron_jobs.JOBS_FILE
        old_output_dir = cron_jobs.OUTPUT_DIR
        cron_jobs.CRON_DIR = home / "cron"
        cron_jobs.JOBS_FILE = cron_jobs.CRON_DIR / "jobs.json"
        cron_jobs.OUTPUT_DIR = cron_jobs.CRON_DIR / "output"
        try:
            result = getattr(cron_jobs, func_name)(*args, **kwargs)
        finally:
            cron_jobs.CRON_DIR = old_cron_dir
            cron_jobs.JOBS_FILE = old_jobs_file
            cron_jobs.OUTPUT_DIR = old_output_dir

    if isinstance(result, list):
        return [_annotate_cron_job(j, profile_name, home) for j in result]
    if isinstance(result, dict):
        return _annotate_cron_job(result, profile_name, home)
    return result


def _find_cron_job_profile(job_id: str) -> Optional[str]:
    for profile in _cron_profile_dicts():
        name = str(profile.get("name") or "")
        if not name:
            continue
        jobs = _call_cron_for_profile(name, "list_jobs", True)
        if any(j.get("id") == job_id or j.get("name") == job_id for j in jobs):
            return name
    return None


@app.get("/api/cron/jobs")
async def list_cron_jobs(profile: str = "all"):
    requested = (profile or "all").strip()
    if requested.lower() != "all":
        return _call_cron_for_profile(requested, "list_jobs", True)

    jobs: List[Dict[str, Any]] = []
    for item in _cron_profile_dicts():
        name = str(item.get("name") or "")
        if not name:
            continue
        try:
            jobs.extend(_call_cron_for_profile(name, "list_jobs", True))
        except Exception:
            _log.exception("Failed to list cron jobs for profile %s", name)
    return jobs


@app.get("/api/cron/jobs/{job_id}")
async def get_cron_job(job_id: str, profile: Optional[str] = None):
    selected = profile or _find_cron_job_profile(job_id)
    if not selected:
        raise HTTPException(status_code=404, detail="Job not found")
    job = _call_cron_for_profile(selected, "get_job", job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.post("/api/cron/jobs")
async def create_cron_job(body: CronJobCreate, profile: str = "default"):
    try:
        return _call_cron_for_profile(
            profile,
            "create_job",
            prompt=body.prompt,
            schedule=body.schedule,
            name=body.name,
            deliver=body.deliver,
        )
    except Exception as e:
        _log.exception("POST /api/cron/jobs failed")
        raise HTTPException(status_code=400, detail=str(e))


@app.put("/api/cron/jobs/{job_id}")
async def update_cron_job(job_id: str, body: CronJobUpdate, profile: Optional[str] = None):
    selected = profile or _find_cron_job_profile(job_id)
    if not selected:
        raise HTTPException(status_code=404, detail="Job not found")
    try:
        job = _call_cron_for_profile(selected, "update_job", job_id, body.updates)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.post("/api/cron/jobs/{job_id}/pause")
async def pause_cron_job(job_id: str, profile: Optional[str] = None):
    selected = profile or _find_cron_job_profile(job_id)
    if not selected:
        raise HTTPException(status_code=404, detail="Job not found")
    job = _call_cron_for_profile(selected, "pause_job", job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.post("/api/cron/jobs/{job_id}/resume")
async def resume_cron_job(job_id: str, profile: Optional[str] = None):
    selected = profile or _find_cron_job_profile(job_id)
    if not selected:
        raise HTTPException(status_code=404, detail="Job not found")
    job = _call_cron_for_profile(selected, "resume_job", job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.post("/api/cron/jobs/{job_id}/trigger")
async def trigger_cron_job(job_id: str, profile: Optional[str] = None):
    selected = profile or _find_cron_job_profile(job_id)
    if not selected:
        raise HTTPException(status_code=404, detail="Job not found")
    job = _call_cron_for_profile(selected, "trigger_job", job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.delete("/api/cron/jobs/{job_id}")
async def delete_cron_job(job_id: str, profile: Optional[str] = None):
    selected = profile or _find_cron_job_profile(job_id)
    if not selected:
        raise HTTPException(status_code=404, detail="Job not found")
    try:
        removed = _call_cron_for_profile(selected, "remove_job", job_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not removed:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"ok": True}


# ---------------------------------------------------------------------------
# MCP server endpoints — list / add / remove / test.
#
# Wraps the same config data layer the CLI uses (hermes_cli.mcp_config), so
# servers managed here show up under `hermes mcp list` and vice versa.  Secrets
# in stdio `env` blocks are redacted on read; the agent picks them up from
# config.yaml at session start exactly as with CLI-added servers.
# ---------------------------------------------------------------------------


class MCPServerCreate(BaseModel):
    name: str
    url: Optional[str] = None
    command: Optional[str] = None
    args: List[str] = []
    # env: KEY=VALUE map for stdio servers (API keys, etc.)
    env: Dict[str, str] = {}
    # auth: "oauth" | "header" | None
    auth: Optional[str] = None


def _redact_mcp_env(env: Dict[str, Any]) -> Dict[str, str]:
    """Mask secret-shaped MCP env values for read responses."""
    out: Dict[str, str] = {}
    for k, v in (env or {}).items():
        try:
            out[str(k)] = redact_key(str(v)) if v else ""
        except Exception:
            out[str(k)] = "***"
    return out


def _mcp_server_summary(name: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    transport = "http" if cfg.get("url") else ("stdio" if cfg.get("command") else "unknown")
    return {
        "name": name,
        "transport": transport,
        "url": cfg.get("url"),
        "command": cfg.get("command"),
        "args": list(cfg.get("args") or []),
        "env": _redact_mcp_env(cfg.get("env") or {}),
        "auth": cfg.get("auth"),
        "enabled": cfg.get("enabled", True) is not False,
        # Tool selection: list of enabled tool names, or None = all.
        "tools": cfg.get("tools"),
    }


@app.get("/api/mcp/servers")
async def list_mcp_servers():
    from hermes_cli.mcp_config import _get_mcp_servers

    servers = _get_mcp_servers()
    return {
        "servers": [
            _mcp_server_summary(name, cfg) for name, cfg in sorted(servers.items())
        ]
    }


@app.post("/api/mcp/servers")
async def add_mcp_server(body: MCPServerCreate):
    from hermes_cli.mcp_config import _get_mcp_servers, _save_mcp_server

    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Server name is required")
    if name in _get_mcp_servers():
        raise HTTPException(status_code=409, detail=f"Server '{name}' already exists")
    if not body.url and not body.command:
        raise HTTPException(
            status_code=400,
            detail="Provide either a URL (HTTP/SSE server) or a command (stdio server)",
        )

    server_config: Dict[str, Any] = {}
    if body.url:
        server_config["url"] = body.url.strip()
    if body.command:
        server_config["command"] = body.command.strip()
        if body.args:
            server_config["args"] = list(body.args)
    if body.env:
        server_config["env"] = dict(body.env)
    if body.auth:
        server_config["auth"] = body.auth

    try:
        _save_mcp_server(name, server_config)
    except Exception as exc:
        _log.exception("POST /api/mcp/servers failed")
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return _mcp_server_summary(name, server_config)


@app.delete("/api/mcp/servers/{name}")
async def remove_mcp_server(name: str):
    from hermes_cli.mcp_config import _remove_mcp_server

    if not _remove_mcp_server(name):
        raise HTTPException(status_code=404, detail=f"Server '{name}' not found")
    return {"ok": True}


@app.post("/api/mcp/servers/{name}/test")
async def test_mcp_server(name: str):
    """Connect to the server, list its tools, disconnect.  Returns tool list."""
    from hermes_cli.mcp_config import _get_mcp_servers, _probe_single_server

    servers = _get_mcp_servers()
    if name not in servers:
        raise HTTPException(status_code=404, detail=f"Server '{name}' not found")

    try:
        # Probe blocks on a dedicated MCP event loop — run in a thread so the
        # FastAPI event loop is never blocked.
        tools = await asyncio.to_thread(_probe_single_server, name, servers[name])
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "tools": [],
        }
    return {
        "ok": True,
        "tools": [{"name": t, "description": d} for t, d in tools],
    }


class MCPEnabledToggle(BaseModel):
    enabled: bool


@app.put("/api/mcp/servers/{name}/enabled")
async def set_mcp_server_enabled(name: str, body: MCPEnabledToggle):
    """Enable or disable an MCP server (takes effect on next session/gateway).

    Toggles the ``enabled`` key on the server's config.yaml entry — the same
    flag the agent reads at startup.  Disabled servers stay in config so they
    can be re-enabled without re-entering their settings.
    """
    cfg = load_config()
    servers = cfg.get("mcp_servers")
    if not isinstance(servers, dict) or name not in servers:
        raise HTTPException(status_code=404, detail=f"Server '{name}' not found")
    if not isinstance(servers[name], dict):
        raise HTTPException(status_code=400, detail="Malformed server config")
    servers[name]["enabled"] = bool(body.enabled)
    save_config(cfg)
    return {"ok": True, "name": name, "enabled": bool(body.enabled)}


@app.get("/api/mcp/catalog")
async def list_mcp_catalog():
    """Browse the Nous-approved MCP catalog (the optional-mcps/ manifests).

    Each entry reports whether it's already installed and enabled so the UI
    can show install / enabled state inline.  This is the same catalog
    `hermes mcp catalog` / `hermes mcp install` read.
    """
    try:
        from hermes_cli import mcp_catalog
    except Exception as exc:
        _log.exception("mcp_catalog import failed")
        raise HTTPException(status_code=500, detail=f"Catalog unavailable: {exc}")

    entries = []
    try:
        for entry in mcp_catalog.list_catalog():
            auth = entry.auth
            entries.append({
                "name": entry.name,
                "description": entry.description,
                "source": entry.source,
                "transport": entry.transport.type,
                "auth_type": getattr(auth, "type", "none"),
                # Env vars the user must supply (names + prompts only, never values).
                "required_env": [
                    {"name": e.name, "prompt": e.prompt, "required": e.required}
                    for e in getattr(auth, "env", []) or []
                ],
                "needs_install": entry.install is not None,
                "installed": mcp_catalog.is_installed(entry.name),
                "enabled": mcp_catalog.is_enabled(entry.name),
            })
    except Exception:
        _log.exception("list_mcp_catalog failed")

    diagnostics = []
    try:
        diagnostics = [
            {"name": n, "kind": k, "message": m}
            for (n, k, m) in mcp_catalog.catalog_diagnostics()
        ]
    except Exception:
        pass

    return {"entries": entries, "diagnostics": diagnostics}


class MCPCatalogInstall(BaseModel):
    name: str
    # env: KEY=VALUE map for catalog entries that declare required env vars.
    env: Dict[str, str] = {}
    enable: bool = True


@app.post("/api/mcp/catalog/install")
async def install_mcp_catalog_entry(body: MCPCatalogInstall):
    """Install a catalog MCP into config.yaml.

    For HTTP/stdio entries with required env vars, those are written to .env
    via the standard env path so the agent can read them at session start.
    Entries that need a git bootstrap (``needs_install``) are installed via
    the CLI action path because the clone can take time.
    """
    from hermes_cli import mcp_catalog

    name = (body.name or "").strip()
    entry = mcp_catalog.get_entry(name)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"No catalog entry '{name}'")

    # Persist any supplied env vars first (catalog entries declare which names
    # they need; we only write the ones the user provided).
    if body.env:
        for k, v in body.env.items():
            if v:
                save_env_value(k, v)

    # Git-bootstrap entries can take a while to clone — run via the background
    # action path so the request returns immediately and the UI can tail logs.
    if entry.install is not None:
        try:
            proc = _spawn_hermes_action(["mcp", "install", name], "mcp-install")
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Install failed: {exc}")
        return {"ok": True, "name": name, "background": True, "action": "mcp-install"}

    # No git step — install synchronously via the catalog API.
    try:
        await asyncio.to_thread(mcp_catalog.install_entry, entry, enable=body.enable)
    except Exception as exc:
        _log.exception("install_mcp_catalog_entry failed")
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "name": name, "background": False}


# Register the mcp-install action log so /api/actions/mcp-install/status works.
_ACTION_LOG_FILES.setdefault("mcp-install", "action-mcp-install.log")


# ---------------------------------------------------------------------------
# Pairing endpoints — approve / revoke / list messaging pairing codes.
#
# These are how a remote admin onboards messaging users (Telegram, Discord, …)
# without shell access.  Wraps gateway.pairing.PairingStore directly.
# ---------------------------------------------------------------------------


class PairingApprove(BaseModel):
    platform: str
    code: str


class PairingRevoke(BaseModel):
    platform: str
    user_id: str


def _pairing_store():
    from gateway.pairing import PairingStore

    return PairingStore()


@app.get("/api/pairing")
async def list_pairing():
    store = _pairing_store()
    return {
        "pending": store.list_pending(),
        "approved": store.list_approved(),
    }


@app.post("/api/pairing/approve")
async def approve_pairing(body: PairingApprove):
    store = _pairing_store()
    platform = (body.platform or "").lower().strip()
    code = (body.code or "").upper().strip()
    if not platform or not code:
        raise HTTPException(status_code=400, detail="platform and code are required")

    result = store.approve_code(platform, code)
    if result:
        return {"ok": True, "user": result}
    if store._is_locked_out(platform):
        raise HTTPException(
            status_code=429,
            detail=f"Platform '{platform}' is locked out after too many failed approvals.",
        )
    raise HTTPException(
        status_code=404,
        detail=f"Code '{code}' not found or expired for platform '{platform}'.",
    )


@app.post("/api/pairing/revoke")
async def revoke_pairing(body: PairingRevoke):
    store = _pairing_store()
    platform = (body.platform or "").lower().strip()
    if not platform or not body.user_id:
        raise HTTPException(status_code=400, detail="platform and user_id are required")
    if store.revoke(platform, body.user_id):
        return {"ok": True}
    raise HTTPException(
        status_code=404,
        detail=f"User {body.user_id} not found in approved list for {platform}.",
    )


@app.post("/api/pairing/clear-pending")
async def clear_pending_pairing():
    store = _pairing_store()
    count = store.clear_pending()
    return {"ok": True, "cleared": count}


# ---------------------------------------------------------------------------
# Webhook subscription endpoints — list / subscribe / remove.
#
# Wraps the same JSON store the CLI uses (hermes_cli.webhook); the webhook
# adapter hot-reloads it without a gateway restart.  Per-route HMAC secrets
# are redacted on read and surfaced once on create.
# ---------------------------------------------------------------------------


class WebhookCreate(BaseModel):
    name: str
    description: Optional[str] = None
    events: List[str] = []
    prompt: Optional[str] = None
    skills: List[str] = []
    deliver: str = "log"
    deliver_only: bool = False
    deliver_chat_id: Optional[str] = None
    # secret: omit to auto-generate
    secret: Optional[str] = None


def _webhook_route_summary(name: str, route: Dict[str, Any], base_url: str) -> Dict[str, Any]:
    return {
        "name": name,
        "description": route.get("description", ""),
        "events": list(route.get("events") or []),
        "deliver": route.get("deliver", "log"),
        "deliver_only": bool(route.get("deliver_only")),
        "prompt": route.get("prompt", ""),
        "skills": list(route.get("skills") or []),
        "created_at": route.get("created_at"),
        "url": f"{base_url}/webhooks/{name}",
        # Secret is masked on read; full value only returned on create.
        "secret_set": bool(route.get("secret")),
        # Default-enabled; only an explicit enabled:false turns a route off.
        "enabled": route.get("enabled", True) is not False,
    }


@app.get("/api/webhooks")
async def list_webhooks():
    import hermes_cli.webhook as wh

    base_url = wh._get_webhook_base_url()
    subs = wh._load_subscriptions()
    return {
        "enabled": wh._is_webhook_enabled(),
        "base_url": base_url,
        "subscriptions": [
            _webhook_route_summary(name, route, base_url)
            for name, route in subs.items()
        ],
    }


@app.post("/api/webhooks")
async def create_webhook(body: WebhookCreate):
    import re as _re
    import secrets as _secrets
    import time as _time
    import hermes_cli.webhook as wh

    if not wh._is_webhook_enabled():
        raise HTTPException(
            status_code=400,
            detail="Webhook platform is not enabled. Enable it in messaging settings first.",
        )

    name = (body.name or "").strip().lower().replace(" ", "-")
    if not _re.match(r"^[a-z0-9][a-z0-9_-]*$", name):
        raise HTTPException(
            status_code=400,
            detail="Invalid name. Use lowercase alphanumeric with hyphens/underscores.",
        )

    if body.deliver_only and body.deliver == "log":
        raise HTTPException(
            status_code=400,
            detail="Direct delivery requires a real target (telegram, discord, …), not 'log'.",
        )

    secret = body.secret or _secrets.token_urlsafe(32)
    route: Dict[str, Any] = {
        "description": body.description or f"Dashboard-created subscription: {name}",
        "events": [e.strip() for e in body.events if e.strip()],
        "secret": secret,
        "prompt": body.prompt or "",
        "skills": [s.strip() for s in body.skills if s.strip()],
        "deliver": body.deliver or "log",
        "created_at": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime()),
    }
    if body.deliver_only:
        route["deliver_only"] = True
    if body.deliver_chat_id:
        route["deliver_extra"] = {"chat_id": body.deliver_chat_id}

    subs = wh._load_subscriptions()
    subs[name] = route
    wh._save_subscriptions(subs)

    base_url = wh._get_webhook_base_url()
    summary = _webhook_route_summary(name, route, base_url)
    # Surface the secret exactly once, on create.
    summary["secret"] = secret
    return summary


@app.delete("/api/webhooks/{name}")
async def delete_webhook(name: str):
    import hermes_cli.webhook as wh

    key = (name or "").strip().lower()
    subs = wh._load_subscriptions()
    if key not in subs:
        raise HTTPException(status_code=404, detail=f"No subscription named '{key}'")
    del subs[key]
    wh._save_subscriptions(subs)
    return {"ok": True}


class WebhookEnabledToggle(BaseModel):
    enabled: bool


@app.put("/api/webhooks/{name}/enabled")
async def set_webhook_enabled(name: str, body: WebhookEnabledToggle):
    """Enable or disable a webhook route.

    Disabled routes stay in the subscriptions file (so they can be
    re-enabled) but the gateway rejects incoming events with 403.  The
    gateway hot-reloads the subscriptions file, so this takes effect on the
    next event without a restart.
    """
    import hermes_cli.webhook as wh

    key = (name or "").strip().lower()
    subs = wh._load_subscriptions()
    if key not in subs:
        raise HTTPException(status_code=404, detail=f"No subscription named '{key}'")
    subs[key]["enabled"] = bool(body.enabled)
    wh._save_subscriptions(subs)
    return {"ok": True, "name": key, "enabled": bool(body.enabled)}


# ---------------------------------------------------------------------------
# Gateway lifecycle endpoints — start / stop.
#
# restart + update already exist above; these complete the lifecycle so a
# remote admin can bring the gateway up or down without shell access.  Both
# spawn the real `hermes gateway <verb>` so behaviour matches the CLI exactly.
# Status is already surfaced by /api/status (gateway_running/state/platforms).
# ---------------------------------------------------------------------------


@app.post("/api/gateway/start")
async def start_gateway():
    try:
        proc = _spawn_hermes_action(["gateway", "start"], "gateway-start")
    except Exception as exc:
        _log.exception("Failed to spawn gateway start")
        raise HTTPException(status_code=500, detail=f"Failed to start gateway: {exc}")
    return {"ok": True, "pid": proc.pid, "name": "gateway-start"}


@app.post("/api/gateway/stop")
async def stop_gateway():
    try:
        proc = _spawn_hermes_action(["gateway", "stop"], "gateway-stop")
    except Exception as exc:
        _log.exception("Failed to spawn gateway stop")
        raise HTTPException(status_code=500, detail=f"Failed to stop gateway: {exc}")
    return {"ok": True, "pid": proc.pid, "name": "gateway-stop"}


# ---------------------------------------------------------------------------
# Credential pool endpoints — list / add / remove rotation keys.
#
# The credential pool (auth.json -> credential_pool.<provider>[]) holds the
# rotating API keys the agent round-robins through.  Secrets are redacted on
# read; only the agent ever sees the raw values at session start.
# ---------------------------------------------------------------------------


class CredentialPoolAdd(BaseModel):
    provider: str
    # api_key for API-key providers; OAuth pooling stays CLI-only (it needs
    # an interactive browser flow that doesn't belong in a single POST).
    api_key: str
    label: Optional[str] = None


def _pool_entry_summary(entry: Any, index: int) -> Dict[str, Any]:
    """Redacted, display-safe view of one PooledCredential.

    ``index`` is 1-based to match CredentialPool.remove_index().
    """
    token = getattr(entry, "access_token", "") or ""
    return {
        "index": index,
        "id": getattr(entry, "id", None),
        "label": getattr(entry, "label", None),
        "auth_type": getattr(entry, "auth_type", None),
        "source": getattr(entry, "source", None),
        "priority": getattr(entry, "priority", 0),
        "last_status": getattr(entry, "last_status", None),
        "request_count": getattr(entry, "request_count", 0),
        "token_preview": redact_key(token) if token else "",
        "has_refresh": bool(getattr(entry, "refresh_token", None)),
    }


@app.get("/api/credentials/pool")
async def list_credential_pool():
    from agent.credential_pool import load_pool
    from hermes_cli.auth import read_credential_pool

    providers = []
    # read_credential_pool(None) lists every provider that has pooled entries;
    # load_pool() then gives us the rich PooledCredential objects per provider.
    raw_pool = read_credential_pool()
    for provider_id in sorted(raw_pool.keys()):
        try:
            pool = load_pool(provider_id)
        except Exception:
            _log.exception("load_pool(%s) failed", provider_id)
            continue
        entries = pool.entries()
        if not entries:
            continue
        providers.append({
            "provider": provider_id,
            "entries": [
                _pool_entry_summary(e, i) for i, e in enumerate(entries, start=1)
            ],
        })
    return {"providers": providers}


@app.post("/api/credentials/pool")
async def add_credential_pool_entry(body: CredentialPoolAdd):
    import uuid as _uuid
    from agent.credential_pool import (
        load_pool,
        PooledCredential,
        AUTH_TYPE_API_KEY,
        SOURCE_MANUAL,
    )

    provider = (body.provider or "").strip().lower()
    api_key = (body.api_key or "").strip()
    if not provider or not api_key:
        raise HTTPException(status_code=400, detail="provider and api_key are required")

    try:
        pool = load_pool(provider)
        label = (body.label or "").strip() or f"key #{len(pool.entries()) + 1}"
        entry = PooledCredential(
            provider=provider,
            id=_uuid.uuid4().hex[:6],
            label=label,
            auth_type=AUTH_TYPE_API_KEY,
            priority=0,
            source=SOURCE_MANUAL,
            access_token=api_key,
        )
        pool.add_entry(entry)
    except Exception as exc:
        _log.exception("POST /api/credentials/pool failed")
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "provider": provider, "count": len(pool.entries())}


@app.delete("/api/credentials/pool/{provider}/{index}")
async def remove_credential_pool_entry(provider: str, index: int):
    """Remove a pool entry.  ``index`` is 1-based (matches the list response)."""
    from agent.credential_pool import load_pool

    provider = (provider or "").strip().lower()
    try:
        pool = load_pool(provider)
        removed = pool.remove_index(index)
    except Exception as exc:
        _log.exception("DELETE /api/credentials/pool failed")
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if removed is None:
        raise HTTPException(status_code=404, detail="No pool entry at that index")
    return {"ok": True, "provider": provider, "count": len(pool.entries())}


# ---------------------------------------------------------------------------
# Memory provider endpoints — status / list providers / select / disable / reset.
#
# Selecting a provider only writes config.memory.provider (full interactive
# provider setup, with its API-key prompts, stays on the CLI via
# `hermes memory setup`).  The dashboard covers the common admin actions:
# see which provider is active, switch the built-in store on/off, and wipe
# built-in memory files.
# ---------------------------------------------------------------------------


class MemoryProviderSelect(BaseModel):
    # "" or "built-in" disables the external provider (built-in only).
    provider: str


class MemoryReset(BaseModel):
    # "all" | "memory" | "user"
    target: str = "all"


@app.get("/api/memory")
async def get_memory_status():
    from plugins.memory import discover_memory_providers

    cfg = load_config()
    active = ""
    mem = cfg.get("memory")
    if isinstance(mem, dict):
        active = str(mem.get("provider") or "")

    providers = []
    try:
        for name, description, configured in discover_memory_providers():
            providers.append({
                "name": name,
                "description": description,
                "configured": bool(configured),
            })
    except Exception:
        _log.exception("discover_memory_providers failed")

    # Built-in memory file sizes (so the UI can show what a reset would erase).
    mem_dir = get_hermes_home() / "memories"
    files = {}
    for fname, key in (("MEMORY.md", "memory"), ("USER.md", "user")):
        path = mem_dir / fname
        files[key] = path.stat().st_size if path.exists() else 0

    return {
        "active": active,
        "providers": providers,
        "builtin_files": files,
    }


@app.put("/api/memory/provider")
async def set_memory_provider(body: MemoryProviderSelect):
    provider = (body.provider or "").strip()
    if provider.lower() in {"built-in", "builtin", "none"}:
        provider = ""

    if provider:
        from plugins.memory import discover_memory_providers

        valid = {name for name, _d, _c in discover_memory_providers()}
        if provider not in valid:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown memory provider '{provider}'. Run `hermes memory setup` to configure a new one.",
            )

    cfg = load_config()
    if not isinstance(cfg.get("memory"), dict):
        cfg["memory"] = {}
    cfg["memory"]["provider"] = provider
    save_config(cfg)
    return {"ok": True, "active": provider}


@app.post("/api/memory/reset")
async def reset_memory(body: MemoryReset):
    target = (body.target or "all").strip().lower()
    if target not in {"all", "memory", "user"}:
        raise HTTPException(status_code=400, detail="target must be all, memory, or user")

    mem_dir = get_hermes_home() / "memories"
    deleted = []
    targets = []
    if target in {"all", "memory"}:
        targets.append("MEMORY.md")
    if target in {"all", "user"}:
        targets.append("USER.md")
    for fname in targets:
        path = mem_dir / fname
        if path.exists():
            try:
                path.unlink()
                deleted.append(fname)
            except OSError as exc:
                raise HTTPException(status_code=500, detail=f"Could not delete {fname}: {exc}")
    return {"ok": True, "deleted": deleted}


# ---------------------------------------------------------------------------
# Operations endpoints — doctor / security audit / backup / import /
# checkpoints / hooks.
#
# Diagnostic and maintenance commands.  The long-running / text-output ones
# (doctor, security audit, backup, import, skills install) are spawned as
# background actions whose logs the dashboard tails via
# /api/actions/{name}/status — same pattern as gateway restart and update.
# The cheap, structured reads (hooks list, checkpoints list) return JSON
# directly.
# ---------------------------------------------------------------------------


@app.post("/api/ops/doctor")
async def run_doctor():
    try:
        proc = _spawn_hermes_action(["doctor"], "doctor")
    except Exception as exc:
        _log.exception("Failed to spawn doctor")
        raise HTTPException(status_code=500, detail=f"Failed to run doctor: {exc}")
    return {"ok": True, "pid": proc.pid, "name": "doctor"}


@app.post("/api/ops/security-audit")
async def run_security_audit():
    try:
        proc = _spawn_hermes_action(["security", "audit"], "security-audit")
    except Exception as exc:
        _log.exception("Failed to spawn security audit")
        raise HTTPException(status_code=500, detail=f"Failed to run security audit: {exc}")
    return {"ok": True, "pid": proc.pid, "name": "security-audit"}


class BackupRequest(BaseModel):
    # Optional output path; defaults to a timestamped zip in the home dir.
    output: Optional[str] = None


@app.post("/api/ops/backup")
async def run_backup(body: BackupRequest):
    args = ["backup"]
    if body.output:
        args.append(body.output.strip())
    try:
        proc = _spawn_hermes_action(args, "backup")
    except Exception as exc:
        _log.exception("Failed to spawn backup")
        raise HTTPException(status_code=500, detail=f"Failed to run backup: {exc}")
    return {"ok": True, "pid": proc.pid, "name": "backup"}


class ImportRequest(BaseModel):
    archive: str


@app.post("/api/ops/import")
async def run_import(body: ImportRequest):
    archive = (body.archive or "").strip()
    if not archive:
        raise HTTPException(status_code=400, detail="archive path is required")
    if not os.path.isfile(archive):
        raise HTTPException(status_code=404, detail=f"Archive not found: {archive}")
    try:
        proc = _spawn_hermes_action(["import", archive], "import")
    except Exception as exc:
        _log.exception("Failed to spawn import")
        raise HTTPException(status_code=500, detail=f"Failed to run import: {exc}")
    return {"ok": True, "pid": proc.pid, "name": "import"}


@app.get("/api/ops/hooks")
async def list_hooks():
    """List configured shell hooks from config.yaml with consent + health.

    Reports each hook's allowlist (consent) status and whether the script is
    currently executable, plus the set of valid hook events so the create
    form can offer them.
    """
    from hermes_cli.config import load_config as _load_config
    from agent import shell_hooks

    try:
        from hermes_cli.plugins import VALID_HOOKS
        valid_events = sorted(VALID_HOOKS)
    except Exception:
        valid_events = []

    specs = []
    try:
        specs = shell_hooks.iter_configured_hooks(_load_config())
    except Exception:
        _log.exception("iter_configured_hooks failed")

    out = []
    for spec in specs:
        entry = None
        try:
            entry = shell_hooks.allowlist_entry_for(spec.event, spec.command)
        except Exception:
            pass
        executable = False
        try:
            executable = shell_hooks.script_is_executable(spec.command)
        except Exception:
            pass
        out.append({
            "event": spec.event,
            "matcher": spec.matcher,
            "command": spec.command,
            "timeout": spec.timeout,
            "allowed": entry is not None,
            "approved_at": (entry or {}).get("approved_at"),
            "executable": executable,
        })

    return {"hooks": out, "valid_events": valid_events}


class HookCreate(BaseModel):
    event: str
    command: str
    matcher: Optional[str] = None
    timeout: Optional[int] = None
    # approve: write the consent allowlist entry too (the operator using the
    # authenticated dashboard is giving consent). Without it the hook is
    # configured but won't fire until approved.
    approve: bool = True


@app.post("/api/ops/hooks")
async def create_hook(body: HookCreate):
    """Add a shell hook to config.yaml (and optionally approve it).

    Shell hooks run arbitrary commands, so this is a privileged action: it
    writes to the ``hooks:`` config block and, when ``approve`` is set, records
    consent in the allowlist so the hook actually fires.  Takes effect on the
    next session / gateway restart.
    """
    from agent import shell_hooks

    event = (body.event or "").strip()
    command = (body.command or "").strip()
    if not event or not command:
        raise HTTPException(status_code=400, detail="event and command are required")

    try:
        from hermes_cli.plugins import VALID_HOOKS
        if event not in VALID_HOOKS:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown event '{event}'. Valid: {', '.join(sorted(VALID_HOOKS))}",
            )
    except HTTPException:
        raise
    except Exception:
        pass

    cfg = load_config()
    hooks_cfg = cfg.get("hooks")
    if not isinstance(hooks_cfg, dict):
        hooks_cfg = {}
        cfg["hooks"] = hooks_cfg
    entries = hooks_cfg.get(event)
    if not isinstance(entries, list):
        entries = []
        hooks_cfg[event] = entries

    new_entry: Dict[str, Any] = {"command": command}
    if body.matcher:
        new_entry["matcher"] = body.matcher
    if body.timeout is not None:
        new_entry["timeout"] = int(body.timeout)
    entries.append(new_entry)
    save_config(cfg)

    approved = False
    if body.approve:
        try:
            shell_hooks._record_approval(event, command)
            approved = True
        except Exception:
            _log.exception("hook consent record failed")

    return {"ok": True, "event": event, "command": command, "approved": approved}


class HookDelete(BaseModel):
    event: str
    command: str


@app.delete("/api/ops/hooks")
async def delete_hook(body: HookDelete):
    """Remove a hook from config.yaml and revoke its consent allowlist entry."""
    from agent import shell_hooks

    event = (body.event or "").strip()
    command = (body.command or "").strip()
    if not event or not command:
        raise HTTPException(status_code=400, detail="event and command are required")

    cfg = load_config()
    hooks_cfg = cfg.get("hooks")
    removed = False
    if isinstance(hooks_cfg, dict) and isinstance(hooks_cfg.get(event), list):
        before = len(hooks_cfg[event])
        hooks_cfg[event] = [
            e for e in hooks_cfg[event]
            if not (isinstance(e, dict) and e.get("command") == command)
        ]
        removed = len(hooks_cfg[event]) < before
        if not hooks_cfg[event]:
            del hooks_cfg[event]
        if not hooks_cfg:
            cfg.pop("hooks", None)
        save_config(cfg)

    # Revoke consent regardless so a re-add re-prompts.
    try:
        shell_hooks.revoke(command)
    except Exception:
        pass

    if not removed:
        raise HTTPException(status_code=404, detail="No matching hook found")
    return {"ok": True}


@app.get("/api/ops/checkpoints")
async def list_checkpoints():
    """List the /rollback shadow store checkpoints (read-only)."""
    # Checkpoints live under <hermes_home>/checkpoints/.  Surface a count +
    # total size so the dashboard can show what a prune would reclaim; the
    # actual prune is a spawned action so confirmation/pruning logic stays
    # in one place (the CLI).
    cp_dir = get_hermes_home() / "checkpoints"
    sessions = []
    total_bytes = 0
    if cp_dir.is_dir():
        for child in sorted(cp_dir.iterdir()):
            if not child.is_dir():
                continue
            size = 0
            count = 0
            for f in child.rglob("*"):
                if f.is_file():
                    try:
                        size += f.stat().st_size
                        count += 1
                    except OSError:
                        pass
            total_bytes += size
            sessions.append({
                "session": child.name,
                "files": count,
                "bytes": size,
            })
    return {"sessions": sessions, "total_bytes": total_bytes}


@app.post("/api/ops/checkpoints/prune")
async def prune_checkpoints():
    try:
        proc = _spawn_hermes_action(["checkpoints", "prune"], "checkpoints-prune")
    except Exception as exc:
        _log.exception("Failed to spawn checkpoints prune")
        raise HTTPException(status_code=500, detail=f"Failed to prune checkpoints: {exc}")
    return {"ok": True, "pid": proc.pid, "name": "checkpoints-prune"}


# ---------------------------------------------------------------------------
# Skills hub endpoints — search / install / uninstall / update.
#
# Search and install touch the network (GitHub, hub sources) and run the same
# complex source-router pipeline the CLI uses, so they're spawned as background
# actions whose logs the dashboard tails.  The already-installed skill list +
# enable/disable toggle live in the existing /api/skills endpoints.
# ---------------------------------------------------------------------------


class SkillInstallRequest(BaseModel):
    identifier: str


@app.post("/api/skills/hub/install")
async def install_skill_hub(body: SkillInstallRequest):
    identifier = (body.identifier or "").strip()
    if not identifier:
        raise HTTPException(status_code=400, detail="identifier is required")
    try:
        proc = _spawn_hermes_action(["skills", "install", identifier], "skills-install")
    except Exception as exc:
        _log.exception("Failed to spawn skills install")
        raise HTTPException(status_code=500, detail=f"Failed to install skill: {exc}")
    return {"ok": True, "pid": proc.pid, "name": "skills-install"}


class SkillUninstallRequest(BaseModel):
    name: str


@app.post("/api/skills/hub/uninstall")
async def uninstall_skill_hub(body: SkillUninstallRequest):
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    try:
        proc = _spawn_hermes_action(["skills", "uninstall", name, "--yes"], "skills-uninstall")
    except Exception as exc:
        _log.exception("Failed to spawn skills uninstall")
        raise HTTPException(status_code=500, detail=f"Failed to uninstall skill: {exc}")
    return {"ok": True, "pid": proc.pid, "name": "skills-uninstall"}


@app.post("/api/skills/hub/update")
async def update_skills_hub():
    try:
        proc = _spawn_hermes_action(["skills", "update"], "skills-update")
    except Exception as exc:
        _log.exception("Failed to spawn skills update")
        raise HTTPException(status_code=500, detail=f"Failed to update skills: {exc}")
    return {"ok": True, "pid": proc.pid, "name": "skills-update"}


@app.get("/api/skills/hub/search")
async def search_skills_hub(q: str = "", source: str = "all", limit: int = 20):
    """Search the skill hub across all configured sources.

    Network-bound (parallel source search); runs in a thread so the FastAPI
    loop isn't blocked.  Returns structured results the UI installs by
    identifier via POST /api/skills/hub/install.
    """
    query = (q or "").strip()
    if not query:
        return {"results": []}

    def _run():
        from tools.skills_hub import create_source_router, unified_search

        sources = create_source_router()
        metas = unified_search(
            query, sources, source_filter=source or "all", limit=min(max(limit, 1), 50)
        )
        return [
            {
                "name": m.name,
                "description": m.description,
                "source": m.source,
                "identifier": m.identifier,
                "trust_level": m.trust_level,
                "repo": m.repo,
                "tags": list(m.tags or []),
            }
            for m in metas
        ]

    try:
        results = await asyncio.to_thread(_run)
    except Exception as exc:
        _log.exception("skills hub search failed")
        raise HTTPException(status_code=502, detail=f"Hub search failed: {exc}")
    return {"results": results}


# ---------------------------------------------------------------------------
# Profile management endpoints (minimal — list/create/rename/delete + SOUL.md)
# ---------------------------------------------------------------------------


class ProfileCreate(BaseModel):
    name: str
    clone_from_default: bool = False
    clone_all: bool = False
    no_skills: bool = False
    description: Optional[str] = None
    # Explicit source profile to clone from (e.g. duplicating an existing
    # profile). When set, it takes precedence over ``clone_from_default``,
    # which always sources from "default". ``clone_all`` still selects a full
    # state copytree vs. a config/skills/SOUL copy.
    clone_from: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None


class ProfileRename(BaseModel):
    new_name: str


class ProfileSoulUpdate(BaseModel):
    content: str


class ProfileActiveUpdate(BaseModel):
    name: str


class ProfileDescriptionUpdate(BaseModel):
    description: str = ""


class ProfileModelUpdate(BaseModel):
    provider: str
    model: str


class ProfileDescribeAuto(BaseModel):
    overwrite: bool = False


def _profile_attr(info, name: str, default: Any = None) -> Any:
    try:
        return getattr(info, name)
    except Exception:
        return default


def _profile_to_dict(info) -> Dict[str, Any]:
    return {
        "name": _profile_attr(info, "name", ""),
        "path": str(_profile_attr(info, "path", "")),
        "is_default": bool(_profile_attr(info, "is_default", False)),
        "model": _profile_attr(info, "model"),
        "provider": _profile_attr(info, "provider"),
        "has_env": bool(_profile_attr(info, "has_env", False)),
        "skill_count": int(_profile_attr(info, "skill_count", 0) or 0),
        "gateway_running": bool(_profile_attr(info, "gateway_running", False)),
        "description": _profile_attr(info, "description", "") or "",
        "description_auto": bool(_profile_attr(info, "description_auto", False)),
        "distribution_name": _profile_attr(info, "distribution_name"),
        "distribution_version": _profile_attr(info, "distribution_version"),
        "distribution_source": _profile_attr(info, "distribution_source"),
        "has_alias": _profile_attr(info, "alias_path") is not None,
    }


def _fallback_profile_dicts(profiles_mod) -> List[Dict[str, Any]]:
    def _safe(callable_, default):
        try:
            return callable_()
        except Exception:
            return default

    profiles: List[Dict[str, Any]] = []
    default_home = profiles_mod._get_default_hermes_home()
    if default_home.is_dir():
        model, provider = _safe(lambda: profiles_mod._read_config_model(default_home), (None, None))
        profiles.append({
            "name": "default",
            "path": str(default_home),
            "is_default": True,
            "model": model,
            "provider": provider,
            "has_env": (default_home / ".env").exists(),
            "skill_count": _safe(lambda: profiles_mod._count_skills(default_home), 0),
            "gateway_running": _safe(lambda: profiles_mod._check_gateway_running(default_home), False),
            "description": _safe(lambda: profiles_mod.read_profile_meta(default_home).get("description", ""), ""),
            "description_auto": _safe(lambda: profiles_mod.read_profile_meta(default_home).get("description_auto", False), False),
            "distribution_name": None,
            "distribution_version": None,
            "distribution_source": None,
            "has_alias": False,
        })

    profiles_root = profiles_mod._get_profiles_root()
    if profiles_root.is_dir():
        for entry in sorted(profiles_root.iterdir()):
            if not entry.is_dir() or not profiles_mod._PROFILE_ID_RE.match(entry.name):
                continue
            model, provider = _safe(lambda entry=entry: profiles_mod._read_config_model(entry), (None, None))
            profiles.append({
                "name": entry.name,
                "path": str(entry),
                "is_default": False,
                "model": model,
                "provider": provider,
                "has_env": (entry / ".env").exists(),
                "skill_count": _safe(lambda entry=entry: profiles_mod._count_skills(entry), 0),
                "gateway_running": _safe(lambda entry=entry: profiles_mod._check_gateway_running(entry), False),
                "description": _safe(lambda entry=entry: profiles_mod.read_profile_meta(entry).get("description", ""), ""),
                "description_auto": _safe(lambda entry=entry: profiles_mod.read_profile_meta(entry).get("description_auto", False), False),
                "distribution_name": None,
                "distribution_version": None,
                "distribution_source": None,
                "has_alias": False,
            })

    return profiles


def _resolve_profile_dir(name: str) -> Path:
    """Validate ``name`` and resolve to its directory or raise an HTTPException."""
    from hermes_cli import profiles as profiles_mod
    try:
        profiles_mod.validate_profile_name(name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not profiles_mod.profile_exists(name):
        raise HTTPException(status_code=404, detail=f"Profile '{name}' does not exist.")
    return profiles_mod.get_profile_dir(name)


def _profile_setup_command(name: str) -> str:
    """Return the shell command used to configure a profile in the CLI."""
    _resolve_profile_dir(name)
    return "hermes setup" if name == "default" else f"{name} setup"


def _write_profile_model(profile_dir: Path, provider: str, model: str) -> None:
    """Write the main model assignment into a specific profile's config.yaml.

    Scopes ``load_config``/``save_config`` to ``profile_dir`` via the
    context-local HERMES_HOME override so the write lands in the target
    profile's config rather than the dashboard process's active profile.
    Clears any stale ``base_url`` / ``context_length`` the same way
    ``POST /api/model/set`` does, since the new model may differ.
    """
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override

    token = set_hermes_home_override(str(profile_dir))
    try:
        cfg = load_config()
        cfg["model"] = _apply_main_model_assignment(cfg.get("model", {}), provider, model)
        save_config(cfg)
    finally:
        reset_hermes_home_override(token)


@app.get("/api/profiles")
async def list_profiles_endpoint():
    from hermes_cli import profiles as profiles_mod
    try:
        return {"profiles": [_profile_to_dict(p) for p in profiles_mod.list_profiles()]}
    except Exception:
        _log.exception("GET /api/profiles failed; falling back to profile directory scan")
        return {"profiles": _fallback_profile_dicts(profiles_mod)}


@app.post("/api/profiles")
async def create_profile_endpoint(body: ProfileCreate):
    from hermes_cli import profiles as profiles_mod
    explicit_source = (body.clone_from or "").strip()
    if explicit_source:
        # Duplicating a specific profile: clone its config/skills/SOUL (or full
        # state when clone_all) from the named source rather than "default".
        clone = True
        clone_from = explicit_source
        clone_config = not body.clone_all
    else:
        clone = body.clone_from_default or body.clone_all
        clone_from = "default" if clone else None
        clone_config = body.clone_from_default and not body.clone_all
    try:
        path = profiles_mod.create_profile(
            name=body.name,
            clone_from=clone_from,
            clone_all=body.clone_all,
            clone_config=clone_config,
            no_skills=body.no_skills,
            description=body.description,
        )
        # Match the CLI's profile-create flow: fresh named profiles get the
        # bundled skills installed. When cloning from default, create_profile()
        # has already copied the source profile's skills, including any
        # user-installed skills. When no_skills=True, create_profile() wrote
        # the opt-out marker and seed_profile_skills() will no-op.
        if not clone:
            profiles_mod.seed_profile_skills(path, quiet=True)

        # Match the CLI's profile-create flow: named profiles should get a
        # wrapper in ~/.local/bin when the alias is safe to create.
        collision = profiles_mod.check_alias_collision(body.name)
        if not collision:
            profiles_mod.create_wrapper_script(body.name)
    except (ValueError, FileExistsError, FileNotFoundError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        _log.exception("POST /api/profiles failed")
        raise HTTPException(status_code=500, detail=str(e))

    # Optional explicit model assignment for the new profile. Best-effort:
    # the profile already exists, so a model-write hiccup must not 500 the
    # whole create — the user can set the model later from the Models page
    # or `<profile> setup`.
    provider = (body.provider or "").strip()
    model = (body.model or "").strip()
    model_set = False
    if provider and model:
        try:
            _write_profile_model(path, provider, model)
            model_set = True
        except Exception:
            _log.exception("Setting model for new profile %s failed", body.name)

    return {"ok": True, "name": body.name, "path": str(path), "model_set": model_set}


@app.get("/api/profiles/active")
async def get_active_profile_endpoint():
    """Return the sticky active profile and the profile this dashboard
    process is currently running as.

    ``active`` is the sticky default written by ``hermes profile use`` —
    the profile new CLI invocations pick up. ``current`` is the profile
    the running dashboard/gateway is scoped to (derived from HERMES_HOME).
    """
    from hermes_cli import profiles as profiles_mod
    try:
        active = profiles_mod.get_active_profile() or "default"
    except Exception:
        active = "default"
    try:
        current = profiles_mod.get_active_profile_name() or "default"
    except Exception:
        current = "default"
    return {"active": active, "current": current}


@app.post("/api/profiles/active")
async def set_active_profile_endpoint(body: ProfileActiveUpdate):
    """Set the sticky active profile (mirrors ``hermes profile use``).

    Note: this does not retarget the already-running dashboard process —
    it changes which profile subsequent CLI commands and gateways use.
    """
    from hermes_cli import profiles as profiles_mod
    try:
        profiles_mod.set_active_profile(body.name)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        _log.exception("POST /api/profiles/active failed")
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True, "active": profiles_mod.normalize_profile_name(body.name)}


@app.get("/api/profiles/{name}/setup-command")
async def get_profile_setup_command(name: str):
    return {"command": _profile_setup_command(name)}


@app.post("/api/profiles/{name}/open-terminal")
async def open_profile_terminal_endpoint(name: str):
    try:
        command = _profile_setup_command(name)

        if sys.platform.startswith("win"):
            subprocess.Popen(["cmd.exe", "/c", "start", "", command])
        elif sys.platform == "darwin":
            escaped = command.replace("\\", "\\\\").replace('"', '\\"')
            applescript = (
                'tell application "Terminal"\n'
                "activate\n"
                f'do script "{escaped}"\n'
                "end tell"
            )
            subprocess.Popen(["osascript", "-e", applescript])
        else:
            terminal_commands = [
                ("x-terminal-emulator", ["x-terminal-emulator", "-e", "sh", "-lc", command]),
                ("gnome-terminal", ["gnome-terminal", "--", "sh", "-lc", command]),
                ("konsole", ["konsole", "-e", "sh", "-lc", command]),
                ("xfce4-terminal", ["xfce4-terminal", "-e", f"sh -lc '{command}'"]),
                ("mate-terminal", ["mate-terminal", "-e", f"sh -lc '{command}'"]),
                ("lxterminal", ["lxterminal", "-e", f"sh -lc '{command}'"]),
                ("tilix", ["tilix", "-e", "sh", "-lc", command]),
                ("alacritty", ["alacritty", "-e", "sh", "-lc", command]),
                ("kitty", ["kitty", "sh", "-lc", command]),
                ("xterm", ["xterm", "-e", "sh", "-lc", command]),
            ]
            for executable, popen_args in terminal_commands:
                if subprocess.call(
                    ["which", executable],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                ) == 0:
                    subprocess.Popen(popen_args)
                    break
            else:
                raise HTTPException(
                    status_code=400,
                    detail="No supported terminal emulator found",
                )
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        _log.exception("POST /api/profiles/%s/open-terminal failed", name)
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True, "command": command}


@app.patch("/api/profiles/{name}")
async def rename_profile_endpoint(name: str, body: ProfileRename):
    from hermes_cli import profiles as profiles_mod
    try:
        path = profiles_mod.rename_profile(name, body.new_name)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except (ValueError, FileExistsError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        _log.exception("PATCH /api/profiles/%s failed", name)
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True, "name": body.new_name, "path": str(path)}


@app.delete("/api/profiles/{name}")
async def delete_profile_endpoint(name: str):
    """Delete a profile. The dashboard collects the user's confirmation in
    its own dialog before this request, so we always pass ``yes=True`` to
    skip the CLI's interactive prompt."""
    from hermes_cli import profiles as profiles_mod
    try:
        path = profiles_mod.delete_profile(name, yes=True)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        _log.exception("DELETE /api/profiles/%s failed", name)
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True, "path": str(path)}


@app.get("/api/profiles/{name}/soul")
async def get_profile_soul(name: str):
    soul_path = _resolve_profile_dir(name) / "SOUL.md"
    if soul_path.exists():
        try:
            return {"content": soul_path.read_text(encoding="utf-8"), "exists": True}
        except OSError as e:
            raise HTTPException(status_code=500, detail=f"Could not read SOUL.md: {e}")
    return {"content": "", "exists": False}


@app.put("/api/profiles/{name}/soul")
async def update_profile_soul(name: str, body: ProfileSoulUpdate):
    soul_path = _resolve_profile_dir(name) / "SOUL.md"
    try:
        soul_path.write_text(body.content, encoding="utf-8")
    except OSError as e:
        _log.exception("PUT /api/profiles/%s/soul failed", name)
        raise HTTPException(status_code=500, detail=f"Could not write SOUL.md: {e}")
    return {"ok": True}


@app.put("/api/profiles/{name}/description")
async def update_profile_description_endpoint(name: str, body: ProfileDescriptionUpdate):
    """Set or clear a profile's role description (kanban routing signal).

    Empty string clears the description. Non-empty stores it as a
    user-authored description (``description_auto: false``) so the
    auto-describer won't overwrite it on a sweep.
    """
    from hermes_cli import profiles as profiles_mod
    profile_dir = _resolve_profile_dir(name)
    text = (body.description or "").strip()
    try:
        profiles_mod.write_profile_meta(
            profile_dir,
            description=text,
            description_auto=False,
        )
    except Exception as e:
        _log.exception("PUT /api/profiles/%s/description failed", name)
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True, "description": text, "description_auto": False}


@app.put("/api/profiles/{name}/model")
async def update_profile_model_endpoint(name: str, body: ProfileModelUpdate):
    """Set the main model (``model.default`` + ``model.provider``) for a
    specific profile's config.yaml, without touching the dashboard's own
    active profile. Mirrors ``POST /api/model/set`` (main scope) but scoped
    to the named profile via the HERMES_HOME override.
    """
    profile_dir = _resolve_profile_dir(name)
    provider = (body.provider or "").strip()
    model = (body.model or "").strip()
    if not provider or not model:
        raise HTTPException(status_code=400, detail="provider and model are required")
    try:
        _write_profile_model(profile_dir, provider, model)
    except Exception as e:
        _log.exception("PUT /api/profiles/%s/model failed", name)
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True, "provider": provider, "model": model}


@app.post("/api/profiles/{name}/describe-auto")
async def describe_profile_auto_endpoint(name: str, body: ProfileDescribeAuto):
    """Auto-generate a profile's description via the auxiliary LLM
    (``auxiliary.profile_describer``). Mirrors ``hermes profile describe
    <name> --auto``.

    A failed generation (no aux client, LLM error, …) is returned as
    ``ok: false`` with a reason rather than an HTTP error so the UI can
    surface it inline and let the operator fix config and retry.
    """
    _resolve_profile_dir(name)
    try:
        from hermes_cli import profile_describer
        outcome = profile_describer.describe_profile(name, overwrite=bool(body.overwrite))
    except Exception as e:
        _log.exception("POST /api/profiles/%s/describe-auto failed", name)
        raise HTTPException(status_code=500, detail=str(e))
    return {
        "ok": bool(outcome.ok),
        "reason": outcome.reason,
        "description": outcome.description,
        # Only a successful generation is an auto-authored description. A failed
        # sweep leaves any existing description untouched, so don't claim it's
        # auto-generated.
        "description_auto": bool(outcome.ok),
    }


# ---------------------------------------------------------------------------
# Skills & Tools endpoints
# ---------------------------------------------------------------------------


class SkillToggle(BaseModel):
    name: str
    enabled: bool


@app.get("/api/skills")
async def get_skills():
    from tools.skills_tool import _find_all_skills
    from hermes_cli.skills_config import get_disabled_skills
    config = load_config()
    disabled = get_disabled_skills(config)
    skills = _find_all_skills(skip_disabled=True)
    for s in skills:
        s["enabled"] = s["name"] not in disabled
    return skills


@app.put("/api/skills/toggle")
async def toggle_skill(body: SkillToggle):
    from hermes_cli.skills_config import get_disabled_skills, save_disabled_skills
    config = load_config()
    disabled = get_disabled_skills(config)
    if body.enabled:
        disabled.discard(body.name)
    else:
        disabled.add(body.name)
    save_disabled_skills(config, disabled)
    return {"ok": True, "name": body.name, "enabled": body.enabled}


@app.get("/api/tools/toolsets")
async def get_toolsets():
    from hermes_cli.tools_config import (
        _get_effective_configurable_toolsets,
        _get_platform_tools,
        _toolset_has_keys,
        gui_toolset_label,
    )
    from toolsets import resolve_toolset

    config = load_config()
    enabled_toolsets = _get_platform_tools(
        config,
        "cli",
        include_default_mcp_servers=False,
    )
    result = []
    for name, label, desc in _get_effective_configurable_toolsets():
        try:
            tools = sorted(set(resolve_toolset(name)))
        except Exception:
            tools = []
        is_enabled = name in enabled_toolsets
        result.append({
            "name": name,
            "label": gui_toolset_label(label),
            "description": desc,
            "enabled": is_enabled,
            "available": is_enabled,
            "configured": _toolset_has_keys(name, config),
            "tools": tools,
        })
    return result


class ToolsetToggle(BaseModel):
    enabled: bool


@app.put("/api/tools/toolsets/{name}")
async def toggle_toolset(name: str, body: ToolsetToggle):
    """Enable/disable a configurable toolset for the desktop (cli) platform.

    Persists to ``platform_toolsets.cli`` via the same ``_save_platform_tools``
    helper the CLI ``hermes tools`` picker uses, so the GUI and CLI stay in
    lockstep. Returns 400 for unknown toolset keys.
    """
    from hermes_cli.tools_config import (
        _get_effective_configurable_toolsets,
        _get_platform_tools,
        _save_platform_tools,
    )

    valid = {ts_key for ts_key, _, _ in _get_effective_configurable_toolsets()}
    if name not in valid:
        raise HTTPException(status_code=400, detail=f"Unknown toolset: {name}")

    config = load_config()
    enabled = set(
        _get_platform_tools(config, "cli", include_default_mcp_servers=False)
    )
    if body.enabled:
        enabled.add(name)
    else:
        enabled.discard(name)
    _save_platform_tools(config, "cli", enabled)
    return {"ok": True, "name": name, "enabled": body.enabled}


@app.get("/api/tools/toolsets/{name}/config")
async def get_toolset_config(name: str):
    """Return the provider matrix + key status for a toolset's config panel.

    Surfaces the same provider rows the CLI ``hermes tools`` picker shows
    (via ``_visible_providers``), each with its ``env_vars`` annotated with
    current ``is_set`` state so the GUI can render provider selection + key
    entry. Toolsets without a ``TOOL_CATEGORIES`` entry return an empty
    provider list and ``has_category: false``. Returns 400 for unknown keys.
    """
    from hermes_cli.tools_config import (
        TOOL_CATEGORIES,
        _get_effective_configurable_toolsets,
        _is_provider_active,
        _visible_providers,
    )
    from hermes_cli.config import get_env_value

    valid = {ts_key for ts_key, _, _ in _get_effective_configurable_toolsets()}
    if name not in valid:
        raise HTTPException(status_code=400, detail=f"Unknown toolset: {name}")

    config = load_config()
    cat = TOOL_CATEGORIES.get(name)
    providers = []
    active_provider = None
    if cat:
        for prov in _visible_providers(cat, config, force_fresh=True):
            env_vars = [
                {
                    "key": e["key"],
                    "prompt": e.get("prompt", e["key"]),
                    "url": e.get("url"),
                    "default": e.get("default"),
                    "is_set": bool(get_env_value(e["key"])),
                }
                for e in prov.get("env_vars", [])
            ]
            # Surface the same active-provider determination the CLI picker
            # uses (``_is_provider_active``) so the GUI highlights the provider
            # actually written to config (e.g. web.backend), not just the first
            # keyless one in the list.
            is_active = _is_provider_active(prov, config, force_fresh=True)
            if is_active and active_provider is None:
                active_provider = prov["name"]
            providers.append({
                "name": prov["name"],
                "badge": prov.get("badge", ""),
                "tag": prov.get("tag", ""),
                "env_vars": env_vars,
                "post_setup": prov.get("post_setup"),
                "requires_nous_auth": bool(prov.get("requires_nous_auth")),
                "is_active": is_active,
            })
    return {
        "name": name,
        "has_category": cat is not None,
        "providers": providers,
        "active_provider": active_provider,
    }


class ToolsetProviderSelect(BaseModel):
    provider: str


@app.put("/api/tools/toolsets/{name}/provider")
async def select_toolset_provider(name: str, body: ToolsetProviderSelect):
    """Persist a provider selection for a toolset (no key prompting).

    Delegates to ``apply_provider_selection`` — the shared, non-interactive
    core extracted from the CLI configurator — so the GUI and ``hermes tools``
    write identical config keys (``web.backend``, ``tts.provider``, etc.).
    API keys and post-setup flows are handled by separate endpoints. Returns
    400 for unknown toolset or provider names.
    """
    from hermes_cli.tools_config import (
        apply_provider_selection,
        _get_effective_configurable_toolsets,
    )

    valid = {ts_key for ts_key, _, _ in _get_effective_configurable_toolsets()}
    if name not in valid:
        raise HTTPException(status_code=400, detail=f"Unknown toolset: {name}")

    config = load_config()
    try:
        apply_provider_selection(name, body.provider, config)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc).strip('"'))
    save_config(config)
    return {"ok": True, "name": name, "provider": body.provider}


# ---------------------------------------------------------------------------
# Raw YAML config endpoint
# ---------------------------------------------------------------------------


class RawConfigUpdate(BaseModel):
    yaml_text: str


@app.get("/api/config/raw")
async def get_config_raw():
    path = get_config_path()
    if not path.exists():
        return {"yaml": ""}
    return {"yaml": path.read_text(encoding="utf-8")}


@app.put("/api/config/raw")
async def update_config_raw(body: RawConfigUpdate):
    try:
        parsed = yaml.safe_load(body.yaml_text)
        if not isinstance(parsed, dict):
            raise HTTPException(status_code=400, detail="YAML must be a mapping")
        save_config(parsed)
        return {"ok": True}
    except yaml.YAMLError as e:
        raise HTTPException(status_code=400, detail=f"Invalid YAML: {e}")


# ---------------------------------------------------------------------------
# Token / cost analytics endpoint
# ---------------------------------------------------------------------------


@app.get("/api/analytics/usage")
async def get_usage_analytics(days: int = 30):
    from hermes_state import SessionDB
    from agent.insights import InsightsEngine

    db = SessionDB()
    try:
        cutoff = time.time() - (days * 86400)
        cur = db._conn.execute("""
            SELECT date(started_at, 'unixepoch') as day,
                   SUM(input_tokens) as input_tokens,
                   SUM(output_tokens) as output_tokens,
                   SUM(cache_read_tokens) as cache_read_tokens,
                   SUM(reasoning_tokens) as reasoning_tokens,
                   COALESCE(SUM(estimated_cost_usd), 0) as estimated_cost,
                   COALESCE(SUM(actual_cost_usd), 0) as actual_cost,
                   COUNT(*) as sessions,
                   SUM(COALESCE(api_call_count, 0)) as api_calls
            FROM sessions WHERE started_at > ?
            GROUP BY day ORDER BY day
        """, (cutoff,))
        daily = [dict(r) for r in cur.fetchall()]

        cur2 = db._conn.execute("""
            SELECT model,
                   SUM(input_tokens) as input_tokens,
                   SUM(output_tokens) as output_tokens,
                   COALESCE(SUM(estimated_cost_usd), 0) as estimated_cost,
                   COUNT(*) as sessions,
                   SUM(COALESCE(api_call_count, 0)) as api_calls
            FROM sessions WHERE started_at > ? AND model IS NOT NULL
            GROUP BY model ORDER BY SUM(input_tokens) + SUM(output_tokens) DESC
        """, (cutoff,))
        by_model = [dict(r) for r in cur2.fetchall()]

        cur3 = db._conn.execute("""
            SELECT SUM(input_tokens) as total_input,
                   SUM(output_tokens) as total_output,
                   SUM(cache_read_tokens) as total_cache_read,
                   SUM(reasoning_tokens) as total_reasoning,
                   COALESCE(SUM(estimated_cost_usd), 0) as total_estimated_cost,
                   COALESCE(SUM(actual_cost_usd), 0) as total_actual_cost,
                   COUNT(*) as total_sessions,
                   SUM(COALESCE(api_call_count, 0)) as total_api_calls
            FROM sessions WHERE started_at > ?
        """, (cutoff,))
        totals = dict(cur3.fetchone())
        insights_report = InsightsEngine(db).generate(days=days)
        skills = insights_report.get("skills", {
            "summary": {
                "total_skill_loads": 0,
                "total_skill_edits": 0,
                "total_skill_actions": 0,
                "distinct_skills_used": 0,
            },
            "top_skills": [],
        })

        return {
            "daily": daily,
            "by_model": by_model,
            "totals": totals,
            "period_days": days,
            "skills": skills,
        }
    finally:
        db.close()


@app.get("/api/analytics/models")
async def get_models_analytics(days: int = 30):
    """Rich per-model analytics for the Models dashboard page.

    Returns token/cost/session breakdown per model plus capability metadata
    from models.dev (context window, vision, tools, reasoning, etc.).
    """
    from hermes_state import SessionDB

    db = SessionDB()
    try:
        cutoff = time.time() - (days * 86400)

        cur = db._conn.execute("""
            SELECT model,
                   billing_provider,
                   SUM(input_tokens) as input_tokens,
                   SUM(output_tokens) as output_tokens,
                   SUM(cache_read_tokens) as cache_read_tokens,
                   SUM(reasoning_tokens) as reasoning_tokens,
                   COALESCE(SUM(estimated_cost_usd), 0) as estimated_cost,
                   COALESCE(SUM(actual_cost_usd), 0) as actual_cost,
                   COUNT(*) as sessions,
                   SUM(COALESCE(api_call_count, 0)) as api_calls,
                   SUM(tool_call_count) as tool_calls,
                   MAX(started_at) as last_used_at,
                   AVG(input_tokens + output_tokens) as avg_tokens_per_session
            FROM sessions WHERE started_at > ? AND model IS NOT NULL AND model != ''
            GROUP BY model, billing_provider
            ORDER BY SUM(input_tokens) + SUM(output_tokens) DESC
        """, (cutoff,))
        rows = [dict(r) for r in cur.fetchall()]

        models = []
        for row in rows:
            provider = row.get("billing_provider") or ""
            model_name = row["model"]
            caps = {}
            try:
                from agent.models_dev import get_model_capabilities
                mc = get_model_capabilities(provider=provider, model=model_name)
                if mc is not None:
                    caps = {
                        "supports_tools": mc.supports_tools,
                        "supports_vision": mc.supports_vision,
                        "supports_reasoning": mc.supports_reasoning,
                        "context_window": mc.context_window,
                        "max_output_tokens": mc.max_output_tokens,
                        "model_family": mc.model_family,
                    }
            except Exception:
                pass

            models.append({
                "model": model_name,
                "provider": provider,
                "input_tokens": row["input_tokens"],
                "output_tokens": row["output_tokens"],
                "cache_read_tokens": row["cache_read_tokens"],
                "reasoning_tokens": row["reasoning_tokens"],
                "estimated_cost": row["estimated_cost"],
                "actual_cost": row["actual_cost"],
                "sessions": row["sessions"],
                "api_calls": row["api_calls"],
                "tool_calls": row["tool_calls"],
                "last_used_at": row["last_used_at"],
                "avg_tokens_per_session": row["avg_tokens_per_session"],
                "capabilities": caps,
            })

        totals_cur = db._conn.execute("""
            SELECT COUNT(DISTINCT model) as distinct_models,
                   SUM(input_tokens) as total_input,
                   SUM(output_tokens) as total_output,
                   SUM(cache_read_tokens) as total_cache_read,
                   SUM(reasoning_tokens) as total_reasoning,
                   COALESCE(SUM(estimated_cost_usd), 0) as total_estimated_cost,
                   COALESCE(SUM(actual_cost_usd), 0) as total_actual_cost,
                   COUNT(*) as total_sessions,
                   SUM(COALESCE(api_call_count, 0)) as total_api_calls
            FROM sessions WHERE started_at > ? AND model IS NOT NULL AND model != ''
        """, (cutoff,))
        totals = dict(totals_cur.fetchone())

        return {
            "models": models,
            "totals": totals,
            "period_days": days,
        }
    finally:
        db.close()


# ---------------------------------------------------------------------------
# /api/pty — PTY-over-WebSocket bridge for the dashboard "Chat" tab.
#
# The endpoint spawns the same ``hermes --tui`` binary the CLI uses, behind
# a POSIX pseudo-terminal, and forwards bytes + resize escapes across a
# WebSocket.  The browser renders the ANSI through xterm.js (see
# web/src/pages/ChatPage.tsx).
#
# Auth: ``?token=<session_token>`` query param (browsers can't set
# Authorization on the WS upgrade).  Same ephemeral ``_SESSION_TOKEN`` as
# REST.  Localhost-only — we defensively reject non-loopback clients even
# though uvicorn binds to 127.0.0.1.
# ---------------------------------------------------------------------------

# PTY bridge is POSIX-only (depends on fcntl/termios/ptyprocess).  On native
# Windows the import raises; catch and leave PtyBridge=None so the rest of
# the dashboard (sessions, jobs, metrics, config editor) still loads and the
# /api/pty endpoint cleanly refuses with a WSL-suggested message.
try:
    from hermes_cli.pty_bridge import PtyBridge, PtyUnavailableError
    _PTY_BRIDGE_AVAILABLE = True
except ImportError as _pty_import_err:  # pragma: no cover - Windows-only path
    PtyBridge = None  # type: ignore[assignment]
    _PTY_BRIDGE_AVAILABLE = False

    class PtyUnavailableError(RuntimeError):  # type: ignore[no-redef]
        """Stub on platforms where pty_bridge can't be imported."""
        pass

_RESIZE_RE = re.compile(rb"\x1b\[RESIZE:(\d+);(\d+)\]")
_PTY_READ_CHUNK_TIMEOUT = 0.2
_VALID_CHANNEL_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
# Starlette's TestClient reports the peer as "testclient"; treat it as
# loopback so tests don't need to rewrite request scope.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})


def _ws_client_reason(ws: "WebSocket") -> Optional[str]:
    """Return a rejection reason for the client IP, or None when allowed.

    Reasons are short machine-parseable tokens logged on the rejection path
    so a "WS keeps closing" report can be diagnosed from agent.log without a
    repro. ``None`` means the peer IP passed this gate.

    See :func:`_ws_client_is_allowed` for the full policy rationale.
    """
    if getattr(app.state, "auth_required", False):
        return None
    bound_host = (getattr(app.state, "bound_host", "") or "").strip().lower()
    if bound_host and bound_host not in _LOOPBACK_HOSTS:
        return None
    client_host = ws.client.host if ws.client else ""
    if not client_host:
        return None
    if client_host in _LOOPBACK_HOSTS:
        return None
    return f"peer_not_loopback peer={client_host} bound={bound_host or '?'}"


def _ws_client_is_allowed(ws: "WebSocket") -> bool:
    """Check if the WebSocket client IP is acceptable.

    Loopback bind: only loopback clients allowed — the legacy
    ``?token=<_SESSION_TOKEN>`` path is the only auth we have, so we
    don't want LAN hosts guessing tokens.

    Explicit non-loopback bind (``--host 0.0.0.0``, ``--host ::``, or a
    specific address such as a Tailscale/LAN IP, always with
    ``--insecure``): allow any peer. The operator explicitly opted into
    non-loopback exposure, so the loopback-only peer restriction does not
    apply. DNS-rebinding is still blocked by the Host/Origin guard in
    :func:`_ws_host_origin_is_allowed`, which mirrors the HTTP layer and
    requires the Host header to match the bound interface — the same
    defence ``_is_accepted_host`` applies to non-loopback HTTP requests.

    Gated mode: any peer is allowed — uvicorn's ``proxy_headers=True``
    (enabled when the OAuth gate is active so cookies can pick up
    ``X-Forwarded-Proto``) rewrites ``ws.client.host`` to the
    X-Forwarded-For value, which is the real internet client IP. The
    OAuth gate + single-use ``?ticket=`` is the auth at that point; the
    Host/Origin guard in :func:`_ws_host_origin_is_allowed` is what
    blocks DNS-rebinding here, not the peer IP.
    """
    if getattr(app.state, "auth_required", False):
        return True
    # Any explicit non-loopback bind (0.0.0.0, ::, or a specific LAN /
    # Tailscale address) means the operator opted into non-loopback
    # access via --insecure.  The loopback-only peer gate only applies to
    # an actual loopback bind; otherwise the WS handshake is rejected even
    # though same-bind HTTP requests pass _is_accepted_host.
    bound_host = (getattr(app.state, "bound_host", "") or "").strip().lower()
    if bound_host and bound_host not in _LOOPBACK_HOSTS:
        return True
    client_host = ws.client.host if ws.client else ""
    if not client_host:
        return True
    return client_host in _LOOPBACK_HOSTS


def _ws_host_origin_reason(ws: "WebSocket") -> Optional[str]:
    """Return a Host/Origin rejection reason, or None when allowed.

    Mirrors :func:`_ws_host_origin_is_allowed` but yields a short
    machine-parseable token (``host_mismatch …`` / ``origin_mismatch …``)
    on rejection so the close path can log *why* the upgrade was refused.
    """
    bound_host = getattr(app.state, "bound_host", None)
    if not bound_host:
        return None

    host_header = ws.headers.get("host", "")
    if not _is_accepted_host(host_header, bound_host):
        return f"host_mismatch host={host_header or '?'} bound={bound_host}"

    origin = ws.headers.get("origin", "")
    if not origin:
        return None

    parsed = urllib.parse.urlparse(origin)
    if parsed.scheme not in {"http", "https"}:
        # Non-web origin (packaged Electron: file://, null, app://). The
        # upstream credential check is the real auth boundary; trust it.
        # See _ws_host_origin_is_allowed for the full rationale.
        return None

    if not parsed.netloc:
        return f"origin_mismatch origin={origin} bound={bound_host}"

    if not _is_accepted_host(parsed.netloc, bound_host):
        return f"origin_mismatch origin={origin} bound={bound_host}"
    return None


def _ws_host_origin_is_allowed(ws: "WebSocket") -> bool:
    """Apply the dashboard Host/Origin guard to WebSocket upgrades.

    FastAPI HTTP middleware does not run for WebSocket routes, so the
    DNS-rebinding Host check used for normal dashboard HTTP requests must be
    repeated here before accepting the upgrade.  Browsers also send an Origin
    header on WebSocket handshakes; when present, require it to target the
    same bound dashboard host.
    """
    return _ws_host_origin_reason(ws) is None


def _ws_request_reason(ws: "WebSocket") -> Optional[str]:
    """First Host/Origin or peer-IP rejection reason, or None when allowed."""
    return _ws_host_origin_reason(ws) or _ws_client_reason(ws)


def _ws_request_is_allowed(ws: "WebSocket") -> bool:
    """Return True when the WebSocket upgrade matches dashboard boundaries."""
    return _ws_host_origin_is_allowed(ws) and _ws_client_is_allowed(ws)


def _ws_auth_mode() -> str:
    """Short label for the active WS auth mode — logged on every connection."""
    if getattr(app.state, "auth_required", False):
        return "gated"
    bound_host = (getattr(app.state, "bound_host", "") or "").strip().lower()
    if bound_host and bound_host not in _LOOPBACK_HOSTS:
        return "insecure"
    return "loopback"


def _ws_auth_reason(ws: "WebSocket") -> tuple[Optional[str], str]:
    """Validate WS-upgrade auth; return ``(reason, credential)``.

    ``reason`` is None when the credential is accepted, else a short
    machine-parseable token explaining the rejection (``no_credential``,
    ``token_mismatch``, ``ticket_invalid``, ``internal_invalid``).
    ``credential`` names which credential type was presented (``ticket``,
    ``internal``, ``token``, or ``none``) so the accepted path can log *how*
    a peer authed, not just that it did.

    Loopback / ``--insecure``: legacy ``?token=<_SESSION_TOKEN>`` query
    parameter, constant-time compared.

    Gated (public bind, no ``--insecure``): one of two credentials —

    * ``?ticket=<single-use>`` — a browser-minted, single-use, 30s-TTL ticket
      consumed against the dashboard-auth ticket store. This is what the SPA
      (and native clients) use.
    * ``?internal=<process-credential>`` — the process-lifetime internal
      credential, used only by WS clients the server spawns itself (the
      embedded-TUI PTY child attaching to ``/api/ws`` and ``/api/pub``). It
      is multi-use and never expires so the child can reconnect, and is never
      injected into the SPA — see ``dashboard_auth.ws_tickets`` for the
      threat model.

    The legacy ``?token=`` path is unconditionally rejected in gated mode
    (the SPA bundle isn't carrying the token any longer, and a leaked
    ``_SESSION_TOKEN`` must not grant WS access once the gate is engaged).

    Audit-logs the rejection so operators can debug "WS keeps closing"
    issues from the log.
    """
    auth_required = bool(getattr(app.state, "auth_required", False))
    if auth_required:
        # Lazy import — keeps this function importable in test harnesses
        # that don't bring in the dashboard_auth layer.
        from hermes_cli.dashboard_auth.audit import AuditEvent, audit_log
        from hermes_cli.dashboard_auth.ws_tickets import (
            TicketInvalid,
            consume_internal_credential,
            consume_ticket,
        )

        # Server-spawned children (PTY child → /api/ws, /api/pub) present the
        # multi-use internal credential rather than a single-use ticket, so
        # they survive reconnects and slow cold boots.
        internal = ws.query_params.get("internal", "")
        if internal:
            try:
                consume_internal_credential(internal)
                return None, "internal"
            except TicketInvalid as exc:
                audit_log(
                    AuditEvent.WS_TICKET_REJECTED,
                    reason=f"internal: {exc}",
                    ip=(ws.client.host if ws.client else ""),
                    path=ws.url.path,
                )
                return "internal_invalid", "internal"

        ticket = ws.query_params.get("ticket", "")
        if not ticket:
            return "no_credential", "none"

        try:
            consume_ticket(ticket)
            return None, "ticket"
        except TicketInvalid as exc:
            audit_log(
                AuditEvent.WS_TICKET_REJECTED,
                reason=str(exc),
                ip=(ws.client.host if ws.client else ""),
                path=ws.url.path,
            )
            return "ticket_invalid", "ticket"

    token = ws.query_params.get("token", "")
    if not token:
        return "no_credential", "none"
    if hmac.compare_digest(token.encode(), _SESSION_TOKEN.encode()):
        return None, "token"
    return "token_mismatch", "token"


def _ws_auth_ok(ws: "WebSocket") -> bool:
    """True when the WS-upgrade credential is accepted. See _ws_auth_reason."""
    return _ws_auth_reason(ws)[0] is None

# Per-channel subscriber registry used by /api/pub (PTY-side gateway → dashboard)
# and /api/events (dashboard → browser sidebar).  Keyed by an opaque channel id
# the chat tab generates on mount; entries auto-evict when the last subscriber
# drops AND the publisher has disconnected.
# (State is initialised in _lifespan on app startup — see above.)


def _resolve_chat_argv(
    resume: Optional[str] = None,
    sidecar_url: Optional[str] = None,
) -> tuple[list[str], Optional[str], Optional[dict]]:
    """Resolve the argv + cwd + env for the chat PTY.

    Default: whatever ``hermes --tui`` would run.  Tests monkeypatch this
    function to inject a tiny fake command (``cat``, ``sh -c 'printf …'``)
    so nothing has to build Node or the TUI bundle.

    Session resume is propagated via the ``HERMES_TUI_RESUME`` env var —
    matching what ``hermes_cli.main._launch_tui`` does for the CLI path.
    Appending ``--resume <id>`` to argv doesn't work because ``ui-tui`` does
    not parse its argv.

    ``HERMES_TUI_GATEWAY_URL`` is injected so the PTY child can attach to
    this process's in-memory ``tui_gateway`` instance instead of spawning
    its own Python gateway subprocess.

    `sidecar_url` (when set) is forwarded as ``HERMES_TUI_SIDECAR_URL`` so
    the spawned ``tui_gateway.entry`` can mirror dispatcher emits to the
    dashboard's ``/api/pub`` endpoint (see :func:`pub_ws`).
    """
    from hermes_cli.main import PROJECT_ROOT, _make_tui_argv

    argv, cwd = _make_tui_argv(PROJECT_ROOT / "ui-tui", tui_dev=False)
    env = os.environ.copy()
    env.setdefault("NODE_ENV", "production")
    # Browser-embedded chat should prefer stable wheel-based scrollback over
    # native terminal mouse tracking. When mouse tracking is enabled, wheel
    # events are consumed by the TUI and forwarded as terminal input, which
    # makes browser-side transcript scrolling feel broken. Keep the terminal
    # build unchanged for native CLI usage; only disable mouse tracking for
    # the dashboard PTY path.
    env.setdefault("HERMES_TUI_DISABLE_MOUSE", "1")
    env.setdefault("HERMES_TUI_INLINE", "1")

    if resume:
        latest_resume, _latest_path = _session_latest_descendant(resume)
        if latest_resume:
            resume = latest_resume
        env["HERMES_TUI_RESUME"] = resume

    if sidecar_url:
        env["HERMES_TUI_SIDECAR_URL"] = sidecar_url

    if gateway_ws_url := _build_gateway_ws_url():
        env["HERMES_TUI_GATEWAY_URL"] = gateway_ws_url

    return list(argv), str(cwd) if cwd else None, env


def _build_gateway_ws_url() -> Optional[str]:
    """ws:// URL the PTY child should attach to for JSON-RPC gateway traffic.

    Loopback / ``--insecure``: ``?token=<_SESSION_TOKEN>``.

    Gated mode: the legacy token path is rejected by ``_ws_auth_ok``, so the
    server-spawned PTY child authenticates with the process-lifetime internal
    credential (``?internal=``). It must NOT use a single-use browser ticket:
    the child reads this URL once at startup and reuses it on every reconnect,
    and a 30s-TTL ticket can expire before a slow cold boot even dials.
    """
    host = getattr(app.state, "bound_host", None)
    port = getattr(app.state, "bound_port", None)

    if not host or not port:
        return None

    netloc = (
        f"[{host}]:{port}"
        if ":" in host and not host.startswith("[")
        else f"{host}:{port}"
    )

    if getattr(app.state, "auth_required", False):
        from hermes_cli.dashboard_auth.ws_tickets import internal_ws_credential

        qs = urllib.parse.urlencode({"internal": internal_ws_credential()})
    else:
        qs = urllib.parse.urlencode({"token": _SESSION_TOKEN})

    return f"ws://{netloc}/api/ws?{qs}"


def _build_sidecar_url(channel: str) -> Optional[str]:
    """ws:// URL the PTY child should publish events to, or None when unbound.

    Loopback / ``--insecure``: uses ``?token=<_SESSION_TOKEN>``.

    Gated mode: authenticates with the process-lifetime internal credential
    (``?internal=``), the same one ``_build_gateway_ws_url`` uses. The PTY
    child is a server-spawned process we trust; the credential is multi-use
    and never expires, so the child can reconnect ``/api/pub`` without a new
    URL. (This previously minted a single-use 30s ticket, which meant the
    child could not reconnect and could miss the window on a slow cold boot.)
    Connections authenticated this way are recorded under the
    ``server-internal`` identity in the audit log.
    """
    host = getattr(app.state, "bound_host", None)
    port = getattr(app.state, "bound_port", None)

    if not host or not port:
        return None

    netloc = f"[{host}]:{port}" if ":" in host and not host.startswith("[") else f"{host}:{port}"

    if getattr(app.state, "auth_required", False):
        # Gated mode — use the internal credential so the WS upgrade survives
        # _ws_auth_ok and the child can reconnect.
        from hermes_cli.dashboard_auth.ws_tickets import internal_ws_credential

        qs = urllib.parse.urlencode(
            {"internal": internal_ws_credential(), "channel": channel}
        )
    else:
        qs = urllib.parse.urlencode({"token": _SESSION_TOKEN, "channel": channel})

    return f"ws://{netloc}/api/pub?{qs}"


async def _broadcast_event(app: Any, channel: str, payload: str) -> None:
    """Fan out one publisher frame to every subscriber on `channel`."""
    event_channels, event_lock = _get_event_state(app)
    async with event_lock:
        subs = list(event_channels.get(channel, ()))

    for sub in subs:
        try:
            await sub.send_text(payload)
        except Exception:
            # Subscriber went away mid-send; the /api/events finally clause
            # will remove it from the registry on its next iteration.
            _log.warning("broadcast send failed for subscriber on %s", channel, exc_info=True)


def _channel_or_close_code(ws: WebSocket) -> Optional[str]:
    """Return the channel id from the query string or None if invalid."""
    channel = ws.query_params.get("channel", "")

    return channel if _VALID_CHANNEL_RE.match(channel) else None


def _ws_close_reason(text: str) -> str:
    """Clamp a WS close reason to the protocol's 123-byte UTF-8 limit.

    RFC 6455 caps the close-frame reason at 123 bytes; uvicorn raises if a
    longer string is passed. Our reasons embed an attacker-controlled origin,
    so truncate defensively rather than crash the close handler.
    """
    encoded = text.encode("utf-8", "replace")
    if len(encoded) <= 123:
        return text
    return encoded[:120].decode("utf-8", "ignore") + "..."


@app.websocket("/api/pty")
async def pty_ws(ws: WebSocket) -> None:
    peer = ws.client.host if ws.client else "?"

    # The raw /api/pty terminal spawns a full `hermes --tui` PTY (slash
    # commands like /config, /model, shell access). It is strictly larger than
    # the structured customer chat (/api/ws) and must not be reachable in
    # customer "assistant" mode, where the HTTP /api admin allowlist already
    # blocks the equivalent surfaces. (WS routes bypass the HTTP middleware, so
    # this gate is enforced here directly.)
    if _assistant_mode_enabled():
        _log.warning("pty refused: raw terminal not available in assistant mode peer=%s", peer)
        await ws.close(code=4403, reason="pty disabled in assistant mode")
        return

    if not _DASHBOARD_EMBEDDED_CHAT_ENABLED:
        _log.info("pty refused: embedded chat disabled peer=%s", peer)
        await ws.close(code=4404, reason="embedded chat disabled")
        return

    # --- auth + host/origin/peer check (before accept so we can close
    #     cleanly AND tell the client WHY via the close code + reason).
    #     Each gate maps to a distinct close code so the log and the
    #     browser banner agree on the cause:
    #       4401 bad credential   4403 host/origin mismatch
    #       4408 peer not allowed  4404 chat disabled
    auth_reason, cred = _ws_auth_reason(ws)
    mode = _ws_auth_mode()
    if auth_reason is not None:
        _log.warning(
            "pty auth rejected reason=%s mode=%s cred=%s peer=%s",
            auth_reason, mode, cred, peer,
        )
        await ws.close(code=4401, reason=_ws_close_reason(f"auth: {auth_reason}"))
        return

    host_origin_reason = _ws_host_origin_reason(ws)
    if host_origin_reason is not None:
        _log.warning("pty refused: %s peer=%s", host_origin_reason, peer)
        await ws.close(code=4403, reason=_ws_close_reason(host_origin_reason))
        return

    client_reason = _ws_client_reason(ws)
    if client_reason is not None:
        _log.warning("pty refused: %s", client_reason)
        await ws.close(code=4408, reason=_ws_close_reason(client_reason))
        return

    await ws.accept()
    _log.info("pty accepted peer=%s mode=%s cred=%s", peer, mode, cred)

    # On native Windows, the POSIX PTY bridge can't be imported.  Tell the
    # client and close cleanly rather than pretending the feature works.
    if not _PTY_BRIDGE_AVAILABLE:
        await ws.send_text(
            "\r\n\x1b[31mChat unavailable: the embedded terminal requires a "
            "POSIX PTY, which native Windows Python doesn't provide.\x1b[0m\r\n"
            "\x1b[33mInstall Hermes inside WSL2 to use the dashboard's /chat "
            "tab — the rest of the dashboard works here.\x1b[0m\r\n"
        )
        await ws.close(code=1011)
        return

    # --- spawn PTY ------------------------------------------------------
    resume = ws.query_params.get("resume") or None
    channel = _channel_or_close_code(ws)
    sidecar_url = _build_sidecar_url(channel) if channel else None

    try:
        argv, cwd, env = _resolve_chat_argv(resume=resume, sidecar_url=sidecar_url)
    except SystemExit as exc:
        # _make_tui_argv calls sys.exit(1) when node/npm is missing.
        await ws.send_text(f"\r\n\x1b[31mChat unavailable: {exc}\x1b[0m\r\n")
        await ws.close(code=1011)
        return


    try:
        bridge = PtyBridge.spawn(argv, cwd=cwd, env=env)
    except PtyUnavailableError as exc:
        await ws.send_text(f"\r\n\x1b[31mChat unavailable: {exc}\x1b[0m\r\n")
        await ws.close(code=1011)
        return
    except (FileNotFoundError, OSError) as exc:
        await ws.send_text(f"\r\n\x1b[31mChat failed to start: {exc}\x1b[0m\r\n")
        await ws.close(code=1011)
        return

    loop = asyncio.get_running_loop()

    # --- reader task: PTY master → WebSocket ----------------------------
    async def pump_pty_to_ws() -> None:
        while True:
            chunk = await loop.run_in_executor(
                None, bridge.read, _PTY_READ_CHUNK_TIMEOUT
            )
            if chunk is None:  # EOF
                return
            if not chunk:  # no data this tick; yield control and retry
                await asyncio.sleep(0)
                continue
            try:
                await ws.send_bytes(chunk)
            except Exception:
                return

    reader_task = asyncio.create_task(pump_pty_to_ws())

    # --- writer loop: WebSocket → PTY master ----------------------------
    try:
        while True:
            msg = await ws.receive()
            msg_type = msg.get("type")
            if msg_type == "websocket.disconnect":
                break
            raw = msg.get("bytes")
            if raw is None:
                text = msg.get("text")
                raw = text.encode("utf-8") if isinstance(text, str) else b""
            if not raw:
                continue

            # Resize escape is consumed locally, never written to the PTY.
            match = _RESIZE_RE.match(raw)
            if match and match.end() == len(raw):
                cols = int(match.group(1))
                rows = int(match.group(2))
                bridge.resize(cols=cols, rows=rows)
                continue

            bridge.write(raw)
    except WebSocketDisconnect:
        pass
    finally:
        reader_task.cancel()
        try:
            await reader_task
        except (asyncio.CancelledError, Exception):
            pass
        bridge.close()


# ---------------------------------------------------------------------------
# /api/ws — JSON-RPC WebSocket sidecar for the dashboard "Chat" tab.
#
# Drives the same `tui_gateway.dispatch` surface Ink uses over stdio, so the
# dashboard can render structured metadata (model badge, tool-call sidebar,
# slash launcher, session info) alongside the xterm.js terminal that PTY
# already paints. Both transports bind to the same session id when one is
# active, so a tool.start emitted by the agent fans out to both sinks.
# ---------------------------------------------------------------------------


@app.websocket("/api/ws")
async def gateway_ws(ws: WebSocket) -> None:
    if not _DASHBOARD_EMBEDDED_CHAT_ENABLED:
        await ws.close(code=4403)
        return

    if not _ws_auth_ok(ws):
        await ws.close(code=4401)
        return

    if not _ws_request_is_allowed(ws):
        await ws.close(code=4403)
        return

    from tui_gateway.ws import handle_ws

    await handle_ws(ws)


# ---------------------------------------------------------------------------
# /api/pub + /api/events — chat-tab event broadcast.
#
# The PTY-side ``tui_gateway.entry`` opens /api/pub at startup (driven by
# HERMES_TUI_SIDECAR_URL set in /api/pty's PTY env) and writes every
# dispatcher emit through it.  The dashboard fans those frames out to any
# subscriber that opened /api/events on the same channel id.  This is what
# gives the React sidebar its tool-call feed without breaking the PTY
# child's stdio handshake with Ink.
# ---------------------------------------------------------------------------


@app.websocket("/api/pub")
async def pub_ws(ws: WebSocket) -> None:
    if not _DASHBOARD_EMBEDDED_CHAT_ENABLED:
        await ws.close(code=4403)
        return

    if not _ws_auth_ok(ws):
        await ws.close(code=4401)
        return

    if not _ws_request_is_allowed(ws):
        await ws.close(code=4403)
        return

    channel = _channel_or_close_code(ws)
    if not channel:
        await ws.close(code=4400)
        return

    await ws.accept()

    try:
        while True:
            await _broadcast_event(ws.app, channel, await ws.receive_text())
    except WebSocketDisconnect:
        pass


@app.websocket("/api/events")
async def events_ws(ws: WebSocket) -> None:
    if not _DASHBOARD_EMBEDDED_CHAT_ENABLED:
        await ws.close(code=4403)
        return

    if not _ws_auth_ok(ws):
        await ws.close(code=4401)
        return

    if not _ws_request_is_allowed(ws):
        await ws.close(code=4403)
        return

    channel = _channel_or_close_code(ws)
    if not channel:
        await ws.close(code=4400)
        return

    await ws.accept()

    event_channels, event_lock = _get_event_state(ws.app)
    async with event_lock:
        event_channels.setdefault(channel, set()).add(ws)

    try:
        while True:
            # Subscribers don't speak — the receive() just blocks until
            # disconnect so the connection stays open as long as the
            # browser holds it.
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        async with event_lock:
            subs = event_channels.get(channel)

            if subs is not None:
                subs.discard(ws)

                if not subs:
                    event_channels.pop(channel, None)


def _normalise_prefix(raw: Optional[str]) -> str:
    """Normalise an X-Forwarded-Prefix header value.

    Thin re-export of :func:`hermes_cli.dashboard_auth.prefix.normalise_prefix`
    — the single source of truth lives in the dashboard_auth package so
    the gate middleware, the OAuth routes, the cookie helpers, and the
    SPA mount all agree on validation rules.
    """
    from hermes_cli.dashboard_auth.prefix import normalise_prefix
    return normalise_prefix(raw)


def mount_spa(application: FastAPI):
    """Mount the built SPA. Falls back to index.html for client-side routing.

    The session token is injected into index.html via a ``<script>`` tag so
    the SPA can authenticate against protected API endpoints without a
    separate (unauthenticated) token-dispensing endpoint.

    When served behind a path-prefix reverse proxy (e.g.
    ``mission-control.tilos.com/hermes/*`` -> local Caddy -> :9119), the
    proxy injects ``X-Forwarded-Prefix: /hermes`` on every request. We
    rewrite the served ``index.html`` so absolute asset URLs (``/assets/...``)
    and the SPA's runtime ``__HERMES_BASE_PATH__`` honour that prefix
    without rebuilding the bundle.
    """
    if not WEB_DIST.exists():
        @application.get("/{full_path:path}")
        async def no_frontend(full_path: str):
            return JSONResponse(
                {"error": "Frontend not built. Run: cd web && npm run build"},
                status_code=404,
            )
        return

    _index_path = WEB_DIST / "index.html"

    def _serve_index(prefix: str = ""):
        """Return index.html with the session token + base-path injected.

        ``prefix`` is the normalised ``X-Forwarded-Prefix`` (e.g. ``/hermes``)
        or empty string when served at root.

        When the OAuth auth gate is active (``app.state.auth_required``),
        the legacy ``_SESSION_TOKEN`` is NOT injected — the SPA reads
        identity from ``/api/auth/me`` over cookie auth instead.  The
        ``__HERMES_AUTH_REQUIRED__`` flag lets the SPA pick the right
        auth scheme for /api/pty and /api/ws (ticket vs token).
        """
        html = _index_path.read_text()
        chat_js = "true" if _DASHBOARD_EMBEDDED_CHAT_ENABLED else "false"
        mode = _DASHBOARD_MODE if _DASHBOARD_MODE in {"admin", "assistant"} else "admin"
        gated = bool(getattr(app.state, "auth_required", False))
        gated_js = "true" if gated else "false"
        user_display_name_js = json.dumps(_assistant_user_display_name() if mode == "assistant" else None)
        common_bootstrap = (
            f"window.__HERMES_DASHBOARD_EMBEDDED_CHAT__={chat_js};"
            f'window.__HERMES_DASHBOARD_MODE__="{mode}";'
            f'window.__HERMES_BASE_PATH__="{prefix}";'
            f"window.__HERMES_AUTH_REQUIRED__={gated_js};"
            f"window.__HERMES_USER_DISPLAY_NAME__={user_display_name_js};"
        )
        if gated:
            bootstrap_script = f"<script>{common_bootstrap}</script>"
        else:
            bootstrap_script = (
                f'<script>window.__HERMES_SESSION_TOKEN__="{_SESSION_TOKEN}";'
                f"{common_bootstrap}</script>"
            )
        if prefix:
            # Rewrite absolute asset URLs baked into the Vite build so the
            # browser fetches them through the same proxy prefix.
            html = html.replace('href="/assets/', f'href="{prefix}/assets/')
            html = html.replace('src="/assets/', f'src="{prefix}/assets/')
            html = html.replace('href="/favicon.ico"', f'href="{prefix}/favicon.ico"')
            html = html.replace('href="/fonts/', f'href="{prefix}/fonts/')
            html = html.replace('href="/ds-assets/', f'href="{prefix}/ds-assets/')
            html = html.replace('src="/ds-assets/', f'src="{prefix}/ds-assets/')
        html = html.replace("</head>", f"{bootstrap_script}</head>", 1)
        return HTMLResponse(
            html,
            headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
        )

    # When served behind a path-prefix proxy, the built CSS contains
    # absolute ``url(/fonts/...)`` and ``url(/ds-assets/...)`` references.
    # Browsers resolve those against the document origin, which means
    # under ``/hermes`` they'd hit ``mission-control.tilos.com/fonts/...``
    # (the MC Pages app), not the Hermes backend. Intercept CSS asset
    # requests BEFORE the StaticFiles mount and rewrite the absolute paths
    # when a prefix is in play.
    @application.get("/assets/{filename}.css")
    async def serve_css(filename: str, request: Request):
        css_path = WEB_DIST / "assets" / f"{filename}.css"
        if not css_path.is_file() or not css_path.resolve().is_relative_to(
            WEB_DIST.resolve()
        ):
            return JSONResponse({"error": "not found"}, status_code=404)
        prefix = _normalise_prefix(request.headers.get("x-forwarded-prefix"))
        css = css_path.read_text()
        if prefix:
            for asset_dir in ("/fonts/", "/fonts-terminal/", "/ds-assets/", "/assets/"):
                css = css.replace(f"url({asset_dir}", f"url({prefix}{asset_dir}")
                css = css.replace(f"url(\"{asset_dir}", f"url(\"{prefix}{asset_dir}")
                css = css.replace(f"url('{asset_dir}", f"url('{prefix}{asset_dir}")
        return Response(content=css, media_type="text/css")

    application.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

    @application.get("/{full_path:path}")
    async def serve_spa(full_path: str, request: Request):
        prefix = _normalise_prefix(request.headers.get("x-forwarded-prefix"))
        # An unmatched /api/* path is a missing/renamed endpoint, NOT a
        # client-side route. Falling through to index.html here returns
        # `<!doctype html>` with status 200, which makes JSON clients (the
        # desktop app's fetchJson, dashboard fetch wrappers) blow up with an
        # opaque `SyntaxError: Unexpected token '<'`. Return a real 404 JSON
        # so the caller sees a clear "no such endpoint" instead.
        if full_path == "api" or full_path.startswith("api/"):
            return JSONResponse(
                {"detail": f"No such API endpoint: /{full_path}"},
                status_code=404,
            )
        file_path = WEB_DIST / full_path
        # Prevent path traversal via url-encoded sequences (%2e%2e/)
        if (
            full_path
            and file_path.resolve().is_relative_to(WEB_DIST.resolve())
            and file_path.exists()
            and file_path.is_file()
        ):
            return FileResponse(file_path)
        return _serve_index(prefix)


# ---------------------------------------------------------------------------
# Dashboard theme endpoints
# ---------------------------------------------------------------------------

# Built-in dashboard themes — label + description only.  The actual color
# definitions live in the frontend (web/src/themes/presets.ts).
_BUILTIN_DASHBOARD_THEMES = [
    {"name": "default",       "label": "Hermes Teal",         "description": "Classic dark teal — the canonical Hermes look"},
    {"name": "default-large", "label": "Hermes Teal (Large)", "description": "Hermes Teal with bigger fonts and roomier spacing"},
    {"name": "nous-blue",     "label": "Nous Blue",           "description": "Light mode — vivid Nous-blue accents on cream canvas"},
    {"name": "midnight",      "label": "Midnight",            "description": "Deep blue-violet with cool accents"},
    {"name": "ember",     "label": "Ember",          "description": "Warm crimson and bronze — forge vibes"},
    {"name": "mono",      "label": "Mono",           "description": "Clean grayscale — minimal and focused"},
    {"name": "cyberpunk", "label": "Cyberpunk",      "description": "Neon green on black — matrix terminal"},
    {"name": "rose",      "label": "Rosé",           "description": "Soft pink and warm ivory — easy on the eyes"},
]


def _parse_theme_layer(value: Any, default_hex: str, default_alpha: float = 1.0) -> Optional[Dict[str, Any]]:
    """Normalise a theme layer spec from YAML into `{hex, alpha}` form.

    Accepts shorthand (a bare hex string) or full dict form.  Returns
    ``None`` on garbage input so the caller can fall back to a built-in
    default rather than blowing up.
    """
    if value is None:
        return {"hex": default_hex, "alpha": default_alpha}
    if isinstance(value, str):
        return {"hex": value, "alpha": default_alpha}
    if isinstance(value, dict):
        hex_val = value.get("hex", default_hex)
        alpha_val = value.get("alpha", default_alpha)
        if not isinstance(hex_val, str):
            return None
        try:
            alpha_f = float(alpha_val)
        except (TypeError, ValueError):
            alpha_f = default_alpha
        return {"hex": hex_val, "alpha": max(0.0, min(1.0, alpha_f))}
    return None


_THEME_DEFAULT_TYPOGRAPHY: Dict[str, str] = {
    "fontSans": 'system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif',
    "fontMono": 'ui-monospace, "SF Mono", "Cascadia Mono", Menlo, Consolas, monospace',
    "baseSize": "15px",
    "lineHeight": "1.55",
    "letterSpacing": "0",
}

_THEME_DEFAULT_LAYOUT: Dict[str, str] = {
    "radius": "0.5rem",
    "density": "comfortable",
}

_THEME_OVERRIDE_KEYS = {
    "card", "cardForeground", "popover", "popoverForeground",
    "primary", "primaryForeground", "secondary", "secondaryForeground",
    "muted", "mutedForeground", "accent", "accentForeground",
    "destructive", "destructiveForeground", "success", "warning",
    "border", "input", "ring",
}

# Well-known named asset slots themes can populate.  Any other keys under
# ``assets.custom`` are exposed as ``--theme-asset-custom-<key>`` CSS vars
# for plugin/shell use.
_THEME_NAMED_ASSET_KEYS = {"bg", "hero", "logo", "crest", "sidebar", "header"}

# Component-style buckets themes can override.  The value under each bucket
# is a mapping from camelCase property name to CSS string; each pair emits
# ``--component-<bucket>-<kebab-property>`` on :root.  The frontend's shell
# components (Card, App header, Backdrop, etc.) consume these vars so themes
# can restyle chrome (clip-path, border-image, segmented progress, etc.)
# without shipping their own CSS.
_THEME_COMPONENT_BUCKETS = {
    "card", "header", "footer", "sidebar", "tab",
    "progress", "badge", "backdrop", "page",
}

_THEME_LAYOUT_VARIANTS = {"standard", "cockpit", "tiled"}

# Cap on customCSS length so a malformed/oversized theme YAML can't blow up
# the response payload or the <style> tag.  32 KiB is plenty for every
# practical reskin (the Strike Freedom demo is ~2 KiB).
_THEME_CUSTOM_CSS_MAX = 32 * 1024


def _normalise_theme_definition(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Normalise a user theme YAML into the wire format `ThemeProvider`
    expects.  Returns ``None`` if the theme is unusable.

    Accepts both the full schema (palette/typography/layout) and a loose
    form with bare hex strings, so hand-written YAMLs stay friendly.
    """
    if not isinstance(data, dict):
        return None
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        return None

    # Palette
    palette_src = data.get("palette", {}) if isinstance(data.get("palette"), dict) else {}
    # Allow top-level `colors.background` as a shorthand too.
    colors_src = data.get("colors", {}) if isinstance(data.get("colors"), dict) else {}

    def _layer(key: str, default_hex: str, default_alpha: float = 1.0) -> Dict[str, Any]:
        spec = palette_src.get(key, colors_src.get(key))
        parsed = _parse_theme_layer(spec, default_hex, default_alpha)
        return parsed if parsed is not None else {"hex": default_hex, "alpha": default_alpha}

    palette = {
        "background": _layer("background", "#041c1c", 1.0),
        "midground": _layer("midground", "#ffe6cb", 1.0),
        "foreground": _layer("foreground", "#ffffff", 0.0),
        "warmGlow": palette_src.get("warmGlow") or data.get("warmGlow") or "rgba(255, 189, 56, 0.35)",
        "noiseOpacity": 1.0,
    }
    raw_noise = palette_src.get("noiseOpacity", data.get("noiseOpacity"))
    try:
        palette["noiseOpacity"] = float(raw_noise) if raw_noise is not None else 1.0
    except (TypeError, ValueError):
        palette["noiseOpacity"] = 1.0

    # Typography
    typo_src = data.get("typography", {}) if isinstance(data.get("typography"), dict) else {}
    typography = dict(_THEME_DEFAULT_TYPOGRAPHY)
    for key in ("fontSans", "fontMono", "fontDisplay", "fontUrl", "baseSize", "lineHeight", "letterSpacing"):
        val = typo_src.get(key)
        if isinstance(val, str) and val.strip():
            typography[key] = val

    # Layout
    layout_src = data.get("layout", {}) if isinstance(data.get("layout"), dict) else {}
    layout = dict(_THEME_DEFAULT_LAYOUT)
    radius = layout_src.get("radius")
    if isinstance(radius, str) and radius.strip():
        layout["radius"] = radius
    density = layout_src.get("density")
    if isinstance(density, str) and density in {"compact", "comfortable", "spacious"}:
        layout["density"] = density

    # Color overrides — keep only valid keys with string values.
    overrides_src = data.get("colorOverrides", {})
    color_overrides: Dict[str, str] = {}
    if isinstance(overrides_src, dict):
        for key, val in overrides_src.items():
            if key in _THEME_OVERRIDE_KEYS and isinstance(val, str) and val.strip():
                color_overrides[key] = val

    # Assets — named slots + arbitrary user-defined keys.  Values must be
    # strings (URLs or CSS ``url(...)``/``linear-gradient(...)`` expressions).
    # We don't fetch remote assets here; the frontend just injects them as
    # CSS vars.  Empty values are dropped so a theme can explicitly clear a
    # slot by setting ``hero: ""``.
    assets_out: Dict[str, Any] = {}
    assets_src = data.get("assets", {}) if isinstance(data.get("assets"), dict) else {}
    for key in _THEME_NAMED_ASSET_KEYS:
        val = assets_src.get(key)
        if isinstance(val, str) and val.strip():
            assets_out[key] = val
    custom_assets_src = assets_src.get("custom")
    if isinstance(custom_assets_src, dict):
        custom_assets: Dict[str, str] = {}
        for key, val in custom_assets_src.items():
            if (
                isinstance(key, str)
                and key.replace("-", "").replace("_", "").isalnum()
                and isinstance(val, str)
                and val.strip()
            ):
                custom_assets[key] = val
        if custom_assets:
            assets_out["custom"] = custom_assets

    # Custom CSS — raw CSS text the frontend injects as a scoped <style>
    # tag on theme apply.  Clipped to _THEME_CUSTOM_CSS_MAX to keep the
    # payload bounded.  We intentionally do NOT parse/sanitise the CSS
    # here — the dashboard is localhost-only and themes are user-authored
    # YAML in ~/.hermes/, same trust level as the config file itself.
    custom_css_val = data.get("customCSS")
    custom_css: Optional[str] = None
    if isinstance(custom_css_val, str) and custom_css_val.strip():
        custom_css = custom_css_val[:_THEME_CUSTOM_CSS_MAX]

    # Component style overrides — per-bucket dicts of camelCase CSS
    # property -> CSS string.  The frontend converts these into CSS vars
    # that shell components (Card, App header, Backdrop) consume.
    component_styles_src = data.get("componentStyles", {})
    component_styles: Dict[str, Dict[str, str]] = {}
    if isinstance(component_styles_src, dict):
        for bucket, props in component_styles_src.items():
            if bucket not in _THEME_COMPONENT_BUCKETS or not isinstance(props, dict):
                continue
            clean: Dict[str, str] = {}
            for prop, value in props.items():
                if (
                    isinstance(prop, str)
                    and prop.replace("-", "").replace("_", "").isalnum()
                    and isinstance(value, (str, int, float))
                    and str(value).strip()
                ):
                    clean[prop] = str(value)
            if clean:
                component_styles[bucket] = clean

    layout_variant_src = data.get("layoutVariant")
    layout_variant = (
        layout_variant_src
        if isinstance(layout_variant_src, str) and layout_variant_src in _THEME_LAYOUT_VARIANTS
        else "standard"
    )

    result: Dict[str, Any] = {
        "name": name,
        "label": data.get("label") or name,
        "description": data.get("description", ""),
        "palette": palette,
        "typography": typography,
        "layout": layout,
        "layoutVariant": layout_variant,
    }
    if color_overrides:
        result["colorOverrides"] = color_overrides
    if assets_out:
        result["assets"] = assets_out
    if custom_css is not None:
        result["customCSS"] = custom_css
    if component_styles:
        result["componentStyles"] = component_styles
    return result


def _discover_user_themes() -> list:
    """Scan ~/.hermes/dashboard-themes/*.yaml for user-created themes.

    Returns a list of fully-normalised theme definitions ready to ship
    to the frontend, so the client can apply them without a secondary
    round-trip or a built-in stub.
    """
    themes_dir = get_hermes_home() / "dashboard-themes"
    if not themes_dir.is_dir():
        return []
    result = []
    for f in sorted(themes_dir.glob("*.yaml")):
        try:
            data = yaml.safe_load(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        normalised = _normalise_theme_definition(data)
        if normalised is not None:
            result.append(normalised)
    return result


@app.get("/api/dashboard/themes")
async def get_dashboard_themes():
    """Return available themes and the currently active one.

    Built-in entries ship name/label/description only (the frontend owns
    their full definitions in `web/src/themes/presets.ts`).  User themes
    from `~/.hermes/dashboard-themes/*.yaml` ship with their full
    normalised definition under `definition`, so the client can apply
    them without a stub.
    """
    config = load_config()
    active = cfg_get(config, "dashboard", "theme", default="default")
    user_themes = _discover_user_themes()
    seen = set()
    themes = []
    for t in _BUILTIN_DASHBOARD_THEMES:
        seen.add(t["name"])
        themes.append(t)
    for t in user_themes:
        if t["name"] in seen:
            continue
        themes.append({
            "name": t["name"],
            "label": t["label"],
            "description": t["description"],
            "definition": t,
        })
        seen.add(t["name"])
    return {"themes": themes, "active": active}


class ThemeSetBody(BaseModel):
    name: str


@app.put("/api/dashboard/theme")
async def set_dashboard_theme(body: ThemeSetBody):
    """Set the active dashboard theme (persists to config.yaml)."""
    config = load_config()
    if "dashboard" not in config:
        config["dashboard"] = {}
    config["dashboard"]["theme"] = body.name
    save_config(config)
    return {"ok": True, "theme": body.name}


# ---------------------------------------------------------------------------
# Dashboard plugin system
# ---------------------------------------------------------------------------

def _safe_plugin_api_relpath(api_field: Any, *, dashboard_dir: Path) -> Optional[str]:
    """Validate the manifest's ``api`` field for the plugin loader.

    The web server later imports this file as a Python module via
    ``importlib.util.spec_from_file_location`` (arbitrary code
    execution by design — that's how plugins extend the backend).
    Pre-#29156 the field was used as-is, which meant:

    * An absolute path swallowed the plugin's dashboard directory
      entirely — ``Path('safe/dashboard') / '/tmp/evil.py'`` resolves
      to ``/tmp/evil.py``, so any attacker-controlled manifest could
      point the import at any Python file on disk (GHSA-5qr3-c538-wm9j).
    * A ``../..`` traversal could climb out of the plugin into
      neighbouring directories on the search path.

    Return the original string when the resolved path stays under
    ``dashboard_dir``; return ``None`` (with a warning logged at the
    call site) otherwise so the plugin still loads its static JS/CSS
    but its backend ``api`` is rejected.
    """
    if not isinstance(api_field, str) or not api_field.strip():
        return None
    candidate = Path(api_field)
    if candidate.is_absolute():
        return None
    try:
        resolved = (dashboard_dir / candidate).resolve()
        base = dashboard_dir.resolve()
    except (OSError, RuntimeError):
        return None
    try:
        resolved.relative_to(base)
    except ValueError:
        return None
    return api_field


def _discover_dashboard_plugins() -> list:
    """Scan plugins/*/dashboard/manifest.json for dashboard extensions.

    Checks three plugin sources (same as hermes_cli.plugins):
    1. User plugins:    ~/.hermes/plugins/<name>/dashboard/manifest.json
    2. Bundled plugins: <repo>/plugins/<name>/dashboard/manifest.json  (memory/, etc.)
    3. Project plugins: ./.hermes/plugins/  (only if HERMES_ENABLE_PROJECT_PLUGINS)
    """
    plugins = []
    seen_names: set = set()

    from hermes_cli.plugins import get_bundled_plugins_dir
    bundled_root = get_bundled_plugins_dir()
    search_dirs = [
        (get_hermes_home() / "plugins", "user"),
        (bundled_root / "memory", "bundled"),
        (bundled_root, "bundled"),
    ]
    # GHSA-5qr3-c538-wm9j (#29156): the previous ``os.environ.get(...)``
    # check treated *any* non-empty string as truthy, so ``=0``, ``=false``,
    # and ``=no`` — all of which the agent loader and operators correctly
    # read as "disabled" — silently *enabled* the untrusted project source
    # in the web server.  Combined with the absolute-path RCE primitive on
    # the manifest's ``api`` field (now patched below), this turned the
    # opt-in into a sticky always-on switch.  Use the shared truthy
    # semantics (``1`` / ``true`` / ``yes`` / ``on``) so the gate matches
    # ``hermes_cli/plugins.py`` and the documented user contract.
    if env_var_enabled("HERMES_ENABLE_PROJECT_PLUGINS"):
        search_dirs.append((Path.cwd() / ".hermes" / "plugins", "project"))

    for plugins_root, source in search_dirs:
        if not plugins_root.is_dir():
            continue
        for child in sorted(plugins_root.iterdir()):
            if not child.is_dir():
                continue
            manifest_file = child / "dashboard" / "manifest.json"
            if not manifest_file.exists():
                continue
            try:
                data = json.loads(manifest_file.read_text(encoding="utf-8"))
                name = data.get("name", child.name)
                if name in seen_names:
                    continue
                seen_names.add(name)
                # Tab options: ``path`` + ``position`` for a new tab, optional
                # ``override`` to replace a built-in route, and ``hidden`` to
                # register the plugin component/slots without adding a tab
                # (useful for slot-only plugins like a header-crest injector).
                raw_tab = data.get("tab", {}) if isinstance(data.get("tab"), dict) else {}
                tab_info = {
                    "path": raw_tab.get("path", f"/{name}"),
                    "position": raw_tab.get("position", "end"),
                }
                override_path = raw_tab.get("override")
                if isinstance(override_path, str) and override_path.startswith("/"):
                    tab_info["override"] = override_path
                if bool(raw_tab.get("hidden")):
                    tab_info["hidden"] = True
                # Slots: list of named slot locations this plugin populates.
                # The frontend exposes ``registerSlot(pluginName, slotName, Component)``
                # on window; plugins with non-empty slots call it from their JS bundle.
                slots_src = data.get("slots")
                slots: List[str] = []
                if isinstance(slots_src, list):
                    slots = [s for s in slots_src if isinstance(s, str) and s]
                # Validate ``api`` at discovery time so the value cached
                # on the plugin entry is already safe to feed into the
                # importer.  An attacker-controlled manifest can name
                # any absolute path or ``..`` traversal here — the
                # web server then imports that file as a Python module
                # (RCE, GHSA-5qr3-c538-wm9j).
                raw_api = data.get("api")
                dashboard_dir = child / "dashboard"
                safe_api = _safe_plugin_api_relpath(raw_api, dashboard_dir=dashboard_dir)
                if raw_api and safe_api is None:
                    _log.warning(
                        "Plugin %s: refusing unsafe api path %r (must be a "
                        "relative file inside the plugin's dashboard/ "
                        "directory); backend routes from this plugin will "
                        "not be mounted",
                        name, raw_api,
                    )
                plugins.append({
                    "name": name,
                    "label": data.get("label", name),
                    "description": data.get("description", ""),
                    "icon": data.get("icon", "Puzzle"),
                    "version": data.get("version", "0.0.0"),
                    "tab": tab_info,
                    "slots": slots,
                    "entry": data.get("entry", "dist/index.js"),
                    "css": data.get("css"),
                    "has_api": bool(safe_api),
                    "source": source,
                    "_dir": str(dashboard_dir),
                    "_api_file": safe_api,
                })
            except Exception as exc:
                _log.warning("Bad dashboard plugin manifest %s: %s", manifest_file, exc)
                continue
    return plugins


# Cache discovered plugins per-process (refresh on explicit re-scan).
_dashboard_plugins_cache: Optional[list] = None


def _get_dashboard_plugins(force_rescan: bool = False) -> list:
    global _dashboard_plugins_cache
    if _dashboard_plugins_cache is None or force_rescan:
        _dashboard_plugins_cache = _discover_dashboard_plugins()
    elif _dashboard_plugins_cache:
        if any(not Path(p["_dir"]).is_dir() for p in _dashboard_plugins_cache):
            _dashboard_plugins_cache = _discover_dashboard_plugins()
    return _dashboard_plugins_cache


@app.get("/api/dashboard/plugins")
async def get_dashboard_plugins():
    """Return discovered dashboard plugins (excludes user-hidden ones)."""
    plugins = _get_dashboard_plugins()
    # Read user's hidden plugins list from config.
    config = load_config()
    hidden: list = cfg_get(config, "dashboard", "hidden_plugins", default=[]) or []
    # Strip internal fields before sending to frontend and filter out hidden.
    return [
        {k: v for k, v in p.items() if not k.startswith("_")}
        for p in plugins
        if p["name"] not in hidden
    ]


@app.get("/api/dashboard/plugins/rescan")
async def rescan_dashboard_plugins():
    """Force re-scan of dashboard plugins."""
    plugins = _get_dashboard_plugins(force_rescan=True)
    return {"ok": True, "count": len(plugins)}


class _AgentPluginInstallBody(BaseModel):
    identifier: str
    force: bool = False
    enable: bool = True


def _strip_dashboard_manifest(p: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in p.items() if not k.startswith("_")}


def _merged_plugins_hub() -> Dict[str, Any]:
    """Agent discovery + dashboard manifests + optional provider picker metadata."""
    from hermes_cli.plugins_cmd import (
        _discover_all_plugins,
        _get_current_context_engine,
        _get_current_memory_provider,
        _discover_context_engines,
        _discover_memory_providers,
        _get_disabled_set,
        _get_enabled_set,
        _read_manifest as _read_plugin_manifest_at,
    )

    dashboard_list = _get_dashboard_plugins()
    dash_by_name = {str(p["name"]): p for p in dashboard_list}

    disabled_set = _get_disabled_set()
    enabled_set = _get_enabled_set()

    # Read user-hidden plugins from config for the user_hidden field.
    config = load_config()
    hidden_plugins: list = cfg_get(config, "dashboard", "hidden_plugins", default=[]) or []

    plugins_root_resolved = (get_hermes_home() / "plugins").resolve()
    rows: List[Dict[str, Any]] = []

    for name, version, description, source, dir_str in _discover_all_plugins():
        if name in disabled_set:
            runtime_status = "disabled"
        elif name in enabled_set:
            runtime_status = "enabled"
        else:
            runtime_status = "inactive"

        dir_path = Path(dir_str)
        dm = dash_by_name.get(name)
        has_dash_manifest = dm is not None or (dir_path / "dashboard" / "manifest.json").exists()

        under_user_tree = False
        try:
            dir_path.resolve().relative_to(plugins_root_resolved)
            under_user_tree = True
        except ValueError:
            pass

        can_remove_update = (
            source in {"user", "git"} and under_user_tree and Path(dir_str).is_dir()
        )

        # Check if this plugin provides tools that require auth
        auth_required = False
        auth_command = ""
        manifest_data = _read_plugin_manifest_at(dir_path)
        provides_tools = manifest_data.get("provides_tools") or []
        if provides_tools:
            try:
                from tools.registry import registry
                for tname in provides_tools:
                    entry = registry.get_entry(tname)
                    if entry and entry.check_fn and not entry.check_fn():
                        auth_required = True
                        auth_command = f"hermes auth {name}"
                        break
            except Exception:
                pass

        rows.append({
            "name": name,
            "version": version or "",
            "description": description or "",
            "source": source,
            "runtime_status": runtime_status,
            "has_dashboard_manifest": has_dash_manifest,
            "dashboard_manifest": _strip_dashboard_manifest(dm) if dm else None,
            "path": dir_str,
            "can_remove": can_remove_update,
            "can_update_git": can_remove_update and (Path(dir_str) / ".git").exists(),
            "auth_required": auth_required,
            "auth_command": auth_command,
            "user_hidden": name in hidden_plugins,
        })

    agent_names = {r["name"] for r in rows}
    orphan_dashboard = [
        _strip_dashboard_manifest(p)
        for p in dashboard_list
        if str(p["name"]) not in agent_names
    ]

    memory_providers: List[Dict[str, str]] = []
    try:
        for n, desc in _discover_memory_providers():
            memory_providers.append({"name": n, "description": desc})
    except Exception:
        memory_providers = []

    context_engines: List[Dict[str, str]] = []
    try:
        for n, desc in _discover_context_engines():
            context_engines.append({"name": n, "description": desc})
    except Exception:
        context_engines = []

    return {
        "plugins": rows,
        "orphan_dashboard_plugins": orphan_dashboard,
        "providers": {
            "memory_provider": _get_current_memory_provider() or "",
            "memory_options": memory_providers,
            "context_engine": _get_current_context_engine(),
            "context_options": context_engines,
        },
    }


@app.get("/api/dashboard/plugins/hub")
async def get_plugins_hub(request: Request):
    """Unified agent plugins + dashboard extension metadata (session protected)."""
    _require_token(request)
    try:
        return _merged_plugins_hub()
    except Exception as exc:
        _log.warning("plugins/hub failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to build plugins hub.") from exc


@app.post("/api/dashboard/agent-plugins/install")
async def post_agent_plugin_install(request: Request, body: _AgentPluginInstallBody):
    _require_token(request)
    from hermes_cli.plugins_cmd import dashboard_install_plugin

    result = dashboard_install_plugin(
        body.identifier.strip(),
        force=body.force,
        enable=body.enable,
    )
    if not result.get("ok"):
        raise HTTPException(
            status_code=400,
            detail=result.get("error") or "Install failed.",
        )
    _get_dashboard_plugins(force_rescan=True)
    # Strip internal paths from the response
    result.pop("after_install_path", None)
    return result


def _validate_plugin_name(name: str) -> str:
    """Reject path-traversal attempts in plugin name URL parameters."""
    name = name.strip("/")
    if not name or ".." in name or "\\" in name:
        raise HTTPException(status_code=400, detail="Invalid plugin name.")
    return name


@app.post("/api/dashboard/agent-plugins/{name:path}/enable")
async def post_agent_plugin_enable(request: Request, name: str):
    _require_token(request)
    name = _validate_plugin_name(name)
    from hermes_cli.plugins_cmd import dashboard_set_agent_plugin_enabled

    result = dashboard_set_agent_plugin_enabled(name, enabled=True)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "Enable failed.")
    return result


@app.post("/api/dashboard/agent-plugins/{name:path}/disable")
async def post_agent_plugin_disable(request: Request, name: str):
    _require_token(request)
    name = _validate_plugin_name(name)
    from hermes_cli.plugins_cmd import dashboard_set_agent_plugin_enabled

    result = dashboard_set_agent_plugin_enabled(name, enabled=False)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "Disable failed.")
    return result


@app.post("/api/dashboard/agent-plugins/{name:path}/update")
async def post_agent_plugin_update(request: Request, name: str):
    _require_token(request)
    name = _validate_plugin_name(name)
    from hermes_cli.plugins_cmd import dashboard_update_user_plugin

    result = dashboard_update_user_plugin(name)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "Update failed.")
    _get_dashboard_plugins(force_rescan=True)
    return result


@app.delete("/api/dashboard/agent-plugins/{name:path}")
async def delete_agent_plugin(request: Request, name: str):
    _require_token(request)
    name = _validate_plugin_name(name)
    from hermes_cli.plugins_cmd import dashboard_remove_user_plugin

    result = dashboard_remove_user_plugin(name)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "Remove failed.")
    _get_dashboard_plugins(force_rescan=True)
    return result


class _PluginProvidersPutBody(BaseModel):
    memory_provider: Optional[str] = None
    context_engine: Optional[str] = None


@app.put("/api/dashboard/plugin-providers")
async def put_plugin_providers(request: Request, body: _PluginProvidersPutBody):
    """Persist memory provider / context engine selection (writes config.yaml)."""
    _require_token(request)
    from hermes_cli.plugins_cmd import (
        _save_context_engine,
        _save_memory_provider,
    )

    if body.memory_provider is not None:
        _save_memory_provider(body.memory_provider)
    if body.context_engine is not None:
        _save_context_engine(body.context_engine)
    return {"ok": True}


class _PluginVisibilityBody(BaseModel):
    hidden: bool


@app.post("/api/dashboard/plugins/{name:path}/visibility")
async def post_plugin_visibility(request: Request, name: str, body: _PluginVisibilityBody):
    """Toggle a plugin's sidebar visibility (persists to config.yaml dashboard.hidden_plugins)."""
    _require_token(request)
    name = _validate_plugin_name(name)

    config = load_config()
    if "dashboard" not in config or not isinstance(config.get("dashboard"), dict):
        config["dashboard"] = {}
    hidden_list: list = config["dashboard"].get("hidden_plugins") or []
    if not isinstance(hidden_list, list):
        hidden_list = []

    if body.hidden and name not in hidden_list:
        hidden_list.append(name)
    elif not body.hidden and name in hidden_list:
        hidden_list.remove(name)

    config["dashboard"]["hidden_plugins"] = hidden_list
    save_config(config)
    return {"ok": True, "name": name, "hidden": body.hidden}


@app.get("/dashboard-plugins/{plugin_name}/{file_path:path}")
async def serve_plugin_asset(plugin_name: str, file_path: str):
    """Serve static assets from a dashboard plugin directory.

    Only serves files from the plugin's ``dashboard/`` subdirectory.
    Path traversal is blocked by checking ``resolve().is_relative_to()``.

    Restricted to a browser-fetchable suffix allowlist (JS/CSS/JSON/HTML/
    SVG/PNG/JPG/WOFF). The dashboard loads plugin JS via ``<script src>``
    and CSS via ``<link href>``, neither of which can attach a custom
    auth header — so this route stays unauthenticated to keep the SPA
    working. But user-installed plugins ship a ``plugin_api.py``
    backend module that the browser never fetches; it's only imported
    by :func:`_mount_plugin_api_routes` at startup. Without a suffix
    allowlist, anyone on the loopback port can curl the ``.py`` source
    of a private third-party plugin. Reject everything outside the
    browser-asset set.
    """
    plugins = _get_dashboard_plugins()
    plugin = next((p for p in plugins if p["name"] == plugin_name), None)
    if not plugin:
        raise HTTPException(status_code=404, detail="Plugin not found")

    base = Path(plugin["_dir"])
    target = (base / file_path).resolve()

    if not target.is_relative_to(base.resolve()):
        raise HTTPException(status_code=403, detail="Path traversal blocked")
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    # Browser-asset suffix allowlist. Everything outside this set is
    # rejected with 404 so we don't leak ``.py`` backend sources, README
    # files, ``.env.example`` templates, etc. — none of which the SPA
    # actually fetches. Add to this set deliberately when a new asset
    # type comes up; do NOT change the default fallback.
    suffix = target.suffix.lower()
    content_types = {
        ".js": "application/javascript",
        ".mjs": "application/javascript",
        ".css": "text/css",
        ".json": "application/json",
        ".html": "text/html",
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".ico": "image/x-icon",
        ".woff2": "font/woff2",
        ".woff": "font/woff",
        ".ttf": "font/ttf",
        ".otf": "font/otf",
        ".map": "application/json",
    }
    if suffix not in content_types:
        raise HTTPException(
            status_code=404,
            detail="File not found",
        )
    media_type = content_types[suffix]
    return FileResponse(
        target,
        media_type=media_type,
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


def _mount_plugin_api_routes():
    """Import and mount backend API routes from plugins that declare them.

    Each plugin's ``api`` field points to a Python file that must expose
    a ``router`` (FastAPI APIRouter).  Routes are mounted under
    ``/api/plugins/<name>/``.

    Backend import is restricted to ``bundled`` and ``user`` sources.
    Project plugins (``./.hermes/plugins/``) ship with the CWD and are
    therefore attacker-controlled in any threat model where the user
    opens a malicious repo; they can extend the dashboard UI via
    static JS/CSS but their Python ``api`` file is never auto-imported
    by the web server.  See GHSA-5qr3-c538-wm9j (#29156).
    """
    for plugin in _get_dashboard_plugins():
        api_file_name = plugin.get("_api_file")
        if not api_file_name:
            continue
        if plugin.get("source") == "project":
            _log.warning(
                "Plugin %s: ignoring backend api=%s (project plugins may "
                "not auto-import Python code; move the plugin to "
                "~/.hermes/plugins/ if you trust it)",
                plugin["name"], api_file_name,
            )
            continue
        dashboard_dir = Path(plugin["_dir"])
        api_path = dashboard_dir / api_file_name
        try:
            resolved_api = api_path.resolve()
            resolved_base = dashboard_dir.resolve()
            resolved_api.relative_to(resolved_base)
        except (OSError, RuntimeError, ValueError):
            # Discovery already filters this, but re-check here in case
            # ``_dir`` was tampered with after caching or a future caller
            # bypasses the validator.  Defence in depth keeps the import
            # primitive contained even if the upstream check regresses.
            _log.warning(
                "Plugin %s: refusing to import api file outside its "
                "dashboard directory (%s)", plugin["name"], api_path,
            )
            continue
        if not api_path.exists():
            _log.warning("Plugin %s declares api=%s but file not found", plugin["name"], api_file_name)
            continue
        try:
            module_name = f"hermes_dashboard_plugin_{plugin['name']}"
            spec = importlib.util.spec_from_file_location(module_name, api_path)
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            # Register in sys.modules BEFORE exec_module so pydantic/FastAPI
            # can resolve forward references (e.g. models defined in a file
            # that uses `from __future__ import annotations`). Without this,
            # TypeAdapter lazy-build fails at first request with
            # "is not fully defined" because the module namespace isn't
            # reachable by name for string-annotation resolution.
            sys.modules[module_name] = mod
            try:
                spec.loader.exec_module(mod)
            except Exception:
                sys.modules.pop(module_name, None)
                raise
            router = getattr(mod, "router", None)
            if router is None:
                _log.warning("Plugin %s api file has no 'router' attribute", plugin["name"])
                continue
            app.include_router(router, prefix=f"/api/plugins/{plugin['name']}")
            _log.info("Mounted plugin API routes: /api/plugins/%s/", plugin["name"])
        except Exception as exc:
            _log.warning("Failed to load plugin %s API routes: %s", plugin["name"], exc)


# Mount plugin API routes before the SPA catch-all.
_mount_plugin_api_routes()

# Mount the dashboard auth routes (/login, /auth/*, /api/auth/*) before the
# SPA catch-all so /{full_path:path} doesn't swallow them.  These are
# always mounted — the gate middleware decides whether to enforce auth,
# not whether the routes exist.
from hermes_cli.dashboard_auth.routes import router as _dashboard_auth_router  # noqa: E402
app.include_router(_dashboard_auth_router)

mount_spa(app)


def start_server(
    host: str = "127.0.0.1",
    port: int = 9119,
    open_browser: bool = True,
    allow_public: bool = False,
    *,
    embedded_chat: bool = False,
    mode: str = "admin",
):
    """Start the web UI server."""
    import uvicorn

    global _DASHBOARD_EMBEDDED_CHAT_ENABLED, _DASHBOARD_MODE
    if mode not in {"admin", "assistant"}:
        raise SystemExit(f"Unsupported dashboard mode: {mode}")
    _DASHBOARD_MODE = mode
    _DASHBOARD_EMBEDDED_CHAT_ENABLED = True

    # Phase 0: stash the auth-gate flag on app.state so middleware / SPA-token
    # injection / WS-auth paths can branch on it consistently.  Phase 3.5
    # uses this to decide whether to refuse the bind, log the gate-on
    # banner, and enable uvicorn proxy_headers.
    app.state.auth_required = should_require_auth(host, allow_public)

    if app.state.auth_required:
        # Phase 3.5: the gate engages on non-loopback binds.  The legacy
        # "refusing to bind" guard is replaced by "require at least one
        # provider to be registered, else fail closed".
        from hermes_cli.dashboard_auth import list_providers
        if not list_providers():
            # Surface the *specific* reason any bundled provider declined
            # to register (e.g. missing HERMES_DASHBOARD_OAUTH_CLIENT_ID).
            # Each provider plugin that ships with Hermes Agent exposes a
            # module-level ``LAST_SKIP_REASON`` string for this purpose;
            # without it the operator would only see "no providers" which
            # is misleading when the provider IS installed but unconfigured.
            skip_reasons: list[str] = []
            try:
                from plugins.dashboard_auth import nous as _nous_plugin

                if _nous_plugin.LAST_SKIP_REASON:
                    skip_reasons.append(
                        f"  • nous: {_nous_plugin.LAST_SKIP_REASON}"
                    )
            except Exception:
                pass

            if skip_reasons:
                raise SystemExit(
                    f"Refusing to bind dashboard to {host} — the OAuth auth "
                    f"gate engages on non-loopback binds, but no auth "
                    f"providers are registered.\n"
                    f"\n"
                    f"Bundled providers reported these issues:\n"
                    + "\n".join(skip_reasons)
                    + "\n"
                    f"\n"
                    f"Or pass --insecure to skip the auth gate (NOT "
                    f"recommended on untrusted networks)."
                )
            raise SystemExit(
                f"Refusing to bind dashboard to {host} — the OAuth auth "
                f"gate engages on non-loopback binds, but no auth providers "
                f"are registered and no bundled plugin reported a reason "
                f"(was the dashboard_auth/nous plugin removed?).\n"
                f"Install a DashboardAuthProvider plugin, or pass --insecure "
                f"to skip the auth gate (NOT recommended on untrusted "
                f"networks)."
            )
        _log.info(
            "Dashboard binding to %s with OAuth auth gate enabled. "
            "Providers: %s",
            host,
            ", ".join(p.name for p in list_providers()),
        )
    elif host not in _LOOPBACK_HOST_VALUES and allow_public:
        # --insecure path — no auth, loud warning.
        _log.warning(
            "Binding to %s with --insecure — the dashboard has no robust "
            "authentication. Only use on trusted networks.", host,
        )

    # Record the bound host so host_header_middleware can validate incoming
    # Host headers against it. Defends against DNS rebinding (GHSA-ppp5-vxwm-4cf7).
    # bound_port is also stashed so /api/pty can build the back-WS URL the
    # PTY child uses to publish events to the dashboard sidebar.
    app.state.bound_host = host
    app.state.bound_port = port

    if open_browser:
        import webbrowser

        # On headless Linux (no DISPLAY or WAYLAND_DISPLAY) some registered
        # browsers are TUI programs (links, lynx, www-browser) that try to
        # take over the terminal.  That can send SIGHUP to the server process
        # and cause an immediate exit even though uvicorn bound successfully.
        # Skip the auto-open attempt on headless systems and let the user
        # open the URL manually.  macOS and Windows are always considered
        # display-capable.
        _has_display = (
            sys.platform != "linux"
            or bool(os.environ.get("DISPLAY"))
            or bool(os.environ.get("WAYLAND_DISPLAY"))
        )

        if _has_display:
            def _open():
                try:
                    time.sleep(1.0)
                    webbrowser.open(f"http://{host}:{port}")
                except Exception:
                    pass

            threading.Thread(target=_open, daemon=True).start()
        else:
            _log.debug(
                "Skipping browser-open: no DISPLAY or WAYLAND_DISPLAY detected "
                "(headless Linux). Pass --no-open to suppress this detection."
            )

    display_name = "AIWerk Customer UI" if _DASHBOARD_MODE == "assistant" else "Hermes dashboard"
    print(f"  {display_name} ({_DASHBOARD_MODE}) → http://{host}:{port}")
    # proxy_headers defaults to False so _ws_client_is_allowed sees the real
    # connection peer rather than X-Forwarded-For's rewritten value. When the
    # OAuth gate is active we need X-Forwarded-Proto for secure cookie handling.
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="warning",
        proxy_headers=bool(app.state.auth_required),
    )
