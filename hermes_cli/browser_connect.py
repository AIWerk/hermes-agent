"""Shared helpers for attaching Hermes to a local Chromium-family CDP port."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import os
import platform
import shlex
import shutil
import subprocess
import time
from typing import Any
from urllib.parse import urlparse
import urllib.error
import urllib.request

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)


DEFAULT_BROWSER_CDP_PORT = 9222
DEFAULT_BROWSER_CDP_URL = f"http://127.0.0.1:{DEFAULT_BROWSER_CDP_PORT}"
DEFAULT_LOCAL_BROWSER_LAUNCHER_PORT = 18765
DEFAULT_LOCAL_BROWSER_LAUNCHER_URL = f"http://127.0.0.1:{DEFAULT_LOCAL_BROWSER_LAUNCHER_PORT}"

_DARWIN_APPS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
)

_WINDOWS_BROWSER_GROUPS = (
    (("chrome.exe", "chrome"), (("Google", "Chrome", "Application", "chrome.exe"),)),
    (
        ("chromium.exe", "chromium"),
        (("Chromium", "Application", "chrome.exe"), ("Chromium", "Application", "chromium.exe")),
    ),
    (("brave.exe", "brave"), (("BraveSoftware", "Brave-Browser", "Application", "brave.exe"),)),
    (("msedge.exe", "msedge"), (("Microsoft", "Edge", "Application", "msedge.exe"),)),
)

_WINDOWS_BIN_NAMES = tuple(name for names, _ in _WINDOWS_BROWSER_GROUPS for name in names)
_WINDOWS_INSTALL_PARTS = tuple(parts for _, group in _WINDOWS_BROWSER_GROUPS for parts in group)

_LINUX_BROWSER_GROUPS = (
    (
        ("google-chrome", "google-chrome-stable"),
        ("/opt/google/chrome/chrome", "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable"),
    ),
    (
        ("chromium-browser", "chromium"),
        ("/usr/bin/chromium-browser", "/usr/bin/chromium"),
    ),
    (
        ("brave-browser", "brave-browser-stable", "brave"),
        (
            "/usr/bin/brave-browser",
            "/usr/bin/brave-browser-stable",
            "/usr/bin/brave",
            "/snap/bin/brave",
            "/opt/brave.com/brave/brave-browser",
            "/opt/brave.com/brave/brave",
            "/opt/brave-bin/brave",
        ),
    ),
    (
        ("microsoft-edge", "microsoft-edge-stable", "msedge"),
        (
            "/usr/bin/microsoft-edge",
            "/usr/bin/microsoft-edge-stable",
            "/opt/microsoft/msedge/microsoft-edge",
            "/opt/microsoft/msedge/msedge",
        ),
    ),
)

_LINUX_BIN_NAMES = tuple(name for names, _ in _LINUX_BROWSER_GROUPS for name in names)
_LINUX_INSTALL_PATHS = tuple(path for _, paths in _LINUX_BROWSER_GROUPS for path in paths)


@dataclass(frozen=True)
class LocalBrowserLauncherConfig:
    """Config for a user-machine local browser launcher reached over localhost."""

    enabled: bool = False
    launcher_url: str = DEFAULT_LOCAL_BROWSER_LAUNCHER_URL
    cdp_url: str = DEFAULT_BROWSER_CDP_URL
    launcher_port: int = DEFAULT_LOCAL_BROWSER_LAUNCHER_PORT
    cdp_port: int = DEFAULT_BROWSER_CDP_PORT
    ssh_target: str = ""
    ssh_port: int | None = None
    ssh_identity_file: str = ""
    launcher_token: str = ""
    browser_profile_dir: str = ""
    browser_binary: str = ""
    start_url: str = "about:blank"
    cdp_poll_timeout_s: float = 20.0
    validation_error: str = ""

    @property
    def configured(self) -> bool:
        return self.enabled and not self.validation_error

    @property
    def launcher_url_explicit(self) -> bool:
        return self.launcher_url != DEFAULT_LOCAL_BROWSER_LAUNCHER_URL


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _as_int(value: Any, default: int | None = None) -> int | None:
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _is_loopback_host(host: str) -> bool:
    return host.lower().strip("[]") in {"127.0.0.1", "localhost", "::1"}


def _normalize_loopback_url(url: str, default_port: int) -> str:
    raw = (url or "").strip()
    if not raw:
        raw = f"http://127.0.0.1:{default_port}"
    parsed = urlparse(raw if "://" in raw else f"http://{raw}")
    scheme = {"ws": "http", "wss": "https"}.get(parsed.scheme, parsed.scheme or "http")
    if scheme not in {"http", "https"}:
        raise ValueError(f"local browser launcher URLs must use http/https schemes, got: {parsed.scheme}")
    host = parsed.hostname or "127.0.0.1"
    if not _is_loopback_host(host):
        raise ValueError(f"local browser launcher URLs must use loopback hosts, got: {host}")
    port = parsed.port or default_port
    return f"{scheme}://{host}:{port}"


def load_local_browser_launcher_config(config: dict[str, Any] | None = None) -> LocalBrowserLauncherConfig:
    """Parse ``browser.local_launcher`` config without enabling it by default.

    The section is intentionally inert unless ``enabled`` is true in the tenant
    profile. This keeps the existing ``/browser connect`` behavior unchanged
    for normal Hermes installs.
    """
    if config is None:
        try:
            from hermes_cli.config import load_config
            config = load_config()
        except Exception:
            config = {}

    browser = config.get("browser", {}) if isinstance(config, dict) else {}
    if not isinstance(browser, dict):
        browser = {}
    raw = browser.get("local_launcher") or browser.get("local_browser_launcher") or {}
    if not isinstance(raw, dict):
        raw = {}

    launcher_port = _as_int(raw.get("launcher_port"), DEFAULT_LOCAL_BROWSER_LAUNCHER_PORT) or DEFAULT_LOCAL_BROWSER_LAUNCHER_PORT
    cdp_port = _as_int(raw.get("cdp_port"), DEFAULT_BROWSER_CDP_PORT) or DEFAULT_BROWSER_CDP_PORT
    try:
        launcher_url = _normalize_loopback_url(str(raw.get("launcher_url") or ""), launcher_port)
        cdp_url = _normalize_loopback_url(str(raw.get("cdp_url") or ""), cdp_port)
        validation_error = ""
    except ValueError as exc:
        launcher_url = DEFAULT_LOCAL_BROWSER_LAUNCHER_URL
        cdp_url = DEFAULT_BROWSER_CDP_URL
        validation_error = str(exc)
        enabled = False
    ssh_port = _as_int(raw.get("ssh_port"), None)
    enabled = _as_bool(raw.get("enabled"), False) and not validation_error

    return LocalBrowserLauncherConfig(
        enabled=enabled,
        launcher_url=launcher_url,
        cdp_url=cdp_url,
        launcher_port=launcher_port,
        cdp_port=cdp_port,
        ssh_target=str(raw.get("ssh_target") or "").strip(),
        ssh_port=ssh_port,
        ssh_identity_file=str(raw.get("ssh_identity_file") or "").strip(),
        launcher_token=str(raw.get("launcher_token") or "").strip(),
        browser_profile_dir=str(raw.get("browser_profile_dir") or "").strip(),
        browser_binary=str(raw.get("browser_binary") or "").strip(),
        start_url=str(raw.get("start_url") or "about:blank").strip() or "about:blank",
        cdp_poll_timeout_s=max(0.1, _as_float(raw.get("cdp_poll_timeout_s"), 20.0)),
        validation_error=validation_error,
    )


def _launcher_endpoint(base_url: str, action: str) -> str:
    path = {
        "health": "/health",
        "status": "/status",
        "open": "/open",
        "up": "/up",
        "down": "/down",
        "close": "/down",
    }.get(action, f"/{action.lstrip('/')}")
    return base_url.rstrip("/") + path


def call_local_browser_launcher(
    config: LocalBrowserLauncherConfig,
    action: str = "open",
    timeout: float = 10.0,
) -> tuple[bool, str]:
    """Call a configured local browser launcher endpoint.

    Returns ``(ok, detail)``. The launcher is expected to be reachable only on
    localhost, often through a user-initiated reverse SSH tunnel.
    """
    if config.validation_error:
        return False, config.validation_error
    if not config.enabled:
        return False, "local browser launcher is not configured"

    url = _launcher_endpoint(config.launcher_url, action)
    # Present the shared secret so the loopback-bound launcher can reject other
    # local processes. Omit the header (and keep the bare-URL call) when no token
    # is configured, preserving backward compatibility with older launchers.
    request: str | urllib.request.Request = url
    if config.launcher_token:
        request = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {config.launcher_token}"}
        )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            body = resp.read(16384).decode("utf-8", errors="replace")
            status = getattr(resp, "status", 200)
    except urllib.error.URLError as exc:
        return False, f"launcher not reachable at {config.launcher_url}: {exc.reason}"
    except Exception as exc:
        return False, f"launcher request failed at {config.launcher_url}: {exc}"

    if not (200 <= status < 300):
        return False, f"launcher returned HTTP {status}: {body[:500]}"

    try:
        payload = json.loads(body) if body.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    if isinstance(payload, dict):
        result = payload.get("result")
        if payload.get("ok") is False:
            return False, json.dumps(payload, ensure_ascii=False)[:1000]
        if isinstance(result, dict) and result.get("exit_code") not in (None, 0):
            return False, json.dumps(payload, ensure_ascii=False)[:1000]
    return True, body[:1000]


def wait_for_browser_debug_ready(url: str, timeout_s: float = 20.0, interval_s: float = 0.5) -> bool:
    deadline = time.monotonic() + max(0.1, timeout_s)
    while time.monotonic() < deadline:
        if is_browser_debug_ready(url, timeout=min(interval_s, 1.0)):
            return True
        time.sleep(interval_s)
    return is_browser_debug_ready(url, timeout=1.0)


def get_chrome_debug_candidates(system: str) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()

    def add(path: str | None) -> None:
        if not path:
            return
        normalized = os.path.normcase(os.path.normpath(path))
        if normalized in seen or not os.path.isfile(path):
            return
        candidates.append(path)
        seen.add(normalized)

    def add_windows_install_paths(
        bases: tuple[str | None, ...],
        install_groups: tuple[tuple[tuple[str, ...], tuple[tuple[str, ...], ...]], ...],
    ) -> None:
        for _, group in install_groups:
            for base in filter(None, bases):
                for parts in group:
                    add(os.path.join(base, *parts))

    if system == "Darwin":
        for app in _DARWIN_APPS:
            add(app)
        return candidates

    if system == "Windows":
        install_bases = (
            os.environ.get("ProgramFiles"),
            os.environ.get("ProgramFiles(x86)"),
            os.environ.get("LOCALAPPDATA"),
        )
        for names, install_parts in _WINDOWS_BROWSER_GROUPS:
            for name in names:
                add(shutil.which(name))
            for base in filter(None, install_bases):
                for parts in install_parts:
                    add(os.path.join(base, *parts))
        return candidates

    for names, paths in _LINUX_BROWSER_GROUPS:
        for name in names:
            add(shutil.which(name))
        for path in paths:
            add(path)
    add_windows_install_paths(("/mnt/c/Program Files", "/mnt/c/Program Files (x86)"), _WINDOWS_BROWSER_GROUPS)
    return candidates


def chrome_debug_data_dir() -> str:
    return str(get_hermes_home() / "chrome-debug")


def _chrome_debug_args(port: int) -> list[str]:
    return [
        f"--remote-debugging-port={port}",
        f"--user-data-dir={chrome_debug_data_dir()}",
        "--no-first-run",
        "--no-default-browser-check",
    ]


def is_browser_debug_ready(url: str, timeout: float = 1.0) -> bool:
    """Return True when ``url`` exposes a reachable Chrome DevTools endpoint."""
    import socket

    parsed = urlparse(url if "://" in url else f"http://{url}")
    try:
        port = parsed.port or (443 if parsed.scheme in {"https", "wss"} else 80)
    except ValueError:
        return False

    if parsed.scheme in {"ws", "wss"} and parsed.path.startswith("/devtools/browser/"):
        if not parsed.hostname:
            return False
        try:
            with socket.create_connection((parsed.hostname, port), timeout=timeout):
                return True
        except OSError:
            return False

    scheme = {"ws": "http", "wss": "https"}.get(parsed.scheme, parsed.scheme)
    if scheme not in {"http", "https"} or not parsed.netloc:
        return False

    root = f"{scheme}://{parsed.netloc}".rstrip("/")
    for probe in (f"{root}/json/version", f"{root}/json"):
        try:
            with urllib.request.urlopen(probe, timeout=timeout) as resp:
                if 200 <= getattr(resp, "status", 200) < 300:
                    return True
        except Exception:
            continue
    return False


def manual_chrome_debug_command(port: int = DEFAULT_BROWSER_CDP_PORT, system: str | None = None) -> str | None:
    system = system or platform.system()
    candidates = get_chrome_debug_candidates(system)

    if candidates:
        argv = [candidates[0], *_chrome_debug_args(port)]
        return subprocess.list2cmdline(argv) if system == "Windows" else shlex.join(argv)

    if system == "Darwin":
        data_dir = chrome_debug_data_dir()
        return (
            f'open -a "Google Chrome" --args --remote-debugging-port={port} '
            f'--user-data-dir="{data_dir}" --no-first-run --no-default-browser-check'
        )

    return None


def _detach_kwargs(system: str) -> dict:
    if system != "Windows":
        return {"start_new_session": True}
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0
    )
    return {"creationflags": flags} if flags else {}


def _wait_for_browser_debug_ready_or_exit(
    proc: subprocess.Popen,
    port: int,
    timeout: float = 2.0,
    interval: float = 0.1,
) -> str:
    """Classify a launched browser as ready, exited, or still starting.

    We only need to wait long enough to catch the common failure mode where a
    candidate binary exists but exits immediately before exposing the CDP port.
    Slower browsers can still finish starting after this grace window.
    """
    cdp_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        if is_browser_debug_ready(cdp_url, timeout=min(interval, 0.2)):
            return "ready"
        if proc.poll() is not None:
            return "exited"
        time.sleep(interval)

    return "starting"


_LAUNCH_STDERR_LOG = "launch-stderr.log"
_STDERR_TAIL_LIMIT = 2000


@dataclass
class LaunchAttempt:
    """Outcome of one candidate-binary launch attempt."""

    binary: str
    state: str  # "ready" | "starting" | "exited" | "spawn-failed"
    returncode: int | None = None
    stderr_tail: str = ""


@dataclass
class ChromeDebugLaunch:
    """Structured result of ``launch_chrome_debug``.

    ``launched`` mirrors the legacy boolean contract: a launch command was
    executed and the browser is ready or still starting (it does NOT
    guarantee the CDP port ever opens). ``attempts`` carries per-candidate
    diagnostics so callers can explain *why* nothing came up.
    """

    launched: bool = False
    attempts: list[LaunchAttempt] = field(default_factory=list)

    @property
    def hint(self) -> str | None:
        """Best user-facing explanation for a failed/soft launch, if any."""
        for attempt in self.attempts:
            if attempt.state == "exited" and attempt.returncode == 0:
                name = os.path.basename(attempt.binary)
                return (
                    f"{name} exited immediately without opening the debug port — an already-running "
                    f"{name} instance likely absorbed the launch (Chromium's single-instance "
                    "behavior). Close ALL of its processes (including background/tray instances) "
                    "and retry /browser connect."
                )
        for attempt in self.attempts:
            if attempt.state == "exited" and attempt.stderr_tail:
                return (
                    f"{os.path.basename(attempt.binary)} exited before the debug port opened: "
                    f"{attempt.stderr_tail.splitlines()[-1].strip()}"
                )
        return None


def _read_stderr_tail(path: str) -> str:
    try:
        with open(path, "rb") as fh:
            data = fh.read()
        return data[-_STDERR_TAIL_LIMIT:].decode("utf-8", errors="replace").strip()
    except OSError:
        return ""


def launch_chrome_debug(
    port: int = DEFAULT_BROWSER_CDP_PORT, system: str | None = None
) -> ChromeDebugLaunch:
    """Launch a Chromium-family browser with remote debugging, with diagnostics.

    Tries each detected candidate binary in turn. A candidate that exits
    before the CDP port opens (crash, singleton forward to an existing
    instance, bad profile dir) is logged — with exit code and a stderr tail —
    and the next candidate is tried.
    """
    system = system or platform.system()
    result = ChromeDebugLaunch()
    candidates = get_chrome_debug_candidates(system)
    if not candidates:
        logger.info("browser debug launch: no Chromium-family binary found (system=%s)", system)
        return result

    data_dir = chrome_debug_data_dir()
    os.makedirs(data_dir, exist_ok=True)
    stderr_path = os.path.join(data_dir, _LAUNCH_STDERR_LOG)

    for candidate in candidates:
        try:
            with open(stderr_path, "wb") as stderr_file:
                proc = subprocess.Popen(
                    [candidate, *_chrome_debug_args(port)],
                    stdout=subprocess.DEVNULL,
                    stderr=stderr_file,
                    **_detach_kwargs(system),
                )
        except Exception as exc:
            result.attempts.append(LaunchAttempt(binary=candidate, state="spawn-failed"))
            logger.info("browser debug launch: failed to spawn %s: %s", candidate, exc)
            continue

        logger.info(
            "browser debug launch: spawned %s (pid=%s) with --remote-debugging-port=%d",
            candidate,
            getattr(proc, "pid", None),
            port,
        )
        state = _wait_for_browser_debug_ready_or_exit(proc, port)
        attempt = LaunchAttempt(binary=candidate, state=state)
        result.attempts.append(attempt)

        if state != "exited":
            result.launched = True
            return result

        attempt.returncode = getattr(proc, "returncode", None)
        attempt.stderr_tail = _read_stderr_tail(stderr_path)
        logger.warning(
            "browser debug launch: %s exited (code=%s) before port %d opened%s",
            candidate,
            attempt.returncode,
            port,
            f"; stderr tail: {attempt.stderr_tail}" if attempt.stderr_tail else "",
        )

    return result


def try_launch_chrome_debug(port: int = DEFAULT_BROWSER_CDP_PORT, system: str | None = None) -> bool:
    return launch_chrome_debug(port, system).launched
