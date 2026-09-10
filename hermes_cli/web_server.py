"""Hermes Agent — Web UI server: FastAPI app assembly, auth/host middleware, ``start_server``.

Route handlers live in ``web_routers/``; their helpers live in the sibling
``web_server_<concern>`` modules and are re-imported here so ``web_server.<name>``
stays the single late-binding seam tests monkeypatch (``web_deps.late``).
Usage: ``python -m hermes_cli.main web [--port 8080]``.
"""

from contextlib import asynccontextmanager
import contextlib

import asyncio
from collections import deque
import copy
import hmac
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import sysconfig
import threading
import time
import urllib.parse
import urllib.request
import zipfile

from hermes_cli.install_identity import get_install_id as _shared_get_install_id
from hermes_cli.pty_session import run_reaper
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from hermes_cli import __version__
from hermes_cli.config import get_hermes_home, load_config, load_env

try:
    from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import FileResponse, JSONResponse, Response
    from starlette.concurrency import run_in_threadpool
except ImportError:
    # First try lazy-installing the dashboard extras. Only the user actually
    # running `hermes dashboard` needs fastapi+uvicorn; lazy install keeps
    # them out of every other install path. After install, re-import.
    try:
        from tools.lazy_deps import ensure as _lazy_ensure
        _lazy_ensure("tool.dashboard", prompt=False)
        from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
        from fastapi.middleware.cors import CORSMiddleware
        from fastapi.responses import FileResponse, JSONResponse, Response
        from starlette.concurrency import run_in_threadpool
    except Exception:
        raise SystemExit(
            "Web UI requires fastapi and uvicorn.\n"
            f"Install with: {sys.executable} -m pip install 'fastapi' 'uvicorn[standard]'"
        )

WEB_DIST = Path(os.environ["HERMES_WEB_DIST"]) if "HERMES_WEB_DIST" in os.environ else Path(__file__).parent / "web_dist"
_log = logging.getLogger(__name__)

_ASSISTANT_TEXT_EXTRACT_LIMIT = 60_000
_ASSISTANT_DOCX_MAX_XML_BYTES = 50 * 1024 * 1024


from hermes_cli.web_server_lifecycle import (  # noqa: E402
    PORT_IN_USE_EXIT_CODE,
    _dashboard_forwarded_allow_ips,
    _eager_reconcile_own_session_db,
    _maybe_open_browser,
    _port_bind_conflict,
    _read_bound_port,
    _report_port_in_use,
    _start_parent_death_watchdog,
    _warm_gateway_module,
    _write_dashboard_ready_file,
    _write_machine_sentinel_line,
)
from hermes_cli.web_server_profiles import (  # noqa: E402
    _disable_unselected_skills,
    _fallback_profile_dicts,
    _hub_action_name,
    _profile_setup_command,
    _profile_to_dict,
    _resolve_profile_dir,
    _write_profile_model,
    _write_profile_mcp_servers,
)


def _start_desktop_cron_ticker(stop_event: "threading.Event", interval: int = 60) -> None:
    """Tick the cron scheduler from inside the desktop dashboard backend.

    The desktop spawns a ``hermes dashboard`` backend, not a gateway, so without
    this a cron created in the app would never fire (no live adapters; delivery
    falls back to the per-platform send path). The primary backend outlives the
    per-profile pool (reaped after ~10 idle minutes), so it ticks EVERY local
    profile's store like a multiplex gateway; external providers keep the
    single-store behavior (registries are not profile-scoped). Cross-process
    safe: the built-in tick takes the per-store ``cron/.tick.lock``.

    Every local profile's store is ticked, not just this backend's own (#69377's desktop sibling): the
    desktop pools per-profile backends and reaps them after ~10 idle minutes, so a secondary profile's
    ticker dies with its backend and that profile's jobs silently stop firing until the user next opens it
    ("tasks on the sleeping profile could be idle" — community report, Aug 2026).
    """
    from cron.scheduler_provider import InProcessCronScheduler, resolve_cron_scheduler

    provider = resolve_cron_scheduler()

    start_kwargs: dict = {"interval": interval}
    if isinstance(provider, InProcessCronScheduler):
        try:
            from hermes_cli.profiles import profiles_to_serve

            profile_homes = list(profiles_to_serve(multiplex=True))
            if len(profile_homes) > 1:
                start_kwargs["profile_homes"] = profile_homes
                # Stand down, per tick, for a profile whose OWN gateway runs:
                # it ticks with live adapters, and the tick-lock race would
                # otherwise deliver through the standalone path (#100489).
                from hermes_cli.profiles import _check_gateway_running

                start_kwargs["profile_gate"] = lambda _name, home: not _check_gateway_running(Path(home))
                from hermes_logging import enable_profile_log_routing

                enable_profile_log_routing(profile_homes)
                _log.info(
                    "Desktop cron scheduler will tick %d profile(s): %s",
                    len(profile_homes),
                    [name for name, _home in profile_homes],
                )
        except Exception:
            # Fail open to the single-store ticker so the active profile keeps firing.
            _log.exception("Desktop cron: profile enumeration failed; ticking active profile only")

    _log.info("Desktop cron scheduler started (provider=%s, interval=%ds)", provider.name, interval)
    provider.start(stop_event, **start_kwargs)


# Desktop `serve` only (start_server(start_mcp_discovery_after_bind=True)):
# seconds after the READY sentinel before the MCP discovery thread starts.
_DESKTOP_MCP_DISCOVERY_DELAY_S = 1.0


@asynccontextmanager
async def _lifespan(app: "FastAPI"):
    app.state.event_channels = {}  # dict[str, set]
    app.state.event_lock = asyncio.Lock()
    app.state.pty_active_session_files = {}  # dict[str, Path]
    # Serializes chat-argv resolution so concurrent /api/pty connections don't
    # overlap ``npm install`` / ``npm run build``. Locks live on app.state (not
    # module globals) so they bind to the running loop, not the import-time one.
    app.state.chat_argv_lock = asyncio.Lock()

    # Bring state.db schema current BEFORE the first session-list poll
    # (#79531/#80037): a store left behind by `hermes update` otherwise 500s
    # every poll while the read-probe heal loses to sibling lock contention.
    # Daemon thread so a locked store never delays the socket (Desktop
    # ready-probe times out at 10s, GH-73083).
    threading.Thread(
        target=_eager_reconcile_own_session_db,
        daemon=True,
        name="statedb-eager-reconcile",
    ).start()

    # Import hermes_cli.gateway *before* the yield: on Windows + 3.11 the
    # import holds the GIL, so run_in_executor still froze the loop 15-22s and
    # the Desktop's 10s ready-probe timed out (GH-73083).
    _warm_gateway_module()

    # Snapshot the checkout revision so lazy-import paths (model picker) can
    # refuse with "restart required" after `hermes update` replaced the code
    # (#86207); the update flow does not reliably restart the dashboard.
    from gateway.code_skew import record_boot_fingerprint

    record_boot_fingerprint()

    # Hosted Bot rooms belong to the backend process. Recovery may need a
    # contended state.db migration, so keep it off the pre-yield path: Group
    # Chat must degrade on its own rather than block every Desktop feature.
    from tui_gateway import methods_groups as _hosted_groups
    if _hosted_groups.get_hosted_room_service() is None:
        server_module = sys.modules.get("tui_gateway.server")
        if server_module is not None:
            _hosted_groups.bind_server(server_module)

    hosted_room_start_cancel = threading.Event()

    def _start_hosted_rooms() -> None:
        try:
            _hosted_groups.start_hosted_room_service()
        except Exception:
            _log.exception("Hosted Group Chat recovery failed during backend startup")
        finally:
            if hosted_room_start_cancel.is_set():
                _hosted_groups.stop_hosted_room_service(timeout=1.0)

    hosted_room_start_thread = threading.Thread(
        target=_start_hosted_rooms,
        daemon=True,
        name="hosted-room-startup",
    )
    hosted_room_start_thread.start()

    # Desktop-spawned backends (HERMES_DESKTOP=1) fire cron jobs themselves,
    # since the app has no gateway running the scheduler. Server `hermes
    # dashboard` is unaffected — it relies on its own gateway.
    cron_stop: "threading.Event | None" = None
    cron_thread: "threading.Thread | None" = None
    if os.getenv("HERMES_DESKTOP") == "1":
        # Reap an orphaned gateway from an abnormal previous exit (reparented to
        # launchd, still holding the platform WebSocket) before forking a fresh
        # one that would race the same credential (#77276). Runs
        # unconditionally; protection of a healthy standalone gateway lives
        # INSIDE the reaper (registration probed with cleanup_stale=False).
        try:
            from hermes_cli.gateway import _reap_unsupervised_gateway_orphans

            _reap_unsupervised_gateway_orphans()
        except Exception:
            _log.exception("Desktop startup: orphan gateway reap failed")

        cron_stop = threading.Event()
        cron_thread = threading.Thread(
            target=_start_desktop_cron_ticker,
            args=(cron_stop,),
            daemon=True,
            name="desktop-cron-ticker",
        )
        cron_thread.start()

    # Reap idle/dead keep-alive PTY sessions (30-min TTL).
    pty_reaper_task = asyncio.create_task(run_reaper(PTY_REGISTRY))
    # Periodic authenticated self-test feeding the ``dashboard`` component on /api/status.
    selftest_task = asyncio.create_task(_dashboard_selftest_loop())
    # Live auto-archive timer, independent of list requests.
    auto_archive_task = asyncio.create_task(_auto_archive_ticker_loop())

    # Managed local runtime (local_runtime.enabled): bring llama-server back so a
    # restart doesn't strand a llamacpp main model. Off-thread and best-effort;
    # failure falls back to cloud providers like a cold start. Server only —
    # models load on first inference (an empty router holds no VRAM).
    def _boot_local_runtime():
        try:
            from hermes_cli.config import load_config
            from hermes_cli.local_runtime.bootstrap import ensure_local_runtime

            ensure_local_runtime(load_config())
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).warning("local runtime boot failed: %s", exc)

    threading.Thread(target=_boot_local_runtime, daemon=True, name="local-runtime-boot").start()

    try:
        yield
    finally:
        hosted_room_start_cancel.set()
        _hosted_groups.stop_hosted_room_service(timeout=5.0)
        hosted_room_start_thread.join(timeout=1.0)
        if cron_stop is not None:
            cron_stop.set()
        pty_reaper_task.cancel()
        selftest_task.cancel()
        auto_archive_task.cancel()
        await PTY_REGISTRY.close_all()
        # Stop the managed llama-server with its parent (an orphan pins VRAM).
        try:
            from hermes_cli.local_runtime.bootstrap import shutdown_local_runtime

            shutdown_local_runtime()
        except Exception:  # noqa: BLE001
            pass
        if os.getenv("HERMES_DESKTOP") == "1":
            _terminate_desktop_managed_gateway()


def _app_state_default(app: "FastAPI", name: str, factory):
    """Return ``app.state.<name>``, lazily creating it for non-``with`` TestClient usages.

    The lifespan normally initialises these on the running event loop (an
    asyncio.Lock created at import time binds to whatever loop was active then).
    """
    try:
        return getattr(app.state, name)
    except AttributeError:
        value = factory()
        setattr(app.state, name, value)
        return value


def _get_chat_argv_lock(app: "FastAPI") -> asyncio.Lock:
    return _app_state_default(app, "chat_argv_lock", asyncio.Lock)


def _get_pty_active_session_files(app: "FastAPI") -> dict[str, Path]:
    return _app_state_default(app, "pty_active_session_files", dict)


app = FastAPI(title="Hermes Agent", version=__version__, lifespan=_lifespan)


# Memory-provider OAuth connect routes live in the memory layer, not here.
from hermes_cli.memory_oauth import router as _memory_oauth_router  # noqa: E402

app.include_router(_memory_oauth_router)

# Session token for sensitive endpoints. The desktop shell mints it via
# HERMES_DASHBOARD_SESSION_TOKEN; otherwise fresh per server start. It dies with
# the process and is injected into the SPA HTML so only the web UI can use it.
def _resolve_session_token() -> str:
    return os.environ.get("HERMES_DASHBOARD_SESSION_TOKEN") or secrets.token_urlsafe(32)


_SESSION_TOKEN = _resolve_session_token()
_SESSION_HEADER_NAME = "X-Hermes-Session-Token"
_SSH_OWNER_NONCE: Optional[str] = None
_SSH_RUNTIME_PURELIB: Optional[Tuple[str, int, int]] = None
_SSH_RUNTIME_MARKER: Optional[str] = None


def _apply_ssh_session_token(token: str) -> None:
    global _SESSION_TOKEN
    if token:
        _SESSION_TOKEN = token


def _apply_ssh_owner_nonce(nonce: Optional[str]) -> None:
    global _SSH_OWNER_NONCE, _SSH_RUNTIME_PURELIB, _SSH_RUNTIME_MARKER
    _SSH_OWNER_NONCE = nonce
    _SSH_RUNTIME_PURELIB = None
    _SSH_RUNTIME_MARKER = None
    if nonce:
        try:
            purelib = sysconfig.get_paths()["purelib"]
        except (KeyError, OSError):
            return
        # Primary identity: a marker FILE in site-packages. A replaced venv
        # loses it deterministically; pip installs leave it. A bare (dev, ino)
        # snapshot alone is NOT enough: ext4 reuses directory inodes at once,
        # so `rm -rf venv && uv venv` can land on the same inode undetected.
        try:
            marker = os.path.join(purelib, f".hermes-ssh-runtime-{nonce}")
            with open(marker, "w", encoding="utf-8") as fh:
                fh.write(f"pid={os.getpid()}\n")
            _SSH_RUNTIME_MARKER = marker
        except OSError:
            pass  # read-only site-packages — fall back to the stat snapshot
        try:
            st = os.stat(purelib)
            _SSH_RUNTIME_PURELIB = (purelib, st.st_dev, st.st_ino)
        except OSError:
            pass


def _ssh_runtime_intact() -> bool:
    if _SSH_RUNTIME_MARKER is not None:
        return os.path.isfile(_SSH_RUNTIME_MARKER)
    # Fallback (read-only site-packages): directory identity snapshot — weaker
    # (inode reuse) but catches cross-device moves and version-bump paths.
    if _SSH_RUNTIME_PURELIB is None:
        return True
    purelib, device, inode = _SSH_RUNTIME_PURELIB
    try:
        st = os.stat(purelib)
    except OSError:
        return False
    return (st.st_dev, st.st_ino) == (device, inode)


# In-browser Chat tab (/chat, /api/pty, /api/ws): always enabled. A module
# constant (not an inlined True) so the WS endpoints and SPA token injection
# share one testable seam.
_DASHBOARD_EMBEDDED_CHAT_ENABLED = True

# Desktop file.attach sends a whole base64 data URL in one JSON-RPC frame;
# uvicorn's 16 MiB default rejects files under the 256 MiB raw attach cap.
_DESKTOP_ATTACHMENT_WS_MAX_BYTES = 384 * 1024 * 1024


# CORS: localhost origins only — allow_origins=["*"] on 0.0.0.0 would let any
# website read/modify config and secrets.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_methods=["*"],
    allow_headers=["*"],
)

# Endpoints that do NOT require the session token; everything else under /api/
# is gated below. Shared with the OAuth gate so the two allowlists cannot
# drift (/api/status once 401'd under the OAuth gate, breaking the portal probe).
from hermes_cli.dashboard_auth.public_paths import PUBLIC_API_PATHS as _PUBLIC_API_PATHS


def _has_valid_session_token(request: Request) -> bool:
    """True if the request carries a valid dashboard session token.

    The dedicated header avoids collisions with reverse proxies that already use
    ``Authorization`` (Caddy ``basic_auth``); the legacy Bearer path stays for
    older dashboard bundles.
    """
    session_header = request.headers.get(_SESSION_HEADER_NAME, "")
    if session_header and hmac.compare_digest(session_header.encode(), _SESSION_TOKEN.encode()):
        return True
    auth = request.headers.get("authorization", "")
    return hmac.compare_digest(auth.encode(), f"Bearer {_SESSION_TOKEN}".encode())


# Routes that may also authenticate via ``?token=`` (download links opened by
# the OS shell / a new tab, where no header can be set). Kept narrow.
_QUERY_TOKEN_API_PATHS: frozenset[str] = frozenset({"/api/files/download"})


def _has_valid_query_token(request: Request, path: str) -> bool:
    if path not in _QUERY_TOKEN_API_PATHS:
        return False
    token = request.query_params.get("token", "")
    return bool(token) and hmac.compare_digest(token.encode(), _SESSION_TOKEN.encode())


def _require_token(request: Request) -> None:
    """Authorize a sensitive endpoint, raising 401 if the caller isn't allowed.

    Loopback mode (``auth_required`` False): validate the SPA-injected
    ``_SESSION_TOKEN``. Gated mode: the token is NOT injected (cookie auth), and
    ``gated_auth_middleware`` already 401'd anything without a verified
    ``request.state.session`` — requiring the absent token here would make every
    ``_require_token`` endpoint unreachable behind the gate, so defer to it.
    """
    if getattr(request.app.state, "auth_required", False):
        ok = getattr(request.state, "session", None) is not None
    else:
        ok = _has_valid_session_token(request)
    if not ok:
        raise HTTPException(status_code=401, detail="Unauthorized")


# Accepted Host values for loopback binds. DNS rebinding TTL-flips an attacker
# hostname to 127.0.0.1 so the browser treats it as same-origin; validating Host
# at the app layer rejects it. See GHSA-ppp5-vxwm-4cf7.
_LOOPBACK_HOST_VALUES: frozenset = frozenset({"localhost", "127.0.0.1", "::1"})


def _dashboard_public_hosts() -> frozenset[str]:
    """Return the exact hostname declared by ``dashboard.public_url``.

    One source of truth for OAuth redirects, Host and WS Origin validation.
    Malformed or unset values fail closed as an empty set.
    """
    from hermes_cli.dashboard_auth.prefix import resolve_public_url

    public_url = resolve_public_url()
    try:
        hostname = urllib.parse.urlparse(public_url).hostname if public_url else None
    except ValueError:
        hostname = None
    return frozenset({hostname.lower()}) if hostname else frozenset()


def should_require_auth(host: str, allow_public: bool = False) -> bool:
    """True iff the auth gate must be active: any non-loopback bind.

    RFC1918 / CGNAT / link-local are deliberately PUBLIC — a hostile LAN device
    is the threat model. ``allow_public`` (legacy ``--insecure``) is accepted for
    old launch scripts but IGNORED since the June 2026 hermes-0day campaign.
    """
    return host not in _LOOPBACK_HOST_VALUES


def should_require_dashboard_auth(
    host: str,
    trusted_public_hosts: Optional[frozenset[str]] = None,
) -> bool:
    """Gate required for a non-loopback bind OR a non-loopback ``dashboard.public_url``.

    Callers may pass the already-resolved host set so startup and request
    validation share one snapshot.
    """
    if trusted_public_hosts is None:
        trusted_public_hosts = _dashboard_public_hosts()
    return should_require_auth(host) or any(h not in _LOOPBACK_HOST_VALUES for h in trusted_public_hosts)


def _desktop_loopback_auth_exempt(
    host: str,
    ssh_session_token: Optional[str] = None,
    ssh_owner_nonce: Optional[str] = None,
) -> bool:
    """True for a Desktop-owned loopback backend (#96490).

    A non-loopback ``dashboard.public_url`` would otherwise engage the
    ticket-only gate for the private loopback backends Desktop spawns, whose
    per-spawn session token the gate's WS path refuses — Desktop could not boot.
    The public dashboard is a separate non-loopback process that stays gated, so
    this never opens the public surface. Requires ALL of: loopback bind,
    ``HERMES_DESKTOP=1``, and an operator-minted credential (env token, SSH
    session token, or owner nonce).
    """
    return (
        host in _LOOPBACK_HOST_VALUES
        and os.environ.get("HERMES_DESKTOP") == "1"
        and bool(os.environ.get("HERMES_DASHBOARD_SESSION_TOKEN") or ssh_session_token or ssh_owner_nonce)
    )


def _host_header_hostname(host_header: str) -> str:
    """Return a normalized hostname from a valid HTTP Host authority.

    Host headers are authorities, not full URLs. Reject ambiguous ports,
    malformed IPv6 brackets, and URL syntax so validation always fails closed.
    """
    value = (host_header or "").strip()
    if not value or "://" in value or any(c in value for c in '"\'<> \n\r\t/?#@'):
        return ""

    if value.startswith("["):
        close = value.find("]")
        if close == -1:
            return ""
        hostname = value[1:close]
        # Bracket notation is reserved for IPv6 literals.
        if ":" not in hostname:
            return ""
        suffix = value[close + 1:]
        if suffix and not re.fullmatch(r":\d+", suffix):
            return ""
        return hostname.lower()

    # Unbracketed IPv6 authorities are ambiguous with a port separator.
    if value.count(":") > 1:
        return ""
    if ":" in value:
        hostname, port = value.rsplit(":", 1)
        if not hostname or not port.isdigit():
            return ""
        return hostname.lower()
    return value.lower()


def _is_accepted_host(
    host_header: str,
    bound_host: str,
    trusted_public_hosts: frozenset[str] = frozenset(),
) -> bool:
    """True if the Host header targets the interface we bound to.

    Accepts:
    - Exact bound host (with or without port suffix)
    - Loopback aliases when bound to loopback
    - Exact operator-declared public hosts (with or without port suffix)
    - Any host when bound to 0.0.0.0 (explicit opt-in to non-loopback,
      no protection possible at this layer)
    """
    host_only = _host_header_hostname(host_header)
    if not host_only:
        return False
    # All-interfaces bind: no Host-layer defence is possible; rely on operator
    # network controls.
    if host_only in trusted_public_hosts or bound_host in {"0.0.0.0", "::"}:
        return True
    bound_lc = bound_host.lower()
    if bound_lc in _LOOPBACK_HOST_VALUES:
        return host_only in _LOOPBACK_HOST_VALUES
    return host_only == bound_lc


@app.middleware("http")
async def _admin_permission_middleware(request: Request, call_next):
    """Bind authenticated actor scope and enforce admin-only actions inside auth."""
    actor = _cui_actor_context_from_request(request)
    token = _current_http_cui_actor.set(actor if actor != {"_restricted": "1"} else None)
    try:
        denied = _enforce_admin_api_permission(request)
        if denied is not None:
            return denied
        return await call_next(request)
    finally:
        _current_http_cui_actor.reset(token)


@app.middleware("http")
async def host_header_middleware(request: Request, call_next):
    """Reject requests whose Host header doesn't match the bound interface (DNS rebinding, GHSA-ppp5-vxwm-4cf7)."""
    # app.state.bound_host is set by start_server() at listen time.
    bound_host = getattr(app.state, "bound_host", None)
    if bound_host and not _is_accepted_host(
        request.headers.get("host", ""), bound_host, getattr(app.state, "trusted_public_hosts", frozenset())
    ):
        return JSONResponse(
            status_code=400,
            content={
                "detail": (
                    "Invalid Host header. Dashboard requests must use the "
                    "bound hostname or the configured public hostname."
                ),
            },
        )
    return await call_next(request)


@app.middleware("http")
async def _plugin_api_runtime_gate(request: Request, call_next):
    """Block requests to disabled plugin API routes at request time.

    :func:`_mount_plugin_api_routes` gates at import time; a plugin disabled
    while running keeps its router mounted until restart, so enforce on every
    ``/api/plugins/{name}/...`` request. Registered BEFORE the auth middlewares
    (runs AFTER them): an unauthenticated caller must get auth's 401, never this
    404, or the status code becomes a plugin-name oracle.
    """
    path = request.url.path
    # parts: ['', 'api', 'plugins', '<name>', ...]
    parts = path.split("/")
    plugin_name = parts[3] if path.startswith("/api/plugins/") and len(parts) >= 4 else ""
    # Only gate authenticated requests. Unauthenticated ones fall through so
    # auth_middleware / the OAuth gate return 401 first and this route can't
    # be used as a plugin-name oracle.
    if plugin_name and (
        getattr(request.state, "token_authenticated", False)
        or getattr(request.app.state, "auth_required", False)
        or _has_valid_session_token(request)
        or _has_valid_query_token(request, path)
    ):
        try:
            # Gate: only serve user plugins that are in plugins.enabled and not in plugins.disabled. This
            # prevents the frontend from loading JS/CSS from plugins the user has not explicitly activated.
            # (#46435)
            from hermes_cli.plugins_cmd import _get_enabled_set, _get_disabled_set
            enabled_set = _get_enabled_set()
            disabled_set = _get_disabled_set()
        except Exception:
            enabled_set = set()
            disabled_set = set()
        # Source from the cached plugin list; unknown => user plugin (safe default — blocks).
        plugin = next((p for p in _get_dashboard_plugins() if p.get("name") == plugin_name), None)
        source = plugin.get("source") if plugin else "user"
        blocked = plugin_name in disabled_set or (source == "user" and plugin_name not in enabled_set)
        if blocked and source in ("user", "bundled"):
            return JSONResponse(status_code=404, content={"detail": "Plugin not found"})
    return await call_next(request)


@app.middleware("http")
async def _dashboard_auth_gate(request: Request, call_next):
    """OAuth gate — active only when start_server flags ``auth_required``; pass-through on loopback.

    Registered between host_header and auth_middleware: host check → cookie auth → token auth.
    """
    from hermes_cli.dashboard_auth.middleware import gated_auth_middleware
    return await gated_auth_middleware(request, call_next)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Require the session token on all /api/ routes except the public list.

    Skipped for requests the token-auth seam already authenticated
    (``token_authenticated``) and when the OAuth gate is active — cookie auth is
    then authoritative and the loopback-only token path must not override it.
    """
    path = request.url.path
    if (
        not getattr(request.state, "token_authenticated", False)
        and not getattr(request.app.state, "auth_required", False)
        and path.startswith("/api/")
        and path not in _PUBLIC_API_PATHS
        and not path.startswith("/api/mcp/oauth/callback/")
        and not _has_valid_session_token(request)
        and not _has_valid_query_token(request, path)
    ):
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    return await call_next(request)


@app.middleware("http")
async def _token_auth_seam(request: Request, call_next):
    """Outermost auth seam: bearer-token auth for opted-in routes (registered LAST = runs FIRST).

    A registered token route is owned here — authenticate, attach the principal
    + ``token_authenticated`` so downstream gates skip enforcement. Non-token
    routes pass through untouched.
    """
    from hermes_cli.dashboard_auth.token_auth import token_auth_middleware
    return await token_auth_middleware(request, call_next)


_DASHBOARD_HEALTH_WINDOW_SECONDS = 300.0


class DashboardHealth:
    """Dashboard-process health: rolling unhandled-error/5xx window + periodic self-test result.

    Feeds ``components`` on the PUBLIC ``/api/status``, so :meth:`snapshot`
    exports counts and enums only — never ``last_error_type``/``last_error_path``.
    """

    def __init__(self, window_seconds: float = _DASHBOARD_HEALTH_WINDOW_SECONDS) -> None:
        self.window_seconds = window_seconds
        self._error_times: "deque[float]" = deque(maxlen=256)
        self.last_error_type: Optional[str] = None
        self.last_error_path: Optional[str] = None  # internal-only, never serialized
        self.last_error_at: Optional[float] = None
        self.selftest_status: str = "unknown"  # unknown | ok | failing
        self.selftest_http_status: Optional[int] = None
        self.selftest_at: Optional[float] = None

    def record_error(self, exc_type: str, path: str) -> None:
        now = time.time()
        self._error_times.append(now)
        self.last_error_type = exc_type
        self.last_error_path = path
        self.last_error_at = now

    def record_selftest(self, passed: bool, http_status: Optional[int]) -> None:
        self.selftest_status = "ok" if passed else "failing"
        self.selftest_http_status = http_status
        self.selftest_at = time.time()

    def recent_error_count(self) -> int:
        cutoff = time.time() - self.window_seconds
        while self._error_times and self._error_times[0] < cutoff:
            self._error_times.popleft()
        return len(self._error_times)

    def snapshot(self) -> Dict[str, Any]:
        """Public component payload: status enum + counts + timestamps only."""
        errors = self.recent_error_count()
        status = "degraded" if (errors or self.selftest_status == "failing") else "ok"
        return {
            "status": status,
            "recent_unhandled_errors": errors,
            "last_error_at": self.last_error_at,
            "selftest": self.selftest_status,
        }


DASHBOARD_HEALTH = DashboardHealth()


@app.middleware("http")
async def _dashboard_health_middleware(request: Request, call_next):
    """Outermost middleware (registered last): count unhandled exceptions and 5xx; re-raises, never alters."""
    try:
        response = await call_next(request)
    except Exception as exc:
        DASHBOARD_HEALTH.record_error(type(exc).__name__, request.url.path)
        raise
    if response.status_code >= 500:
        DASHBOARD_HEALTH.record_error(f"http_{response.status_code}", request.url.path)
    return response


# Authenticated-route self-test: one in-process request per minute against a
# cheap DB-touching route, catching "liveness fine but every authed request 500s".
_DASHBOARD_SELFTEST_INTERVAL_SECONDS = 60.0
_DASHBOARD_SELFTEST_ROUTE = "/api/sessions?limit=1"


async def _dashboard_selftest_once() -> None:
    """Run one authenticated in-process self-test request and record it."""
    try:
        import httpx
    except ImportError:
        return  # optional dependency — leave status "unknown"
    try:
        # Loopback base_url so the Host-header middleware accepts the request.
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            resp = await client.get(_DASHBOARD_SELFTEST_ROUTE, headers={_SESSION_HEADER_NAME: _SESSION_TOKEN})
        DASHBOARD_HEALTH.record_selftest(resp.status_code == 200, resp.status_code)
    except Exception:
        DASHBOARD_HEALTH.record_selftest(False, None)


async def _dashboard_selftest_loop() -> None:
    """Periodic self-test driver started from the lifespan."""
    try:
        import httpx  # noqa: F401
    except ImportError:
        _log.debug("httpx unavailable — dashboard self-test disabled")
        return
    while True:
        await asyncio.sleep(_DASHBOARD_SELFTEST_INTERVAL_SECONDS)
        # OAuth-gated binds don't honour the session token; the probe would false-alarm 401.
        if getattr(app.state, "auth_required", False):
            continue
        await _dashboard_selftest_once()




# Action registries/spawner are owned by web_server_gateway; routers and tests reach them
# there, so this module reads them through the module too (one patch seam).
from hermes_cli import web_server_gateway as _gateway_mod  # noqa: E402
from hermes_cli.web_server_gateway import _ACTION_LOG_FILES, _terminate_desktop_managed_gateway  # noqa: E402
from hermes_cli.web_server_sessions import _auto_archive_ticker_loop  # noqa: E402
from hermes_cli.web_server_chat import PTY_REGISTRY  # noqa: E402
from hermes_cli.web_server_dashboard import (  # noqa: E402
    _discover_dashboard_plugins, _mount_plugin_api_routes, mount_spa as _mount_spa_impl,
)


_GATEWAY_HEALTH_URL = os.getenv("GATEWAY_HEALTH_URL")
_GATEWAY_HEALTH_TIMEOUT_MAX = 1.0
try:
    _GATEWAY_HEALTH_TIMEOUT = float(os.getenv("GATEWAY_HEALTH_TIMEOUT", "1"))
except (ValueError, TypeError):
    _log.warning(
        "Invalid GATEWAY_HEALTH_TIMEOUT value %r — using default 1.0s",
        os.getenv("GATEWAY_HEALTH_TIMEOUT"),
    )
    _GATEWAY_HEALTH_TIMEOUT = 1.0
if _GATEWAY_HEALTH_TIMEOUT <= 0:
    _log.warning(
        "Invalid non-positive GATEWAY_HEALTH_TIMEOUT value %.3fs — using default 1.0s",
        _GATEWAY_HEALTH_TIMEOUT,
    )
    _GATEWAY_HEALTH_TIMEOUT = 1.0
elif _GATEWAY_HEALTH_TIMEOUT > _GATEWAY_HEALTH_TIMEOUT_MAX:
    _log.warning(
        "Capping GATEWAY_HEALTH_TIMEOUT %.3fs to %.3fs for dashboard liveness probes",
        _GATEWAY_HEALTH_TIMEOUT,
        _GATEWAY_HEALTH_TIMEOUT_MAX,
    )
    _GATEWAY_HEALTH_TIMEOUT = _GATEWAY_HEALTH_TIMEOUT_MAX


_MANAGED_FILE_MAX_BYTES = 100 * 1024 * 1024
_FS_DATA_URL_MAX_BYTES = 16 * 1024 * 1024
# Multipart uploads stream to a temp file in fixed chunks and rename into
# place: constant memory, no base64 inflation, no proxy body-size 502s (NS-501).
_UPLOAD_CHUNK_BYTES = 1024 * 1024

# Stable install identity for /api/status: one uuid4 hex per physical install,
# persisted under the ROOT Hermes home (not the profile HERMES_HOME) so every
# profile reports the same id and the desktop can collapse duplicate roster rows
# for one backend. Must never change across restarts, so cached per process.
_INSTALL_ID_CACHE: Dict[str, Optional[str]] = {"root": None, "value": None}


def get_install_id() -> Optional[str]:
    """Process-lifetime-cached stable install id."""
    return _shared_get_install_id(cache=_INSTALL_ID_CACHE)


# Serializes config.yaml read-modify-write cycles for handlers on worker threads
# (asyncio.to_thread): config.py's _CONFIG_LOCK covers each load/save call, not
# the span between them, so two off-loop updates could drop each other's writes.
# RLock so nested helpers that also take it can't self-deadlock.
_CONFIG_MUTATION_LOCK = threading.RLock()

# A finished ``gateway-restart`` child does not mean the gateway is back (it
# exits once the restart is handed off), so in-flight reuse stops coalescing
# exactly when a stale frontend re-fires every few seconds (#89034: 77 restarts,
# state.db corrupted mid-FTS5-write). MAINTAINER DECISION: a fixed window, not
# "until healthy" — a gateway that never returns must not leave the action
# inert. 10s is above the ~3.5s storm spacing and below an operator's retry.
GATEWAY_RESTART_COOLDOWN_SECONDS = 10.0

# ``(monotonic spawn time, Popen, command)`` of the last restart. Deliberately
# NOT read from ``_ACTION_PROCS``: entries there vanish when the child exits.
_LAST_GATEWAY_RESTART: Optional[Tuple[float, subprocess.Popen, Tuple[str, ...]]] = None


def _spawn_gateway_restart(profile: Optional[str] = None) -> Tuple[subprocess.Popen, bool]:
    """Spawn ``hermes gateway restart``, reusing an in-flight or recent restart.

    Concurrent children race each other on the kill-and-start path, so a live
    child is reused; requests within ``GATEWAY_RESTART_COOLDOWN_SECONDS`` for the
    same profile coalesce onto the last spawn too (#89034). Orphaned gateways
    are reaped first so the fresh one doesn't stack a duplicate (#77276).
    Returns ``(proc, reused)``.
    """
    try:
        from hermes_cli.gateway import _reap_unsupervised_gateway_orphans

        _reap_unsupervised_gateway_orphans()
    except Exception:
        pass  # best-effort — don't block the restart on a reap failure

    global _LAST_GATEWAY_RESTART

    subcommand = _gateway_mod._gateway_subcommand(profile, "restart")
    existing = _gateway_mod._ACTION_PROCS.get("gateway-restart")
    if existing is not None and existing.poll() is None:
        existing_command = _gateway_mod._ACTION_COMMANDS.get("gateway-restart")
        if existing_command is None or existing_command == tuple(subcommand):
            return existing, True
        raise RuntimeError("gateway restart already in progress for another profile")

    recent = _LAST_GATEWAY_RESTART
    if recent is not None:
        spawned_at, recent_proc, recent_command = recent
        age = time.monotonic() - spawned_at if recent_command == tuple(subcommand) else None
        if age is not None and age < GATEWAY_RESTART_COOLDOWN_SECONDS:
            _log.info(
                "Coalescing gateway restart: one was started %.1fs ago "
                "(pid %s) and the gateway may still be coming back; not "
                "spawning another (#89034).",
                age,
                getattr(recent_proc, "pid", "?"),
            )
            return recent_proc, True

    proc = _gateway_mod._spawn_hermes_action(subcommand, "gateway-restart")
    _LAST_GATEWAY_RESTART = (time.monotonic(), proc, tuple(subcommand))
    return proc, False


# Collapses repeated identical ElevenLabs voice-list failures (the desktop
# re-polls on every settings focus) to one log line; re-arms on success or a
# changed signature.
_voice_list_last_error: Optional[str] = None


def _voice_list_error_logged_once(signature: Optional[str]) -> bool:
    """True if ``signature`` is new and should be logged now; ``None`` clears the latch."""
    global _voice_list_last_error
    if signature is None:
        _voice_list_last_error = None
        return False
    if signature == _voice_list_last_error:
        return False
    _voice_list_last_error = signature
    return True


_ACTION_LOG_FILES.setdefault("computer-use-grant", "action-computer-use-grant.log")

# Cache discovered plugins per-process (refresh on explicit re-scan).
_dashboard_plugins_cache: Optional[list] = None


def _get_dashboard_plugins(force_rescan: bool = False) -> list:
    global _dashboard_plugins_cache
    stale = _dashboard_plugins_cache is None or force_rescan or any(
        not Path(p["_dir"]).is_dir() for p in _dashboard_plugins_cache
    )
    if stale:
        _dashboard_plugins_cache = _discover_dashboard_plugins()
    return _dashboard_plugins_cache


# Router mounting. ORDER IS ROUTE-MATCHING ORDER: literal paths must land before
# templated siblings (e.g. /api/sessions/bulk-delete before /api/sessions/{id}).
from hermes_cli.web_routers import (  # noqa: E402
    files as _files_routes,
    git as _git_routes,
    local_models as _local_models_routes,
    status as _status_routes,
    actions as _actions_routes,
    audio as _audio_routes,
    sessions as _sessions_routes,
    profiles as _profiles_routes,
    memory_providers as _memory_providers_routes,
    config_env as _config_env_routes,
    models as _models_routes,
    messaging as _messaging_routes,
    oauth as _oauth_routes,
    cron as _cron_routes,
    mcp as _mcp_routes,
    ops as _ops_routes,
    skills as _skills_routes,
    tools as _tools_routes,
    analytics as _analytics_routes,
    chat_ws as _chat_ws_routes,
    dashboard_ui as _dashboard_ui_routes,
)

app.include_router(_files_routes.router)
app.include_router(_git_routes.router)
app.include_router(_local_models_routes.router)
app.include_router(_status_routes.router)
app.include_router(_actions_routes.router)
app.include_router(_audio_routes.router)
app.include_router(_actions_routes.status_router)
app.include_router(_sessions_routes.list_router)
app.include_router(_profiles_routes.sessions_router)
app.include_router(_sessions_routes.search_router)
app.include_router(_memory_providers_routes.router)
app.include_router(_config_env_routes.config_router)
app.include_router(_models_routes.router)
app.include_router(_config_env_routes.router)
app.include_router(_messaging_routes.router)
app.include_router(_oauth_routes.router)
app.include_router(_sessions_routes.manage_router)
app.include_router(_status_routes.logs_router)
app.include_router(_cron_routes.router)
app.include_router(_mcp_routes.router)
app.include_router(_ops_routes.router)
app.include_router(_skills_routes.hub_router)
app.include_router(_profiles_routes.router)
app.include_router(_skills_routes.router)
app.include_router(_tools_routes.router)
app.include_router(_analytics_routes.router)
app.include_router(_chat_ws_routes.router)
app.include_router(_dashboard_ui_routes.router)

# Plugin API routes and the dashboard auth routes (/login, /auth/*, /api/auth/*)
# mount before the SPA catch-all so /{full_path:path} doesn't swallow them. Auth
# routes are always mounted — the gate middleware decides enforcement.
_mount_plugin_api_routes()
from hermes_cli.dashboard_auth.routes import router as _dashboard_auth_router  # noqa: E402

app.include_router(_dashboard_auth_router)


# ---- AIWerk assistant/customer facade surface ----
import contextvars as _contextvars  # noqa: E402
import hashlib as _hashlib  # noqa: E402
import html as _html  # noqa: E402
import json as _json  # noqa: E402
import unicodedata as _unicodedata  # noqa: E402
from datetime import datetime as _datetime, timedelta as _timedelta, timezone as _timezone  # noqa: E402

try:  # noqa: E402
    from pydantic import BaseModel
except Exception:  # pragma: no cover
    class BaseModel:  # type: ignore[no-redef]
        def __init__(self, **kwargs: Any) -> None:
            self.__dict__.update(kwargs)


_DASHBOARD_MODE = "admin"
_current_http_cui_actor: _contextvars.ContextVar[Dict[str, str] | None] = _contextvars.ContextVar(
    "current_http_cui_actor", default=None
)

_CUI_MANAGED_AUTONOMY_FEATURE_ENABLED = True
_CUI_MANAGED_AUTONOMY_ROLES = frozenset({"admin", "operator"})
_CUI_MANAGED_AUTONOMY_ENV_KEYS = (
    "HERMES_CUI_MANAGED_AUTONOMY",
    "HERMES_CUI_MANAGED_ACTOR_ID",
    "HERMES_CUI_MANAGED_ACTOR_ROLE",
)


_ASSISTANT_ALLOWED_HTTP = {
    "/api/status": {"GET"},
    "/api/auth/me": {"GET"},
    "/api/assistant/resources": {"GET"},
    "/api/assistant/artifacts/open": {"GET", "HEAD", "OPTIONS"},
    "/api/assistant/shared-folder/open": {"GET", "POST"},
    "/api/assistant/calendar/view": {"GET", "POST"},
    "/api/model/info": {"GET"},
    "/api/dashboard/themes": {"GET"},
    "/api/dashboard/font": {"GET"},
    "/api/cui/contacts/search": {"GET", "POST"},
    "/api/cui/context/contacts": {"GET"},
    "/api/cui/contacts/frequent": {"GET"},
    "/api/auth/ws-ticket": {"POST"},
    "/api/assistant/audio/tts": {"POST"},
    "/api/assistant/audio/transcribe": {"POST"},
    "/api/assistant/tts": {"POST"},
    "/api/assistant/transcribe": {"POST"},
    "/api/assistant/attachments": {"POST"},
    "/api/assistant/attachments/resource": {"POST"},
    "/api/assistant/support": {"POST"},
    "/api/assistant/todos": {"POST"},
    "/api/assistant/todos/add": {"POST"},
    "/api/assistant/todos/update": {"POST"},
    "/api/assistant/todos/edit": {"POST"},
    "/api/cui/contacts": {"POST"},
    "/api/cui/contacts/hide": {"POST"},
    "/api/assistant/email/view": {"GET", "HEAD", "OPTIONS", "POST"},
    "/api/assistant/shared-folder/open-folder": {"POST"},
    "/api/sessions": {"GET"},
}
_ASSISTANT_HTTP_GET_EXACT = frozenset(
    path for path, methods in _ASSISTANT_ALLOWED_HTTP.items() if "GET" in methods
)
_ASSISTANT_ALLOWED_API_PREFIXES = ("/api/sessions/",)
_ASSISTANT_HTTP_GET_PREFIXES = _ASSISTANT_ALLOWED_API_PREFIXES
_ASSISTANT_HTTP_POST_EXACT = frozenset(
    path for path, methods in _ASSISTANT_ALLOWED_HTTP.items() if "POST" in methods
)

_ASSISTANT_ALLOWED_RPC_METHODS = {
    "gateway.ping",
    "session.create",
    "session.resume",
    "session.title",
    "session.notes",
    "session.usage",
    "session.interrupt",
    "session.steer",
    "session.side.start",
    "session.side.back",
    "session.events.since",
    "prompt.submit",
    "prompt.learn",
    "approval.respond",
    "config.get",
    "config.set",
    "commands.catalog",
    "slash.exec",
}
_ASSISTANT_ALLOWED_CONFIG_KEYS = frozenset({"busy", "reasoning", "fast", "yolo"})
_ASSISTANT_ALLOWED_SLASH_COMMANDS = frozenset({"stop", "compress", "reload-mcp"})
_ASSISTANT_ACTOR_PARAM_KEYS = frozenset({
    "_cui_actor_role",
    "_cui_actor_id",
    "_cui_tenant_id",
    "actor_role",
})
_ASSISTANT_ALLOWED_ROLES = frozenset({"user", "customer"})


class AssistantTTSRequest(BaseModel):
    text: str = ""
    voice: str = "alloy"


class AssistantSupportRequest(BaseModel):
    category: str = ""
    session_id: str = ""
    session_title: str = ""
    include_diagnostics: bool = False
    connection: str = ""
    subject: str = ""
    message: str = ""
    diagnostics: Dict[str, Any] | None = None


class AssistantTodoAddRequest(BaseModel):
    text: str = ""


class AssistantTodoUpdateRequest(BaseModel):
    id: str = ""
    line: int = 0
    done: bool = False


class AssistantTodoEditRequest(BaseModel):
    id: str = ""
    line: int = 0
    text: str = ""
    done: bool | None = None


class CuiContactCreateRequest(BaseModel):
    name: str = ""
    email: str = ""
    phone: str = ""


class CuiContactHideRequest(BaseModel):
    key: str = ""


class _CalendarHtmlToTextParser:
    def feed(self, html_text: str) -> None:
        self.text = _html_fragment_to_plain_text(html_text)

    def close(self) -> None:
        return None

    def get_text(self) -> str:
        return getattr(self, "text", "")


def _set_dashboard_mode(mode: str) -> None:
    if mode not in {"admin", "assistant"}:
        raise SystemExit(f"Unsupported dashboard mode: {mode}")
    global _DASHBOARD_MODE
    _DASHBOARD_MODE = mode
    app.state.dashboard_mode = mode


def _assistant_mode_enabled() -> bool:
    return _DASHBOARD_MODE == "assistant"


def _dashboard_mode_bootstrap_js() -> str:
    mode = _DASHBOARD_MODE if _DASHBOARD_MODE in {"admin", "assistant"} else "admin"
    return f'window.__HERMES_DASHBOARD_MODE__="{mode}";'


def _assistant_ui_bootstrap_js() -> str:
    mode = _DASHBOARD_MODE if _DASHBOARD_MODE in {"admin", "assistant"} else "admin"
    config = load_config()
    user_display_name = _assistant_user_display_name() if mode == "assistant" else None
    agent_display_name = _assistant_display_name_from_config(config) if mode == "assistant" else None
    assistant_ui_locale = _assistant_ui_locale_from_config(config) if mode == "assistant" else None
    return (
        _dashboard_mode_bootstrap_js()
        +
        f"window.__HERMES_USER_DISPLAY_NAME__={json.dumps(user_display_name)};"
        f"window.__HERMES_AGENT_DISPLAY_NAME__={json.dumps(agent_display_name)};"
        f"window.__AIWERK_CUI_LOCALE__={json.dumps(assistant_ui_locale)};"
    )


def mount_spa(application: FastAPI):
    """AIWerk bootstrap composition over the extracted upstream SPA owner."""
    return _mount_spa_impl(
        application,
        bootstrap_js_getter=lambda: _assistant_ui_bootstrap_js(),
    )


def _assistant_api_allowed(path: str, method: str) -> bool:
    method = method.upper()
    if method in {"HEAD", "OPTIONS"}:
        method = "GET"
    if method == "GET":
        return path in _ASSISTANT_HTTP_GET_EXACT or path == "/api/sessions" or path.startswith(_ASSISTANT_HTTP_GET_PREFIXES)
    if method == "POST":
        return path in _ASSISTANT_HTTP_POST_EXACT
    return False


@app.middleware("http")
async def _assistant_mode_http_gate(request: Request, call_next):
    if _assistant_mode_enabled() and request.url.path.startswith("/api/"):
        if not _assistant_api_allowed(request.url.path, request.method):
            return JSONResponse(status_code=404, content={"detail": "Not found"})
    return await call_next(request)


def _assistant_identity_complete(identity: Any) -> bool:
    if not isinstance(identity, dict):
        return False
    role = str(identity.get("role") or "").strip().lower()
    actor_id = str(identity.get("actor_id") or identity.get("user_id") or "").strip()
    tenant_id = str(identity.get("tenant_id") or "").strip()
    return role in _ASSISTANT_ALLOWED_ROLES and bool(actor_id and tenant_id)


def _inject_trusted_cui_actor(params: Dict[str, Any], identity: Dict[str, Any]) -> None:
    for key in _ASSISTANT_ACTOR_PARAM_KEYS:
        params.pop(key, None)
    params["_cui_actor_role"] = str(identity.get("role") or "").strip().lower()
    params["_cui_actor_id"] = str(identity.get("actor_id") or identity.get("user_id") or "").strip()
    params["_cui_tenant_id"] = str(identity.get("tenant_id") or "").strip()


def _param_keys(params: Dict[str, Any]) -> set[str]:
    return set(params) - set(_ASSISTANT_ACTOR_PARAM_KEYS)


def _assistant_ws_request_gate(request: Any, auth_identity: Any = None) -> str | None:
    if not _assistant_identity_complete(auth_identity):
        return "authenticated customer identity required in assistant mode"
    if not isinstance(request, dict):
        return "invalid request"
    method = request.get("method")
    params = request.get("params")
    if params is None:
        params = {}
    if method not in _ASSISTANT_ALLOWED_RPC_METHODS:
        return "method is not available in assistant mode"
    if not isinstance(params, dict):
        return "params must be an object"
    supplied_actor_keys = set(params) & _ASSISTANT_ACTOR_PARAM_KEYS
    if supplied_actor_keys:
        actor_keys_match = (
            params.get("_cui_actor_role") == str(auth_identity.get("role") or "").strip().lower()
            and params.get("_cui_actor_id") == str(auth_identity.get("actor_id") or auth_identity.get("user_id") or "").strip()
            and params.get("_cui_tenant_id") == str(auth_identity.get("tenant_id") or "").strip()
            and "actor_role" not in params
        )
        if method != "session.create" and not actor_keys_match:
            return "request parameters are not available in assistant mode"

    if method == "gateway.ping":
        allowed = set()
    elif method == "session.create":
        allowed = {"source", "close_on_disconnect", "cols", "title"}
        if params.get("source") != "web" or params.get("close_on_disconnect") is not True:
            return "session.create is restricted in assistant mode"
    elif method == "session.events.since":
        allowed = {"session_id", "last_seen"}
        last_seen = params.get("last_seen")
        if not isinstance(last_seen, int) or isinstance(last_seen, bool) or last_seen < 0:
            return "invalid replay cursor"
    elif method in {"config.get", "config.set"}:
        allowed = {"session_id", "key"} | ({"value"} if method == "config.set" else set())
        if params.get("key") not in _ASSISTANT_ALLOWED_CONFIG_KEYS:
            return "config key is not available in assistant mode"
    elif method == "approval.respond":
        allowed = {"session_id", "request_id", "choice"}
        if not params.get("request_id") or params.get("choice") != "once":
            return "approval response is restricted in assistant mode"
    elif method == "commands.catalog":
        allowed = {"session_id"}
    elif method == "session.resume":
        allowed = {"session_id", "cols"}
        if params.get("cols") != 100:
            return "session.resume is restricted in assistant mode"
    elif method == "slash.exec":
        allowed = {"session_id", "command"}
        command = str(params.get("command") or "").strip()
        command_parts = (command[1:] if command.startswith("/") else command).split(maxsplit=1)
        command_name = command_parts[0] if command_parts else ""
        if command_name not in _ASSISTANT_ALLOWED_SLASH_COMMANDS:
            return "slash command is not available in assistant mode"
    elif method in {"prompt.submit", "prompt.learn", "session.steer"}:
        allowed = {"session_id", "text"}
        if not str(params.get("text") or "").strip():
            return "text is required"
    elif method == "session.title":
        allowed = {"session_id", "title"}
    elif method == "session.notes":
        allowed = {"session_id", "limit"}
        if not isinstance(params.get("limit"), int) or isinstance(params.get("limit"), bool) or params.get("limit") < 1:
            return "limit is required"
    else:
        allowed = {"session_id"}

    if not _param_keys(params).issubset(allowed):
        return "request parameters are not available in assistant mode"
    if method not in {"gateway.ping", "commands.catalog", "session.create"} and not str(params.get("session_id") or "").strip():
        return "session_id is required"
    _inject_trusted_cui_actor(params, auth_identity)
    return None


def _coerce_mapping(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = _json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except _json.JSONDecodeError:
            return {}
    return {}


def _session_model_config(row: Dict[str, Any]) -> Dict[str, Any]:
    return _coerce_mapping(row.get("model_config"))


def _cui_actor_context_from_request(request: Any) -> Dict[str, str]:
    session = getattr(getattr(request, "state", None), "session", None)
    if session is None:
        return {}
    role = str(getattr(session, "role", "") or "").strip().lower()
    actor_id = str(getattr(session, "actor_id", "") or getattr(session, "user_id", "") or "").strip()
    tenant_id = str(getattr(session, "tenant_id", "") or "").strip()
    if role in _ASSISTANT_ALLOWED_ROLES and actor_id and tenant_id:
        return {"tenant_id": tenant_id, "actor_id": actor_id, "role": role}
    from hermes_cli.dashboard_auth.identity import normalize_role
    if normalize_role(role) is not None and actor_id and tenant_id:
        return {}
    return {"_restricted": "1"}


def _session_visible_to_cui_actor(row: Dict[str, Any], actor: Dict[str, Any] | None) -> bool:
    if not row:
        return False
    if not actor:
        return True
    if actor.get("_restricted"):
        return False
    config = _session_model_config(row)
    return (
        config.get("_cui_visibility_scope") == "customer"
        and config.get("_cui_actor_role") in _ASSISTANT_ALLOWED_ROLES
        and config.get("_cui_actor_id") == actor.get("actor_id")
        and config.get("_cui_tenant_id") == actor.get("tenant_id")
    )


class _CuiSessionNotFound(Exception):
    pass


def _scoped_session_list_row_visible(db: Any, row: Dict[str, Any], actor: Dict[str, Any]) -> bool:
    if not _session_visible_to_cui_actor(row, actor):
        return False
    projected_id = row.get("id")
    lineage_root_id = row.get("_lineage_root_id")
    if not projected_id or not lineage_root_id or projected_id == lineage_root_id:
        return True
    return _session_visible_to_cui_actor(db.get_session(projected_id), actor)


class _CuiActorScopedSessionDB:
    def __init__(self, db: Any, actor: Dict[str, Any]) -> None:
        self._db = db
        self._actor = actor

    def _visible_rows(self) -> list[Dict[str, Any]]:
        rows: list[Dict[str, Any]] = []
        offset = 0
        page_size = 100
        while True:
            page = self._db.list_sessions_rich(limit=page_size, offset=offset)
            if not page:
                break
            rows.extend(
                row for row in page
                if _scoped_session_list_row_visible(self._db, row, self._actor)
            )
            if len(page) < page_size:
                break
            offset += page_size
        return rows

    def list_sessions_rich(self, *, limit: int, offset: int = 0, **_kwargs: Any) -> list[Dict[str, Any]]:
        return self._visible_rows()[offset: offset + limit]

    def session_count(self, **_kwargs: Any) -> int:
        return len(self._visible_rows())

    def get_session(self, sid: str) -> Dict[str, Any] | None:
        row = self._db.get_session(sid)
        return row if row and _session_visible_to_cui_actor(row, self._actor) else None

    def get_session_by_title(self, title: str) -> Dict[str, Any] | None:
        row = self._db.get_session_by_title(title)
        return row if row and _session_visible_to_cui_actor(row, self._actor) else None

    def rename_session(self, sid: str, title: str) -> Any:
        if not self.get_session(sid):
            raise _CuiSessionNotFound(sid)
        return self._db.rename_session(sid, title)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._db, name)


_PUBLIC_SAFE_URI_RE = re.compile(
    r"\b(?:https?://|shared://)[^\s<>'\"]+", re.IGNORECASE
)
_PUBLIC_LOCAL_PATH_RE = re.compile(
    r"(?ix)"
    r"(?<![\w:/\\])(?:"
    r"/+[^ \t\r\n,'\")<>\]\[{}]+"
    r"|[A-Z]:[\\/][^ \t\r\n,'\")<>\]\[{}]+"
    r"|(?:\\\\|//)[^\\/\s,'\")<>\]\[{}]+[\\/][^ \t\r\n,'\")<>\]\[{}]+"
    r")"
)


def _redact_public_path_text(value: str) -> str:
    placeholders: dict[str, str] = {}

    def protect_uri(match: re.Match[str]) -> str:
        marker = f"\x00HERMES_PUBLIC_URI_{len(placeholders)}\x00"
        placeholders[marker] = match.group(0)
        return marker

    protected = _PUBLIC_SAFE_URI_RE.sub(protect_uri, value)
    redacted = _PUBLIC_LOCAL_PATH_RE.sub("[REDACTED_PATH]", protected)
    for marker, original in placeholders.items():
        redacted = redacted.replace(marker, original)
    return redacted


def _redact_public_paths(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_public_path_text(value)
    if isinstance(value, list):
        return [_redact_public_paths(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_public_paths(val) for key, val in value.items()}
    return value


def _redact_sensitive_text(value: str) -> str:
    from agent.tool_argument_projection import sanitize_tool_display_text

    return _redact_public_path_text(sanitize_tool_display_text(value))


def _sanitize_public_message_value(value: Any) -> Any:
    from agent.tool_argument_projection import sanitize_tool_display_value

    if isinstance(value, dict):
        value = {
            key: val
            for key, val in value.items()
            if key not in {"model_config", "cwd", "host_path", "headers"}
        }
    return _redact_public_paths(sanitize_tool_display_value(value))


_SESSION_LIST_PUBLIC_DERIVED_FIELDS = {
    "last_active",
    "preview",
    "summary",
    "topics",
    "is_active",
    "profile",
    "is_default_profile",
}
_CUI_PUBLIC_MESSAGE_FIELDS = {
    "id",
    "role",
    "content",
    "display_content",
    "timestamp",
    "tool_call_id",
    "tool_name",
    "reasoning",
    "reasoning_content",
    "reasoning_details",
    "display_kind",
    "display_metadata",
}


def _project_session_list_rows_public(rows: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
    allowed = {
        "id",
        "title",
        "started_at",
        "ended_at",
        "source",
        "message_count",
    } | _SESSION_LIST_PUBLIC_DERIVED_FIELDS
    return [{key: _sanitize_public_message_value(row.get(key)) for key in allowed if key in row} for row in rows]


def _project_cui_message_rows_public(rows: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
    allowed = set(_CUI_PUBLIC_MESSAGE_FIELDS) | {"created_at", "type"}
    return [{key: _sanitize_public_message_value(row.get(key)) for key in allowed if key in row} for row in rows]


def _enforce_cui_session_visible(db: Any, session_id: str, actor: Dict[str, Any] | None) -> None:
    if actor and not _session_visible_to_cui_actor(db.get_session(session_id) or {}, actor):
        raise HTTPException(status_code=404, detail="Session not found")


def _list_sessions_rich_all(db: Any, **kwargs: Any) -> list[Dict[str, Any]]:
    """Page a rich-session query to exhaustion without per-page pinned backfill."""
    page_size = 1000
    offset = 0
    rows: list[Dict[str, Any]] = []
    query = dict(kwargs)
    query.pop("limit", None)
    query.pop("offset", None)
    # Exhaustive pagination visits pinned rows in their natural SQL position.
    # Per-page pinned backfill augments page length and corrupts base-row offsets.
    query["include_pinned"] = False
    while True:
        page = db.list_sessions_rich(limit=page_size, offset=offset, **query)
        rows.extend(page)
        if len(page) < page_size:
            break
        offset += len(page)
    return rows


def _session_hidden_from_cui_recents(row: Dict[str, Any]) -> bool:
    return bool(row.get("hidden_from_cui") or row.get("cui_hidden"))


def _session_gateway_subject_id(row: Dict[str, Any]) -> str:
    config = _session_model_config(row)
    return str(config.get("_cui_actor_id") or row.get("gateway_subject_id") or "")


def _cui_actor_owns_gateway_session(row: Dict[str, Any], actor: Dict[str, Any]) -> bool:
    return _session_visible_to_cui_actor(row, actor)


def _dashboard_user_for_cui_actor(actor: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": actor.get("actor_id"), "tenant_id": actor.get("tenant_id"), "role": actor.get("role")}


_MCP_BRIDGE_SESSIONS: dict[str, str] = {}
_MCP_BRIDGE_REQUEST_IDS: dict[str, int] = {}
_MCP_BRIDGE_LOCK = threading.Lock()
_MCP_BRIDGE_FINGERPRINT_KEY = secrets.token_bytes(32)
_AIWERK_BRIDGE_READ_TOOLS = frozenset({
    "list_subservers",
    "list_tools",
    "search_gmail_messages",
    "get_gmail_messages_content_batch",
    "list_calendar_events",
    "search_calendar_events",
    "get_calendar_event",
    "search_contacts",
    "list_contacts",
    "get_contacts",
    "get_events",
    "get-calendar-view",
    "get-specific-calendar-view",
    "get-calendar-event",
    "get-specific-calendar-event",
    "health_check",
})


def _mcp_bridge_actor_scope() -> Dict[str, Any]:
    actor = _current_http_cui_actor.get()
    return dict(actor) if isinstance(actor, dict) else {}


def _expand_env_refs(value: Any, env: Dict[str, str]) -> Any:
    if isinstance(value, str):
        return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", lambda m: os.environ.get(m.group(1), env.get(m.group(1), "")), value)
    if isinstance(value, dict):
        return {key: _expand_env_refs(val, env) for key, val in value.items()}
    if isinstance(value, list):
        return [_expand_env_refs(item, env) for item in value]
    return value


def _mcp_bridge_config(config: Dict[str, Any] | None = None) -> Dict[str, Any]:
    raw = (config or load_config()).get("mcp_servers", {}).get("aiwerk_bridge", {})
    bridge = _expand_env_refs(raw, load_env())
    return bridge if isinstance(bridge, dict) else {}


def _secret_free_bridge_config(config: Dict[str, Any]) -> Dict[str, Any]:
    bridge = _mcp_bridge_config(config)
    return {key: value for key, value in bridge.items() if key.lower() not in {"headers", "authorization", "token", "api_key"}}


def _hash_payload(payload: Dict[str, Any]) -> str:
    raw = _json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return _hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _mcp_bridge_authority_fingerprint(config: Dict[str, Any]) -> str:
    """Process-local, non-reversible identity for the expanded bridge authority."""
    raw = _json.dumps(
        _mcp_bridge_config(config), sort_keys=True, separators=(",", ":"), default=str
    )
    return hmac.new(
        _MCP_BRIDGE_FINGERPRINT_KEY, raw.encode("utf-8"), _hashlib.sha256
    ).hexdigest()


def _mcp_bridge_session_key(config: Dict[str, Any] | None = None) -> str:
    resolved = config or load_config()
    payload = {
        "bridge": _secret_free_bridge_config(resolved),
        "authority": _mcp_bridge_authority_fingerprint(resolved),
        "actor": _mcp_bridge_actor_scope(),
    }
    return f"aiwerk:{_hash_payload(payload)}"


def _mcp_bridge_next_request_id(session_key: str) -> int:
    with _MCP_BRIDGE_LOCK:
        next_id = _MCP_BRIDGE_REQUEST_IDS.get(session_key, 1)
        _MCP_BRIDGE_REQUEST_IDS[session_key] = next_id + 1
    return next_id


def _parse_mcp_bridge_response(raw: bytes | str, *, content_type: str = "application/json", request_id: int | None = 1) -> Dict[str, Any]:
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    if "text/event-stream" in (content_type or ""):
        payloads = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
        text = payloads[-1] if payloads else "{}"
    payload = _json.loads(text or "{}")
    if request_id is not None and (not isinstance(payload.get("id"), int) or isinstance(payload.get("id"), bool) or payload.get("id") != request_id):
        raise RuntimeError("MCP bridge response request id mismatch")
    return payload


def _mcp_bridge_rpc(config: Dict[str, Any], method: str, params: Dict[str, Any], *, session_id: str | None = None, request_id: int | None = 1) -> tuple[Dict[str, Any], str | None]:
    bridge = _mcp_bridge_config(config)
    url = str(bridge.get("url") or "").strip()
    if not url:
        raise RuntimeError("AIWerk bridge MCP URL is not configured")
    body: Dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params}
    if request_id is not None:
        body["id"] = request_id
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    headers.update(bridge.get("headers") or {})
    if session_id:
        headers["MCP-Session-Id"] = session_id
    request = urllib.request.Request(url, data=_json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=float(bridge.get("timeout", 30))) as response:
        content_type = response.headers.get("Content-Type", "")
        response_session = response.headers.get("MCP-Session-Id") or session_id
        return _parse_mcp_bridge_response(response.read(), content_type=content_type, request_id=request_id), response_session


def _mcp_bridge_initialize(config: Dict[str, Any], session_key: str) -> str:
    result, session_id = _mcp_bridge_rpc(
        config,
        "initialize",
        {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "hermes-dashboard", "version": __version__}},
        request_id=1,
    )
    if not session_id:
        session_id = str((result.get("result") or {}).get("session_id") or "")
    _mcp_bridge_rpc(config, "notifications/initialized", {}, session_id=session_id, request_id=None)
    with _MCP_BRIDGE_LOCK:
        _MCP_BRIDGE_SESSIONS[session_key] = session_id
        _MCP_BRIDGE_REQUEST_IDS[session_key] = 2
    return session_id


def _mcp_bridge_session(config: Dict[str, Any]) -> tuple[str, str]:
    key = _mcp_bridge_session_key(config)
    with _MCP_BRIDGE_LOCK:
        session_id = _MCP_BRIDGE_SESSIONS.get(key)
    if not session_id:
        session_id = _mcp_bridge_initialize(config, key)
    return key, session_id


def _mcp_bridge_forget_session(session_key: str) -> None:
    with _MCP_BRIDGE_LOCK:
        _MCP_BRIDGE_SESSIONS.pop(session_key, None)
        _MCP_BRIDGE_REQUEST_IDS.pop(session_key, None)


def _mcp_bridge_forget_all_sessions() -> None:
    with _MCP_BRIDGE_LOCK:
        _MCP_BRIDGE_SESSIONS.clear()
        _MCP_BRIDGE_REQUEST_IDS.clear()


def _mcp_bridge_router_call(config: Dict[str, Any], server: str, tool: str, params: Dict[str, Any]) -> Dict[str, Any]:
    key, session_id = _mcp_bridge_session(config)
    request_id = _mcp_bridge_next_request_id(key)
    payload = {"name": "mcp", "arguments": {"server": server, "tool": tool, "params": params}}
    result, _ = _mcp_bridge_rpc(config, "tools/call", payload, session_id=session_id, request_id=request_id)
    return result


def _call_aiwerk_bridge_tool(config: Dict[str, Any], *, server: str, tool: str, params: Dict[str, Any]) -> Dict[str, Any]:
    if tool not in _AIWERK_BRIDGE_READ_TOOLS:
        raise ValueError("AIWerk bridge dashboard access is read-only")
    result = _mcp_bridge_router_call(config, server, tool, params)
    content = ((result.get("result") or {}).get("content") or [])
    if content and isinstance(content[0], dict) and isinstance(content[0].get("text"), str):
        try:
            return _json.loads(content[0]["text"])
        except _json.JSONDecodeError:
            return {"text": content[0]["text"]}
    return result


def _bridge_error_status(exc: BaseException) -> str:
    text = str(exc).lower()
    return "auth_required" if "auth" in text or "401" in text or "403" in text else "error"


def _parse_gmail_bridge_date(value: Any) -> str:
    if not value:
        return ""
    try:
        timestamp = int(value)
        if timestamp > 10_000_000_000:
            timestamp = timestamp // 1000
        return _datetime.fromtimestamp(timestamp, tz=_timezone.utc).isoformat()
    except Exception:
        return str(value)


def _gmail_bridge_metadata_to_items(payload: Dict[str, Any]) -> list[Dict[str, Any]]:
    items = payload.get("messages") or payload.get("items") or []
    if isinstance(items, list) and items:
        return [dict(item) for item in items if isinstance(item, dict)]
    parsed: list[Dict[str, Any]] = []
    for block in re.split(r"\n(?=Message ID:\s*)", _extract_bridge_text(payload) or ""):
        if "Message ID:" not in block:
            continue
        item: Dict[str, Any] = {}
        for line in block.splitlines():
            key, _, value = line.partition(":")
            normalized = key.strip().lower()
            if not normalized or not value:
                continue
            field = "id" if normalized == "message id" else normalized.replace("-", "_")
            item[field] = value.strip()
        if item:
            parsed.append(item)
    return parsed


def _parse_gmail_bridge_metadata_blocks(payload: Dict[str, Any]) -> list[Dict[str, Any]]:
    return _gmail_bridge_metadata_to_items(payload)


def _gmail_bridge_search_message_ids(
    config: Dict[str, Any],
    query: str = "",
    *,
    server: str | None = None,
    user_google_email: str | None = None,
    page_size: int | None = None,
) -> list[str]:
    params: Dict[str, Any] = {"query": query}
    if user_google_email:
        params["user_google_email"] = user_google_email
    if page_size is not None:
        params["page_size"] = page_size
    payload = _call_aiwerk_bridge_tool(
        config,
        server=server or "google-workspace-aiwerk",
        tool="search_gmail_messages",
        params=params,
    )
    return [str(item.get("id")) for item in _gmail_bridge_metadata_to_items(payload) if item.get("id")]


def _gmail_bridge_metadata_items_for_ids(
    config: Dict[str, Any],
    message_ids: list[str],
    *,
    server: str | None = None,
    user_google_email: str | None = None,
    format: str = "metadata",
) -> list[Dict[str, Any]]:
    params: Dict[str, Any] = {"message_ids": message_ids}
    if user_google_email:
        params["user_google_email"] = user_google_email
    if format:
        params["format"] = format
    payload = _call_aiwerk_bridge_tool(
        config,
        server=server or "google-workspace-aiwerk",
        tool="get_gmail_messages_content_batch",
        params=params,
    )
    return _gmail_bridge_metadata_to_items(payload)


def _gmail_bridge_message_items(
    config: Dict[str, Any],
    query: str = "",
    *,
    server: str | None = None,
    user_google_email: str | None = None,
    page_size: int | None = None,
) -> list[Dict[str, Any]]:
    ids = _gmail_bridge_search_message_ids(
        config,
        query,
        server=server,
        user_google_email=user_google_email,
        page_size=page_size,
    )
    return _gmail_bridge_metadata_items_for_ids(
        config,
        ids,
        server=server,
        user_google_email=user_google_email,
    ) if ids else []


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


def _aiwerk_bridge_subserver_label(name: str, details: Dict[str, Any] | None = None) -> str:
    if details and details.get("label"):
        return str(details["label"])
    clean = str(name or "").strip()
    return _AIWERK_BRIDGE_SUBSERVER_LABELS.get(clean, clean.replace("_", " ").replace("-", " ").title())


def _aiwerk_bridge_subserver_description(name: str, details: Dict[str, Any] | None = None) -> str:
    if details:
        for key in ("description", "summary"):
            value = str(details.get(key) or "").strip()
            if value:
                return value
    clean = str(name or "").strip()
    if clean.startswith("google-workspace-"):
        return "Gmail, Kalender und Drive"
    return _AIWERK_BRIDGE_SUBSERVER_DESCRIPTIONS.get(clean, "MCP-Werkzeug über Bridge")


def _aiwerk_bridge_catalog_slug(name: str, details: Dict[str, Any] | None = None) -> str:
    if details:
        for key in ("catalog_slug", "catalog"):
            value = str(details.get(key) or "").strip()
            if value:
                return value
    clean = str(name or "").strip()
    if clean.startswith("google-workspace-"):
        return "google-workspace"
    return _AIWERK_BRIDGE_CATALOG_SLUGS.get(clean, clean)


def _safe_resource_id(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "").strip()).strip("-").lower()


def _resource_status_label(status: str) -> str:
    return {
        "connected": "Verbunden",
        "auth_required": "Anmeldung nötig",
        "limited": "Eingeschränkt",
        "disabled": "Deaktiviert",
        "error": "Fehler",
    }.get(status, "Eingeschränkt")


def _normalize_aiwerk_bridge_subserver_status(value: Any) -> str:
    if isinstance(value, dict):
        if value.get("enabled", True) is False:
            return "disabled"
        value = value.get("status") or value.get("state") or "connected"
    elif value in (None, ""):
        return "connected"
    text = str(value or "").strip().lower()
    if text in {"connected", "running", "ready", "ok", "healthy"}:
        return "connected"
    if text in {"auth_required"}:
        return "auth_required"
    if text in {"disabled", "off"}:
        return "disabled"
    if text in {"error", "failed", "unhealthy"}:
        return "error"
    if text in {"disconnected", "stopped", "starting", "pending", "idle", "lazy"}:
        return "limited"
    return "limited" if text else "connected"


def _aiwerk_bridge_subserver_item(item: Dict[str, Any]) -> Dict[str, Any]:
    name = str(item.get("name") or item.get("id") or "")
    status = _normalize_aiwerk_bridge_subserver_status(item)
    catalog_slug = _aiwerk_bridge_catalog_slug(name, item)
    return {
        "id": f"aiwerk-bridge-{_safe_resource_id(name)}",
        "name": name,
        "label": _aiwerk_bridge_subserver_label(name, item),
        "description": _aiwerk_bridge_subserver_description(name, item),
        "catalog_slug": catalog_slug,
        "status": status,
        "status_label": _resource_status_label(status),
        "capabilities": ["Bridge-Subserver"],
        "open_url": f"https://aiwerkmcp.com/#/catalog/{urllib.parse.quote(catalog_slug, safe='-_.~')}"
        if catalog_slug else "",
    }


def _aiwerk_bridge_live_subservers(config: Dict[str, Any]) -> list[Dict[str, Any]]:
    result = _mcp_bridge_router_call(config, "aiwerk", "list_subservers", {})
    content = ((result.get("result") or {}).get("content") or [])
    payload: Dict[str, Any] = {}
    if content and isinstance(content[0], dict):
        try:
            payload = _json.loads(content[0].get("text") or "{}")
        except _json.JSONDecodeError:
            payload = {}
    return [_aiwerk_bridge_subserver_item(item) for item in payload.get("servers", []) if isinstance(item, dict)]


def _aiwerk_bridge_subservers(config: Dict[str, Any]) -> list[Dict[str, Any]]:
    servers = config.get("mcp_servers") if isinstance(config.get("mcp_servers"), dict) else {}
    bridge = servers.get("aiwerk_bridge") if isinstance(servers, dict) else {}
    configured = bridge.get("subservers") if isinstance(bridge, dict) else None
    if isinstance(configured, dict) and configured:
        return [
            _aiwerk_bridge_subserver_item({"id": name, **(details if isinstance(details, dict) else {})})
            for name, details in configured.items()
        ]
    try:
        return _aiwerk_bridge_live_subservers(config)
    except Exception as exc:
        return [{"id": "aiwerk-bridge", "name": "aiwerk_bridge", "label": "AIWerk Bridge", "status": _bridge_error_status(exc)}]


def _vault_bridge_summary(_config: Dict[str, Any] | None = None) -> Dict[str, Any]:
    return {"status": "unknown", "summary": "Vault status unavailable", "items": [], "weak_count": 0, "reused_count": 0, "compromised_count": 0}


_ASSISTANT_RESOURCE_CACHE_TTLS = {
    "email": 60 * 60,
    "calendar": 30 * 60,
    "shared_folder": 60 * 60,
    "vault": 15 * 60,
    "todos": 60,
    "contacts": 30 * 60,
    "connectors": 60 * 60,
}
_ASSISTANT_RESOURCE_CACHE_ENV_KEYS = (
    "AIWERK_CUI_AGENT_NAME",
    "AIWERK_CUI_CALENDAR_HORIZON_DAYS",
    "AIWERK_CUI_CALENDAR_MAX_RESULTS",
    "AIWERK_CUI_CALENDAR_SUMMARY_JSON",
    "AIWERK_CUI_CONTACTS_DISABLE_AIWERK_BRIDGE",
    "AIWERK_CUI_CONTACTS_DISABLE_GMAIL_INTERACTIONS",
    "AIWERK_CUI_CONTACTS_DISABLE_HIMALAYA_INTERACTIONS",
    "AIWERK_CUI_CONTACTS_HIMALAYA_INBOX_FOLDER",
    "AIWERK_CUI_CONTACTS_HIMALAYA_SENT_FOLDER",
    "AIWERK_CUI_CONTACTS_INBOX_QUERY",
    "AIWERK_CUI_CONTACTS_INTERACTION_SCAN_LIMIT",
    "AIWERK_CUI_CONTACTS_PAGE_SIZE",
    "AIWERK_CUI_CONTACTS_RELEVANCE_WINDOW_DAYS",
    "AIWERK_CUI_CONTACTS_SAVED_TOP_UP_TARGET",
    "AIWERK_CUI_CONTACTS_SENT_QUERY",
    "AIWERK_CUI_EMAIL_ACCOUNT",
    "AIWERK_CUI_EMAIL_BACKEND",
    "AIWERK_CUI_EMAIL_DISABLE_AIWERK_BRIDGE",
    "AIWERK_CUI_EMAIL_DISABLE_HIMALAYA",
    "AIWERK_CUI_EMAIL_FOLDER",
    "AIWERK_CUI_EMAIL_SUMMARY_JSON",
    "AIWERK_CUI_GMAIL_LATEST_QUERY",
    "AIWERK_CUI_GMAIL_UNREAD_QUERY",
    "AIWERK_CUI_GOOGLE_EMAIL",
    "AIWERK_CUI_GOOGLE_WORKSPACE_SERVER",
    "AIWERK_CUI_LANGUAGE",
    "AIWERK_CUI_MAILDIR",
    "AIWERK_CUI_SUPPORT_LOG",
    "AIWERK_CUI_SUPPORT_TARGET",
    "AIWERK_CUI_USER_DISPLAY_NAME",
    "AIWERK_CUI_USER_NAME",
    "AIWERK_CUI_VAULT_SUMMARY_JSON",
    "AIWERK_CUI_VAULT_URL",
    "AIWERK_SHARED_FOLDER",
    "AIWERK_SYSTEM_TARGET",
    "HERMES_CUI_LOCALE",
    "HERMES_SHARED_DIR",
    "HERMES_SHARED_FOLDER",
    "HERMES_USER_DISPLAY_NAME",
    "HIMALAYA_ACCOUNT",
    "HIMALAYA_FOLDER",
    "MAILDIR",
    "WHATSAPP_MODE",
    "AIWERK_CUI_SHARED_FOLDER",
    "AIWERK_CUI_CONTACTS_JSON",
    "AIWERK_CUI_TODO_PATH",
    "HERMES_CUI_ALLOW_REMOTE_FILE_MANAGER_OPEN",
)

_ASSISTANT_RESOURCE_CACHE: dict[str, Dict[str, Any]] = {}
_ASSISTANT_RESOURCE_CACHE_GENERATIONS: dict[str, int] = {}
_ASSISTANT_RESOURCE_REFRESHING: set[str] = set()
_ASSISTANT_RESOURCE_LOCK = threading.Lock()
_ASSISTANT_RESOURCE_CACHE_LOCK = _ASSISTANT_RESOURCE_LOCK


def _assistant_resource_config_signature(config: Dict[str, Any], resource: str | None = None) -> str:
    env_scope = {key: os.environ.get(key, "") for key in _ASSISTANT_RESOURCE_CACHE_ENV_KEYS}
    payload = {
        "resource": resource,
        "config": _secret_free_bridge_config(config),
        "authority": _mcp_bridge_authority_fingerprint(config),
        "actor": _mcp_bridge_actor_scope(),
        "env": env_scope,
    }
    return _hash_payload(payload)


def _assistant_write_resource_cache(
    full_key: str,
    payload: Any,
    ttl_seconds: int,
    *,
    expected_generation: int | None = None,
) -> Dict[str, Any]:
    now = time.time()
    expires_at = now + ttl_seconds
    with _ASSISTANT_RESOURCE_LOCK:
        current_generation = int(_ASSISTANT_RESOURCE_CACHE_GENERATIONS.get(full_key, 0))
        if expected_generation is not None and current_generation != expected_generation:
            return {
                "cached": False,
                "updated_at": now,
                "expires_at": expires_at,
                "ttl_seconds": ttl_seconds,
                "discarded": True,
            }
        generation = current_generation + 1
        _ASSISTANT_RESOURCE_CACHE_GENERATIONS[full_key] = generation
        _ASSISTANT_RESOURCE_CACHE[full_key] = {
            "payload": copy.deepcopy(payload),
            "updated_at": now,
            "expires_at": expires_at,
            "ttl_seconds": ttl_seconds,
            "generation": generation,
        }
    return {"cached": False, "updated_at": now, "expires_at": expires_at, "ttl_seconds": ttl_seconds}


def _assistant_schedule_resource_refresh(full_key: str, builder, ttl_seconds: int, expected_generation: int | None = None) -> bool:
    actor = _mcp_bridge_actor_scope()
    with _ASSISTANT_RESOURCE_LOCK:
        if full_key in _ASSISTANT_RESOURCE_REFRESHING:
            return False
        _ASSISTANT_RESOURCE_REFRESHING.add(full_key)
        generation = expected_generation
        if generation is None:
            generation = int(_ASSISTANT_RESOURCE_CACHE_GENERATIONS.get(full_key, 0))

    def refresh() -> None:
        token = _current_http_cui_actor.set(actor or None)
        try:
            payload = builder()
            _assistant_write_resource_cache(
                full_key,
                payload,
                ttl_seconds,
                expected_generation=generation,
            )
        except Exception:
            _log.exception("Background assistant resource refresh failed for %s", full_key.split(":", 1)[0])
        finally:
            _current_http_cui_actor.reset(token)
            with _ASSISTANT_RESOURCE_LOCK:
                _ASSISTANT_RESOURCE_REFRESHING.discard(full_key)

    thread = threading.Thread(target=refresh, name="assistant-resource-refresh", daemon=True)
    thread.start()
    return True


def _assistant_cached_resource(
    name: str,
    ttl_seconds: int,
    cache_key: str,
    builder,
    *,
    stale_while_revalidate: bool = False,
    force_refresh: bool = False,
    initial_payload: Any | None = None,
):
    full_key = f"{name}:{cache_key}"
    now = time.time()
    with _ASSISTANT_RESOURCE_LOCK:
        if force_refresh:
            _ASSISTANT_RESOURCE_CACHE_GENERATIONS[full_key] = int(_ASSISTANT_RESOURCE_CACHE_GENERATIONS.get(full_key, 0)) + 1
        entry = _ASSISTANT_RESOURCE_CACHE.get(full_key)
    if entry and not force_refresh and entry["expires_at"] > now:
        return copy.deepcopy(entry["payload"]), {"cached": True, "updated_at": entry["updated_at"], "expires_at": entry["expires_at"], "ttl_seconds": ttl_seconds}
    if entry and not force_refresh and stale_while_revalidate:
        scheduled = _assistant_schedule_resource_refresh(full_key, builder, ttl_seconds, expected_generation=int(entry.get("generation", 0)))
        return copy.deepcopy(entry["payload"]), {
            "cached": True,
            "stale": True,
            "refreshing": scheduled,
            "updated_at": entry["updated_at"],
            "expires_at": entry["expires_at"],
            "ttl_seconds": ttl_seconds,
        }
    if not entry and not force_refresh and stale_while_revalidate and initial_payload is not None:
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
    except Exception:
        with _ASSISTANT_RESOURCE_LOCK:
            entry = _ASSISTANT_RESOURCE_CACHE.get(full_key)
        if not entry:
            raise
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
    meta = _assistant_write_resource_cache(full_key, payload, ttl_seconds)
    return copy.deepcopy(payload), meta


def _json_file_payload(env_name: str) -> Dict[str, Any] | None:
    value = os.getenv(env_name)
    if not value:
        return None
    try:
        path = Path(value)
        if path.exists():
            return _json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        pass
    try:
        payload = _json.loads(value)
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


_ASSISTANT_EMAIL_PREVIEW_ITEMS = 5
_ASSISTANT_EMAIL_UNREAD_SCAN_LIMIT = 50
_ASSISTANT_CONTACT_PREVIEW_ITEMS = 20
_ASSISTANT_CONTACT_RELEVANCE_WINDOW_DAYS = 10
_ASSISTANT_CONTACT_SAVED_TOP_UP_TARGET = 16
_ASSISTANT_CONTACT_SEARCH_LIMIT = 20
_ASSISTANT_EMAIL_TIMEOUT_SECONDS = 12
_ASSISTANT_MCP_BRIDGE_TIMEOUT_SECONDS = 30
_ASSISTANT_EMAIL_BLOCKED_SENDER_DOMAINS = {"attractivewedding.info"}
_ASSISTANT_EMAIL_BRAND_DOMAINS = {"migros": {"migros.ch", "migros.com", "migrosbank.ch"}}


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(number, maximum))


def _env_truthy(name: str) -> bool:
    return str(os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _assistant_config_section(config: Dict[str, Any], *path: str) -> Dict[str, Any]:
    cur: Any = config
    for part in path:
        if not isinstance(cur, dict):
            return {}
        cur = cur.get(part)
    return cur if isinstance(cur, dict) else {}


def _assistant_email_accounts(config: Dict[str, Any]) -> list[Dict[str, Any]]:
    for section in (
        _assistant_config_section(config, "assistant", "email"),
        _assistant_config_section(config, "dashboard", "email"),
        config.get("email") if isinstance(config.get("email"), dict) else {},
    ):
        accounts = section.get("accounts") if isinstance(section, dict) else None
        if isinstance(accounts, list):
            return [dict(item) for item in accounts if isinstance(item, dict)]
    return []


def _email_backend_name(account: Dict[str, Any] | None) -> str:
    return str((account or {}).get("backend") or (account or {}).get("type") or "").strip().lower()


def _email_account_label(account: Dict[str, Any]) -> str:
    return str(account.get("label") or account.get("name") or account.get("address") or account.get("account") or "").strip()


def _email_account_address(account: Dict[str, Any]) -> str:
    return str(
        account.get("address")
        or account.get("email")
        or account.get("user_google_email")
        or account.get("google_email")
        or account.get("account")
        or ""
    ).strip()


def _email_account_configs(config: Dict[str, Any]) -> list[Dict[str, Any]]:
    accounts = _assistant_email_accounts(config)
    if accounts:
        return accounts
    backend = str(os.environ.get("AIWERK_CUI_EMAIL_BACKEND") or "").strip()
    account = os.environ.get("AIWERK_CUI_EMAIL_ACCOUNT") or os.environ.get("HIMALAYA_ACCOUNT")
    folder = os.environ.get("AIWERK_CUI_EMAIL_FOLDER") or os.environ.get("HIMALAYA_FOLDER")
    google_email = os.environ.get("AIWERK_CUI_GOOGLE_EMAIL")
    if backend or account or folder or google_email:
        resolved_backend = backend or ("google_workspace" if google_email else "himalaya")
        return [{
            "backend": resolved_backend,
            "account": account,
            "address": google_email or account or "",
            "folder": folder,
        }]
    return []


def _google_workspace_server(account: Dict[str, Any] | None = None, *, env_first: bool = True) -> str:
    account = account or {}
    env = os.environ.get("AIWERK_CUI_GOOGLE_WORKSPACE_SERVER")
    configured = account.get("server") or account.get("mcp_server")
    return str((env if env_first and env else None) or configured or env or "google-workspace-aiwerk")


def _google_workspace_user_email(account: Dict[str, Any] | None = None, *, env_first: bool = True) -> str:
    account = account or {}
    env = os.environ.get("AIWERK_CUI_GOOGLE_EMAIL")
    configured = account.get("user_google_email") or account.get("google_email") or account.get("address") or account.get("email")
    return str((env if env_first and env else None) or configured or env or "me")


def _calendar_accounts(config: Dict[str, Any]) -> list[Dict[str, Any]]:
    accounts = _assistant_config_section(config, "calendar").get("accounts")
    if isinstance(accounts, list):
        return [dict(item) for item in accounts if isinstance(item, dict)]
    accounts = _assistant_config_section(config, "dashboard", "calendar").get("accounts")
    if isinstance(accounts, list):
        return [dict(item) for item in accounts if isinstance(item, dict)]
    return [
        dict(account)
        for account in _assistant_email_accounts(config)
        if str(account.get("backend") or "").lower() in {"google_workspace", "google_calendar"}
    ]


def _extract_bridge_text(payload: Dict[str, Any]) -> str:
    if not isinstance(payload, dict):
        return ""
    result = payload.get("result") if isinstance(payload.get("result"), dict) else payload
    structured = result.get("structuredContent") if isinstance(result, dict) else None
    if isinstance(structured, dict) and isinstance(structured.get("result"), str):
        return structured["result"]
    content = result.get("content") if isinstance(result, dict) else None
    if isinstance(content, list):
        return "\n".join(str(item.get("text") or "") for item in content if isinstance(item, dict))
    return str(payload.get("text") or "")


def _bridge_payload_is_auth_error(payload: Dict[str, Any]) -> bool:
    text = repr(payload).lower()
    return bool(payload.get("isError") or (isinstance(payload.get("result"), dict) and payload["result"].get("isError"))) and (
        "auth" in text or "token" in text or "expired" in text or "revoked" in text
    )


def _status_from_summaries(summaries: list[Dict[str, Any]]) -> str:
    statuses = {item.get("status") for item in summaries}
    if "connected" in statuses:
        return "connected"
    if "auth_required" in statuses:
        return "auth_required"
    if "error" in statuses:
        return "error"
    return "not_configured"


def _resource_payload(name: str, config: Dict[str, Any] | None = None, request: Request | None = None) -> Dict[str, Any]:
    config = config or load_config()
    if name == "email":
        return _email_summary(config)
    if name == "calendar":
        return _calendar_summary(config)
    if name == "shared_folder":
        return _shared_folder_summary(config, request=request)
    if name == "vault":
        return _vaultwarden_summary(config)
    if name == "todos":
        return _todo_summary(config)
    if name == "contacts":
        return _contacts_summary(config, _email_summary(config), _calendar_summary(config))
    payload: Dict[str, Any] = {"status": "available", "summary": name.replace("_", " ").title(), "items": []}
    if name in {"email", "calendar"}:
        payload["accounts"] = []
    if name == "email":
        payload["unread_count"] = 0
    if name == "shared_folder":
        payload["can_open_folder"] = False
    if name == "vault":
        payload.update({"weak_count": 0, "reused_count": 0, "compromised_count": 0})
    if name == "todos":
        payload["open_count"] = 0
    if name == "contacts":
        payload.update({"relevant": [], "frequent": [], "total_count": 0})
    return payload


def _assistant_resources_payload(request: Request | None = None, *, force_refresh: bool = False, refresh_resource: str | None = None) -> Dict[str, Any]:
    config = load_config()
    if force_refresh and refresh_resource in {"email", "calendar", "contacts", "connectors"}:
        _mcp_bridge_forget_session(_mcp_bridge_session_key(config))
    signature = _assistant_resource_config_signature(config, None)
    resources: Dict[str, Any] = {}
    cache_meta: Dict[str, Any] = {}
    for name in ("email", "calendar", "shared_folder", "vault", "todos", "contacts"):
        payload, meta = _assistant_cached_resource(
            name,
            _ASSISTANT_RESOURCE_CACHE_TTLS[name],
            f"{signature}:{name}",
            lambda name=name: _resource_payload(name, config, request),
            force_refresh=force_refresh and refresh_resource in {None, name},
            stale_while_revalidate=bool((not force_refresh) and (refresh_resource == name or refresh_resource is None)),
        )
        if name == "contacts" and isinstance(payload, dict):
            payload = _filter_contacts_payload(
                payload,
                own_emails=_contacts_own_email_set(
                    config,
                    resources.get("email") if isinstance(resources.get("email"), dict) else {},
                    resources.get("calendar") if isinstance(resources.get("calendar"), dict) else {},
                ),
            )
        resources[name] = {**payload, "cache": meta}
        cache_meta[name] = meta
    connectors, connectors_meta = _assistant_cached_resource(
        "connectors",
        _ASSISTANT_RESOURCE_CACHE_TTLS["connectors"],
        f"{signature}:connectors",
        lambda: _connector_summary(
            config,
            resources["shared_folder"],
            resources["email"],
            resources["calendar"],
            include_live_bridge_children=not force_refresh or refresh_resource in {None, "connectors"},
        ),
        force_refresh=force_refresh and refresh_resource in {None, "connectors"},
        stale_while_revalidate=bool((not force_refresh) and (refresh_resource == "connectors" or refresh_resource is None)),
        initial_payload=[] if refresh_resource is None and not force_refresh else None,
    )
    resources["connectors"] = connectors
    cache_meta["connectors"] = connectors_meta
    resources["cache"] = {
        "cached": any(meta.get("cached") for meta in cache_meta.values()),
        "resources": cache_meta,
    }
    return resources


@app.get("/api/assistant/resources")
def get_assistant_resources(request: Request, refresh: str | None = None, resource: str | None = None) -> Dict[str, Any]:
    _require_token(request)
    return _assistant_resources_payload(request, force_refresh=bool(refresh), refresh_resource=resource)


async def _read_upload(upload: Any, limit: int) -> bytes:
    data = await upload.read(limit + 1) if hasattr(upload, "read") else b""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return data


@app.post("/api/assistant/audio/transcribe")
@app.post("/api/assistant/transcribe")
async def transcribe_assistant_audio(
    request: Request,
    file: UploadFile = File(...),
    session_id: str = Form(""),
) -> Dict[str, Any]:
    _require_token(request)
    filename = Path(str(file.filename or "voice.webm")).name
    suffix = Path(filename).suffix.lower()
    if suffix not in _ASSISTANT_AUDIO_EXTENSIONS:
        raise HTTPException(status_code=415, detail="Unsupported audio type")
    data = await _read_upload(file, _ASSISTANT_AUDIO_MAX_BYTES)
    if len(data) > _ASSISTANT_AUDIO_MAX_BYTES:
        raise HTTPException(status_code=413, detail="Audio upload is too large")
    if not data:
        raise HTTPException(status_code=400, detail="Empty audio file")
    session_part = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(session_id or "session")).strip("._-") or "session"
    target_dir = _assistant_upload_root() / session_part / f"voice-{int(time.time() * 1000)}-{secrets.token_hex(4)}"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / filename
    target.write_bytes(data)
    try:
        target.chmod(0o600)
    except Exception:
        pass
    try:
        from tools.transcription_tools import transcribe_audio
        result = await asyncio.to_thread(transcribe_audio, str(target))
    except Exception as exc:
        _log.exception("Assistant audio transcription failed")
        raise HTTPException(status_code=500, detail="Transcription failed") from exc
    if not result.get("success"):
        raise HTTPException(status_code=500, detail=result.get("error") or "Transcription failed")
    return {"text": str(result.get("transcript") or "").strip(), "provider": result.get("provider")}


@app.post("/api/assistant/audio/tts")
async def synthesize_assistant_speech(request: Request, payload: AssistantTTSRequest) -> Dict[str, Any]:
    _require_token(request)
    return {"status": "unavailable", "audio_url": None}


_ASSISTANT_TTS_CACHE: dict[str, bytes] = {}


@app.post("/api/assistant/tts")
async def synthesize_assistant_tts_alias(request: Request, payload: AssistantTTSRequest) -> Response:
    _require_token(request)
    key = _hash_payload({"text": payload.text, "voice": payload.voice})
    if key in _ASSISTANT_TTS_CACHE:
        return Response(_ASSISTANT_TTS_CACHE[key], media_type="audio/mpeg", headers={"X-Hermes-TTS-Cache": "hit"})
    from tempfile import NamedTemporaryFile
    from tools import tts_tool

    with NamedTemporaryFile(suffix=".mp3", delete=False) as fh:
        output_path = fh.name
    try:
        result = tts_tool.text_to_speech_tool(payload.text, output_path)
        data = _json.loads(result) if isinstance(result, str) else dict(result or {})
        audio_path = Path(data.get("file_path") or output_path)
        audio = audio_path.read_bytes()
    finally:
        with contextlib.suppress(OSError):
            Path(output_path).unlink()
    _ASSISTANT_TTS_CACHE[key] = audio
    return Response(audio, media_type="audio/mpeg", headers={"X-Hermes-TTS-Cache": "miss"})


_ASSISTANT_UPLOAD_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".txt", ".md",
    ".csv", ".json", ".yaml", ".yml", ".docx", ".mp3", ".m4a", ".wav",
    ".webm", ".ogg", ".aac", ".flac", ".mp4", ".mov", ".mkv",
})
_ASSISTANT_UPLOAD_EXTS = _ASSISTANT_UPLOAD_EXTENSIONS
_ASSISTANT_AUDIO_EXTENSIONS = frozenset({
    ".mp3", ".mp4", ".mpeg", ".mpga", ".m4a", ".wav", ".webm", ".ogg", ".aac", ".flac"
})
_ASSISTANT_AUDIO_MAX_BYTES = 25 * 1024 * 1024
_ASSISTANT_UPLOAD_MAX_FILES = 10
_ASSISTANT_UPLOAD_MAX_FILE_BYTES = 12 * 1024 * 1024
_ASSISTANT_UPLOAD_MAX_TOTAL_BYTES = 32 * 1024 * 1024
_ASSISTANT_UPLOAD_MAX_BYTES = _ASSISTANT_UPLOAD_MAX_FILE_BYTES
_SHARED_FOLDER_ACTIVE_CONTENT_EXTENSIONS = frozenset({
    ".html", ".htm", ".xhtml", ".xht", ".xhtm", ".shtml",
    ".svg", ".svgz", ".xml", ".xsl", ".xslt", ".js", ".mjs",
    ".cjs", ".mhtml", ".mht", ".htc",
})
_SHARED_FOLDER_ACTIVE_CONTENT_MEDIA_TYPES = frozenset({
    "text/html",
    "application/xhtml+xml",
    "image/svg+xml",
    "application/xml",
    "text/xml",
    "application/xslt+xml",
    "text/javascript",
    "application/javascript",
    "application/ecmascript",
    "text/ecmascript",
    "text/x-component",
    "message/rfc822",
})
_ASSISTANT_RESOURCE_HIDDEN_NAMES = frozenset({
    ".env",
    ".env.local",
    ".envrc",
    "config.yaml",
    "auth.json",
    "credentials.json",
    "id_rsa",
    "id_ed25519",
    "known_hosts",
})
_ASSISTANT_SHARED_FILE_OPEN_MAX_BYTES = 100 * 1024 * 1024
_ASSISTANT_RESOURCE_MAX_SHARED_ITEMS = 40
_ASSISTANT_RESOURCE_MAX_SHARED_DEPTH = 5
_ASSISTANT_RESOURCE_DEFAULT_VISIBLE_ITEMS = 12
_ASSISTANT_ARTIFACT_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})
_ASSISTANT_ARTIFACT_EXTENSIONS = (
    _ASSISTANT_UPLOAD_EXTS | _ASSISTANT_ARTIFACT_IMAGE_EXTENSIONS | _SHARED_FOLDER_ACTIVE_CONTENT_EXTENSIONS
)
_ASSISTANT_ARTIFACT_MAX_BYTES = 25 * 1024 * 1024


def _is_active_shared_media_type(name: str, media_type: str) -> bool:
    guessed = (mimetypes.guess_type(name)[0] or "").lower()
    candidate = (media_type or "").split(";", 1)[0].strip().lower()
    return (
        any(item in _SHARED_FOLDER_ACTIVE_CONTENT_MEDIA_TYPES or item.endswith("+xml") for item in (guessed, candidate))
        or Path(name).suffix.lower() in _SHARED_FOLDER_ACTIVE_CONTENT_EXTENSIONS
    )


def _safe_shared_open_disposition(
    name: str,
    media_type: str,
    *,
    allow_inline_active: bool = False,
) -> tuple[str, str]:
    if _is_active_shared_media_type(name, media_type):
        if allow_inline_active and Path(name).suffix.lower() in {".html", ".htm"}:
            return media_type, "inline"
        return "application/octet-stream", "attachment"
    if Path(name).suffix.lower() == ".json":
        return media_type, "attachment"
    return media_type, "inline"


def _assistant_preview_kind(name: str, media_type: str) -> str:
    ext = Path(name).suffix.lower()
    if ext in _SHARED_FOLDER_ACTIVE_CONTENT_EXTENSIONS or _is_active_shared_media_type(name, media_type):
        return "file"
    if ext == ".json":
        return "file"
    if media_type.startswith("image/"):
        return "image"
    if media_type == "application/pdf" or ext == ".pdf":
        return "pdf"
    if media_type.startswith("audio/") or ext in {".mp3", ".m4a", ".wav", ".webm", ".ogg", ".aac", ".flac"}:
        return "audio"
    if media_type.startswith("video/") or ext in {".mp4", ".mov", ".webm", ".mkv"}:
        return "video"
    if media_type.startswith("text/") or ext in {".txt", ".md", ".csv", ".yaml", ".yml"}:
        return "text"
    return "file"


def _assistant_upload_root() -> Path:
    root = get_hermes_home() / "dashboard_uploads"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_upload_component(value: str, fallback: str = "upload") -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", value or "").strip(".-_")
    return (safe or fallback)[:80]


def _assistant_attachment_target_dir(session_id: str, prefix: str = "resource") -> Path:
    session_part = _safe_upload_component(session_id or "session", "session")
    batch_part = f"{prefix}-{int(time.time() * 1000)}-{secrets.token_hex(4)}"
    target_dir = _assistant_upload_root() / session_part / batch_part
    target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir


def _assistant_artifact_roots() -> tuple[Path, ...]:
    return (_assistant_upload_root().resolve(),)


def _resolve_assistant_artifact(raw_path: str) -> Path | None:
    try:
        target = Path(urllib.parse.unquote(raw_path or "")).expanduser().resolve()
        if target.suffix.lower() not in _ASSISTANT_ARTIFACT_EXTENSIONS:
            return None
        if not target.is_file() or target.stat().st_size > _ASSISTANT_ARTIFACT_MAX_BYTES:
            return None
        for root in _assistant_artifact_roots():
            if target == root or root in target.parents:
                return target
    except Exception:
        return None
    return None


def _upload_shared_file_to_cloud(
    config: Dict[str, Any], source: Path, rel_path: str
) -> str | None:
    return None


def _create_shared_file_public_link(
    config: Dict[str, Any], rel_path: str, *, name: str | None = None
) -> Dict[str, str] | None:
    return None


def _shared_cloud_config(config: Dict[str, Any]) -> Dict[str, Any] | None:
    for section_name in ("assistant", "dashboard", "shared", "shared_folder"):
        section = config.get(section_name)
        if isinstance(section, dict):
            cloud = section.get("shared_cloud") or section.get("cloud_share")
            if isinstance(cloud, dict):
                return cloud
    return None


def _discover_dav_shared_folder_root(config: Dict[str, Any]) -> Path | None:
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    get_uid = getattr(os, "getuid", None)
    if not runtime_dir and callable(get_uid):
        runtime_dir = f"/run/user/{get_uid()}"
    if not runtime_dir:
        return None
    gvfs_root = Path(runtime_dir) / "gvfs"
    if not gvfs_root.is_dir():
        return None
    wanted_hosts = {"dav.aiwerk.ch"}
    cloud = _shared_cloud_config(config)
    if isinstance(cloud, dict):
        for raw in (cloud.get("dav_url"), cloud.get("webdav_url"), cloud.get("mount_url")):
            if isinstance(raw, str) and raw.strip():
                hostname = urllib.parse.urlparse(raw).hostname
                if hostname:
                    wanted_hosts.add(hostname.lower())
    try:
        for mount in sorted(gvfs_root.iterdir(), key=lambda path: path.name):
            mount_name = mount.name.lower()
            if not mount.is_dir() or not any(f"host={host}" in mount_name for host in wanted_hosts):
                continue
            direct = mount / "Hermes-Shared"
            if direct.is_dir():
                return direct
            for owner_dir in sorted(
                (path for path in mount.iterdir() if path.is_dir()),
                key=lambda path: path.name.lower(),
            ):
                candidate = owner_dir / "Hermes-Shared"
                if candidate.is_dir():
                    return candidate
    except Exception:
        return None
    return None


def _resolve_shared_folder_root(config: Dict[str, Any] | None = None) -> Path | None:
    config = config or {}
    candidates: list[Any] = [
        os.environ.get("AIWERK_CUI_SHARED_FOLDER"),
        os.environ.get("AIWERK_SHARED_FOLDER"),
        os.environ.get("HERMES_SHARED_FOLDER"),
        os.environ.get("HERMES_SHARED_DIR"),
    ]
    for section_name in ("assistant", "dashboard", "shared_folder", "shared"):
        section = config.get(section_name)
        if isinstance(section, dict):
            for key in (
                "shared_folder",
                "shared_dir",
                "shared_path",
                "path",
                "root",
                "mount_path",
                "dav_path",
                "webdav_path",
            ):
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


def _shared_attachment_rel_path(item: Dict[str, Any]) -> str | None:
    open_url = str(item.get("open_url") or "")
    if not open_url:
        return None
    parsed = urllib.parse.urlparse(open_url)
    query = urllib.parse.parse_qs(parsed.query)
    path_values = query.get("path") or []
    if not path_values:
        return None
    rel = urllib.parse.unquote(path_values[0]).replace("\\", "/")
    parts = [part for part in rel.split("/") if part]
    if not parts or any(part in {".", ".."} or part.startswith(".") for part in parts):
        return None
    return "/".join(parts)


def _create_shared_file_attachment(config: Dict[str, Any], item: Dict[str, Any], session_id: str) -> Dict[str, Any]:
    rel_path = _shared_attachment_rel_path(item)
    if not rel_path:
        raise HTTPException(status_code=400, detail="Shared file path missing")
    filename = Path(rel_path).name
    suffix = Path(filename).suffix.lower()
    if suffix not in _ASSISTANT_UPLOAD_EXTS:
        raise HTTPException(status_code=415, detail=f"Unsupported file type: {filename}")
    shared_root = _resolve_shared_folder_root(config)
    source: Path | None = None
    if shared_root:
        source = _safe_shared_path(shared_root.resolve(), rel_path)
        if not source.is_file():
            raise HTTPException(status_code=404, detail="Shared file not found")
        data = source.read_bytes()
        media_type = mimetypes.guess_type(filename)[0] or str(item.get("mime") or "application/octet-stream")
    else:
        cloud = _shared_cloud_config(config)
        downloaded = None
        if isinstance(cloud, dict):
            downloaded = (
                _download_webdav_cloud_file(cloud, rel_path)
                if _shared_cloud_uses_webdav(cloud)
                else _download_sftpgo_pubshare_file(cloud, rel_path)
            )
        if not downloaded:
            raise HTTPException(status_code=404, detail="Shared file not found")
        data, media_type, downloaded_name = downloaded
        filename = downloaded_name or filename
        suffix = Path(filename).suffix.lower()
        if suffix not in _ASSISTANT_UPLOAD_EXTS:
            raise HTTPException(status_code=415, detail=f"Unsupported file type: {filename}")
    if len(data) > _ASSISTANT_UPLOAD_MAX_BYTES:
        raise HTTPException(status_code=413, detail=f"File too large: {filename}")
    target_dir = _assistant_attachment_target_dir(session_id, "shared")
    safe_name = _safe_upload_component(filename, "shared-file")
    target = target_dir / safe_name
    target.write_bytes(data)
    payload: Dict[str, Any] = {
        "name": filename,
        "path": str(target),
        "type": media_type,
        "size": len(data),
        "is_image": suffix in _ASSISTANT_IMAGE_EXTS,
        "extraction": "image" if suffix in _ASSISTANT_IMAGE_EXTS else "text",
    }
    if payload["extraction"] == "text":
        payload["extracted_text"] = data.decode("utf-8", "replace")
    return payload


@app.options("/api/assistant/artifacts/open")
@app.head("/api/assistant/artifacts/open")
@app.get("/api/assistant/artifacts/open")
async def open_assistant_artifact(request: Request):
    _require_token(request)
    target = _resolve_assistant_artifact(request.query_params.get("path") or "")
    if not target:
        raise HTTPException(status_code=404, detail="Artifact not found")
    media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    media_type, disposition = _safe_shared_open_disposition(target.name, media_type)
    return FileResponse(
        target,
        media_type=media_type,
        filename=target.name,
        headers={
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{urllib.parse.quote(target.name)}",
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
_ASSISTANT_IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})


@app.post("/api/assistant/attachments")
async def upload_assistant_attachments_route(request: Request) -> Dict[str, Any]:
    _require_token(request)
    form = await request.form()
    files = form.getlist("files")
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded")
    if len(files) > _ASSISTANT_UPLOAD_MAX_FILES:
        raise HTTPException(status_code=413, detail="Too many files")
    from hermes_constants import get_hermes_home

    upload_root = get_hermes_home() / "dashboard_uploads"
    upload_root.mkdir(parents=True, exist_ok=True)
    attachments: list[Dict[str, Any]] = []
    total = 0
    for upload in files:
        filename = Path(str(getattr(upload, "filename", "") or "")).name
        suffix = Path(filename).suffix.lower()
        if suffix not in _ASSISTANT_UPLOAD_EXTS:
            raise HTTPException(status_code=415, detail="Unsupported attachment type")
        remaining = _ASSISTANT_UPLOAD_MAX_TOTAL_BYTES - total
        if remaining <= 0:
            raise HTTPException(status_code=413, detail="Attachment batch too large")
        read_limit = min(_ASSISTANT_UPLOAD_MAX_FILE_BYTES, remaining)
        data = await _read_upload(upload, read_limit)
        if len(data) > _ASSISTANT_UPLOAD_MAX_FILE_BYTES:
            raise HTTPException(status_code=413, detail=f"File too large: {filename}")
        total += len(data)
        if total > _ASSISTANT_UPLOAD_MAX_TOTAL_BYTES:
            raise HTTPException(status_code=413, detail="Attachment batch too large")
        safe_name = filename or f"attachment{suffix}"
        path = upload_root / f"{secrets.token_hex(8)}-{safe_name}"
        path.write_bytes(data)
        content_type = getattr(upload, "content_type", "") or mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
        extracted_text, extraction = _extract_uploaded_text(path, content_type)
        item = {
            "name": safe_name,
            "path": str(path),
            "type": content_type,
            "size": len(data),
            "is_image": content_type.startswith("image/") or suffix in _ASSISTANT_IMAGE_EXTS,
            "extraction": extraction,
        }
        if extracted_text:
            item["extracted_text"] = extracted_text
        attachments.append(item)
    return {"attachments": attachments}


def _ws_auth_ok(ws: Any) -> bool:
    from hermes_cli.web_server_chat import _ws_auth_ok as _chat_ws_auth_ok
    return _chat_ws_auth_ok(ws)


def _ws_request_is_allowed(ws: Any) -> bool:
    from hermes_cli.web_server_chat import _ws_request_is_allowed as _chat_ws_request_is_allowed
    return _chat_ws_request_is_allowed(ws)


async def gateway_ws(ws: Any) -> None:
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

    request_gate_cb = None
    if _assistant_mode_enabled():
        auth_identity = getattr(ws, "_hermes_auth_identity", None)
        if _assistant_ws_request_gate({"method": "gateway.ping", "params": {}}, auth_identity) is not None:
            await ws.close(code=4403, reason="authenticated customer identity required")
            return
        request_gate_cb = lambda request: _assistant_ws_request_gate(request, auth_identity)
    await handle_ws(
        ws,
        auth_identity=getattr(ws, "_hermes_auth_identity", None),
        subprotocol=getattr(ws, "_hermes_ws_subprotocol", None),
        request_gate=request_gate_cb,
    )


async def speak_stream_ws(ws: Any) -> None:
    if _assistant_mode_enabled():
        await ws.close(code=4403, reason="websocket disabled in assistant mode")
        return
    from hermes_cli.web_routers.audio import speak_stream_ws as _speak_stream_ws
    await _speak_stream_ws(ws)


async def console_ws(ws: Any) -> None:
    if _assistant_mode_enabled():
        await ws.close(code=4403, reason="websocket disabled in assistant mode")
        return
    from hermes_cli.web_routers.chat_ws import console_ws as _console_ws
    await _console_ws(ws)


async def pty_ws(ws: Any) -> None:
    if _assistant_mode_enabled():
        await ws.close(code=4403, reason="pty disabled in assistant mode")
        return
    from hermes_cli.web_routers.chat_ws import pty_ws as _pty_ws
    await _pty_ws(ws)


async def pub_ws(ws: Any) -> None:
    if _assistant_mode_enabled():
        await ws.close(code=4403, reason="websocket disabled in assistant mode")
        return
    from hermes_cli.web_routers.chat_ws import pub_ws as _pub_ws
    await _pub_ws(ws)


async def events_ws(ws: Any) -> None:
    if _assistant_mode_enabled():
        await ws.close(code=4403, reason="websocket disabled in assistant mode")
        return
    from hermes_cli.web_routers.chat_ws import events_ws as _events_ws
    await _events_ws(ws)


def _newline(*parts: Any) -> str:
    return "\n".join(str(part) for part in parts if part is not None)


def _html_fragment_to_plain_text(fragment: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", _html.unescape(fragment or ""))).strip()


def _plain_email_reader_html(item: Dict[str, Any]) -> str:
    return _html.escape(str(item.get("body") or item.get("snippet") or ""))


def _plain_calendar_reader_html(item: Dict[str, Any]) -> str:
    return _html.escape(str(item.get("description") or item.get("summary") or ""))


def _clean_calendar_reader_body(value: str) -> str:
    return _html_fragment_to_plain_text(value)


def _format_swiss_datetime(value: Any) -> str:
    return str(value or "")


def _clean_dashboard_display_name(value: Any) -> str:
    return str(value or "").strip()


def _assistant_user_display_name_from_config(config: Dict[str, Any]) -> str:
    return _clean_dashboard_display_name(
        os.environ.get("AIWERK_CUI_USER_DISPLAY_NAME")
        or os.environ.get("AIWERK_CUI_USER_NAME")
        or os.environ.get("HERMES_USER_DISPLAY_NAME")
        or config.get("assistant_user_display_name")
        or config.get("display_name")
    )


def _assistant_display_name_from_config(config: Dict[str, Any]) -> str:
    display = _assistant_config_section(config, "display")
    return _clean_dashboard_display_name(
        os.environ.get("AIWERK_CUI_AGENT_NAME")
        or display.get("agent_name")
        or config.get("assistant_display_name")
        or "Hermes"
    )


def _assistant_user_display_name() -> str:
    return _assistant_user_display_name_from_config(load_config())


def _clean_assistant_ui_locale(value: Any) -> str:
    text = str(value or "").strip().replace("_", "-")
    return text or "en"


def _assistant_ui_locale_from_config(config: Dict[str, Any]) -> str:
    env_locale = os.getenv("AIWERK_CUI_LOCALE") or os.getenv("AIWERK_CUI_LANGUAGE") or os.getenv("HERMES_CUI_LOCALE")
    if env_locale is not None and env_locale.strip():
        language = str(env_locale).strip().lower()
        if language in {"magyar", "hungarian", "hu"}:
            return "hu"
        return _clean_assistant_ui_locale(env_locale)
    dashboard = _assistant_config_section(config, "dashboard")
    dashboard_locale = dashboard.get("cui_locale") or dashboard.get("locale")
    if dashboard_locale:
        return _clean_assistant_ui_locale(str(dashboard_locale).split("_", 1)[0])
    language = str(_assistant_config_section(config, "assistant").get("language") or "").strip().lower()
    if language in {"magyar", "hungarian", "hu"}:
        return "hu"
    return _clean_assistant_ui_locale(config.get("assistant_ui_locale") or config.get("locale") or "de")


def _assistant_invalidate_resource_cache(resource: str | None = None) -> None:
    with _ASSISTANT_RESOURCE_LOCK:
        keys = set(_ASSISTANT_RESOURCE_CACHE) | set(_ASSISTANT_RESOURCE_CACHE_GENERATIONS)
        for key in list(keys):
            if resource is None or key.startswith(f"{resource}:"):
                _ASSISTANT_RESOURCE_CACHE.pop(key, None)
                _ASSISTANT_RESOURCE_CACHE_GENERATIONS[key] = int(_ASSISTANT_RESOURCE_CACHE_GENERATIONS.get(key, 0)) + 1


def _decode_text_bytes(data: bytes) -> str:
    for encoding in ("utf-8", "utf-16", "latin-1"):
        try:
            return data.decode(encoding).replace("\x00", "")
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace").replace("\x00", "")


def _extract_uploaded_text(path: Path, content_type: str = "") -> tuple[str, str]:
    """Best-effort bounded text extraction for customer UI attachments."""
    ext = path.suffix.lower()
    if content_type.startswith("image/") or ext in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
        return "", "image"
    if ext in {".txt", ".md", ".csv", ".json", ".yaml", ".yml"} or content_type.startswith("text/"):
        data = path.read_bytes()[: _ASSISTANT_TEXT_EXTRACT_LIMIT + 1]
        text = _decode_text_bytes(data)[:_ASSISTANT_TEXT_EXTRACT_LIMIT]
        return text, "text" if text else "empty"
    if ext == ".docx":
        try:
            from defusedxml.ElementTree import fromstring as _safe_fromstring

            with zipfile.ZipFile(path) as archive:
                try:
                    info = archive.getinfo("word/document.xml")
                except KeyError:
                    return "", "docx-extraction-failed"
                if info.file_size > _ASSISTANT_DOCX_MAX_XML_BYTES:
                    return "", "docx-too-large"
                with archive.open(info) as member:
                    xml = member.read(_ASSISTANT_DOCX_MAX_XML_BYTES + 1)
            if len(xml) > _ASSISTANT_DOCX_MAX_XML_BYTES:
                return "", "docx-too-large"
            root = _safe_fromstring(xml, forbid_dtd=True)
            text = " ".join(node.text for node in root.iter() if node.text).strip()
            return text[:_ASSISTANT_TEXT_EXTRACT_LIMIT], "docx" if text else "empty-docx"
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


def _support_log_path() -> Path:
    from hermes_constants import get_hermes_home
    raw = os.environ.get("AIWERK_CUI_SUPPORT_LOG")
    return Path(raw).expanduser() if raw else get_hermes_home() / "aiwerk-support" / "inbox.jsonl"


def _safe_support_text(value: Any) -> str:
    return _redact_sensitive_text(str(value or "").strip())


def _safe_support_multiline(value: Any) -> str:
    return _safe_support_text(value)


def _safe_support_diagnostics(value: Any) -> Dict[str, Any]:
    return _sanitize_public_message_value(value if isinstance(value, dict) else {})


def _format_support_message(payload: AssistantSupportRequest) -> str:
    title = "AIWerk Supportmeldung"
    category = _safe_support_text(payload.category or payload.subject or "Sonstiges")
    return _newline(
        title,
        f"Kategorie: {category}",
        f"Sitzung: {_safe_support_text(payload.session_title or payload.session_id)}" if (payload.session_id or payload.session_title) else None,
        "",
        _safe_support_multiline(payload.message),
    )


def _telegram_target_from_chat_id(chat_id: Any) -> Dict[str, Any]:
    return f"telegram:{chat_id}"


def _configured_gateway_user_ids(config: Dict[str, Any]) -> list[str]:
    users = config.get("gateway_user_ids") or config.get("gateway", {}).get("user_ids") or []
    return [str(user) for user in users] if isinstance(users, list) else []


def _explicit_delivery_targets(config: Dict[str, Any]) -> list[Dict[str, Any]]:
    targets = config.get("assistant_support_targets") or config.get("dashboard", {}).get("assistant_support_targets") or []
    return list(targets) if isinstance(targets, list) else []


def _system_delivery_targets(config: Dict[str, Any]) -> list[Dict[str, Any]]:
    chat_id = os.getenv("AIWERK_SYSTEM_TELEGRAM_CHAT_ID")
    target = os.getenv("AIWERK_SYSTEM_TARGET")
    if target:
        return [{"target": target}]
    return [_telegram_target_from_chat_id(chat_id)] if chat_id else []


def _support_delivery_targets(config: Dict[str, Any]) -> list[Dict[str, Any]]:
    explicit = _explicit_delivery_targets(config)
    if explicit:
        return explicit
    target = os.getenv("AIWERK_CUI_SUPPORT_TARGET")
    if target and target.strip().lower() not in {"telegram", "gateway", "home"}:
        return [{"target": target}]
    chat_id = os.getenv("AIWERK_SUPPORT_TELEGRAM_CHAT_ID") or os.getenv("AIWERK_CUI_SUPPORT_TELEGRAM_CHAT_ID")
    return [_telegram_target_from_chat_id(chat_id)] if chat_id else []


def _deliver_support_message(targets: list[Dict[str, Any]], message: str) -> bool:
    return bool(targets and message), []


def _handle_assistant_support(payload: AssistantSupportRequest) -> Dict[str, Any]:
    path = _support_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    message = _format_support_message(payload)
    support_id = f"sup_{secrets.token_hex(8)}"
    record = {
        "support_id": support_id,
        "category": payload.category or payload.subject,
        "message": _safe_support_multiline(payload.message),
        "session_id": payload.session_id,
        "session_title": payload.session_title,
        "connection": payload.connection,
        "diagnostics": _safe_support_diagnostics(payload.diagnostics) if payload.include_diagnostics else {},
        "created_at": time.time(),
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(_json.dumps(record, sort_keys=True) + "\n")
    delivered, errors = _deliver_support_message(_support_delivery_targets(load_config()), message)
    return {
        "ok": True,
        "support_id": support_id,
        "delivered": bool(delivered),
        "queued": not bool(delivered),
        "errors": errors,
        "path": str(path),
    }


@app.post("/api/assistant/support")
def submit_assistant_support(request: Request, payload: AssistantSupportRequest) -> Dict[str, Any]:
    _require_token(request)
    return _handle_assistant_support(payload)


def _contacts_store_path() -> Path:
    return Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "cui_contacts.json"


def _read_contacts_store_payload() -> Dict[str, Any]:
    try:
        return _json.loads(_contacts_store_path().read_text(encoding="utf-8"))
    except Exception:
        return {"contacts": [], "hidden": []}


def _read_manual_contacts() -> list[Dict[str, Any]]:
    payload = os.getenv("AIWERK_CUI_CONTACTS_JSON")
    if payload:
        try:
            data = _json.loads(payload)
            if isinstance(data, list):
                return [dict(item) for item in data if isinstance(item, dict)]
            if isinstance(data, dict):
                return [dict(item) for item in data.get("contacts", []) if isinstance(item, dict)]
        except Exception:
            return []
    return [dict(item) for item in _read_contacts_store_payload().get("contacts", []) if isinstance(item, dict)]


def _write_contacts_store_payload(payload: Dict[str, Any]) -> None:
    path = _contacts_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json.dumps(payload, sort_keys=True), encoding="utf-8")


def _write_manual_contacts(contacts: list[Dict[str, Any]]) -> None:
    payload = _read_contacts_store_payload()
    payload["contacts"] = contacts
    _write_contacts_store_payload(payload)


def _write_hidden_contact_keys(keys: list[str]) -> None:
    payload = _read_contacts_store_payload()
    payload["hidden"] = keys
    _write_contacts_store_payload(payload)


def _normalize_contact(value: Any) -> str:
    return _unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii").lower()


def _contact_search_haystack(contact: Dict[str, Any]) -> str:
    return _normalize_contact(" ".join(str(contact.get(key) or "") for key in ("name", "email", "phone")))


def _contact_matches_query(contact: Dict[str, Any], query: str) -> bool:
    needle = _normalize_contact(query)
    if not needle:
        return True
    haystack = _contact_search_haystack(contact)
    if needle in haystack:
        return True
    terms = [term for term in re.split(r"\s+", needle) if term]
    return bool(terms) and all(term in haystack for term in terms)


def _contact_hide_keys(contact: Dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    for key in ("key", "id", "email"):
        value = str(contact.get(key) or "").strip().lower()
        if value:
            keys.add(value)
            keys.add(f"{key}:{value}")
    name = _normalize_contact(contact.get("display_name") or contact.get("name"))
    if name:
        keys.add(f"name:{name}")
    return keys


def _read_hidden_contact_keys() -> set[str]:
    raw = _read_contacts_store_payload().get("hidden") or []
    if not isinstance(raw, list):
        return set()
    return {str(item).strip().lower() for item in raw if str(item).strip()}


def _filter_hidden_contacts(contacts: list[Dict[str, Any]], *, hidden_keys: set[str] | None = None) -> list[Dict[str, Any]]:
    hidden = hidden_keys if hidden_keys is not None else _read_hidden_contact_keys()
    if not hidden:
        return contacts
    return [contact for contact in contacts if not (_contact_hide_keys(contact) & hidden)]


def _contacts_own_email_set(config: Dict[str, Any] | None, email_resource: Dict[str, Any] | None, calendar_resource: Dict[str, Any] | None) -> set[str]:
    own: set[str] = set()
    for account in [*_email_account_configs(config or {}), *_calendar_accounts(config or {}), *_contact_account_configs(config or {})]:
        if not isinstance(account, dict):
            continue
        for key in ("address", "email", "user_google_email", "google_email"):
            email_addr = str(account.get(key) or "").strip().lower()
            if email_addr and email_addr != "me":
                own.add(email_addr)
    for resource in (email_resource, calendar_resource):
        if not isinstance(resource, dict):
            continue
        for account in resource.get("accounts") or []:
            if not isinstance(account, dict):
                continue
            for key in ("address", "email", "account_address"):
                email_addr = str(account.get(key) or "").strip().lower()
                if email_addr:
                    own.add(email_addr)
    return {item for item in own if item}


def _filter_human_contacts(contacts: list[Dict[str, Any]], *, own_emails: set[str]) -> list[Dict[str, Any]]:
    return [contact for contact in contacts if _contact_is_customer_safe(contact, own_emails)]


def _dedupe_contacts(contacts: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
    deduped: list[Dict[str, Any]] = []
    seen: set[str] = set()
    for contact in contacts:
        email_key = str(contact.get("email") or "").strip().lower()
        key = email_key or str(contact.get("key") or contact.get("id") or contact.get("display_name") or contact.get("name") or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(contact)
    return deduped


def _filter_contacts_payload(payload: Dict[str, Any], *, own_emails: set[str]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return payload
    filtered = dict(payload)
    for key in ("items", "contacts", "frequent", "relevant"):
        value = filtered.get(key)
        if isinstance(value, list):
            filtered[key] = _filter_human_contacts([item for item in value if isinstance(item, dict)], own_emails=own_emails)
    if not filtered.get("items") and "items" in filtered:
        filtered["total_count"] = 0
    return filtered


def _contacts_from_google_workspace_query_interactions(
    config: Dict[str, Any],
    query: str,
    *,
    own_emails: set[str] | None = None,
    limit: int | None = None,
) -> list[Dict[str, Any]]:
    if not query.strip():
        return []
    if _env_truthy("AIWERK_CUI_CONTACTS_DISABLE_GMAIL_INTERACTIONS") or _env_truthy("AIWERK_CUI_EMAIL_DISABLE_AIWERK_BRIDGE"):
        return []
    page_size = _bounded_int(limit, default=_ASSISTANT_CONTACT_SEARCH_LIMIT * 10, minimum=1, maximum=200)
    blocked_own = {email.strip().lower() for email in (own_emails or set()) if email}
    contacts: list[Dict[str, Any]] = []
    for account in _contact_account_configs(config):
        server = str(account.get("server") or "google-workspace-aiwerk")
        user_google_email = str(account.get("user_google_email") or "me")
        try:
            ids = _gmail_bridge_search_message_ids(
                config,
                query,
                server=server,
                user_google_email=user_google_email,
                page_size=page_size,
            )
            for item in _gmail_bridge_metadata_items_for_ids(
                config,
                ids[:page_size],
                server=server,
                user_google_email=user_google_email,
            ) if ids else []:
                for value in (item.get("from"), item.get("sender"), item.get("to"), item.get("cc"), item.get("bcc")):
                    contacts.extend(_contacts_from_address_text(value, source="Gmail"))
        except Exception as exc:
            _log.debug("CUI Gmail contact query scan failed for %s/%s/%s: %s", server, user_google_email, query, exc)
    filtered: list[Dict[str, Any]] = []
    seen: set[str] = set()
    for contact in contacts:
        email_key = str(contact.get("email") or "").strip().lower()
        if not email_key or email_key in seen or not _contact_is_customer_safe(contact, blocked_own):
            continue
        seen.add(email_key)
        filtered.append(contact)
    return filtered


def _contacts_from_gmail_query_blocks(config: Dict[str, Any], query: str) -> list[Dict[str, Any]]:
    return _contacts_from_google_workspace_query_interactions(config, query)


def _parse_google_contacts(text: str) -> list[Dict[str, Any]]:
    contacts: list[Dict[str, Any]] = []
    current: Dict[str, Any] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.lower().startswith("contact id:"):
            if current:
                contacts.append(current)
            current = {"id": line.split(":", 1)[1].strip()}
        elif line.lower().startswith("name:"):
            current["display_name"] = line.split(":", 1)[1].strip()
        elif line.lower().startswith("email:"):
            email = line.split(":", 1)[1].strip().split(" ", 1)[0]
            current["email"] = email
    if current:
        contacts.append(current)
    return contacts


def _contact_account_configs(config: Dict[str, Any]) -> list[Dict[str, Any]]:
    contacts_cfg = _assistant_config_section(config, "contacts")
    accounts = contacts_cfg.get("accounts")
    if isinstance(accounts, list) and accounts:
        source = [dict(item) for item in accounts if isinstance(item, dict)]
    else:
        source = [
            dict(account)
            for account in _email_account_configs(config)
            if _email_backend_name(account) in {"google_workspace", "google", "gmail"}
        ]
    if not source and (os.environ.get("AIWERK_CUI_GOOGLE_EMAIL") or os.environ.get("AIWERK_CUI_GOOGLE_WORKSPACE_SERVER")):
        source = [{"backend": "google_workspace"}]
    normalized: list[Dict[str, Any]] = []
    for account in source:
        account["server"] = _google_workspace_server(account, env_first=False)
        account["user_google_email"] = _google_workspace_user_email(account, env_first=False)
        normalized.append(account)
    return normalized


def _contacts_from_google_workspace(config: Dict[str, Any], query: str = "", limit: int | None = None, sort_order: str | None = None) -> list[Dict[str, Any]]:
    if _env_truthy("AIWERK_CUI_CONTACTS_DISABLE_AIWERK_BRIDGE"):
        return []
    contacts: list[Dict[str, Any]] = []
    requested_limit = limit if limit is not None else os.environ.get("AIWERK_CUI_CONTACTS_PAGE_SIZE")
    page_size = _bounded_int(requested_limit, default=100, minimum=1, maximum=1000)
    if query:
        page_size = min(page_size, 30)
    for account in _contact_account_configs(config):
        server = str(account.get("server") or "google-workspace-aiwerk")
        user_google_email = str(account.get("user_google_email") or "me")
        params: Dict[str, Any] = {
            "user_google_email": user_google_email,
            "page_size": page_size,
        }
        tool = "search_contacts" if query else "list_contacts"
        if query:
            params["query"] = query
        if sort_order:
            params["sort_order"] = sort_order
        try:
            payload = _call_aiwerk_bridge_tool(config, server=server, tool=tool, params=params)
        except Exception:
            continue
        items = payload.get("contacts") or payload.get("items") if isinstance(payload, dict) else None
        if isinstance(items, list):
            contacts.extend(dict(item) for item in items if isinstance(item, dict))
        else:
            contacts.extend(_parse_google_contacts(_extract_bridge_text(payload)))
    return contacts[:page_size]


def _contacts_from_google_workspace_interactions(config: Dict[str, Any], own_emails: set[str]) -> list[Dict[str, Any]]:
    if _env_truthy("AIWERK_CUI_CONTACTS_DISABLE_GMAIL_INTERACTIONS") or _env_truthy("AIWERK_CUI_EMAIL_DISABLE_AIWERK_BRIDGE"):
        return []
    limit = _bounded_int(os.environ.get("AIWERK_CUI_CONTACTS_INTERACTION_SCAN_LIMIT"), default=40, minimum=1, maximum=100)
    window_days = _contacts_relevance_window_days()
    queries = (
        os.environ.get("AIWERK_CUI_CONTACTS_SENT_QUERY") or f"in:sent newer_than:{window_days}d",
        os.environ.get("AIWERK_CUI_CONTACTS_INBOX_QUERY") or f"newer_than:{window_days}d -in:sent",
    )
    contacts: list[Dict[str, Any]] = []
    for account in _contact_account_configs(config):
        server = str(account.get("server") or "google-workspace-aiwerk")
        user_google_email = str(account.get("user_google_email") or "me")
        for query in queries:
            ids = _gmail_bridge_search_message_ids(
                config,
                query,
                server=server,
                user_google_email=user_google_email,
                page_size=limit,
            )
            for item in _gmail_bridge_metadata_items_for_ids(
                config,
                ids[:limit],
                server=server,
                user_google_email=user_google_email,
            ) if ids else []:
                for value in (item.get("sender"), item.get("from"), item.get("to"), item.get("cc")):
                    contacts.extend(_contacts_from_address_text(value, source="Gmail"))
    return [contact for contact in contacts if _contact_is_customer_safe(contact, own_emails)]


def _contacts_from_address_text(value: Any, *, source: str) -> list[Dict[str, Any]]:
    text = str(value or "")
    contacts: list[Dict[str, Any]] = []
    for name, email in re.findall(r"([^<,;]+)<([^>]+)>", text):
        contacts.append({"display_name": name.strip(), "email": email.strip(), "source_badges": [source]})
    for email in re.findall(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", text):
        if not any(contact.get("email") == email for contact in contacts):
            contacts.append({"display_name": email, "email": email, "source_badges": [source]})
    return contacts


def _himalaya_contact_accounts(config: Dict[str, Any]) -> list[Dict[str, Any]]:
    accounts = [
        dict(account)
        for account in _email_account_configs(config)
        if _email_backend_name(account) in {"himalaya", "imap"} or account.get("account")
    ]
    if not accounts and (os.environ.get("AIWERK_CUI_EMAIL_ACCOUNT") or os.environ.get("HIMALAYA_ACCOUNT")):
        accounts = [{"backend": "himalaya", "account": os.environ.get("AIWERK_CUI_EMAIL_ACCOUNT") or os.environ.get("HIMALAYA_ACCOUNT")}]
    return accounts


def _himalaya_contact_folder(account: Dict[str, Any], *, sent: bool) -> str:
    if sent:
        return str(
            account.get("sent_folder")
            or account.get("sent_mailbox")
            or account.get("sent")
            or os.environ.get("AIWERK_CUI_CONTACTS_HIMALAYA_SENT_FOLDER")
            or "Sent"
        )
    return str(
        account.get("inbox_folder")
        or account.get("folder")
        or os.environ.get("AIWERK_CUI_CONTACTS_HIMALAYA_INBOX_FOLDER")
        or "INBOX"
    )


def _contacts_from_himalaya_interactions(config: Dict[str, Any], own_emails: set[str]) -> list[Dict[str, Any]]:
    if _env_truthy("AIWERK_CUI_CONTACTS_DISABLE_HIMALAYA_INTERACTIONS") or _env_truthy("AIWERK_CUI_EMAIL_DISABLE_HIMALAYA"):
        return []
    limit = _bounded_int(os.environ.get("AIWERK_CUI_CONTACTS_INTERACTION_SCAN_LIMIT"), default=40, minimum=1, maximum=100)
    contacts: list[Dict[str, Any]] = []
    for account in _himalaya_contact_accounts(config):
        account_name = str(account.get("account") or account.get("address") or "")
        for sent in (True, False):
            folder = _himalaya_contact_folder(account, sent=sent)
            for item in _run_himalaya_envelope_list(page_size=limit, account=account_name or None, folder=folder):
                for value in (item.get("from"), item.get("sender"), item.get("to"), item.get("cc")):
                    contacts.extend(_contacts_from_address_text(value, source="E-Mail"))
    return [contact for contact in contacts if _contact_is_customer_safe(contact, own_emails)]


def _normalize_contact_item(contact: Dict[str, Any]) -> Dict[str, Any]:
    email = str(contact.get("email") or "").strip()
    name = str(contact.get("display_name") or contact.get("name") or email).strip()
    return {
        **contact,
        "display_name": name,
        "name": name,
        "email": email,
        "source_badges": list(contact.get("source_badges") or ["Google"]),
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


def _contacts_page_size(config: Dict[str, Any] | None = None) -> int:
    raw = os.environ.get("AIWERK_CUI_CONTACTS_PAGE_SIZE")
    if raw is None and config:
        raw = _assistant_config_section(config, "contacts").get("page_size")
    return _bounded_int(raw, default=100, minimum=1, maximum=500)


def _contacts_relevance_window_days() -> int:
    return _bounded_int(
        os.environ.get("AIWERK_CUI_CONTACTS_RELEVANCE_WINDOW_DAYS"),
        default=_ASSISTANT_CONTACT_RELEVANCE_WINDOW_DAYS,
        minimum=1,
        maximum=90,
    )


def _contacts_saved_top_up_target() -> int:
    return _bounded_int(
        os.environ.get("AIWERK_CUI_CONTACTS_SAVED_TOP_UP_TARGET"),
        default=_ASSISTANT_CONTACT_SAVED_TOP_UP_TARGET,
        minimum=_ASSISTANT_CONTACT_PREVIEW_ITEMS,
        maximum=50,
    )


def _contact_is_customer_safe(contact: Dict[str, Any], own_emails: set[str]) -> bool:
    email = str(contact.get("email") or "").strip().lower()
    name = str(contact.get("display_name") or contact.get("name") or "").lower()
    if not email or email in own_emails:
        return False
    local = email.split("@", 1)[0] if "@" in email else ""
    compact_local = re.sub(r"[^a-z0-9]", "", local)
    haystack = f"{name} {contact.get('organization') or ''} {email}".lower()
    if local in _SYSTEM_CONTACT_LOCALPARTS or compact_local in {"noreply", "donotreply"}:
        return False
    if any(local.startswith(f"{prefix}-") or local.startswith(f"{prefix}+") for prefix in _SYSTEM_CONTACT_LOCALPARTS):
        return False
    if any(pattern in haystack for pattern in _SYSTEM_CONTACT_TEXT_PATTERNS):
        return False
    return "synthetic" not in email and "cf-test" not in email and "synthetic" not in name


def _search_contacts_payload(query: str = "", *, limit: int = _ASSISTANT_CONTACT_SEARCH_LIMIT) -> Dict[str, Any]:
    config = load_config()
    resources = _assistant_resources_payload(force_refresh=False)
    email = resources.get("email") if isinstance(resources.get("email"), dict) else _email_summary(config)
    calendar = resources.get("calendar") if isinstance(resources.get("calendar"), dict) else _calendar_summary(config)
    own_emails = _contacts_own_email_set(config, email, calendar)
    resource_contacts = []
    contacts_resource = resources.get("contacts") if isinstance(resources.get("contacts"), dict) else {}
    if isinstance(contacts_resource, dict):
        resource_contacts = [item for item in contacts_resource.get("items") or contacts_resource.get("relevant") or [] if isinstance(item, dict)]
    needle = (query or "").strip()
    all_contacts = _filter_human_contacts(_dedupe_contacts([
        *[_normalize_contact_item(c) for c in _read_manual_contacts()],
        *[_normalize_contact_item(c) for c in resource_contacts],
    ]), own_emails=own_emails)
    if needle:
        query_variants = [needle]
        normalized_needle = _normalize_contact(needle)
        if normalized_needle and normalized_needle != needle.casefold():
            query_variants.append(normalized_needle)
        query_variants.extend(term.strip() for term in re.split(r"\s+", needle) if len(term.strip()) >= 3)
        if " " not in needle and len(needle) >= 4:
            for first_name in ("Adam", "Ádám"):
                query_variants.extend((f"{first_name} {needle}", f"{needle} {first_name}"))
        seen_queries: set[str] = set()
        executed_queries: list[str] = []
        bridge_contacts: list[Dict[str, Any]] = []
        for contact_query in query_variants:
            contact_query = contact_query.strip()
            query_key = contact_query.casefold()
            if not contact_query or query_key in seen_queries:
                continue
            seen_queries.add(query_key)
            executed_queries.append(contact_query)
            bridge_contacts.extend(
                _normalize_contact_item(c)
                for c in _contacts_from_google_workspace(config, query=contact_query, limit=max(limit, 50))
            )
        saved_lookup_limit = max(limit * 50, 1000)
        saved_contacts = [
            _normalize_contact_item(c)
            for c in _dedupe_contacts([
                *_contacts_from_google_workspace(config, limit=saved_lookup_limit),
                *_contacts_from_google_workspace(config, limit=saved_lookup_limit, sort_order="FIRST_NAME_ASCENDING"),
            ])
        ]
        interaction_contacts: list[Dict[str, Any]] = []
        for contact_query in executed_queries:
            interaction_contacts.extend(
                _normalize_contact_item(c)
                for c in _contacts_from_google_workspace_query_interactions(
                    config,
                    contact_query,
                    own_emails=own_emails,
                    limit=max(limit * 10, 200),
                )
            )
        all_contacts = _filter_human_contacts(
            _dedupe_contacts([*bridge_contacts, *interaction_contacts, *saved_contacts, *all_contacts]),
            own_emails=own_emails,
        )
        all_contacts = [contact for contact in all_contacts if _contact_matches_query(contact, needle)]
    all_contacts = _filter_hidden_contacts(all_contacts)
    max_items = min(limit, _contacts_page_size(config))
    payload = {"items": all_contacts[:max_items], "contacts": all_contacts[:max_items], "total_count": len(all_contacts), "query": query or ""}
    return _filter_contacts_payload(payload, own_emails=own_emails)


@app.get("/api/cui/contacts/frequent")
def get_cui_frequent_contacts(request: Request) -> Dict[str, Any]:
    _require_token(request)
    resources = _assistant_resources_payload(request, force_refresh=False)
    contacts = resources.get("contacts", {}) if isinstance(resources, dict) else {}
    return {"items": contacts.get("frequent") or [], "total_count": contacts.get("total_count") or 0}


@app.get("/api/cui/context/contacts")
def get_cui_context_contacts(request: Request, session_id: str = "") -> Dict[str, Any]:
    _require_token(request)
    resources = _assistant_resources_payload(request, force_refresh=False)
    contacts = resources.get("contacts", {}) if isinstance(resources, dict) else {}
    return {"items": contacts.get("relevant") or [], "session_id": session_id}


@app.post("/api/cui/contacts/search")
def search_cui_contacts(request: Request, payload: Dict[str, Any]) -> Dict[str, Any]:
    _require_token(request)
    return _search_contacts_payload(str(payload.get("query") or ""))


@app.get("/api/cui/contacts/search")
def search_cui_contacts_get(request: Request, q: str = "") -> Dict[str, Any]:
    _require_token(request)
    return _search_contacts_payload(q)


@app.post("/api/cui/contacts")
def create_cui_contact(request: Request, payload: CuiContactCreateRequest) -> Dict[str, Any]:
    _require_token(request)
    contact = {"name": payload.name, "email": payload.email, "phone": payload.phone, "key": payload.email or payload.name}
    store = _read_contacts_store_payload()
    store.setdefault("contacts", []).append(contact)
    _write_contacts_store_payload(store)
    return {"contact": contact}


@app.post("/api/cui/contacts/hide")
def hide_cui_contact(request: Request, payload: CuiContactHideRequest) -> Dict[str, Any]:
    _require_token(request)
    store = _read_contacts_store_payload()
    hidden = set(store.get("hidden") or [])
    hidden.add(payload.key)
    store["hidden"] = sorted(hidden)
    _write_contacts_store_payload(store)
    return {"ok": True}


def _todo_path() -> Path:
    return Path(os.getenv("AIWERK_CUI_TODO_PATH") or (Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "assistant_todos.md"))


def _read_todo_lines(path: Path | None = None) -> list[str]:
    path = path or _todo_path()
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []


def _write_todo_lines(lines: list[str], path: Path | None = None) -> None:
    path = path or _todo_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def _todo_line_number(line: int) -> int:
    return max(1, int(line))


_TODO_RE = re.compile(r"^(?P<prefix>\s*[-*]\s+\[(?P<done>[ xX])\]\s*)(?P<text>.*?)(?P<meta>\s*<!--\s*hermes:[^>]*-->\s*)?$")


def _todo_items(lines: list[str]) -> list[Dict[str, Any]]:
    items: list[Dict[str, Any]] = []
    for index, line in enumerate(lines):
        match = _TODO_RE.match(line)
        if not match:
            continue
        meta = match.group("meta") or ""
        id_match = re.search(r"\bid=([A-Za-z0-9_.:-]+)", meta)
        item_id = id_match.group(1) if id_match else f"line-{index + 1}"
        full_text = re.sub(r"\s*<!--\s*hermes:[^>]*-->\s*$", "", match.group("text")).strip()
        items.append({
            "id": item_id,
            "line": index + 1,
            "text": full_text,
            "full_text": full_text,
            "done": match.group("done").lower() == "x",
            "metadata": meta.strip(),
        })
    return items


def _todo_index_for_ref(lines: list[str], ref: str | int) -> int:
    ref_text = str(ref or "").strip()
    if ref_text.isdigit():
        numeric = int(ref_text)
        if 1 <= numeric <= len(lines):
            return numeric - 1
    for item in _todo_items(lines):
        if item["id"] == ref_text:
            return int(item["line"]) - 1
    raise HTTPException(status_code=400, detail="Todo item not found")


def _update_todo_item_done(lines: list[str], line: int | str, done: bool) -> list[str]:
    index = _todo_index_for_ref(lines, line)
    if index < len(lines):
        lines[index] = re.sub(r"^- \[[ xX]\]", f"- [{'x' if done else ' '}]", lines[index])
    return lines


def _update_todo_item_text(lines: list[str], line: int | str, text: str) -> list[str]:
    index = _todo_index_for_ref(lines, line)
    clean = re.sub(r"\s+", " ", text or "").strip()
    if not clean:
        raise HTTPException(status_code=400, detail="Todo text is required")
    if index < len(lines):
        match = _TODO_RE.match(lines[index])
        done = bool(match and match.group("done").lower() == "x")
        meta = (match.group("meta") if match else "") or ""
        prefix = "- [x]" if done else "- [ ]"
        lines[index] = f"{prefix} {clean}{meta}"
    return lines


def _add_todo_item(text: str) -> Dict[str, Any]:
    clean = re.sub(r"\s+", " ", text or "").strip()
    if not clean:
        raise HTTPException(status_code=400, detail="Todo text is required")
    lines = _read_todo_lines()
    lines.append(f"- [ ] {clean} <!-- hermes:id=cui-{secrets.token_hex(6)} status=pending -->")
    _write_todo_lines(lines)
    _assistant_invalidate_resource_cache("todos")
    return _todo_summary(load_config())


def _todo_response() -> Dict[str, Any]:
    return {"todos": _todo_summary(load_config())}


def _todo_summary(_config: Dict[str, Any]) -> Dict[str, Any]:
    items = _todo_items(_read_todo_lines())
    open_items = [item for item in items if not item["done"]]
    done_count = len(items) - len(open_items)
    return {
        "status": "connected" if items else "not_configured",
        "summary": f"{len(open_items)} offene Aufgaben",
        "items": open_items,
        "open_count": len(open_items),
        "done_count": done_count,
        "total_count": len(items),
    }


@app.post("/api/assistant/todos")
def add_assistant_todo(request: Request, payload: AssistantTodoAddRequest) -> Dict[str, Any]:
    _require_token(request)
    return _add_todo_item(payload.text)


@app.post("/api/assistant/todos/add")
def add_assistant_todo_alias(request: Request, payload: AssistantTodoAddRequest) -> Dict[str, Any]:
    _require_token(request)
    _add_todo_item(payload.text)
    return _todo_response()


@app.post("/api/assistant/todos/update")
def update_assistant_todo(request: Request, payload: AssistantTodoUpdateRequest) -> Dict[str, Any]:
    _require_token(request)
    lines = _update_todo_item_done(_read_todo_lines(), payload.id or payload.line, payload.done)
    _write_todo_lines(lines)
    _assistant_invalidate_resource_cache("todos")
    return _todo_response()


@app.post("/api/assistant/todos/edit")
def edit_assistant_todo(request: Request, payload: AssistantTodoEditRequest) -> Dict[str, Any]:
    _require_token(request)
    lines = _update_todo_item_text(_read_todo_lines(), payload.id or payload.line, payload.text)
    if payload.done is not None:
        lines = _update_todo_item_done(lines, payload.id or payload.line, payload.done)
    _write_todo_lines(lines)
    _assistant_invalidate_resource_cache("todos")
    return _todo_response()


def _assistant_support_section(*_args: Any, **_kwargs: Any) -> Dict[str, Any]:
    return {"status": "available"}


def _calendar_account_config_for_ref(config: Dict[str, Any], ref: str) -> Dict[str, Any]:
    ref_l = str(ref or "").strip().lower()
    for account in _calendar_accounts(config):
        candidates = (
            account.get("id"),
            account.get("email"),
            account.get("address"),
            account.get("user_principal_name"),
            account.get("user_google_email"),
        )
        if any(str(candidate or "").strip().lower() == ref_l for candidate in candidates):
            return account
    return {}


def _parse_google_workspace_event_detail(payload: Dict[str, Any]) -> Dict[str, Any]:
    text = _extract_bridge_text(payload)
    detail: Dict[str, Any] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("- ") and ":" in line:
            key, value = line[2:].split(":", 1)
            detail[key.strip().lower()] = value.strip()
    return detail or dict(payload)


def _fetch_google_workspace_calendar_event_detail(config: Dict[str, Any], account: Dict[str, Any], event_id: str) -> Dict[str, Any]:
    return _call_aiwerk_bridge_tool(
        config,
        server=str(account.get("mcp_server") or "google-workspace-aiwerk"),
        tool="get_events",
        params={
            "calendar_id": str(account.get("address") or account.get("user_google_email") or "primary"),
            "user_google_email": str(account.get("user_google_email") or account.get("address") or ""),
            "event_id": event_id,
            "max_results": 1,
            "detailed": True,
        },
    )


def _fetch_microsoft_calendar_event_detail(config: Dict[str, Any], account: Dict[str, Any], event_id: str) -> Dict[str, Any]:
    return _call_aiwerk_bridge_tool(
        config,
        server=str(account.get("mcp_server") or "microsoft-calendar"),
        tool="get-calendar-event",
        params={"eventId": event_id},
    )


def _iter_nested_display_candidates(value: Any):
    if isinstance(value, dict):
        for key in ("display", "summary", "title", "name", "text"):
            if key in value:
                yield value[key]
        for nested in value.values():
            yield from _iter_nested_display_candidates(nested)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_nested_display_candidates(item)


def _format_swiss_datetime(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    try:
        from zoneinfo import ZoneInfo
        dt = _datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone(ZoneInfo("Europe/Zurich"))
        return dt.strftime("%d.%m.%Y, %H:%M Uhr")
    except Exception:
        return text


def _sanitize_reader_text(value: Any) -> str:
    text = _html_fragment_to_plain_text(str(value or ""))
    text = re.sub(r"https?://\S+", "[LINK]", text)
    return re.sub(r"(?:/[^\s<>'\"]+){2,}", "[PATH]", text)


def _sanitize_reader_literal(value: Any) -> str:
    text = re.sub(r"https?://\S+", "[LINK]", str(value or ""))
    return re.sub(r"(?:/[^\s<>'\"]+){2,}", "[PATH]", text)


def _reader_response(title: str, rows: list[tuple[str, Any]]) -> Response:
    body = ["<!doctype html><html><head><meta charset='utf-8'><title>Nur-Leseansicht</title></head><body>"]
    body.append("<h1>Nur-Leseansicht</h1>")
    body.append(f"<h2>{_html.escape(title)}</h2>")
    for label, value in rows:
        if value in (None, ""):
            continue
        body.append(f"<p><strong>{_html.escape(label)}</strong> {_html.escape(str(value))}</p>")
    body.append("</body></html>")
    return Response(
        "".join(body),
        media_type="text/html; charset=utf-8",
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
        },
    )


def _find_email_account_config(config: Dict[str, Any], account_ref: str) -> Dict[str, Any] | None:
    wanted = str(account_ref or "").strip().lower()
    for account in _email_account_configs(config):
        candidates = {
            _email_account_address(account),
            _email_account_label(account),
            str(account.get("account") or ""),
            str(account.get("user_google_email") or ""),
        }
        if wanted in {candidate.strip().lower() for candidate in candidates if candidate}:
            return account
    if os.environ.get("AIWERK_CUI_EMAIL_BACKEND") or os.environ.get("AIWERK_CUI_GOOGLE_EMAIL") or os.environ.get("AIWERK_CUI_EMAIL_ACCOUNT"):
        for account in _email_account_configs(config):
            if account:
                return account
    return None


def _is_google_email_backend(backend: str) -> bool:
    return str(backend or "").strip().lower() in {"google_workspace", "google", "gmail"}


def _email_open_url(account: Dict[str, Any], item: Dict[str, Any]) -> str:
    account_ref = str(account.get("address") or account.get("label") or account.get("account") or "")
    message_id = str(item.get("message_id") or item.get("id") or "")
    return (
        "/api/assistant/email/view?"
        f"account={urllib.parse.quote(account_ref)}&id={urllib.parse.quote(message_id)}"
    )


def _attach_email_open_urls(account: Dict[str, Any], items: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
    account["items"] = items
    for item in items:
        if isinstance(item, dict) and (item.get("message_id") or item.get("id")):
            item["open_url"] = _email_open_url(account, item)
    return items


@app.get("/api/assistant/email/view")
def view_assistant_email(request: Request, account: str, id: str) -> Response:
    _require_token(request)
    account_ref = str(account or "").strip()
    message_id = str(id or "").strip()
    if not account_ref or not message_id:
        raise HTTPException(status_code=400, detail="Missing email account or message id")
    config = load_config()
    account_cfg = _find_email_account_config(config, account_ref)
    if not account_cfg:
        raise HTTPException(status_code=404, detail="Email account not configured")
    account_label = _email_account_address(account_cfg) or _email_account_label(account_cfg) or account_ref
    sender = ""
    subject = "Ohne Betreff"
    received_at = ""
    try:
        resources = _assistant_resources_payload(request, force_refresh=False)
        accounts = ((resources.get("email") or {}).get("accounts") or []) if isinstance(resources, dict) else []
        for account_entry in accounts:
            if str(account_entry.get("address") or account_entry.get("label") or "").strip().lower() != account_ref.lower():
                continue
            for item in account_entry.get("items") or []:
                if message_id in {str(item.get("message_id") or ""), str(item.get("id") or "")}:
                    sender = str(item.get("sender") or "")
                    subject = str(item.get("subject") or subject)
                    received_at = str(item.get("received_at") or "")
                    break
    except Exception:
        _log.debug("Could not hydrate email metadata for reader", exc_info=True)
    try:
        backend = _email_backend_name(account_cfg)
        if _is_google_email_backend(backend):
            body = _run_google_workspace_message_read(config, account_cfg, message_id)
        else:
            body = _run_himalaya_message_read(
                message_id,
                account=str(account_cfg.get("account") or account_cfg.get("name") or "") or None,
                folder=str(account_cfg.get("folder") or account_cfg.get("mailbox") or "") or None,
            )
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid message id")
    except FileNotFoundError:
        raise HTTPException(status_code=503, detail="Himalaya is not installed")
    except Exception as exc:
        _log.debug("CUI email reader failed: %s", exc)
        raise HTTPException(status_code=502, detail="Email could not be loaded")
    return _reader_response(
        subject,
        [
            ("Konto:", _sanitize_reader_literal(account_label)),
            ("Von:", _sanitize_reader_literal(sender)),
            ("Empfangen:", _format_swiss_datetime(received_at)),
            ("Inhalt:", _sanitize_reader_text(body)),
        ],
    )


@app.post("/api/assistant/email/view")
def view_assistant_email_post(request: Request, payload: Dict[str, Any]) -> Dict[str, Any]:
    _require_token(request)
    return {"html": _plain_email_reader_html(payload)}


@app.post("/api/assistant/attachments/resource")
def attach_assistant_resource(request: Request, payload: Dict[str, Any]) -> Dict[str, Any]:
    _require_token(request)
    from hermes_constants import get_hermes_home
    item = dict(payload.get("item") or {})
    session_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(payload.get("session_id") or "session"))
    kind = str(payload.get("kind") or item.get("kind") or "").strip().lower()
    if kind == "shared_file":
        return {"attachments": [_create_shared_file_attachment(load_config(), item, session_id)]}
    root = get_hermes_home() / "dashboard_uploads"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{secrets.token_hex(8)}-{session_id}-resource.txt"
    lines = [
        str(item.get("title") or item.get("name") or "Resource"),
        str(item.get("account_address") or ""),
        _sanitize_reader_text(item.get("location_hint") or item.get("description") or item.get("html_link") or ""),
        _sanitize_reader_text(item.get("html_link") or ""),
    ]
    path.write_text("\n".join(line for line in lines if line), encoding="utf-8")
    return {"attachments": [{"name": path.name, "path": str(path), "extraction": "text", "is_image": False}]}


@app.get("/api/assistant/calendar/view")
def view_assistant_calendar_event(request: Request, account: str, id: str) -> Response:
    _require_token(request)
    resources = _assistant_resources_payload(request)
    found: Dict[str, Any] | None = None
    for acct in resources.get("calendar", {}).get("accounts", []):
        if str(acct.get("address") or "").lower() != account.lower():
            continue
        for item in acct.get("items", []):
            if str(item.get("id") or item.get("event_id") or "") == id:
                found = dict(item)
                break
    if found is None:
        raise HTTPException(status_code=404, detail="Calendar event not found")
    config = load_config()
    account_cfg = _calendar_account_config_for_ref(config, account)
    source = str(found.get("source") or account_cfg.get("backend") or "").lower()
    if (not found.get("description") and not found.get("location_hint")) or source in {"microsoft_calendar", "outlook"}:
        try:
            detail_payload = (
                _fetch_microsoft_calendar_event_detail(config, account_cfg, id)
                if source in {"microsoft_calendar", "outlook"}
                else _fetch_google_workspace_calendar_event_detail(config, account_cfg, id)
            )
            detail_text = _extract_bridge_text(detail_payload)
            if source in {"microsoft_calendar", "outlook"}:
                try:
                    detail = _json.loads(detail_text)
                except Exception:
                    detail = {}
                found.update({
                    "title": detail.get("subject") or detail.get("title") or found.get("title"),
                    "description": detail.get("bodyPreview") or detail.get("description") or found.get("description"),
                    "location_hint": (detail.get("location") or {}).get("displayName") if isinstance(detail.get("location"), dict) else found.get("location_hint"),
                    "starts_at": _microsoft_graph_datetime(detail.get("start")) or found.get("starts_at"),
                    "ends_at": _microsoft_graph_datetime(detail.get("end")) or found.get("ends_at"),
                    "html_link": detail.get("webLink") or found.get("html_link"),
                })
            else:
                detail = _parse_google_workspace_event_detail(detail_payload)
                found.update({
                    "title": detail.get("title") or found.get("title"),
                    "description": detail.get("description") or found.get("description"),
                    "location_hint": detail.get("location") or found.get("location_hint"),
                    "starts_at": detail.get("starts") or found.get("starts_at"),
                    "ends_at": detail.get("ends") or found.get("ends_at"),
                    "html_link": detail.get("link") or found.get("html_link"),
                })
        except Exception:
            pass
    return _reader_response(
        str(found.get("title") or "Termin"),
        [
            ("Start:", _format_swiss_datetime(found.get("starts_at"))),
            ("Ende:", _format_swiss_datetime(found.get("ends_at"))),
            ("Ort:", _sanitize_reader_literal(found.get("location_hint"))),
            ("Beschreibung:", _sanitize_reader_text(found.get("description") or found.get("summary") or found.get("html_link"))),
            ("Link:", _sanitize_reader_text(found.get("html_link") or "[LINK]")),
        ],
    )


def _shared_folder_agent_downloads_html(path: Path) -> str:
    return _html.escape(str(path))


def _run_google_workspace_message_read(config: Dict[str, Any], account_cfg: Dict[str, Any], message_id: str) -> str:
    server = _google_workspace_server(account_cfg, env_first=True)
    user_google_email = _google_workspace_user_email(account_cfg, env_first=True)
    payload = _call_aiwerk_bridge_tool(
        config,
        server=server,
        tool="get_gmail_messages_content_batch",
        params={"message_ids": [str(message_id)], "user_google_email": user_google_email, "format": "full"},
    )
    return _extract_bridge_text(payload) or _json.dumps(payload, sort_keys=True)


def _google_workspace_email_summary(
    config: Dict[str, Any],
    account_cfg: Dict[str, Any] | None = None,
) -> Dict[str, Any] | None:
    if _env_truthy("AIWERK_CUI_EMAIL_DISABLE_AIWERK_BRIDGE"):
        return None
    account_cfg = dict(account_cfg or {})
    if not account_cfg:
        accounts = [
            account for account in _email_account_configs(config)
            if _email_backend_name(account) in {"google_workspace", "google", "gmail"}
        ]
        if accounts:
            account_cfg = accounts[0]
        elif not (
            os.environ.get("AIWERK_CUI_EMAIL_BACKEND")
            or os.environ.get("AIWERK_CUI_GOOGLE_EMAIL")
            or os.environ.get("AIWERK_CUI_GOOGLE_WORKSPACE_SERVER")
        ):
            return None
        else:
            account_cfg = {"backend": os.environ.get("AIWERK_CUI_EMAIL_BACKEND")}
    backend = os.environ.get("AIWERK_CUI_EMAIL_BACKEND") if not account_cfg else ""
    backend = (backend or _email_backend_name(account_cfg) or "google_workspace").lower()
    if backend not in {"google_workspace", "google", "gmail"}:
        return None
    server = _google_workspace_server(account_cfg, env_first=True)
    user_google_email = _google_workspace_user_email(account_cfg, env_first=True)
    unread_query = str(account_cfg.get("unread_query") or os.environ.get("AIWERK_CUI_GMAIL_UNREAD_QUERY") or "in:inbox is:unread")
    latest_query = str(account_cfg.get("latest_query") or os.environ.get("AIWERK_CUI_GMAIL_LATEST_QUERY") or "in:inbox")
    try:
        unread_ids = _gmail_bridge_search_message_ids(
            config,
            unread_query,
            server=server,
            user_google_email=user_google_email,
            page_size=_ASSISTANT_EMAIL_UNREAD_SCAN_LIMIT,
        )
        latest_items = _gmail_bridge_message_items(
            config,
            latest_query,
            server=server,
            user_google_email=user_google_email,
            page_size=_ASSISTANT_EMAIL_PREVIEW_ITEMS,
        )
    except Exception as exc:
        return {
            "label": _email_account_label(account_cfg) or user_google_email,
            "address": user_google_email,
            "backend": "google_workspace",
            "status": _bridge_error_status(exc),
            "summary": "Gmail neu verbinden",
            "items": [],
            "unread_count": 0,
        }
    return {
        "label": _email_account_label(account_cfg) or user_google_email,
        "address": user_google_email,
        "backend": "google_workspace",
        "status": "connected",
        "summary": f"{len(unread_ids)} ungelesene E-Mails",
        "items": _attach_email_open_urls(
            {"address": user_google_email, "label": _email_account_label(account_cfg) or user_google_email},
            latest_items[:_ASSISTANT_EMAIL_PREVIEW_ITEMS],
        ),
        "unread_count": len(unread_ids),
    }


def _himalaya_account_value(account_cfg: Dict[str, Any] | None = None) -> str:
    account_cfg = account_cfg or {}
    return str(account_cfg.get("account") or account_cfg.get("address") or os.environ.get("AIWERK_CUI_EMAIL_ACCOUNT") or os.environ.get("HIMALAYA_ACCOUNT") or "").strip()


def _himalaya_folder_value(account_cfg: Dict[str, Any] | None = None) -> str:
    account_cfg = account_cfg or {}
    return str(account_cfg.get("folder") or account_cfg.get("mailbox") or os.environ.get("AIWERK_CUI_EMAIL_FOLDER") or os.environ.get("HIMALAYA_FOLDER") or "INBOX").strip()


def _run_himalaya_envelope_list(
    query: str | None = None,
    page_size: int = _ASSISTANT_EMAIL_PREVIEW_ITEMS,
    account: str | None = None,
    folder: str | None = None,
) -> list[Dict[str, Any]]:
    binary = shutil.which("himalaya")
    if not binary:
        return []
    resolved_account = account or os.environ.get("AIWERK_CUI_EMAIL_ACCOUNT") or os.environ.get("HIMALAYA_ACCOUNT")
    resolved_folder = folder or os.environ.get("AIWERK_CUI_EMAIL_FOLDER") or os.environ.get("HIMALAYA_FOLDER") or "INBOX"
    cmd = [binary, "envelope", "list"]
    if resolved_account:
        cmd.extend(["--account", str(resolved_account)])
    if resolved_folder:
        cmd.extend(["--folder", str(resolved_folder)])
    cmd.extend(["--page-size", str(page_size), "--output", "json"])
    if query:
        cmd.append(str(query))
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=_ASSISTANT_EMAIL_TIMEOUT_SECONDS,
        check=False,
    )
    if proc.returncode != 0:
        return []
    try:
        payload = _json.loads(proc.stdout or "[]")
    except Exception:
        return []
    if isinstance(payload, dict):
        payload = payload.get("items") or payload.get("messages") or payload.get("envelopes") or []
    return [dict(item) for item in payload if isinstance(item, dict)]


def _run_himalaya_message_read(message_id: str, account: str | None = None, folder: str | None = None) -> str:
    binary = shutil.which("himalaya")
    if not binary:
        raise FileNotFoundError("himalaya not installed")
    resolved_account = account or os.environ.get("AIWERK_CUI_EMAIL_ACCOUNT") or os.environ.get("HIMALAYA_ACCOUNT")
    resolved_folder = folder or os.environ.get("AIWERK_CUI_EMAIL_FOLDER") or os.environ.get("HIMALAYA_FOLDER") or "INBOX"
    cmd = [binary, "message", "read", "--preview", "--output", "plain"]
    if resolved_account:
        cmd.extend(["--account", str(resolved_account)])
    if resolved_folder:
        cmd.extend(["--folder", str(resolved_folder)])
    cmd.append(str(message_id))
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=_ASSISTANT_EMAIL_TIMEOUT_SECONDS,
        check=False,
    )
    return proc.stdout if proc.returncode == 0 else ""


def _himalaya_email_summary(
    config: Dict[str, Any],
    account_cfg: Dict[str, Any] | None = None,
) -> Dict[str, Any] | None:
    if _env_truthy("AIWERK_CUI_EMAIL_DISABLE_HIMALAYA"):
        return None
    account_cfg = dict(account_cfg or {})
    if not account_cfg:
        accounts = [
            account for account in _email_account_configs(config)
            if _email_backend_name(account) in {"himalaya", "imap", ""}
        ]
        if accounts:
            account_cfg = accounts[0]
        elif not (
            os.environ.get("AIWERK_CUI_EMAIL_BACKEND")
            or os.environ.get("AIWERK_CUI_EMAIL_ACCOUNT")
            or os.environ.get("HIMALAYA_ACCOUNT")
            or os.environ.get("AIWERK_CUI_EMAIL_FOLDER")
            or os.environ.get("HIMALAYA_FOLDER")
        ):
            return None
        else:
            account_cfg = {}
    backend = os.environ.get("AIWERK_CUI_EMAIL_BACKEND") if not account_cfg else ""
    backend = (backend or _email_backend_name(account_cfg) or "himalaya").lower()
    if backend not in {"himalaya", "imap"}:
        return None
    account = _himalaya_account_value(account_cfg)
    folder = _himalaya_folder_value(account_cfg)
    items = _run_himalaya_envelope_list(page_size=_ASSISTANT_EMAIL_PREVIEW_ITEMS, account=account or None, folder=folder)
    account_payload = {
        "label": _email_account_label(account_cfg) or account or folder,
        "address": _email_account_address(account_cfg) or account,
        "backend": "himalaya",
        "folder": folder,
        "status": "connected" if items or account or folder else "not_configured",
        "summary": f"{len(items)} E-Mails" if items else "Himalaya verbunden",
        "items": items[:_ASSISTANT_EMAIL_PREVIEW_ITEMS],
        "unread_count": len(items),
    }
    _attach_email_open_urls(account_payload, account_payload["items"])
    return account_payload


def _maildir_email_summary(maildir: str | None) -> Dict[str, Any] | None:
    if not maildir:
        return None
    root = Path(maildir).expanduser()
    new_dir = root / "new"
    try:
        unread_count = sum(1 for item in new_dir.iterdir() if item.is_file()) if new_dir.exists() else 0
    except OSError:
        unread_count = 0
    return {
        "label": root.name or "Maildir",
        "address": str(root),
        "backend": "maildir",
        "folder": "new",
        "status": "connected",
        "summary": f"{unread_count} ungelesene E-Mails",
        "items": [],
        "unread_count": unread_count,
    }


def _merge_email_summaries(summaries: list[Dict[str, Any]]) -> Dict[str, Any]:
    accounts = [summary for summary in summaries if summary]
    items: list[Dict[str, Any]] = []
    unread_count = 0
    for account in accounts:
        account_items = [dict(item) for item in account.get("items", []) if isinstance(item, dict)]
        _attach_email_open_urls(account, account_items)
        items.extend(account_items)
        unread_count += int(account.get("unread_count") or 0)
    status = _status_from_summaries(accounts)
    if status == "auth_required":
        summary_text = "E-Mail neu verbinden"
    elif accounts:
        summary_text = f"{unread_count} ungelesene E-Mails" if unread_count else "E-Mail verbunden"
    else:
        summary_text = "Keine E-Mail verbunden"
    return {
        "status": status,
        "summary": summary_text,
        "accounts": accounts,
        "items": items[:_ASSISTANT_EMAIL_PREVIEW_ITEMS],
        "unread_count": unread_count,
    }


def _email_summary(_config: Dict[str, Any]) -> Dict[str, Any]:
    config = _config or {}
    payload = _json_file_payload("AIWERK_CUI_EMAIL_SUMMARY_JSON")
    if payload is not None:
        payload.setdefault("status", "connected")
        payload.setdefault("accounts", [])
        return payload
    summaries: list[Dict[str, Any]] = []
    accounts = _email_account_configs(config)
    if accounts:
        for account in accounts:
            backend = _email_backend_name(account)
            if backend in {"google_workspace", "google", "gmail"}:
                summary = _google_workspace_email_summary(config, account)
            elif backend in {"himalaya", "imap"} or account.get("account") or account.get("folder"):
                summary = _himalaya_email_summary(config, account)
            else:
                summary = None
            if summary:
                summaries.append(summary)
    else:
        for summary in (_google_workspace_email_summary(config), _himalaya_email_summary(config)):
            if summary and summary.get("status") != "not_configured":
                summaries.append(summary)
    maildir_summary = _maildir_email_summary(os.environ.get("AIWERK_CUI_MAILDIR") or os.environ.get("MAILDIR"))
    if maildir_summary:
        summaries.append(maildir_summary)
    return _merge_email_summaries(summaries)


def _vaultwarden_summary(_config: Dict[str, Any]) -> Dict[str, Any]:
    payload = _json_file_payload("AIWERK_CUI_VAULT_SUMMARY_JSON")
    if payload is not None:
        return payload
    url = _vault_url_from_config(_config)
    return {"status": "not_configured", "summary": "Kein Tresor verbunden", "url": url, "items": [], "weak_count": 0, "reused_count": 0, "compromised_count": 0}


def _vault_url_from_config(config: Dict[str, Any]) -> str:
    vault = _assistant_config_section(config or {}, "vault")
    return str(os.environ.get("AIWERK_CUI_VAULT_URL") or vault.get("url") or (config or {}).get("vault_url") or "https://pass.aiwerk.ch")


def _connector_summary(
    config: Dict[str, Any],
    shared_folder: Dict[str, Any],
    email: Dict[str, Any],
    calendar: Dict[str, Any],
    *,
    include_live_bridge_children: bool = True,
) -> list[Dict[str, Any]]:
    connectors: list[Dict[str, Any]] = []
    servers = config.get("mcp_servers") if isinstance(config.get("mcp_servers"), dict) else {}
    if isinstance(servers.get("aiwerk_bridge"), dict) and servers["aiwerk_bridge"].get("enabled", True):
        bridge = {
            "id": "aiwerk_bridge",
            "label": "AIWerk Bridge",
            "status": "connected",
            "capabilities": ["MCP"],
            "children": _aiwerk_bridge_subservers(config) if include_live_bridge_children else [],
        }
        connectors.append(bridge)
    if isinstance(servers.get("hermes_neo4j"), dict) and servers["hermes_neo4j"].get("enabled", True):
        connectors.append({"id": "hermes_neo4j", "label": "Wissensbasis", "status": "connected", "capabilities": ["MCP"]})
    return connectors


def _merge_calendar_summaries(summaries: list[Dict[str, Any]]) -> Dict[str, Any]:
    accounts = [summary for summary in summaries if summary]
    items: list[Dict[str, Any]] = []
    for account in accounts:
        items.extend(dict(item) for item in account.get("items", []) if isinstance(item, dict))
    items.sort(key=lambda item: str(item.get("starts_at") or ""))
    status = _status_from_summaries(accounts)
    if status == "auth_required":
        summary_text = "Kalender neu verbinden"
    elif items:
        summary_text = f"{len(items)} kommende Termine" + (f" in {len(accounts)} Kalendern" if len(accounts) > 1 else "")
    else:
        summary_text = "Keine kommenden Termine" if accounts else "Kein Kalender verbunden"
    return {"status": status, "summary": summary_text, "accounts": accounts, "items": items}


def _calendar_open_url(account: Dict[str, Any], item: Dict[str, Any]) -> str:
    address = str(account.get("address") or account.get("user_google_email") or account.get("user_principal_name") or "")
    event_id = str(item.get("id") or item.get("event_id") or "")
    return f"/api/assistant/calendar/view?account={urllib.parse.quote(address)}&id={urllib.parse.quote(event_id)}"


def _google_workspace_calendar_summary(config: Dict[str, Any] | None, account_cfg: Dict[str, Any], *, now: _datetime | None = None) -> Dict[str, Any] | None:
    config = config or {}
    server = str(account_cfg.get("mcp_server") or "google-workspace-aiwerk")
    email = str(account_cfg.get("user_google_email") or account_cfg.get("address") or "")
    now = now or _datetime.now(_timezone.utc)
    start = now.astimezone(_timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        horizon_days = _bounded_int(
            account_cfg.get("horizon_days") or os.environ.get("AIWERK_CUI_CALENDAR_HORIZON_DAYS"),
            default=7,
            minimum=1,
            maximum=365,
        )
        max_results = _bounded_int(
            account_cfg.get("max_results") or os.environ.get("AIWERK_CUI_CALENDAR_MAX_RESULTS"),
            default=5,
            minimum=1,
            maximum=50,
        )
        payload = _call_aiwerk_bridge_tool(
            config,
            server=server,
            tool="get_events",
            params={
                "calendar_id": email or "primary",
                "user_google_email": email,
                "time_min": start.isoformat().replace("+00:00", "Z"),
                "time_max": (start + _timedelta(days=horizon_days)).isoformat().replace("+00:00", "Z"),
                "max_results": max_results,
                "event_types": ["default"],
            },
        )
    except Exception as exc:
        return {"label": account_cfg.get("label") or email, "address": email, "source": "google_calendar", "status": _bridge_error_status(exc), "summary": "Google Kalender neu verbinden", "items": []}
    if _bridge_payload_is_auth_error(payload):
        return {"label": account_cfg.get("label") or email, "address": email, "source": "google_calendar", "status": "auth_required", "summary": "Google Kalender neu verbinden", "items": []}
    text = _extract_bridge_text(payload)
    items: list[Dict[str, Any]] = []
    for match in re.finditer(r'-\s+"(?P<title>[^"]+)"\s+\(Starts:\s*(?P<start>[^,]+),\s*Ends:\s*(?P<end>[^)]+)\)\s*ID:\s*(?P<id>[^\s|]+)', text):
        item = {
            "id": match.group("id"),
            "event_id": match.group("id"),
            "title": match.group("title"),
            "starts_at": match.group("start"),
            "ends_at": match.group("end"),
            "account_label": account_cfg.get("label") or email,
            "account_address": email,
            "source": "google_calendar",
        }
        item["open_url"] = _calendar_open_url({"address": email}, item)
        items.append(item)
    return {
        "label": account_cfg.get("label") or email,
        "address": email,
        "calendar_id": email or "primary",
        "source": "google_calendar",
        "status": "connected",
        "summary": f"{len(items)} kommende Termine" if items else "Keine kommenden Termine",
        "items": items,
    }


def _microsoft_graph_datetime(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    if value.get("date"):
        return str(value.get("date"))
    dt = str(value.get("dateTime") or "")
    tz = str(value.get("timeZone") or "")
    if not dt:
        return ""
    if tz.upper() == "UTC":
        return dt if dt.endswith("Z") else dt + "Z"
    return f"{dt} [{tz}]" if tz else dt


def _microsoft_calendar_summary(config: Dict[str, Any] | None, account_cfg: Dict[str, Any], *, now: _datetime | None = None) -> Dict[str, Any] | None:
    config = config or {}
    server = str(account_cfg.get("mcp_server") or "microsoft-calendar")
    address = str(account_cfg.get("address") or account_cfg.get("user_principal_name") or "")
    now = now or _datetime.now(_timezone.utc)
    start = now.astimezone(_timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        horizon_days = _bounded_int(
            account_cfg.get("horizon_days") or os.environ.get("AIWERK_CUI_CALENDAR_HORIZON_DAYS"),
            default=14,
            minimum=1,
            maximum=365,
        )
        max_results = _bounded_int(
            account_cfg.get("max_results") or os.environ.get("AIWERK_CUI_CALENDAR_MAX_RESULTS"),
            default=5,
            minimum=1,
            maximum=50,
        )
        payload = _call_aiwerk_bridge_tool(
            config,
            server=server,
            tool="get-calendar-view",
            params={
                "startDateTime": start.isoformat().replace("+00:00", "Z"),
                "endDateTime": (start + _timedelta(days=horizon_days)).isoformat().replace("+00:00", "Z"),
            },
        )
    except Exception as exc:
        return {"label": account_cfg.get("label") or address, "address": address, "source": "microsoft_calendar", "status": _bridge_error_status(exc), "summary": "Outlook Kalender neu verbinden", "items": []}
    if _bridge_payload_is_auth_error(payload):
        return {"label": account_cfg.get("label") or address, "address": address, "source": "microsoft_calendar", "status": "auth_required", "summary": "Outlook Kalender neu verbinden", "items": []}
    text = _extract_bridge_text(payload)
    try:
        data = _json.loads(text)
    except Exception:
        data = payload
    events = data.get("value") if isinstance(data, dict) else []
    items: list[Dict[str, Any]] = []
    for event in events or []:
        if not isinstance(event, dict):
            continue
        item = {
            "id": str(event.get("id") or ""),
            "event_id": str(event.get("id") or ""),
            "title": str(event.get("subject") or event.get("title") or ""),
            "starts_at": _microsoft_graph_datetime(event.get("start")),
            "ends_at": _microsoft_graph_datetime(event.get("end")),
            "location_hint": (event.get("location") or {}).get("displayName") if isinstance(event.get("location"), dict) else "",
            "account_label": account_cfg.get("label") or address,
            "account_address": address,
            "source": "microsoft_calendar",
        }
        item["open_url"] = _calendar_open_url({"address": address}, item)
        items.append(item)
    items.sort(key=lambda item: item.get("starts_at") or "")
    items = items[:max_results]
    return {
        "label": account_cfg.get("label") or address,
        "address": address,
        "source": "microsoft_calendar",
        "status": "connected",
        "summary": f"{len(items)} kommende Termine" if items else "Keine kommenden Termine",
        "items": items,
    }


def _calendar_summary(config: Dict[str, Any]) -> Dict[str, Any]:
    payload = _json_file_payload("AIWERK_CUI_CALENDAR_SUMMARY_JSON")
    if payload is not None:
        payload.setdefault("status", "connected")
        payload.setdefault("accounts", [])
        return payload
    summaries: list[Dict[str, Any]] = []
    for account in _calendar_accounts(config):
        backend = str(account.get("backend") or "").lower()
        if backend in {"google_workspace", "google_calendar"}:
            summary = _google_workspace_calendar_summary(config, account)
        elif backend in {"microsoft_calendar", "outlook"}:
            summary = _microsoft_calendar_summary(config, account)
        else:
            summary = None
        if summary:
            for item in summary.get("items", []):
                item.setdefault("open_url", _calendar_open_url(summary, item))
            summaries.append(summary)
    return _merge_calendar_summaries(summaries)


def _contacts_summary(_config: Dict[str, Any], email: Dict[str, Any], calendar: Dict[str, Any]) -> Dict[str, Any]:
    config = _config or {}
    own = {str(account.get("address") or "").lower() for account in email.get("accounts", [])}
    own.update(str(account.get("address") or "").lower() for account in calendar.get("accounts", []))
    contacts = [_normalize_contact_item(item) for item in _read_manual_contacts()]
    contacts.extend(_normalize_contact_item(item) for item in _contacts_from_google_workspace(config, limit=_contacts_saved_top_up_target()))
    contacts.extend(_normalize_contact_item(item) for item in _contacts_from_google_workspace_interactions(config, own))
    contacts.extend(_normalize_contact_item(item) for item in _contacts_from_himalaya_interactions(config, own))
    for account in email.get("accounts", []):
        for item in account.get("items", []):
            sender = str(item.get("sender") or "")
            match = re.search(r"([^<]+)<([^>]+)>", sender)
            if match:
                contacts.append({"display_name": match.group(1).strip(), "email": match.group(2).strip(), "source_badges": ["E-Mail"]})
    filtered: list[Dict[str, Any]] = []
    seen: set[str] = set()
    hidden = set(_read_contacts_store_payload().get("hidden") or [])
    for contact in contacts:
        email_key = str(contact.get("email") or "").strip().lower()
        key = str(contact.get("key") or contact.get("email") or contact.get("display_name") or "")
        if not email_key or email_key in seen or key in hidden:
            continue
        if not _contact_is_customer_safe(contact, own):
            continue
        seen.add(email_key)
        filtered.append(contact)
    return {
        "status": "connected" if filtered else "not_configured",
        "source_label": "Relevante Kontakte",
        "relevant": filtered[:_contacts_page_size(config)],
        "frequent": filtered[:_ASSISTANT_CONTACT_PREVIEW_ITEMS],
        "total_count": len(filtered),
    }


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
        attachment_lines.append(f"- {filename} ({descriptor})" if filename else f"- {descriptor}")
    return kept, attachment_lines


def _replace_email_reader_links(body: str) -> str:
    text = str(body or "")
    text = _EMAIL_READER_URL_RE.sub("[LINK]", text)
    text = _EMAIL_READER_WWW_RE.sub("[LINK]", text)
    return re.sub(r"(?:\[LINK\](?:\s*[,;|·-]\s*)?){2,}", "[LINK]", text)


def _wrap_long_email_reader_body_text(text: str) -> str:
    non_empty_lines = [line for line in str(text or "").splitlines() if line.strip()]
    if len(non_empty_lines) == 1 and len(non_empty_lines[0]) > 500:
        long_line = non_empty_lines[0]
        long_line = _EMAIL_READER_LONG_BODY_HINT_RE.sub("\n\n", long_line)
        long_line = _EMAIL_READER_LONG_BODY_BOUNDARY_RE.sub("\n\n", long_line)
        return re.sub(r"\n{3,}", "\n\n", long_line).strip()
    return str(text or "").strip()


def _normalize_email_reader_body_text(body: str) -> str:
    text = _EMAIL_READER_INVISIBLE_RE.sub("", str(body or "")).replace("\r\n", "\n").replace("\r", "\n")
    normalized_lines = [re.sub(r"[ \t\f\v]{2,}", " ", line).strip() for line in text.split("\n")]
    text = "\n".join(normalized_lines)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return _wrap_long_email_reader_body_text(text)


def _strip_email_reader_transport_metadata(body: str) -> str:
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
        cleaned = f"{cleaned.rstrip()}\n\nAnhaenge:\n" + "\n".join(attachment_summaries) if cleaned else "Anhaenge:\n" + "\n".join(attachment_summaries)
    cleaned = _wrap_long_email_reader_body_text(cleaned)
    if not cleaned and not stripped_any_header:
        cleaned = text
    return _replace_email_reader_links(cleaned)


def _pass_first_line(entry: str) -> str:
    try:
        import subprocess as _subprocess
        out = _subprocess.check_output(
            ["pass", str(entry)], text=True, encoding="utf-8", errors="replace", timeout=5
        )
        return out.splitlines()[0] if out else ""
    except Exception:
        return ""


def _clean_shared_relative_path(value: str) -> str | None:
    parts = [part for part in str(value or "").replace("\\", "/").split("/") if part]
    clean_parts: list[str] = []
    for part in parts:
        if part in {".", ".."} or "/" in part or _is_hidden_shared_item(Path(part)):
            return None
        clean_parts.append(part)
    return "/".join(clean_parts) if clean_parts else None


def _clean_shared_cloud_path(value: str) -> str | None:
    clean = _clean_shared_relative_path(value)
    return f"/{clean}" if clean else "/"


def _shared_reference_uri(rel_path: str) -> str | None:
    clean = _clean_shared_relative_path(rel_path)
    return f"shared://{urllib.parse.quote(clean, safe='/')}" if clean else None


def _shared_cloud_uses_webdav(cloud: Dict[str, Any] | None) -> bool:
    if not isinstance(cloud, dict):
        return False
    kind = str(cloud.get("type") or cloud.get("kind") or "").strip().lower().replace("-", "_")
    return kind in {"webdav", "sftpgo_webdav", "webdav_sftpgo"} or bool(cloud.get("webdav_url") or cloud.get("dav_url"))


def _shared_cloud_browse_url(cloud: Dict[str, Any] | None, rel_path: str | None = None) -> str | None:
    if not isinstance(cloud, dict):
        return None
    base_url = str(cloud.get("base_url") or "").rstrip("/")
    share_id = str(cloud.get("share_id") or "").strip().strip("/")
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or not share_id:
        return None
    root_path = _clean_shared_cloud_path(str(cloud.get("path") or "/")) or "/"
    if rel_path:
        clean_rel = _clean_shared_relative_path(rel_path)
        if not clean_rel:
            return None
        browse_path = _clean_shared_cloud_path(root_path.rstrip("/") + "/" + clean_rel)
    else:
        browse_path = root_path
    if not browse_path:
        return None
    return (
        f"{base_url}/web/client/pubshares/{urllib.parse.quote(share_id, safe='')}"
        f"/browse?path={urllib.parse.quote(browse_path, safe='')}"
    )


def _urlopen_text(opener: urllib.request.OpenerDirector, request: urllib.request.Request, timeout: int = 20) -> tuple[int, str]:
    with opener.open(request, timeout=timeout) as response:
        data = response.read(512_000)
        return response.status, data.decode("utf-8", errors="replace")


def _urlopen_json(opener: urllib.request.OpenerDirector, request: urllib.request.Request, timeout: int = 20) -> Any:
    with opener.open(request, timeout=timeout) as response:
        data = response.read(512_000)
        return json.loads(data.decode("utf-8", errors="replace"))


def _sftpgo_item_kind(raw: Dict[str, Any]) -> str:
    raw_type = raw.get("type")
    if raw_type in (1, "1", "dir", "directory", "folder"):
        return "folder"
    return "file"


def _sftpgo_modified_at(raw: Dict[str, Any]) -> str | None:
    for key in ("modified_time", "mtime", "last_modified"):
        value = raw.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, (int, float)) and value > 0:
            seconds = value / 1000 if value > 10_000_000_000 else value
            return datetime.fromtimestamp(seconds, timezone.utc).isoformat().replace("+00:00", "Z")
    return None


def _sftpgo_pubshare_items(cloud: Dict[str, Any]) -> list[Dict[str, Any]]:
    import http.cookiejar

    base_url = str(cloud.get("base_url") or "").rstrip("/")
    share_id = str(cloud.get("share_id") or "").strip().strip("/")
    pass_entry = str(cloud.get("password_pass_entry") or cloud.get("pass_entry") or "").strip()
    root_path = _clean_shared_cloud_path(str(cloud.get("path") or "/")) or "/"
    max_depth = int(cloud.get("max_depth") or _ASSISTANT_RESOURCE_MAX_SHARED_DEPTH)
    password = _pass_first_line(pass_entry)
    if not base_url or not share_id or not pass_entry or not password:
        return []

    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    quoted_share_id = urllib.parse.quote(share_id, safe="")
    login_next = urllib.parse.quote(f"/web/client/pubshares/{share_id}/browse", safe="")
    login_url = f"{base_url}/web/client/pubshares/{quoted_share_id}/login?next={login_next}"
    try:
        status, login_html = _urlopen_text(opener, urllib.request.Request(login_url, headers={"User-Agent": "Hermes-CUI/1.0"}))
        if status >= 400:
            return []
        match = re.search(r'name="_form_token"\s+value="([^"]+)"', login_html)
        if not match:
            return []
        body = urllib.parse.urlencode({"share_password": password, "_form_token": _html.unescape(match.group(1))}).encode()
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
    except Exception:
        return []
    csrf_match = re.search(r"'X-CSRF-TOKEN':\s*'([^']+)'", browse_html)
    headers = {"User-Agent": "Hermes-CUI/1.0"}
    if csrf_match:
        headers["X-CSRF-TOKEN"] = csrf_match.group(1)

    def child_path(parent: str, name: str) -> str:
        return _clean_shared_cloud_path(parent.rstrip("/") + "/" + name) or "/"

    def list_path(path: str, depth: int) -> list[Dict[str, Any]]:
        dirs_url = (
            f"{base_url}/web/client/pubshares/{quoted_share_id}/dirs"
            f"?path={urllib.parse.quote(path, safe='')}"
        )
        try:
            raw_items = _urlopen_json(opener, urllib.request.Request(dirs_url, headers=headers))
        except Exception:
            return []
        if not isinstance(raw_items, list):
            return []
        items: list[Dict[str, Any]] = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name") or "").strip()
            if not name or "/" in name or name in {".", ".."} or _is_hidden_shared_item(Path(name)):
                continue
            kind = _sftpgo_item_kind(raw)
            item_path = child_path(path, name)
            rel_path = _clean_shared_relative_path(item_path[len(root_path.rstrip("/") + "/"):] if item_path.startswith(root_path.rstrip("/") + "/") else name)
            if not rel_path:
                continue
            size = raw.get("size")
            size_bytes = int(size) if kind == "file" and isinstance(size, (int, float, str)) and str(size).isdigit() else None
            item: Dict[str, Any] = {
                "id": _safe_resource_id(rel_path),
                "name": name,
                "kind": kind,
                "mime": (mimetypes.guess_type(name)[0] or "application/octet-stream") if kind == "file" else None,
                "size_bytes": size_bytes,
                "modified_at": _sftpgo_modified_at(raw),
            }
            if kind == "file":
                item["open_url"] = f"/api/assistant/shared-folder/open?path={urllib.parse.quote(rel_path, safe='')}"
                reference_uri = _shared_reference_uri(rel_path)
                if reference_uri:
                    item["reference_uri"] = reference_uri
            elif kind == "folder":
                cloud_url = _shared_cloud_browse_url(cloud, rel_path)
                if cloud_url:
                    item["cloud_url"] = cloud_url
                if depth > 0:
                    item["children"] = list_path(item_path, depth - 1)
                    item["child_count"] = len(item["children"])
            items.append(item)
            if len(items) >= _ASSISTANT_RESOURCE_MAX_SHARED_ITEMS:
                break
        return items

    return list_path(root_path, max(0, min(max_depth, _ASSISTANT_RESOURCE_MAX_SHARED_DEPTH)))


def _webdav_cloud_url(cloud: Dict[str, Any]) -> str | None:
    raw_url = str(cloud.get("webdav_url") or cloud.get("dav_url") or cloud.get("base_url") or "").strip()
    parsed = urllib.parse.urlparse(raw_url.rstrip("/"))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "", ""))


def _webdav_cloud_root_path(cloud: Dict[str, Any]) -> str:
    return _clean_shared_cloud_path(str(cloud.get("path") or cloud.get("root_path") or "/")) or "/"


def _webdav_auth_header(cloud: Dict[str, Any]) -> str | None:
    import base64

    username = str(cloud.get("username") or cloud.get("user") or "").strip()
    pass_entry = str(cloud.get("password_pass_entry") or cloud.get("pass_entry") or "").strip()
    password = _pass_first_line(pass_entry)
    if not username or not pass_entry or not password:
        return None
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _webdav_request_url(base_url: str, path: str, *, directory: bool = True) -> str:
    clean_path = _clean_shared_cloud_path(path) or "/"
    suffix = "/" if directory and not clean_path.endswith("/") else ""
    return f"{base_url}{urllib.parse.quote(clean_path, safe='/')}{suffix}"


def _webdav_response_prop(response: Any, name: str) -> str | None:
    value = response.findtext(f".//{{DAV:}}{name}")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _webdav_child_rel_path(root_path: str, href: str) -> str | None:
    clean_href = _clean_shared_cloud_path(urllib.parse.unquote(urllib.parse.urlparse(href).path))
    clean_root = _clean_shared_cloud_path(root_path) or "/"
    if not clean_href or clean_href.rstrip("/") == clean_root.rstrip("/"):
        return None
    prefix = clean_root.rstrip("/") + "/"
    if not clean_href.startswith(prefix):
        return None
    return _clean_shared_relative_path(clean_href[len(prefix):])


def _webdav_cloud_items(cloud: Dict[str, Any]) -> list[Dict[str, Any]]:
    import xml.etree.ElementTree as ET

    base_url = _webdav_cloud_url(cloud)
    auth_header = _webdav_auth_header(cloud)
    root_path = _webdav_cloud_root_path(cloud)
    max_depth = int(cloud.get("max_depth") or _ASSISTANT_RESOURCE_MAX_SHARED_DEPTH)
    if not base_url or not auth_header:
        return []
    propfind_body = (
        '<?xml version="1.0" encoding="utf-8" ?>'
        '<D:propfind xmlns:D="DAV:"><D:prop><D:displayname/><D:getcontentlength/>'
        '<D:getlastmodified/><D:resourcetype/></D:prop></D:propfind>'
    ).encode("utf-8")

    def list_path(path: str, depth: int) -> list[Dict[str, Any]]:
        request = urllib.request.Request(
            _webdav_request_url(base_url, path),
            data=propfind_body,
            method="PROPFIND",
            headers={"Authorization": auth_header, "Depth": "1", "Content-Type": "application/xml", "User-Agent": "Hermes-CUI/1.0"},
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                raw_xml = response.read(512_000)
            tree = ET.fromstring(raw_xml)
        except Exception:
            return []
        items: list[Dict[str, Any]] = []
        for raw_response in tree.findall("{DAV:}response"):
            href = raw_response.findtext("{DAV:}href") or ""
            current_rel = _webdav_child_rel_path(path, href)
            rel_path = _webdav_child_rel_path(root_path, href)
            if not current_rel or not rel_path:
                continue
            name = _webdav_response_prop(raw_response, "displayname") or Path(rel_path).name
            if not name or "/" in name or name in {".", ".."} or _is_hidden_shared_item(Path(name)):
                continue
            is_folder = raw_response.find(".//{DAV:}resourcetype/{DAV:}collection") is not None
            size_raw = _webdav_response_prop(raw_response, "getcontentlength")
            item: Dict[str, Any] = {
                "id": _safe_resource_id(rel_path),
                "name": name,
                "kind": "folder" if is_folder else "file",
                "mime": None if is_folder else (mimetypes.guess_type(name)[0] or "application/octet-stream"),
                "size_bytes": None if is_folder or not size_raw or not size_raw.isdigit() else int(size_raw),
                "modified_at": _webdav_response_prop(raw_response, "getlastmodified"),
            }
            if is_folder:
                cloud_url = _shared_cloud_browse_url(cloud, rel_path)
                if cloud_url:
                    item["cloud_url"] = cloud_url
                if depth > 0:
                    item["children"] = list_path(root_path.rstrip("/") + "/" + rel_path, depth - 1)
                    item["child_count"] = len(item["children"])
            else:
                item["open_url"] = f"/api/assistant/shared-folder/open?path={urllib.parse.quote(rel_path, safe='')}"
                reference_uri = _shared_reference_uri(rel_path)
                if reference_uri:
                    item["reference_uri"] = reference_uri
            items.append(item)
            if len(items) >= _ASSISTANT_RESOURCE_MAX_SHARED_ITEMS:
                break
        return items

    return list_path(root_path, max(0, min(max_depth, _ASSISTANT_RESOURCE_MAX_SHARED_DEPTH)))


def _download_webdav_cloud_file(cloud: Dict[str, Any], rel_path: str) -> tuple[bytes, str, str] | None:
    clean = _clean_shared_relative_path(rel_path)
    base_url = _webdav_cloud_url(cloud)
    auth_header = _webdav_auth_header(cloud)
    if not clean or not base_url or not auth_header:
        return None
    target_path = _clean_shared_cloud_path(_webdav_cloud_root_path(cloud).rstrip("/") + "/" + clean)
    if not target_path:
        return None
    filename = Path(clean).name
    request = urllib.request.Request(
        _webdav_request_url(base_url, target_path, directory=False),
        headers={"Authorization": auth_header, "User-Agent": "Hermes-CUI/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            content_type = response.headers.get("content-type", mimetypes.guess_type(filename)[0] or "application/octet-stream")
            data = response.read(_ASSISTANT_SHARED_FILE_OPEN_MAX_BYTES + 1)
            if response.status >= 400 or len(data) > _ASSISTANT_SHARED_FILE_OPEN_MAX_BYTES:
                return None
            return data, content_type, filename
    except Exception:
        return None


def _download_sftpgo_pubshare_file(cloud: Dict[str, Any], rel_path: str) -> tuple[bytes, str, str] | None:
    import http.cookiejar

    clean = _clean_shared_relative_path(rel_path)
    if not clean:
        return None
    base_url = str(cloud.get("base_url") or "").rstrip("/")
    share_id = str(cloud.get("share_id") or "").strip().strip("/")
    pass_entry = str(cloud.get("password_pass_entry") or cloud.get("pass_entry") or "").strip()
    password = _pass_first_line(pass_entry)
    if not base_url or not share_id or not pass_entry or not password:
        return None
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    quoted_share_id = urllib.parse.quote(share_id, safe="")
    login_next = urllib.parse.quote(f"/web/client/pubshares/{share_id}/browse", safe="")
    login_url = f"{base_url}/web/client/pubshares/{quoted_share_id}/login?next={login_next}"
    try:
        status, login_html = _urlopen_text(opener, urllib.request.Request(login_url, headers={"User-Agent": "Hermes-CUI/1.0"}))
        if status >= 400:
            return None
        match = re.search(r'name="_form_token"\s+value="([^"]+)"', login_html)
        if not match:
            return None
        body = urllib.parse.urlencode({"share_password": password, "_form_token": _html.unescape(match.group(1))}).encode()
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
        root_path = _clean_shared_cloud_path(str(cloud.get("path") or "/")) or "/"
        target_path = _clean_shared_cloud_path(root_path.rstrip("/") + "/" + clean)
        if not target_path:
            return None
        file_url = f"{base_url}/web/client/pubshares/{quoted_share_id}/browse?path={urllib.parse.quote(target_path, safe='')}"
        with opener.open(urllib.request.Request(file_url, headers=headers), timeout=30) as response:
            filename = Path(clean).name
            content_type = response.headers.get("content-type", mimetypes.guess_type(filename)[0] or "application/octet-stream")
            data = response.read(_ASSISTANT_SHARED_FILE_OPEN_MAX_BYTES + 1)
            if response.status >= 400 or len(data) > _ASSISTANT_SHARED_FILE_OPEN_MAX_BYTES or "text/html" in content_type.lower():
                return None
            return data, content_type, filename
    except Exception:
        return None


def _shared_folder_root() -> Path | None:
    return _resolve_shared_folder_root(load_config())


def _can_open_system_folder() -> bool:
    return True


def _request_looks_local(request: Request | None) -> bool:
    if request is None:
        return False
    host = (request.headers.get("host") or "").split(":", 1)[0]
    return host in {"127.0.0.1", "localhost", "::1"}


def _remote_open_allowed(request: Request | None) -> bool:
    return os.getenv("HERMES_CUI_ALLOW_REMOTE_FILE_MANAGER_OPEN", "").lower() in {"1", "true", "yes"} or _request_looks_local(request)


def _open_system_folder(path: Path, **_kwargs: Any) -> bool:
    if sys.platform == "darwin":
        args = ["open", str(path)]
    elif os.name == "nt":
        args = ["explorer", str(path)]
    else:
        args = ["xdg-open", str(path)]
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True


def _shared_cloud_url(config: Dict[str, Any], relative_path: str = "") -> str | None:
    return _shared_cloud_browse_url(_shared_cloud_config(config), relative_path or None)


def _safe_shared_path(root: Path, relative: str) -> Path:
    rel = urllib.parse.unquote(relative or "").lstrip("/")
    clean = _clean_shared_relative_path(rel)
    if not clean:
        raise HTTPException(status_code=404, detail="Not found")
    target = (root / rel).resolve()
    if root not in target.parents and target != root:
        raise HTTPException(status_code=404, detail="Not found")
    if any(part.startswith(".") for part in target.relative_to(root).parts):
        raise HTTPException(status_code=404, detail="Not found")
    return target


def _is_hidden_shared_item(path: Path) -> bool:
    lower = path.name.lower()
    return (
        path.name.startswith(".")
        or lower in _ASSISTANT_RESOURCE_HIDDEN_NAMES
        or any(marker in lower for marker in ("secret", "credential", "password", "token", "private-key"))
    )


def _shared_item(path: Path, root: Path, config: Dict[str, Any], depth: int = 0, max_depth: int = 4) -> Dict[str, Any]:
    rel = path.relative_to(root).as_posix()
    item = {"name": path.name, "kind": "folder" if path.is_dir() else "file"}
    cloud_url = _shared_cloud_url(config, rel)
    if cloud_url:
        item["cloud_url"] = cloud_url
    if path.is_dir():
        if depth < max_depth:
            children = [
                p
                for p in sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
                if not _is_hidden_shared_item(p)
            ][: _ASSISTANT_RESOURCE_MAX_SHARED_ITEMS]
            item["children"] = [_shared_item(child, root, config, depth + 1, max_depth) for child in children]
    else:
        item["open_url"] = "/api/assistant/shared-folder/open?path=" + urllib.parse.quote(rel)
        item["reference_uri"] = "shared://" + rel
    return item


def _shared_folder_summary(config: Dict[str, Any], request: Request | None = None) -> Dict[str, Any]:
    cloud = _shared_cloud_config(config)
    root = _resolve_shared_folder_root(config)
    if root is not None:
        paths = [
            path
            for path in sorted(root.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
            if not _is_hidden_shared_item(path)
        ][: _ASSISTANT_RESOURCE_MAX_SHARED_ITEMS]
        items = [
            _shared_item(path, root, config, max_depth=_ASSISTANT_RESOURCE_MAX_SHARED_DEPTH)
            for path in paths
        ]
        can_open = bool(_can_open_system_folder() and _remote_open_allowed(request))
        payload = {
            "status": "connected",
            "summary": f"{len(items)} Dateien",
            "items": items,
            "source": "local",
            "can_open_folder": can_open,
            "visible_items": _ASSISTANT_RESOURCE_DEFAULT_VISIBLE_ITEMS,
            "max_depth": _ASSISTANT_RESOURCE_MAX_SHARED_DEPTH,
        }
        cloud_url = _shared_cloud_url(config)
        if cloud_url:
            payload["cloud_url"] = cloud_url
        return payload

    if isinstance(cloud, dict):
        items = _webdav_cloud_items(cloud) if _shared_cloud_uses_webdav(cloud) else _sftpgo_pubshare_items(cloud)
        payload = {
            "status": "connected" if items else "error",
            "summary": f"{len(items)} Dateien" if items else "Cloud-Ordner konnte nicht geprüft werden",
            "items": items,
            "source": "cloud",
            "can_open_folder": False,
            "visible_items": _ASSISTANT_RESOURCE_DEFAULT_VISIBLE_ITEMS,
            "max_depth": _ASSISTANT_RESOURCE_MAX_SHARED_DEPTH,
        }
        cloud_url = _shared_cloud_url(config)
        if cloud_url:
            payload["cloud_url"] = cloud_url
        return payload

    payload = {
        "status": "not_configured",
        "summary": "Nicht eingerichtet",
        "items": [],
        "source": "none",
        "can_open_folder": False,
        "visible_items": _ASSISTANT_RESOURCE_DEFAULT_VISIBLE_ITEMS,
        "max_depth": _ASSISTANT_RESOURCE_MAX_SHARED_DEPTH,
    }
    cloud_url = _shared_cloud_url(config)
    if cloud_url:
        payload["cloud_url"] = cloud_url
    return payload


def _content_type_for_shared_file(path: Path) -> tuple[str, str, Dict[str, str]]:
    import mimetypes
    ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    rel = path.as_posix()
    active_ext = {".html", ".htm", ".svg", ".xht", ".xhtm", ".xml"}
    disposition = "inline"
    headers = {"X-Content-Type-Options": "nosniff"}
    if path.suffix.lower() in active_ext and "/Agent-Downloads/" not in rel:
        ctype = "application/octet-stream"
        disposition = "attachment"
    elif path.suffix.lower() in {".html", ".htm"} and "/Agent-Downloads/" in rel:
        headers["Content-Security-Policy"] = "sandbox"
    return ctype, disposition, headers


def open_assistant_shared_folder_root(request: Request) -> Dict[str, Any]:
    _require_token(request)
    root = _shared_folder_root()
    if root is None or not _remote_open_allowed(request):
        raise HTTPException(status_code=409, detail="File manager open is not available")
    return {"ok": bool(_open_system_folder(root))}


@app.post("/api/assistant/shared-folder/open-folder")
def open_assistant_shared_folder_root_alias(request: Request) -> Dict[str, Any]:
    return open_assistant_shared_folder_root(request)


def open_assistant_shared_folder_file(request: Request, payload: Dict[str, Any]) -> Dict[str, Any]:
    _require_token(request)
    return {"ok": True, "path": _redact_sensitive_text(str(payload.get("path") or ""))}


@app.get("/api/assistant/shared-folder/open")
def get_assistant_shared_folder_file(request: Request, path: str) -> Response:
    _require_token(request)
    config = load_config()
    root = _resolve_shared_folder_root(config)
    if root is None:
        clean = _clean_shared_relative_path(path)
        cloud = _shared_cloud_config(config)
        downloaded = None
        if clean and isinstance(cloud, dict):
            downloaded = (
                _download_webdav_cloud_file(cloud, clean)
                if _shared_cloud_uses_webdav(cloud)
                else _download_sftpgo_pubshare_file(cloud, clean)
            )
        if not downloaded:
            raise HTTPException(status_code=404, detail="Not found")
        data, ctype, filename = downloaded
        ctype, disposition = _safe_shared_open_disposition(filename, ctype)
        return Response(
            data,
            media_type=ctype,
            headers={
                "Content-Disposition": f"{disposition}; filename*=UTF-8''{urllib.parse.quote(filename)}",
                "X-Content-Type-Options": "nosniff",
            },
        )
    target = _safe_shared_path(root.resolve(), path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    if target.stat().st_size > _ASSISTANT_SHARED_FILE_OPEN_MAX_BYTES:
        raise HTTPException(status_code=413, detail="File too large")
    ctype, disposition, headers = _content_type_for_shared_file(target)
    headers["Content-Disposition"] = f'{disposition}; filename="{target.name}"'
    return Response(target.read_bytes(), media_type=ctype, headers=headers)


def upload_assistant_attachments(request: Request) -> Dict[str, Any]:
    _require_token(request)
    return {"attachments": []}


def _snapshot_custom_endpoint_env() -> Dict[str, str]:
    return {
        "_dotenv": {key: value for key, value in load_env().items() if key.startswith("HERMES_CUSTOM_")},
        "_process": {key: value for key, value in os.environ.items() if key.startswith("HERMES_CUSTOM_")},
    }


def _restore_custom_endpoint_env(snapshot: Dict[str, str]) -> None:
    from hermes_cli.config import remove_env_value

    dotenv_snapshot = snapshot.get("_dotenv", {}) if isinstance(snapshot.get("_dotenv"), dict) else {}
    process_snapshot = snapshot.get("_process", {}) if isinstance(snapshot.get("_process"), dict) else {}
    current = {key: value for key, value in load_env().items() if key.startswith("HERMES_CUSTOM_")}
    for key in current:
        if key not in dotenv_snapshot:
            remove_env_value(key)
    for key, value in dotenv_snapshot.items():
        save_env_value(key, value)
    for key in list(os.environ):
        if key.startswith("HERMES_CUSTOM_") and key not in process_snapshot:
            os.environ.pop(key, None)
    os.environ.update(process_snapshot)


_ADMIN_API_ACTIONS = (
    ("POST", "/api/credentials/pool", "security.policy_weaken"),
    ("DELETE", "/api/credentials/pool", "security.policy_weaken"),
    ("POST", "/api/mcp/servers", "tool.allowlist.change"),
    ("DELETE", "/api/mcp/servers", "tool.allowlist.change"),
    ("PUT", "/api/mcp/servers", "tool.allowlist.change"),
    ("POST", "/api/mcp/catalog/install", "tool.allowlist.change"),
    ("POST", "/api/gateway/restart", "runtime.restart_shared_prod"),
    ("POST", "/api/gateway/start", "runtime.restart_shared_prod"),
    ("POST", "/api/gateway/stop", "runtime.restart_shared_prod"),
    ("POST", "/api/gateway/drain", "runtime.restart_shared_prod"),
    ("POST", "/api/memory/reset", "memory.reset_all"),
    ("POST", "/api/sessions/owner-backfill", "tenant.cross_access"),
    ("POST", "/api/sessions/prune", "tenant.cross_access"),
    ("POST", "/api/ops/security-audit", "security.policy_weaken"),
    ("DELETE", "/api/ops/hooks", "security.policy_weaken"),
    ("POST", "/api/ops/hooks", "security.policy_weaken"),
    ("POST", "/api/webhooks", "security.policy_weaken"),
    ("DELETE", "/api/webhooks", "security.policy_weaken"),
    ("POST", "/api/pairing/approve", "identity.user_invite"),
    ("POST", "/api/pairing/revoke", "identity.user_remove"),
    ("POST", "/api/providers/custom-endpoints", "mcp.policy.change"),
    ("DELETE", "/api/providers/custom-endpoints", "mcp.policy.change"),
    ("POST", "/api/providers/validate", "mcp.policy.change"),
)


def _admin_api_action_for(method_or_request: Any, path: str | None = None) -> str | None:
    if path is None:
        request = method_or_request
        method = str(getattr(request, "method", "") or "").upper()
        path = str(getattr(getattr(request, "url", None), "path", "") or "")
    else:
        method = str(method_or_request or "").upper()
    for action_method, prefix, action in _ADMIN_API_ACTIONS:
        if method == action_method and (path == prefix or path.startswith(prefix.rstrip("/") + "/")):
            return action
    return None


def _enforce_admin_api_permission(request: Request) -> Optional[JSONResponse]:
    action = _admin_api_action_for(request)
    if action is None:
        return None
    session = getattr(getattr(request, "state", None), "session", None)
    if session is None:
        return None
    from hermes_cli.dashboard_auth.permissions import decide_dashboard_permission

    decision = decide_dashboard_permission(action, session=session, scope="own_tenant")
    if decision.allowed:
        return None
    return JSONResponse(
        status_code=403,
        content={"detail": "admin_required"},
    )


def request_gate(request: Any) -> str | None:
    return _assistant_ws_request_gate(request, _current_http_cui_actor.get())


def get_profiles_sessions_sidebar_compat(*_args: Any, **_kwargs: Any) -> Dict[str, Any]:
    return {"sessions": []}


def _open_session_db_for_profile(profile: Optional[str], *, read_only: bool):
    from hermes_cli.web_server_sessions import _open_session_db_for_profile as _open
    return _open(profile, read_only=read_only)


def _open_session_db_at_path(path: Path, *, read_only: bool):
    from hermes_cli.web_server_sessions import _open_session_db_at_path as _open
    return _open(path, read_only=read_only)


def _maybe_auto_archive_for_profile(profile: Optional[str]) -> None:
    from hermes_cli.web_server_sessions import _maybe_auto_archive_for_profile as _archive
    _archive(profile)


def _strip_session_list_rows(rows: list[Dict[str, Any]]) -> None:
    from hermes_cli.web_server_gateway import _strip_session_list_rows as _strip
    _strip(rows)


from hermes_cli.config import save_config as save_config  # noqa: E402,F401
from hermes_cli.config import save_env_value as save_env_value  # noqa: E402,F401


_WAVE1_RESTORED_ROUTE_PATHS = {
    "/api/assistant/resources",
    "/api/assistant/attachments/resource",
    "/api/cui/contacts/search",
    "/api/cui/context/contacts",
    "/api/cui/contacts/frequent",
    "/api/cui/contacts",
    "/api/cui/contacts/hide",
    "/api/assistant/support",
    "/api/assistant/todos/add",
    "/api/assistant/todos/update",
    "/api/assistant/todos/edit",
    "/api/assistant/email/view",
    "/api/assistant/calendar/view",
    "/api/assistant/shared-folder/open-folder",
    "/api/assistant/shared-folder/open",
    "/api/assistant/attachments",
    "/api/assistant/transcribe",
    "/api/assistant/tts",
    "/api/profiles/sessions/sidebar",
}


def _assert_wave1_restored_routes_registered() -> None:
    registered = {getattr(route, "path", None) for route in app.routes}
    missing = sorted(path for path in _WAVE1_RESTORED_ROUTE_PATHS if path not in registered)
    if missing:
        raise RuntimeError(f"Wave 1 restored routes not registered: {', '.join(missing)}")


_assert_wave1_restored_routes_registered()


mount_spa(app)


def _no_auth_provider_message(host: str) -> str:
    """Actionable SystemExit text for a gated bind with no registered auth provider.

    Names the exact trigger: on a loopback bind the ONLY trigger is
    dashboard.public_url, so print the offending URL and the remove-it exit.
    Bundled providers expose ``LAST_SKIP_REASON`` so an installed-but-
    unconfigured provider is not reported as merely "no providers".
    """
    skip_reasons: list[str] = []
    try:
        from plugins.dashboard_auth import nous as _nous_plugin

        if _nous_plugin.LAST_SKIP_REASON:
            skip_reasons.append(f"  • nous: {_nous_plugin.LAST_SKIP_REASON}")
    except Exception:
        pass

    if host in _LOOPBACK_HOST_VALUES:
        public_url = ""
        try:
            from hermes_cli.dashboard_auth.prefix import resolve_public_url

            public_url = resolve_public_url()
        except Exception:
            pass
        gate_reason = (
            f"dashboard.public_url is set to "
            f"{public_url or '<a non-loopback URL>'} — an "
            f"operator-declared external URL engages the auth gate "
            f"even on a loopback bind"
        )
        fix_hint = (
            "If this dashboard should be LOCAL-ONLY (no reverse "
            "proxy), remove dashboard.public_url from config.yaml "
            "(and unset HERMES_DASHBOARD_PUBLIC_URL) to restore the "
            "unauthenticated loopback mode.\n"
        )
    else:
        gate_reason = f"the auth gate engages on non-loopback binds ({host})"
        fix_hint = ""

    fix_hint += (
        "Configure an auth provider before exposing the dashboard:\n"
        "  • Password: set dashboard.basic_auth.username + "
        "password_hash in config.yaml\n"
        "    (hash with: python -c \"from "
        "plugins.dashboard_auth.basic import hash_password; "
        "print(hash_password('your-password'))\")\n"
        "  • OAuth: run `hermes dashboard register` (Nous Portal) or "
        "install a DashboardAuthProvider plugin.\n"
        "There is no unauthenticated public-dashboard option. For "
        "local-only use, bind 127.0.0.1 and leave dashboard.public_url "
        "unset; a configured external public URL requires auth even "
        "when a local reverse proxy reaches a loopback backend."
    )
    # Credentials exist but the bundled provider is disabled (#54489). Basic
    # auth needs a username AND a credential; a half-configured block is silent.
    try:
        from hermes_cli.config import load_config as _load_cfg
        from hermes_cli.plugins_cmd import _BASIC_AUTH_PLUGIN_KEYS

        cfg = _load_cfg()
        ba = (cfg.get("dashboard") or {}).get("basic_auth") or {}
        disabled = (cfg.get("plugins") or {}).get("disabled") or []
        has_creds = bool(ba.get("username")) and bool(ba.get("password_hash") or ba.get("password"))
        if has_creds and (set(disabled) & _BASIC_AUTH_PLUGIN_KEYS):
            fix_hint = (
                "The 'basic' dashboard-auth plugin is in "
                "plugins.disabled but dashboard.basic_auth is "
                "configured.\n"
                "Remove 'basic' from plugins.disabled (or run "
                "`hermes plugins enable basic`), then restart the "
                "dashboard.\n\n"
            ) + fix_hint
    except Exception:
        pass
    msg = (
        f"Refusing to bind dashboard to {host} — {gate_reason}, "
        f"but no auth providers are registered.\n\n"
    )
    if skip_reasons:
        msg += "Bundled providers reported these issues:\n" + "\n".join(skip_reasons) + "\n\n"
    return msg + fix_hint


def _configure_auth_gate(
    host: str,
    allow_public: bool,
    ssh_session_token: Optional[str],
    ssh_owner_nonce: Optional[str],
) -> None:
    """Resolve the trusted public hosts + auth-gate flag onto ``app.state``.

    Fails closed (``SystemExit`` with an actionable message) when the gate
    engages but no dashboard auth provider is registered.
    """
    # dashboard.public_url is also the exact Host/Origin trust declaration for
    # reverse-proxy deployments; resolved once so middleware never reloads
    # config. A non-loopback public hostname engages the gate even on a loopback
    # backend, else the SPA's local session token becomes remotely reachable.
    app.state.trusted_public_hosts = _dashboard_public_hosts()
    # auth_required drives middleware, SPA-token injection, WS auth, the
    # startup refusal, the gate-on banner and uvicorn proxy_headers.
    if _desktop_loopback_auth_exempt(host, ssh_session_token, ssh_owner_nonce):
        # public_url describes the operator's PUBLIC deployment, not this
        # Desktop-owned loopback backend (#96490), which authenticates with the
        # per-spawn session token the ticket-only gate would refuse.
        app.state.auth_required = should_require_auth(host)
        _log.info(
            "Desktop-owned loopback backend: dashboard.public_url does not "
            "engage the ticket gate for this process; the public deployment "
            "keeps its own gate.",
        )
    else:
        app.state.auth_required = should_require_dashboard_auth(host, app.state.trusted_public_hosts)

    # ``--insecure`` no longer disables the gate (June 2026 hermes-0day
    # hardening); warn that it is a no-op rather than silently ignore it.
    if allow_public and host not in _LOOPBACK_HOST_VALUES:
        _log.warning(
            "--insecure no longer bypasses dashboard authentication. A "
            "non-loopback bind (%s) now ALWAYS requires an auth provider "
            "(OAuth or the bundled password provider). Configure one — see "
            "below — or bind to 127.0.0.1 and reach it over an SSH tunnel / "
            "Tailscale.", host,
        )

    if app.state.auth_required:
        # No escape hatch serves a gated dashboard without a provider.
        from hermes_cli.dashboard_auth import list_providers
        if not list_providers():
            raise SystemExit(_no_auth_provider_message(host))
        _log.info(
            "Dashboard binding to %s with auth gate enabled. Providers: %s",
            host,
            ", ".join(p.name for p in list_providers()),
        )


def _build_uvicorn_server(host: str, port: int, *, ssh_isolated: bool = False):
    """Build the uvicorn ``Config`` + ``Server`` for this bind (reads ``app.state.auth_required``).

    uvicorn.Server is driven directly (not uvicorn.run) so startup is split from
    the main loop: after startup() the socket is bound and held by uvicorn, so the
    OS-assigned port can be read with no pre-bind-then-close TOCTOU. Explicit
    taken ports are caught by the #93608 preflight probe; uvicorn's own bind
    error stays the fallback for races.
    """
    import uvicorn

    # WS keepalive ping runs ON the agent event loop; a GIL-holding worker call
    # can starve it for minutes, so uvicorn misses the pong and drops a healthy
    # local socket (#53773/#48445/#50005). The ping only detects half-open
    # connections (proxy 524, dropped tunnels), impossible on loopback where a
    # dead client sends a real FIN/RST -> WebSocketDisconnect. So: no ping on
    # loopback; non-loopback sits behind a Cloudflare Tunnel (~100s idle) and
    # keeps a config-driven cadence (dashboard.ws_ping_interval/_timeout,
    # #79635) defaulting to 20/20.
    _is_loopback = host in _LOOPBACK_HOST_VALUES
    try:
        _dash_cfg = load_config().get("dashboard") or {}
    except Exception:
        _dash_cfg = {}

    def _ws_ping_setting(key: str, default: float = 20.0) -> float:
        try:
            return float(_dash_cfg.get(key, default))
        except (TypeError, ValueError):
            return default

    # A Desktop-owned SSH-isolated backend is loopback on the SERVER, but the client sits at the far
    # end of a tunnel: the local socket stays healthy while the laptop sleeps, so only a slow WS ping
    # notices the half-open tunnel (#101626). Its client count is tracked at the ASGI boundary so
    # the idle watchdog can retire the backend once nothing is connected.
    served_app = app
    ping_interval, ping_timeout = (None, None) if _is_loopback else (
        _ws_ping_setting("ws_ping_interval"), _ws_ping_setting("ws_ping_timeout"))
    if ssh_isolated:
        from hermes_cli.web_server_idle_exit import (
            TUNNEL_WS_PING_INTERVAL_S, TUNNEL_WS_PING_TIMEOUT_S, IdleClientTracker, wrap_asgi_with_ws_tracking)
        app.state.ssh_isolated_clients = IdleClientTracker()
        served_app = wrap_asgi_with_ws_tracking(app, app.state.ssh_isolated_clients)
        ping_interval, ping_timeout = TUNNEL_WS_PING_INTERVAL_S, TUNNEL_WS_PING_TIMEOUT_S

    config = uvicorn.Config(
        served_app, host=host, port=port, log_level="warning",
        # Off by default so _ws_client_is_allowed sees the real peer, not
        # X-Forwarded-For. Gated mode runs behind a TLS terminator and needs
        # X-Forwarded-Proto for cookie Secure flags.
        proxy_headers=bool(app.state.auth_required),
        # Loopback-only unless the operator trusts a bounded upstream proxy, so
        # spoofed X-Forwarded-* from arbitrary callers is never honoured.
        forwarded_allow_ips=_dashboard_forwarded_allow_ips(_dash_cfg),
        ws_ping_interval=ping_interval,
        ws_ping_timeout=ping_timeout,
        ws_max_size=_DESKTOP_ATTACHMENT_WS_MAX_BYTES,
    )
    return config, uvicorn.Server(config)


def _best_effort(what: str, fn) -> None:
    """Run a best-effort startup step; any failure (import included) is a debug line."""
    try:
        fn()
    except Exception as exc:
        _log.debug("%s skipped: %s", what, exc)


def _on_server_started(
    server,
    *,
    host: str,
    port: int,
    headless: bool,
    open_browser: bool,
    initial_profile: str,
    start_mcp_discovery_after_bind: bool,
) -> None:
    """Post-bind arming on the serving loop right after ``server.startup()``.

    Reap prior corpses, parent-death watchdog, process identity, READY
    announcement, browser open, deferred MCP discovery, loop-noise filter,
    loop heartbeat.
    """
    # Clear corpses from a previous unclean Desktop exit (crash/SIGKILL/update
    # handoff leaves an orphaned backend + its MCP subtree) before stacking a
    # new tree (EMFILE / missing tabs). The watchdog only protects *this*
    # process going forward.
    def _reap_desktop_serves() -> None:
        from hermes_cli.dashboard_procs import _reap_orphaned_desktop_local_serves

        _reap_orphaned_desktop_local_serves()

    def _reap_mcp_helpers() -> None:
        from hermes_cli.process_identity import reap_orphaned_mcp_helpers

        reap_orphaned_mcp_helpers()

    if os.getenv("HERMES_DESKTOP") == "1":
        _best_effort("orphan desktop-local serve reap", _reap_desktop_serves)
    # Same sweep for stdio MCP helpers (#61514): positive identity only (spawn
    # ledger + spawner provably dead); anything alive or unprovable is untouched.
    _best_effort("orphan MCP helper reap", _reap_mcp_helpers)

    # No-op for standalone `hermes serve` (no HERMES_PARENT_PID).
    _start_parent_death_watchdog()
    # SSH-isolated backends are detached from any parent on purpose (#91668); their liveness signal
    # is "does a client still hold a WebSocket" (#101626).
    if getattr(app.state, "ssh_isolated_clients", None) is not None:
        from hermes_cli.web_server_idle_exit import DEFAULT_IDLE_GRACE_S, start_idle_watchdog
        try:
            grace = float((load_config().get("dashboard") or {}).get("ssh_isolated_idle_grace_s", DEFAULT_IDLE_GRACE_S))
        except (TypeError, ValueError):
            grace = DEFAULT_IDLE_GRACE_S
        start_idle_watchdog(server, app.state.ssh_isolated_clients, grace_s=grace)

    actual_port = _read_bound_port(server, fallback=port)
    app.state.bound_port = actual_port

    # Positive process identity in the machine spawn ledger (+ Windows
    # kill-on-close job). Registered AFTER the bind so the entry carries the
    # ACTUAL port — what lets `hermes update` relaunch a manually-started serve
    # on its real endpoint (#63206).
    def _register_identity() -> None:
        from hermes_cli.process_identity import attach_self_to_kill_on_close_job, register_self

        register_self(
            "serve" if headless else "dashboard",
            detail={"host": host, "port": actual_port, "profile": initial_profile or ""},
        )
        attach_self_to_kill_on_close_job()

    _best_effort("process-identity registration", _register_identity)

    _write_dashboard_ready_file(actual_port)
    # Port-discovery sentinel parsed by the Desktop spawn (matches either
    # token). Written to fd 1: tui_gateway.server redirects sys.stdout to
    # stderr at import, and the Desktop watches child.stdout (#96282).
    ready_token = "HERMES_BACKEND_READY" if headless else "HERMES_DASHBOARD_READY"
    _write_machine_sentinel_line(f"{ready_token} port={actual_port}")
    if headless:
        # Auth-gated JSON-RPC/WS only — announce the bind, not a URL. flush:
        # a piped stdout otherwise surfaces this minutes after the sentinel.
        print(f"  Hermes backend listening on {host}:{actual_port}", flush=True)
    else:
        print(f"  Hermes Web UI → http://{host}:{actual_port}")
    _maybe_open_browser(host, actual_port, open_browser, initial_profile)

    if start_mcp_discovery_after_bind:
        # Desktop `serve`: the ~350ms `mcp` SDK import holds the GIL while the
        # renderer does its WS handshake + first hydration reads, so arm it one
        # second later when the shell is painted and idle. An agent build inside
        # that second fires the deferred start itself (wait_for_mcp_discovery).
        try:
            from hermes_cli.mcp_startup import defer_background_mcp_discovery

            defer_background_mcp_discovery(
                logger=_log,
                thread_name="dashboard-mcp-discovery",
                delay=_DESKTOP_MCP_DISCOVERY_DELAY_S,
            )
        except Exception:
            _log.debug("Deferred MCP discovery arm failed", exc_info=True)

    # Collapse the peer-hangup teardown flood (#50005): 50+ identical WinError
    # 10054 tracebacks per Desktop disconnect become one debug line.
    def _install_noise_filter() -> None:
        from tui_gateway.loop_noise import install_loop_noise_filter

        install_loop_noise_filter(asyncio.get_running_loop())

    _best_effort("loop noise filter install", _install_noise_filter)

    # Loop heartbeat watchdog (CF-1): a 2s call_later tick whose drift equals
    # any GIL stall, so a stalled-loop WS drop is diagnosable from the log.
    # call_later (not a task) dies with the loop — nothing to cancel.
    _hb_interval = 2.0
    _hb_stall_threshold = 5.0
    _hb_loop = asyncio.get_running_loop()

    def _loop_heartbeat(expected: float) -> None:
        now = _hb_loop.time()
        drift = now - expected
        if drift > _hb_stall_threshold:
            _log.warning("event loop stalled %.1fs (GIL pressure suspected)", drift)
        _hb_loop.call_later(_hb_interval, _loop_heartbeat, now + _hb_interval)

    _hb_loop.call_later(_hb_interval, _loop_heartbeat, _hb_loop.time() + _hb_interval)


def _run_serve(serve, config, host: str, port: int) -> None:
    """Drive ``serve()`` on the loop uvicorn expects.

    POSIX keeps ``asyncio.run`` (already a SelectorEventLoop / uvloop). On
    Windows ``asyncio.run`` defaults to a ProactorEventLoop, on which uvicorn
    binds a socket that never accepts (#50641), so mirror uvicorn's own runner +
    loop factory there (hand-installed selector policy for uvicorn < 0.36).
    Ctrl+C -> clean return; probe-to-bind port race -> sentinel + exit code.
    """
    runner = asyncio.run
    runner_kwargs: dict = {}
    if sys.platform == "win32":
        # Resolved FIRST; the serve call is outside this try so genuine
        # serve-time errors (port in use) propagate instead of double-running.
        try:
            from uvicorn._compat import asyncio_run as runner

            runner_kwargs = {"loop_factory": config.get_loop_factory()}
        except Exception:
            runner = asyncio.run
            runner_kwargs = {}
            try:
                asyncio.set_event_loop_policy(
                    asyncio.WindowsSelectorEventLoopPolicy()  # type: ignore[attr-defined]
                )
            except Exception:
                pass

    # ``capture_signals()`` re-raises the captured signal after graceful
    # shutdown; console Ctrl+C lands as KeyboardInterrupt = clean exit.
    # (Re-raised SIGTERM/SIGBREAK keep their terminate disposition.)
    try:
        runner(serve(), **runner_kwargs)
    except KeyboardInterrupt:
        return
    except SystemExit as exc:
        # Probe-to-bind race (#93608): uvicorn's bind_socket() exits 1 — re-check
        # and translate a confirmed conflict into the sentinel + distinct code.
        if exc.code == 1 and _port_bind_conflict(host, port):
            _report_port_in_use(host, port)
            raise SystemExit(PORT_IN_USE_EXIT_CODE) from None
        raise


def start_server(
    host: str = "127.0.0.1",
    port: int = 9119,
    open_browser: bool = True,
    allow_public: bool = False,
    initial_profile: str = "",
    headless: bool = False,
    mode: str = "admin",
    ssh_session_token: Optional[str] = None,
    ssh_owner_nonce: Optional[str] = None,
    start_mcp_discovery_after_bind: bool = False,
):
    """Start the web UI server.

    ``initial_profile`` is appended to the auto-opened URL as ``?profile=<name>``
    (profile alias ``<profile> dashboard``). ``headless`` is the ``serve`` path:
    JSON-RPC/WS backend, no UI build, no SPA mount (``HERMES_SERVE_HEADLESS``).
    ``ssh_session_token``/``ssh_owner_nonce`` are process-local Desktop SSH
    bootstrap state, never persisted or exported to children.
    ``start_mcp_discovery_after_bind`` (Desktop ``serve``) defers MCP discovery
    until the ready sentinel is written so its SDK import can't hold the GIL
    against the pre-bind path.
    """
    _set_dashboard_mode(mode)
    _apply_ssh_session_token(ssh_session_token or "")
    _apply_ssh_owner_nonce(ssh_owner_nonce)

    # Dashboard-mode starts don't route through main.py's `serve` path, which
    # applies the same RLIMIT_NOFILE floor (policy in resource_limits, #81547).
    from hermes_cli.resource_limits import apply_nofile_soft_limit

    apply_nofile_soft_limit()

    import uvicorn  # noqa: F401 — fail fast (before any side effects) when the dashboard extra is missing

    try:
        from hermes_cli.nous_auth_keepalive import start_nous_auth_keepalive

        start_nous_auth_keepalive()
    except Exception as exc:
        _log.debug("Nous auth keepalive did not start: %s", exc)

    _configure_auth_gate(host, allow_public, ssh_session_token, ssh_owner_nonce)

    # host_header_middleware validates Host against this (DNS rebinding,
    # GHSA-ppp5-vxwm-4cf7).
    app.state.bound_host = host

    config, server = _build_uvicorn_server(host, port, ssh_isolated=bool(ssh_session_token))

    # Flush-on-kill guard (#94724): chaining SIGTERM/SIGINT handlers persist
    # in-memory transcripts to state.db before shutdown. Installed BEFORE
    # uvicorn's capture_signals() so uvicorn re-raises into them as the
    # "original" handlers — kills outside the serve window are covered too.
    try:
        from tui_gateway.server import install_exit_flush_signal_handlers

        install_exit_flush_signal_handlers()
    except Exception as exc:
        _log.debug("exit-flush signal handlers not installed: %s", exc)

    # #93608: uvicorn's bind_socket() would exit 1 with a bare ERROR line,
    # indistinguishable from "backend broken". Probe first so a conflict
    # surfaces as the BACKEND_PORT_IN_USE sentinel + distinct exit code.
    # ``--port 0`` is skipped by the probe.
    if _port_bind_conflict(host, port):
        _report_port_in_use(host, port)
        raise SystemExit(PORT_IN_USE_EXIT_CODE)

    async def _serve():
        # startup split from main_loop so the bound (ephemeral) port is readable.
        if not config.loaded:
            config.load()
        server.lifespan = config.lifespan_class(config)
        with server.capture_signals():
            await server.startup()
            if server.should_exit:
                return

            _on_server_started(
                server,
                host=host,
                port=port,
                headless=headless,
                open_browser=open_browser,
                initial_profile=initial_profile,
                start_mcp_discovery_after_bind=start_mcp_discovery_after_bind,
            )

            await server.main_loop()
            if server.started:
                await server.shutdown()

    _run_serve(_serve, config, host, port)


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.
from typing import List  # noqa: F401,E402
from typing import Literal  # noqa: F401,E402
import atexit  # noqa: F401,E402
import base64  # noqa: F401,E402
import binascii  # noqa: F401,E402
import concurrent.futures  # noqa: F401,E402
import contextlib  # noqa: F401,E402
from contextlib import contextmanager  # noqa: F401,E402
from dataclasses import dataclass  # noqa: F401,E402
from datetime import datetime  # noqa: F401,E402
import functools  # noqa: F401,E402
import hashlib  # noqa: F401,E402
import importlib.util  # noqa: F401,E402
import inspect  # noqa: F401,E402
import ipaddress  # noqa: F401,E402
import json  # noqa: F401,E402
import math  # noqa: F401,E402
import mimetypes  # noqa: F401,E402
import queue  # noqa: F401,E402
import shlex  # noqa: F401,E402
import shutil  # noqa: F401,E402
import stat  # noqa: F401,E402
import tempfile  # noqa: F401,E402
from datetime import timezone  # noqa: F401,E402
import yaml  # noqa: F401,E402
import zipfile  # noqa: F401,E402


_PLUGIN_COMPAT_LAZY = {
    'AudioTranscriptionRequest': ('hermes_cli.web_models', 'AudioTranscriptionRequest'),
    'AutomationBlueprintInstantiate': ('hermes_cli.web_models', 'AutomationBlueprintInstantiate'),
    'BackupRequest': ('hermes_cli.web_models', 'BackupRequest'),
    'BulkDeleteSessions': ('hermes_cli.web_models', 'BulkDeleteSessions'),
    'CONFIG_SCHEMA': ('hermes_cli.web_server_config', 'CONFIG_SCHEMA'),
    'ChatImageUpload': ('hermes_cli.web_models', 'ChatImageUpload'),
    'ConfigUpdate': ('hermes_cli.web_models', 'ConfigUpdate'),
    'CredentialPoolAdd': ('hermes_cli.web_models', 'CredentialPoolAdd'),
    'CronJobCreate': ('hermes_cli.web_models', 'CronJobCreate'),
    'CronJobUpdate': ('hermes_cli.web_models', 'CronJobUpdate'),
    'CuratorPause': ('hermes_cli.web_models', 'CuratorPause'),
    'CustomEndpointUpdate': ('hermes_cli.web_models', 'CustomEndpointUpdate'),
    'DEFAULT_CONFIG': ('hermes_cli.config', 'DEFAULT_CONFIG'),
    'DebugShareRequest': ('hermes_cli.web_models', 'DebugShareRequest'),
    'EnvVarDelete': ('hermes_cli.web_models', 'EnvVarDelete'),
    'EnvVarReveal': ('hermes_cli.web_models', 'EnvVarReveal'),
    'EnvVarUpdate': ('hermes_cli.web_models', 'EnvVarUpdate'),
    'FontSetBody': ('hermes_cli.web_models', 'FontSetBody'),
    'FsWriteText': ('hermes_cli.web_models', 'FsWriteText'),
    'GitBranchSwitchBody': ('hermes_cli.web_models', 'GitBranchSwitchBody'),
    'GitCommitBody': ('hermes_cli.web_models', 'GitCommitBody'),
    'GitFileBody': ('hermes_cli.web_models', 'GitFileBody'),
    'GitPathBody': ('hermes_cli.web_models', 'GitPathBody'),
    'GitWorktreeAddBody': ('hermes_cli.web_models', 'GitWorktreeAddBody'),
    'GitWorktreeRemoveBody': ('hermes_cli.web_models', 'GitWorktreeRemoveBody'),
    'HookCreate': ('hermes_cli.web_models', 'HookCreate'),
    'HookDelete': ('hermes_cli.web_models', 'HookDelete'),
    'ImportRequest': ('hermes_cli.web_models', 'ImportRequest'),
    'LearningNodeEdit': ('hermes_cli.web_models', 'LearningNodeEdit'),
    'LearningNodeRef': ('hermes_cli.web_models', 'LearningNodeRef'),
    'MCPCatalogInstall': ('hermes_cli.web_models', 'MCPCatalogInstall'),
    'MCPEnabledToggle': ('hermes_cli.web_models', 'MCPEnabledToggle'),
    'MCPServerCreate': ('hermes_cli.web_models', 'MCPServerCreate'),
    'MCPServersReplace': ('hermes_cli.web_models', 'MCPServersReplace'),
    'ManagedDirectoryCreate': ('hermes_cli.web_models', 'ManagedDirectoryCreate'),
    'ManagedFileDelete': ('hermes_cli.web_models', 'ManagedFileDelete'),
    'ManagedFileUpload': ('hermes_cli.web_models', 'ManagedFileUpload'),
    'ManagedFilesPolicy': ('hermes_cli.web_server_files', 'ManagedFilesPolicy'),
    'MemoryProviderConfigUpdate': ('hermes_cli.web_models', 'MemoryProviderConfigUpdate'),
    'MemoryProviderSelect': ('hermes_cli.web_models', 'MemoryProviderSelect'),
    'MemoryProviderSetupRequest': ('hermes_cli.web_models', 'MemoryProviderSetupRequest'),
    'MemoryReset': ('hermes_cli.web_models', 'MemoryReset'),
    'MessagingPlatformUpdate': ('hermes_cli.web_models', 'MessagingPlatformUpdate'),
    'MoaConfigPayload': ('hermes_cli.web_models', 'MoaConfigPayload'),
    'MoaModelSlot': ('hermes_cli.web_models', 'MoaModelSlot'),
    'MoaPresetPayload': ('hermes_cli.web_models', 'MoaPresetPayload'),
    'ModelAssignment': ('hermes_cli.web_models', 'ModelAssignment'),
    'OAuthSubmitBody': ('hermes_cli.web_models', 'OAuthSubmitBody'),
    'OPTIONAL_ENV_VARS': ('hermes_cli.config', 'OPTIONAL_ENV_VARS'),
    'PairingApprove': ('hermes_cli.web_models', 'PairingApprove'),
    'PairingRevoke': ('hermes_cli.web_models', 'PairingRevoke'),
    'ProfileActiveUpdate': ('hermes_cli.web_models', 'ProfileActiveUpdate'),
    'ProfileCreate': ('hermes_cli.web_models', 'ProfileCreate'),
    'ProfileDescribeAuto': ('hermes_cli.web_models', 'ProfileDescribeAuto'),
    'ProfileDescriptionUpdate': ('hermes_cli.web_models', 'ProfileDescriptionUpdate'),
    'ProfileModelUpdate': ('hermes_cli.web_models', 'ProfileModelUpdate'),
    'ProfileRename': ('hermes_cli.web_models', 'ProfileRename'),
    'ProfileSoulUpdate': ('hermes_cli.web_models', 'ProfileSoulUpdate'),
    'ProviderConfigSchema': ('plugins.memory.config_schema', 'ProviderConfigSchema'),
    'ProviderField': ('plugins.memory.config_schema', 'ProviderField'),
    'PtyBridge': ('hermes_cli.pty_bridge', 'PtyBridge'),
    'PtySessionRegistry': ('hermes_cli.pty_session', 'PtySessionRegistry'),
    'PtyUnavailableError': ('hermes_cli.pty_bridge', 'PtyUnavailableError'),
    'RawConfigUpdate': ('hermes_cli.web_models', 'RawConfigUpdate'),
    'RegistryFull': ('hermes_cli.pty_session', 'RegistryFull'),
    'STORAGE_HONCHO_HOST_BLOCK': ('plugins.memory.config_schema', 'STORAGE_HONCHO_HOST_BLOCK'),
    'SessionImport': ('hermes_cli.web_models', 'SessionImport'),
    'SessionPrune': ('hermes_cli.web_models', 'SessionPrune'),
    'SessionRename': ('hermes_cli.web_models', 'SessionRename'),
    'SkillContentUpdate': ('hermes_cli.web_models', 'SkillContentUpdate'),
    'SkillCreate': ('hermes_cli.web_models', 'SkillCreate'),
    'SkillInstallRequest': ('hermes_cli.web_models', 'SkillInstallRequest'),
    'SkillToggle': ('hermes_cli.web_models', 'SkillToggle'),
    'SkillUninstallRequest': ('hermes_cli.web_models', 'SkillUninstallRequest'),
    'SkillsUpdateRequest': ('hermes_cli.web_models', 'SkillsUpdateRequest'),
    'TTSLeaseRequest': ('hermes_cli.web_models', 'TTSLeaseRequest'),
    'TTSSpeakRequest': ('hermes_cli.web_models', 'TTSSpeakRequest'),
    'TelegramOnboardingApply': ('hermes_cli.web_models', 'TelegramOnboardingApply'),
    'TelegramOnboardingStart': ('hermes_cli.web_models', 'TelegramOnboardingStart'),
    'TerminalBackendSelect': ('hermes_cli.web_models', 'TerminalBackendSelect'),
    'ThemeSetBody': ('hermes_cli.web_models', 'ThemeSetBody'),
    'ToolsetEnvUpdate': ('hermes_cli.web_models', 'ToolsetEnvUpdate'),
    'ToolsetModelSelect': ('hermes_cli.web_models', 'ToolsetModelSelect'),
    'ToolsetPostSetup': ('hermes_cli.web_models', 'ToolsetPostSetup'),
    'ToolsetProviderSelect': ('hermes_cli.web_models', 'ToolsetProviderSelect'),
    'ToolsetToggle': ('hermes_cli.web_models', 'ToolsetToggle'),
    'WebhookCreate': ('hermes_cli.web_models', 'WebhookCreate'),
    'WebhookEnabledToggle': ('hermes_cli.web_models', 'WebhookEnabledToggle'),
    'WhatsAppOnboardingApply': ('hermes_cli.web_models', 'WhatsAppOnboardingApply'),
    'WhatsAppOnboardingStart': ('hermes_cli.web_models', 'WhatsAppOnboardingStart'),
    'activate_custom_endpoint': ('hermes_cli.web_routers.config_env', 'activate_custom_endpoint'),
    'add_credential_pool_entry': ('hermes_cli.web_routers.ops', 'add_credential_pool_entry'),
    'add_mcp_server': ('hermes_cli.web_routers.mcp', 'add_mcp_server'),
    'apply_telegram_onboarding': ('hermes_cli.web_routers.messaging', 'apply_telegram_onboarding'),
    'apply_whatsapp_onboarding': ('hermes_cli.web_routers.messaging', 'apply_whatsapp_onboarding'),
    'approve_pairing': ('hermes_cli.web_routers.ops', 'approve_pairing'),
    'auth_mcp_server': ('hermes_cli.web_routers.mcp', 'auth_mcp_server'),
    'build_cron_model_impact': ('hermes_cli.config', 'build_cron_model_impact'),
    'bulk_delete_sessions_endpoint': ('hermes_cli.web_routers.sessions', 'bulk_delete_sessions_endpoint'),
    'cancel_oauth_session': ('hermes_cli.web_routers.oauth', 'cancel_oauth_session'),
    'cancel_telegram_onboarding': ('hermes_cli.web_routers.messaging', 'cancel_telegram_onboarding'),
    'cancel_whatsapp_onboarding': ('hermes_cli.web_routers.messaging', 'cancel_whatsapp_onboarding'),
    'cfg_get': ('hermes_cli.config', 'cfg_get'),
    'check_config_version': ('hermes_cli.config', 'check_config_version'),
    'check_hermes_update': ('hermes_cli.web_routers.actions', 'check_hermes_update'),
    'clear_model_endpoint_credentials': ('hermes_cli.config', 'clear_model_endpoint_credentials'),
    'clear_pending_pairing': ('hermes_cli.web_routers.ops', 'clear_pending_pairing'),
    'coerce_provider_id': ('hermes_cli.config', 'coerce_provider_id'),
    'console_ws': ('hermes_cli.web_routers.chat_ws', 'console_ws'),
    'count_empty_sessions_endpoint': ('hermes_cli.web_routers.sessions', 'count_empty_sessions_endpoint'),
    'create_cron_job': ('hermes_cli.web_routers.cron', 'create_cron_job'),
    'create_hook': ('hermes_cli.web_routers.ops', 'create_hook'),
    'create_managed_directory': ('hermes_cli.web_routers.files', 'create_managed_directory'),
    'create_profile_endpoint': ('hermes_cli.web_routers.profiles', 'create_profile_endpoint'),
    'create_skill': ('hermes_cli.web_routers.skills', 'create_skill'),
    'create_webhook': ('hermes_cli.web_routers.ops', 'create_webhook'),
    'cron_fire_webhook': ('hermes_cli.web_routers.cron', 'cron_fire_webhook'),
    'custom_endpoint_key_env': ('hermes_cli.config', 'custom_endpoint_key_env'),
    'delete_agent_plugin': ('hermes_cli.web_routers.dashboard_ui', 'delete_agent_plugin'),
    'delete_cron_job': ('hermes_cli.web_routers.cron', 'delete_cron_job'),
    'delete_custom_endpoint': ('hermes_cli.web_routers.config_env', 'delete_custom_endpoint'),
    'delete_empty_sessions_endpoint': ('hermes_cli.web_routers.sessions', 'delete_empty_sessions_endpoint'),
    'delete_hook': ('hermes_cli.web_routers.ops', 'delete_hook'),
    'delete_learning_node': ('hermes_cli.web_routers.status', 'delete_learning_node'),
    'delete_managed_file': ('hermes_cli.web_routers.files', 'delete_managed_file'),
    'delete_profile_endpoint': ('hermes_cli.web_routers.profiles', 'delete_profile_endpoint'),
    'delete_session_endpoint': ('hermes_cli.web_routers.sessions', 'delete_session_endpoint'),
    'delete_webhook': ('hermes_cli.web_routers.ops', 'delete_webhook'),
    'derive_gateway_busy': ('gateway.status', 'derive_gateway_busy'),
    'derive_gateway_drainable': ('gateway.status', 'derive_gateway_drainable'),
    'describe_profile_auto_endpoint': ('hermes_cli.web_routers.profiles', 'describe_profile_auto_endpoint'),
    'detect_install_method': ('hermes_cli.config', 'detect_install_method'),
    'disconnect_oauth_provider': ('hermes_cli.web_routers.oauth', 'disconnect_oauth_provider'),
    'download_dashboard_backup': ('hermes_cli.web_routers.ops', 'download_dashboard_backup'),
    'download_managed_file': ('hermes_cli.web_routers.files', 'download_managed_file'),
    'enable_webhooks': ('hermes_cli.web_routers.ops', 'enable_webhooks'),
    'env_var_enabled': ('utils', 'env_var_enabled'),
    'events_ws': ('hermes_cli.web_routers.chat_ws', 'events_ws'),
    'export_session_endpoint': ('hermes_cli.web_routers.sessions', 'export_session_endpoint'),
    'find_provider_entry': ('hermes_cli.config', 'find_provider_entry'),
    'format_docker_update_message': ('hermes_cli.config', 'format_docker_update_message'),
    'fs_default_cwd': ('hermes_cli.web_routers.files', 'fs_default_cwd'),
    'fs_download': ('hermes_cli.web_routers.files', 'fs_download'),
    'fs_git_root': ('hermes_cli.web_routers.files', 'fs_git_root'),
    'fs_list': ('hermes_cli.web_routers.files', 'fs_list'),
    'fs_read_data_url': ('hermes_cli.web_routers.files', 'fs_read_data_url'),
    'fs_read_text': ('hermes_cli.web_routers.files', 'fs_read_text'),
    'fs_write_text': ('hermes_cli.web_routers.files', 'fs_write_text'),
    'gateway_drain': ('hermes_cli.web_routers.actions', 'gateway_drain'),
    'gateway_ws': ('hermes_cli.web_routers.chat_ws', 'gateway_ws'),
    'get_action_status': ('hermes_cli.web_routers.actions', 'get_action_status'),
    'get_active_profile_endpoint': ('hermes_cli.web_routers.profiles', 'get_active_profile_endpoint'),
    'get_auxiliary_models': ('hermes_cli.web_routers.models', 'get_auxiliary_models'),
    'get_client_voice_config': ('hermes_cli.web_routers.audio', 'get_client_voice_config'),
    'get_computer_use_status': ('hermes_cli.web_routers.tools', 'get_computer_use_status'),
    'get_config': ('hermes_cli.web_routers.config_env', 'get_config'),
    'get_config_path': ('hermes_cli.config', 'get_config_path'),
    'get_config_raw': ('hermes_cli.web_routers.analytics', 'get_config_raw'),
    'get_cron_delivery_targets': ('hermes_cli.web_routers.cron', 'get_cron_delivery_targets'),
    'get_cron_job': ('hermes_cli.web_routers.cron', 'get_cron_job'),
    'get_curator_status': ('hermes_cli.web_routers.status', 'get_curator_status'),
    'get_dashboard_font': ('hermes_cli.web_routers.dashboard_ui', 'get_dashboard_font'),
    'get_dashboard_plugins': ('hermes_cli.web_routers.dashboard_ui', 'get_dashboard_plugins'),
    'get_dashboard_themes': ('hermes_cli.web_routers.dashboard_ui', 'get_dashboard_themes'),
    'get_defaults': ('hermes_cli.web_routers.config_env', 'get_defaults'),
    'get_egress_status': ('hermes_cli.web_routers.config_env', 'get_egress_status'),
    'get_elevenlabs_voices': ('hermes_cli.web_routers.audio', 'get_elevenlabs_voices'),
    'get_env_path': ('hermes_cli.config', 'get_env_path'),
    'get_env_vars': ('hermes_cli.web_routers.config_env', 'get_env_vars'),
    'get_health': ('hermes_cli.web_routers.status', 'get_health'),
    'get_hermes_home': ('hermes_cli.config', 'get_hermes_home'),
    'get_learning_graph': ('hermes_cli.web_routers.status', 'get_learning_graph'),
    'get_learning_node': ('hermes_cli.web_routers.status', 'get_learning_node'),
    'get_logs': ('hermes_cli.web_routers.status', 'get_logs'),
    'get_media': ('hermes_cli.web_routers.files', 'get_media'),
    'get_memory_provider_config': ('hermes_cli.web_routers.memory_providers', 'get_memory_provider_config'),
    'get_memory_status': ('hermes_cli.web_routers.ops', 'get_memory_status'),
    'get_messaging_platforms': ('hermes_cli.web_routers.messaging', 'get_messaging_platforms'),
    'get_moa_models': ('hermes_cli.web_routers.models', 'get_moa_models'),
    'get_model_info': ('hermes_cli.web_routers.models', 'get_model_info'),
    'get_model_options': ('hermes_cli.web_routers.models', 'get_model_options'),
    'get_models_analytics': ('hermes_cli.web_routers.analytics', 'get_models_analytics'),
    'get_plugins_hub': ('hermes_cli.web_routers.dashboard_ui', 'get_plugins_hub'),
    'get_portal_status': ('hermes_cli.web_routers.status', 'get_portal_status'),
    'get_process_hermes_home': ('hermes_cli.config', 'get_process_hermes_home'),
    'get_profile_setup_command': ('hermes_cli.web_routers.profiles', 'get_profile_setup_command'),
    'get_profile_soul': ('hermes_cli.web_routers.profiles', 'get_profile_soul'),
    'get_profiles_sessions': ('hermes_cli.web_routers.profiles', 'get_profiles_sessions'),
    'get_profiles_sessions_sidebar': ('hermes_cli.web_routers.profiles', 'get_profiles_sessions_sidebar'),
    'get_provider_config_schema': ('plugins.memory.config_schema', 'get_provider_config_schema'),
    'get_recommended_default_model': ('hermes_cli.web_routers.models', 'get_recommended_default_model'),
    'get_running_pid': ('gateway.status', 'get_running_pid'),
    'get_running_pid_cached': ('gateway.status', 'get_running_pid_cached'),
    'get_runtime_status_running_pid': ('gateway.status', 'get_runtime_status_running_pid'),
    'get_schema': ('hermes_cli.web_routers.config_env', 'get_schema'),
    'get_session_detail': ('hermes_cli.web_routers.sessions', 'get_session_detail'),
    'get_session_latest_descendant': ('hermes_cli.web_routers.sessions', 'get_session_latest_descendant'),
    'get_session_messages': ('hermes_cli.web_routers.sessions', 'get_session_messages'),
    'get_session_stats': ('hermes_cli.web_routers.sessions', 'get_session_stats'),
    'get_sessions': ('hermes_cli.web_routers.sessions', 'get_sessions'),
    'get_skill_content': ('hermes_cli.web_routers.skills', 'get_skill_content'),
    'get_skills': ('hermes_cli.web_routers.skills', 'get_skills'),
    'get_ssh_ownership': ('hermes_cli.web_routers.status', 'get_ssh_ownership'),
    'get_status': ('hermes_cli.web_routers.status', 'get_status'),
    'get_system_stats': ('hermes_cli.web_routers.status', 'get_system_stats'),
    'get_telegram_onboarding_status': ('hermes_cli.web_routers.messaging', 'get_telegram_onboarding_status'),
    'get_terminal_backends': ('hermes_cli.web_routers.tools', 'get_terminal_backends'),
    'get_toolset_config': ('hermes_cli.web_routers.tools', 'get_toolset_config'),
    'get_toolset_models': ('hermes_cli.web_routers.tools', 'get_toolset_models'),
    'get_toolsets': ('hermes_cli.web_routers.tools', 'get_toolsets'),
    'get_update_receipt': ('hermes_cli.web_routers.actions', 'get_update_receipt'),
    'get_usage_analytics': ('hermes_cli.web_routers.analytics', 'get_usage_analytics'),
    'get_whatsapp_onboarding_status': ('hermes_cli.web_routers.messaging', 'get_whatsapp_onboarding_status'),
    'git_base_branches_route': ('hermes_cli.web_routers.git', 'git_base_branches_route'),
    'git_branch_switch_route': ('hermes_cli.web_routers.git', 'git_branch_switch_route'),
    'git_branches_route': ('hermes_cli.web_routers.git', 'git_branches_route'),
    'git_commit_context_route': ('hermes_cli.web_routers.git', 'git_commit_context_route'),
    'git_commit_route': ('hermes_cli.web_routers.git', 'git_commit_route'),
    'git_create_pr_route': ('hermes_cli.web_routers.git', 'git_create_pr_route'),
    'git_file_diff_route': ('hermes_cli.web_routers.git', 'git_file_diff_route'),
    'git_push_route': ('hermes_cli.web_routers.git', 'git_push_route'),
    'git_rev_parse_route': ('hermes_cli.web_routers.git', 'git_rev_parse_route'),
    'git_revert_route': ('hermes_cli.web_routers.git', 'git_revert_route'),
    'git_review_diff_route': ('hermes_cli.web_routers.git', 'git_review_diff_route'),
    'git_review_list_route': ('hermes_cli.web_routers.git', 'git_review_list_route'),
    'git_ship_info_route': ('hermes_cli.web_routers.git', 'git_ship_info_route'),
    'git_stage_route': ('hermes_cli.web_routers.git', 'git_stage_route'),
    'git_status_route': ('hermes_cli.web_routers.git', 'git_status_route'),
    'git_unstage_route': ('hermes_cli.web_routers.git', 'git_unstage_route'),
    'git_worktree_add_route': ('hermes_cli.web_routers.git', 'git_worktree_add_route'),
    'git_worktree_remove_route': ('hermes_cli.web_routers.git', 'git_worktree_remove_route'),
    'git_worktrees_route': ('hermes_cli.web_routers.git', 'git_worktrees_route'),
    'grant_computer_use_permissions': ('hermes_cli.web_routers.tools', 'grant_computer_use_permissions'),
    'import_sessions_endpoint': ('hermes_cli.web_routers.sessions', 'import_sessions_endpoint'),
    'install_mcp_catalog_entry': ('hermes_cli.web_routers.mcp', 'install_mcp_catalog_entry'),
    'install_skill_hub': ('hermes_cli.web_routers.skills', 'install_skill_hub'),
    'instantiate_blueprint': ('hermes_cli.web_routers.cron', 'instantiate_blueprint'),
    'is_nix_install_method': ('hermes_cli.config', 'is_nix_install_method'),
    'list_checkpoints': ('hermes_cli.web_routers.ops', 'list_checkpoints'),
    'list_credential_pool': ('hermes_cli.web_routers.ops', 'list_credential_pool'),
    'list_cron_blueprints': ('hermes_cli.web_routers.cron', 'list_cron_blueprints'),
    'list_cron_job_runs': ('hermes_cli.web_routers.cron', 'list_cron_job_runs'),
    'list_cron_jobs': ('hermes_cli.web_routers.cron', 'list_cron_jobs'),
    'list_custom_endpoints': ('hermes_cli.web_routers.config_env', 'list_custom_endpoints'),
    'list_hooks': ('hermes_cli.web_routers.ops', 'list_hooks'),
    'list_managed_files': ('hermes_cli.web_routers.files', 'list_managed_files'),
    'list_mcp_catalog': ('hermes_cli.web_routers.mcp', 'list_mcp_catalog'),
    'list_mcp_servers': ('hermes_cli.web_routers.mcp', 'list_mcp_servers'),
    'list_oauth_providers': ('hermes_cli.web_routers.oauth', 'list_oauth_providers'),
    'list_pairing': ('hermes_cli.web_routers.ops', 'list_pairing'),
    'list_profiles_endpoint': ('hermes_cli.web_routers.profiles', 'list_profiles_endpoint'),
    'list_skills_hub_sources': ('hermes_cli.web_routers.skills', 'list_skills_hub_sources'),
    'list_webhooks': ('hermes_cli.web_routers.ops', 'list_webhooks'),
    'load_env': ('hermes_cli.config', 'load_env'),
    'mcp_oauth_callback': ('hermes_cli.web_routers.mcp', 'mcp_oauth_callback'),
    'mcp_oauth_flow_status': ('hermes_cli.web_routers.mcp', 'mcp_oauth_flow_status'),
    'normalize_updated_at': ('gateway.status', 'normalize_updated_at'),
    'open_profile_terminal_endpoint': ('hermes_cli.web_routers.profiles', 'open_profile_terminal_endpoint'),
    'parse_active_agents': ('gateway.status', 'parse_active_agents'),
    'pause_cron_job': ('hermes_cli.web_routers.cron', 'pause_cron_job'),
    'poll_oauth_session': ('hermes_cli.web_routers.oauth', 'poll_oauth_session'),
    'post_agent_plugin_disable': ('hermes_cli.web_routers.dashboard_ui', 'post_agent_plugin_disable'),
    'post_agent_plugin_enable': ('hermes_cli.web_routers.dashboard_ui', 'post_agent_plugin_enable'),
    'post_agent_plugin_install': ('hermes_cli.web_routers.dashboard_ui', 'post_agent_plugin_install'),
    'post_agent_plugin_update': ('hermes_cli.web_routers.dashboard_ui', 'post_agent_plugin_update'),
    'post_plugin_visibility': ('hermes_cli.web_routers.dashboard_ui', 'post_plugin_visibility'),
    'preview_skill_hub': ('hermes_cli.web_routers.skills', 'preview_skill_hub'),
    'prune_checkpoints': ('hermes_cli.web_routers.ops', 'prune_checkpoints'),
    'prune_sessions_endpoint': ('hermes_cli.web_routers.sessions', 'prune_sessions_endpoint'),
    'pty_ws': ('hermes_cli.web_routers.chat_ws', 'pty_ws'),
    'pub_ws': ('hermes_cli.web_routers.chat_ws', 'pub_ws'),
    'put_plugin_providers': ('hermes_cli.web_routers.dashboard_ui', 'put_plugin_providers'),
    'read_managed_file': ('hermes_cli.web_routers.files', 'read_managed_file'),
    'read_raw_config': ('hermes_cli.config', 'read_raw_config'),
    'read_runtime_status': ('gateway.status', 'read_runtime_status'),
    'recommended_update_command_for_method': ('hermes_cli.config', 'recommended_update_command_for_method'),
    'redact_key': ('hermes_cli.config', 'redact_key'),
    'remove_credential_pool_entry': ('hermes_cli.web_routers.ops', 'remove_credential_pool_entry'),
    'remove_env_value': ('hermes_cli.config', 'remove_env_value'),
    'remove_env_var': ('hermes_cli.web_routers.config_env', 'remove_env_var'),
    'remove_mcp_server': ('hermes_cli.web_routers.mcp', 'remove_mcp_server'),
    'rename_profile_endpoint': ('hermes_cli.web_routers.profiles', 'rename_profile_endpoint'),
    'rename_session_endpoint': ('hermes_cli.web_routers.sessions', 'rename_session_endpoint'),
    'replace_mcp_servers': ('hermes_cli.web_routers.mcp', 'replace_mcp_servers'),
    'rescan_dashboard_plugins': ('hermes_cli.web_routers.dashboard_ui', 'rescan_dashboard_plugins'),
    'reset_memory': ('hermes_cli.web_routers.ops', 'reset_memory'),
    'resolve_cron_model_drift_defaults': ('hermes_cli.config', 'resolve_cron_model_drift_defaults'),
    'resolve_gateway_liveness': ('gateway.status', 'resolve_gateway_liveness'),
    'restart_gateway': ('hermes_cli.web_routers.actions', 'restart_gateway'),
    'resume_cron_job': ('hermes_cli.web_routers.cron', 'resume_cron_job'),
    'reveal_env_var': ('hermes_cli.web_routers.config_env', 'reveal_env_var'),
    'revoke_pairing': ('hermes_cli.web_routers.ops', 'revoke_pairing'),
    'run_backup': ('hermes_cli.web_routers.ops', 'run_backup'),
    'run_config_migrate': ('hermes_cli.web_routers.status', 'run_config_migrate'),
    'run_curator': ('hermes_cli.web_routers.status', 'run_curator'),
    'run_debug_share_endpoint': ('hermes_cli.web_routers.status', 'run_debug_share_endpoint'),
    'run_doctor': ('hermes_cli.doctor', 'run_doctor'),
    'run_dump': ('hermes_cli.dump', 'run_dump'),
    'run_import': ('hermes_cli.web_routers.ops', 'run_import'),
    'run_import_upload': ('hermes_cli.web_routers.ops', 'run_import_upload'),
    'run_prompt_size': ('hermes_cli.web_routers.status', 'run_prompt_size'),
    'run_security_audit': ('hermes_cli.web_routers.ops', 'run_security_audit'),
    'run_toolset_post_setup': ('hermes_cli.web_routers.tools', 'run_toolset_post_setup'),
    'save_config': ('hermes_cli.config', 'save_config'),
    'save_env_value': ('hermes_cli.config', 'save_env_value'),
    'save_toolset_env': ('hermes_cli.web_routers.tools', 'save_toolset_env'),
    'scan_skill_hub': ('hermes_cli.web_routers.skills', 'scan_skill_hub'),
    'search_sessions': ('hermes_cli.web_routers.sessions', 'search_sessions'),
    'search_skills_hub': ('hermes_cli.web_routers.skills', 'search_skills_hub'),
    'select_terminal_backend': ('hermes_cli.web_routers.tools', 'select_terminal_backend'),
    'select_toolset_model': ('hermes_cli.web_routers.tools', 'select_toolset_model'),
    'select_toolset_provider': ('hermes_cli.web_routers.tools', 'select_toolset_provider'),
    'serve_plugin_asset': ('hermes_cli.web_routers.dashboard_ui', 'serve_plugin_asset'),
    'set_active_profile_endpoint': ('hermes_cli.web_routers.profiles', 'set_active_profile_endpoint'),
    'set_curator_paused': ('hermes_cli.web_routers.status', 'set_curator_paused'),
    'set_dashboard_font': ('hermes_cli.web_routers.dashboard_ui', 'set_dashboard_font'),
    'set_dashboard_theme': ('hermes_cli.web_routers.dashboard_ui', 'set_dashboard_theme'),
    'set_env_var': ('hermes_cli.web_routers.config_env', 'set_env_var'),
    'set_mcp_server_enabled': ('hermes_cli.web_routers.mcp', 'set_mcp_server_enabled'),
    'set_memory_provider': ('hermes_cli.web_routers.ops', 'set_memory_provider'),
    'set_moa_models': ('hermes_cli.web_routers.models', 'set_moa_models'),
    'set_model_assignment': ('hermes_cli.web_routers.models', 'set_model_assignment'),
    'set_webhook_enabled': ('hermes_cli.web_routers.ops', 'set_webhook_enabled'),
    'setup_memory_provider': ('hermes_cli.web_routers.memory_providers', 'setup_memory_provider'),
    'speak_stream_ws': ('hermes_cli.web_routers.audio', 'speak_stream_ws'),
    'speak_text': ('hermes_cli.web_routers.audio', 'speak_text'),
    'start_gateway': ('hermes_cli.web_routers.ops', 'start_gateway'),
    'start_oauth_login': ('hermes_cli.web_routers.oauth', 'start_oauth_login'),
    'start_telegram_onboarding': ('hermes_cli.web_routers.messaging', 'start_telegram_onboarding'),
    'start_whatsapp_onboarding': ('hermes_cli.web_routers.messaging', 'start_whatsapp_onboarding'),
    'stop_gateway': ('hermes_cli.web_routers.ops', 'stop_gateway'),
    'stream_managed_file': ('hermes_cli.web_routers.files', 'stream_managed_file'),
    'submit_oauth_code': ('hermes_cli.web_routers.oauth', 'submit_oauth_code'),
    'test_mcp_server': ('hermes_cli.web_routers.mcp', 'test_mcp_server'),
    'test_messaging_platform': ('hermes_cli.web_routers.messaging', 'test_messaging_platform'),
    'toggle_skill': ('hermes_cli.web_routers.skills', 'toggle_skill'),
    'toggle_toolset': ('hermes_cli.web_routers.tools', 'toggle_toolset'),
    'transcribe_audio_upload': ('hermes_cli.web_routers.audio', 'transcribe_audio_upload'),
    'trigger_cron_job': ('hermes_cli.web_routers.cron', 'trigger_cron_job'),
    'tts_lease': ('hermes_cli.web_routers.audio', 'tts_lease'),
    'uninstall_skill_hub': ('hermes_cli.web_routers.skills', 'uninstall_skill_hub'),
    'update_config': ('hermes_cli.web_routers.config_env', 'update_config'),
    'update_config_raw': ('hermes_cli.web_routers.analytics', 'update_config_raw'),
    'update_cron_job': ('hermes_cli.web_routers.cron', 'update_cron_job'),
    'update_hermes': ('hermes_cli.web_routers.actions', 'update_hermes'),
    'update_learning_node': ('hermes_cli.web_routers.status', 'update_learning_node'),
    'update_memory_provider_config': ('hermes_cli.web_routers.memory_providers', 'update_memory_provider_config'),
    'update_messaging_platform': ('hermes_cli.web_routers.messaging', 'update_messaging_platform'),
    'update_profile_description_endpoint': ('hermes_cli.web_routers.profiles', 'update_profile_description_endpoint'),
    'update_profile_model_endpoint': ('hermes_cli.web_routers.profiles', 'update_profile_model_endpoint'),
    'update_profile_soul': ('hermes_cli.web_routers.profiles', 'update_profile_soul'),
    'update_skill_content': ('hermes_cli.web_routers.skills', 'update_skill_content'),
    'update_skills_hub': ('hermes_cli.web_routers.skills', 'update_skills_hub'),
    'upload_chat_image': ('hermes_cli.web_routers.files', 'upload_chat_image'),
    'upload_managed_file': ('hermes_cli.web_routers.files', 'upload_managed_file'),
    'upload_managed_file_stream': ('hermes_cli.web_routers.files', 'upload_managed_file_stream'),
    'upsert_custom_endpoint': ('hermes_cli.web_routers.config_env', 'upsert_custom_endpoint'),
    'validate_custom_endpoint': ('hermes_cli.web_routers.config_env', 'validate_custom_endpoint'),
    'validate_provider_credential': ('hermes_cli.web_routers.config_env', 'validate_provider_credential'),
    'windows_detach_flags': ('hermes_cli._subprocess_compat', 'windows_detach_flags'),
    'windows_hide_flags': ('hermes_cli._subprocess_compat', 'windows_hide_flags'),
    '_write_custom_endpoint': ('hermes_cli.web_routers.config_env', '_write_custom_endpoint'),
    'write_platform_config_field': ('hermes_cli.config', 'write_platform_config_field'),
}


def __getattr__(name):  # PEP 562 — lazy so no import cycles
    target = _PLUGIN_COMPAT_LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib
    from hermes_cli.plugin_compat import warn_once
    warn_once(__name__, name, *target)
    return getattr(importlib.import_module(target[0]), target[1])
# ---- END PLUGIN-COMPAT ----
