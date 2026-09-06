#!/usr/bin/env python3
"""Authenticated headless Chrome/CDP cutover smoke for the AIWerk CUI.

Requires ``websocket-client`` and a Netscape-format authenticated cookie jar.
Writes one canonical JSON report and one PNG screenshot. Secrets and query
strings are never written to the report.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import ipaddress
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, TypeVar

ACTIVE_SESSION_STORAGE_KEY = "aiwerk-cui.active-session-id"
LOGIN_BACKGROUND_TOKEN = "#f4f1ec"
EXPECTED_PALETTE = (
    "/back",
    "/compress",
    "/help",
    "/learn",
    "/new",
    "/reload-mcp",
    "/side",
    "/status",
    "/stop",
    "/usage",
)

T = TypeVar("T")


class StepLog:
    """Record named smoke steps in the report and on stderr."""

    def __init__(self, steps: list[dict[str, Any]], *, stderr: Any = None) -> None:
        self.steps = steps
        self.stderr = sys.stderr if stderr is None else stderr

    def run(self, name: str, operation: Callable[[], T]) -> T:
        print(f"[cui-smoke] START {name}", file=self.stderr, flush=True)
        started = time.monotonic()
        try:
            result = operation()
        except Exception as exc:
            elapsed = round(time.monotonic() - started, 3)
            self.steps.append(
                {"name": name, "elapsed_seconds": elapsed, "status": "FAIL"}
            )
            print(
                f"[cui-smoke] FAIL {name} ({elapsed:.3f}s)",
                file=self.stderr,
                flush=True,
            )
            if isinstance(exc, TimeoutError):
                raise TimeoutError(f"{name}: {exc}") from exc
            raise
        elapsed = round(time.monotonic() - started, 3)
        self.steps.append(
            {"name": name, "elapsed_seconds": elapsed, "status": "PASS"}
        )
        print(
            f"[cui-smoke] PASS {name} ({elapsed:.3f}s)",
            file=self.stderr,
            flush=True,
        )
        return result


def completed_response_count(cdp: Any, scope_selector: str) -> int:
    value = cdp.evaluate(
        "(() => { const root=document.querySelector("
        f"{json.dumps(scope_selector)}); return root ? "
        "root.querySelectorAll('button[aria-label=\"Diese Antwort vorlesen\"]').length : 0; })()"
    )
    return int(value or 0)


def turn_completion_expression(scope_selector: str, completed_before: int) -> str:
    """Require a newly completed answer and no scoped running indicator."""
    return (
        "(() => { const root=document.querySelector("
        f"{json.dumps(scope_selector)}); if(!root)return false; "
        "const completed=root.querySelectorAll("
        "'button[aria-label=\"Diese Antwort vorlesen\"]').length; "
        "const running=Boolean(root.querySelector("
        "'[role=\"status\"][aria-label=\"Der Assistent arbeitet an der Antwort\"]')); "
        f"return completed > {completed_before} && !running; }})()"
    )


def _public_netloc(parsed: urllib.parse.SplitResult) -> str:
    host = parsed.hostname
    if not host:
        raise ValueError("URL host is required")
    rendered = f"[{host}]" if ":" in host else host
    return f"{rendered}:{parsed.port}" if parsed.port is not None else rendered


def normalize_base_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("base URL must use http or https")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("base URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("base URL must not contain a query or fragment")
    return urllib.parse.urlunsplit(
        (parsed.scheme, _public_netloc(parsed), parsed.path.rstrip("/"), "", "")
    )


def public_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return "[redacted-url]"
    return urllib.parse.urlunsplit(
        (parsed.scheme, _public_netloc(parsed), parsed.path, "", "")
    )


def public_error(exc: Exception, step: str | None = None) -> dict[str, str]:
    error = {"type": type(exc).__name__, "message": "smoke execution failed"}
    if step:
        error["step"] = step
    return error


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    with contextlib.suppress(ValueError):
        return ipaddress.ip_address(host).is_loopback
    return False


def load_cookie_records(path: Path, base_url: str) -> list[dict[str, Any]]:
    """Parse curl/Netscape cookies without logging their values."""
    parsed = urllib.parse.urlsplit(normalize_base_url(base_url))
    target_host = (parsed.hostname or "").lower().rstrip(".")
    origin = urllib.parse.urlunsplit((parsed.scheme, _public_netloc(parsed), "", "", ""))
    now = int(time.time())
    records: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        http_only = raw.startswith("#HttpOnly_")
        line = raw.removeprefix("#HttpOnly_") if http_only else raw
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 7:
            raise ValueError("invalid Netscape cookie record")
        domain, include_subdomains, cookie_path, secure, expires_raw, name, value = parts
        domain = domain.lstrip(".").lower().rstrip(".")
        include = include_subdomains.upper() == "TRUE"
        if include_subdomains.upper() not in {"TRUE", "FALSE"}:
            raise ValueError("invalid cookie subdomain flag")
        domain_matches = target_host == domain or (
            include and target_host.endswith(f".{domain}")
        )
        if not domain or not domain_matches:
            raise ValueError("cookie domain does not match target host")
        if not cookie_path.startswith("/") or not name:
            raise ValueError("invalid cookie path or name")
        try:
            expires = int(expires_raw)
        except ValueError as exc:
            raise ValueError("invalid cookie expiry") from exc
        if expires < 0:
            raise ValueError("invalid cookie expiry")
        if expires and expires <= now:
            continue
        is_secure = secure.upper() == "TRUE"
        if secure.upper() not in {"TRUE", "FALSE"}:
            raise ValueError("invalid cookie secure flag")
        if is_secure and parsed.scheme != "https" and not _is_loopback(target_host):
            raise ValueError("secure cookie requires HTTPS")
        record: dict[str, Any] = {
            "httpOnly": http_only,
            "name": name,
            "path": cookie_path,
            "secure": is_secure,
            "url": origin,
            "value": value,
        }
        if include:
            record["domain"] = f".{domain}"
        if expires:
            record["expires"] = expires
        records.append(record)
    if not records:
        raise ValueError("cookie jar contains no cookies")
    return records


def client_error_responses(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for event in events:
        status = int(event.get("status", 0))
        if 400 <= status < 500:
            failures.append(
                {
                    "status": status,
                    "url": public_url(str(event.get("url", ""))),
                }
            )
    return failures


class CDP:
    def __init__(self, websocket_url: str) -> None:
        import websocket  # dependency checked only for actual browser runs

        self._ws = websocket.create_connection(websocket_url, timeout=60)
        self._timeout_error = websocket.WebSocketTimeoutException
        self._next_id = 0
        self.events: list[dict[str, Any]] = []

    def close(self) -> None:
        self._ws.close()

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        self._ws.send(json.dumps({"id": request_id, "method": method, "params": params or {}}))
        while True:
            message = json.loads(self._ws.recv())
            if message.get("id") == request_id:
                if "error" in message:
                    raise RuntimeError(f"CDP {method} failed: {message['error'].get('message', 'unknown error')}")
                return message.get("result", {})
            self.events.append(message)

    def evaluate(self, expression: str, *, await_promise: bool = False) -> Any:
        result = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": await_promise,
            },
        )["result"]
        if result.get("subtype") == "error":
            raise RuntimeError(result.get("description", "browser evaluation failed"))
        return result.get("value")

    def wait_for(self, expression: str, timeout: float, label: str) -> Any:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = self.evaluate(expression)
            if value:
                return value
            time.sleep(0.25)
        raise TimeoutError(f"timed out waiting for {label}")

    def drain(self, seconds: float = 0.5) -> None:
        deadline = time.monotonic() + seconds
        self._ws.settimeout(0.1)
        try:
            while time.monotonic() < deadline:
                try:
                    self.events.append(json.loads(self._ws.recv()))
                except self._timeout_error:
                    pass
        finally:
            self._ws.settimeout(60)


def _browser_binary(explicit: str | None) -> str:
    if explicit:
        return explicit
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    raise RuntimeError("Chrome/Chromium not found")


def _json_endpoint(url: str, timeout: float) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.load(response)


def _start_browser(binary: str, profile: Path) -> subprocess.Popen[bytes]:
    profile.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen(
        [
            binary,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--remote-allow-origins=*",
            "--remote-debugging-port=0",
            f"--user-data-dir={profile}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def read_devtools_port(profile: Path) -> int:
    lines = (profile / "DevToolsActivePort").read_text(encoding="utf-8").splitlines()
    if len(lines) != 2 or not lines[0].isdigit() or not lines[1].startswith(
        "/devtools/browser/"
    ):
        raise ValueError("invalid owned DevToolsActivePort marker")
    port = int(lines[0])
    if not 1 <= port <= 65535:
        raise ValueError("invalid owned DevTools port")
    return port


def _wait_debugger(
    profile: Path, browser: subprocess.Popen[bytes], timeout: float
) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if browser.poll() is not None:
            raise RuntimeError("owned Chrome process exited before DevTools became ready")
        try:
            port = read_devtools_port(profile)
            version = _json_endpoint(f"http://127.0.0.1:{port}/json/version", 1)
            marker_path = (profile / "DevToolsActivePort").read_text(
                encoding="utf-8"
            ).splitlines()[1]
            if not str(version.get("webSocketDebuggerUrl", "")).endswith(marker_path):
                raise RuntimeError("DevTools endpoint does not match owned Chrome profile")
            return port
        except Exception:
            time.sleep(0.2)
    raise TimeoutError("Chrome DevTools endpoint did not become ready")


def _new_tab(port: int, url: str) -> dict[str, Any]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/json/new?{urllib.parse.quote(url, safe=':/')}",
        method="PUT",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


def websocket_rpc_result(
    events: list[dict[str, Any]], method_name: str
) -> dict[str, Any] | None:
    """Return the response correlated to a WebSocket JSON-RPC method call."""
    request_methods: dict[tuple[str, str | int], Any] = {}
    poisoned: set[tuple[str, str | int]] = set()
    for event in events:
        event_method = event.get("method")
        if event_method not in {
            "Network.webSocketFrameSent",
            "Network.webSocketFrameReceived",
        }:
            continue
        try:
            socket_id = event["params"]["requestId"]
            payload = json.loads(event["params"]["response"]["payloadData"])
        except (KeyError, TypeError, json.JSONDecodeError):
            continue
        if not isinstance(socket_id, str) or not socket_id or not isinstance(payload, dict):
            continue
        rpc_id = payload.get("id")
        if isinstance(rpc_id, bool) or not isinstance(rpc_id, (str, int)):
            continue
        request_key = (socket_id, rpc_id)
        if event_method == "Network.webSocketFrameSent":
            sent_method = payload.get("method")
            if (
                request_key in poisoned
                or request_key in request_methods
                or not isinstance(sent_method, str)
                or "result" in payload
                or "error" in payload
            ):
                request_methods.pop(request_key, None)
                poisoned.add(request_key)
                continue
            request_methods[request_key] = sent_method
            continue
        if event_method == "Network.webSocketFrameReceived":
            if (
                request_key in poisoned
                or request_key not in request_methods
                or "method" in payload
                or (("result" in payload) == ("error" in payload))
            ):
                request_methods.pop(request_key, None)
                poisoned.add(request_key)
                continue
            sent_method = request_methods.pop(request_key, None)
            if sent_method != method_name:
                continue
            if "error" in payload:
                raise RuntimeError(f"{method_name} RPC failed")
            result = payload.get("result")
            if not isinstance(result, dict):
                raise RuntimeError(f"{method_name} RPC returned an invalid result")
            return result
    return None


def message_complete_result(
    events: list[dict[str, Any]], prompt_text: str
) -> dict[str, Any] | None:
    """Return successful completion on the socket that submitted this prompt."""
    submitted_sockets: set[str] = set()
    for event in events:
        event_method = event.get("method")
        if event_method not in {
            "Network.webSocketFrameSent",
            "Network.webSocketFrameReceived",
        }:
            continue
        try:
            socket_id = event["params"]["requestId"]
            frame = json.loads(event["params"]["response"]["payloadData"])
        except (KeyError, TypeError, json.JSONDecodeError):
            continue
        if not isinstance(socket_id, str) or not socket_id or not isinstance(frame, dict):
            continue
        if event_method == "Network.webSocketFrameSent":
            params = frame.get("params")
            if (
                frame.get("method") == "prompt.submit"
                and isinstance(params, dict)
                and params.get("text") == prompt_text
            ):
                submitted_sockets.add(socket_id)
            continue
        if socket_id not in submitted_sockets or frame.get("method") != "event":
            continue
        params = frame.get("params")
        if not isinstance(params, dict) or params.get("type") != "message.complete":
            continue
        payload = params.get("payload")
        if not isinstance(payload, dict):
            raise RuntimeError("message.complete returned an invalid payload")
        if payload.get("status") == "error":
            raise RuntimeError("message.complete reported an error")
        return payload
    return None


def wait_for_message_complete(
    cdp: Any,
    prompt_text: str,
    *,
    start_index: int,
    timeout: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = message_complete_result(cdp.events[start_index:], prompt_text)
        if result is not None:
            return result
        cdp.drain(min(0.25, max(0.01, deadline - time.monotonic())))
    raise TimeoutError("timed out waiting for successful message.complete")


def wait_for_websocket_rpc(
    cdp: Any,
    method_name: str,
    *,
    start_index: int,
    timeout: float,
) -> dict[str, Any]:
    """Wait for a browser-originated WebSocket RPC and its matching response."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = websocket_rpc_result(cdp.events[start_index:], method_name)
        if result is not None:
            return result
        cdp.drain(min(0.25, max(0.01, deadline - time.monotonic())))
    raise TimeoutError(f"timed out waiting for {method_name} RPC")


def _network_observations(events: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int, int, int]:
    responses: list[dict[str, Any]] = []
    catalog_sent = catalog_received = ws_101 = 0
    catalog_ids: set[Any] = set()
    for event in events:
        method = event.get("method")
        params = event.get("params", {})
        if method == "Network.responseReceived":
            response = params.get("response", {})
            responses.append({"status": response.get("status", 0), "url": response.get("url", "")})
        elif method == "Network.webSocketHandshakeResponseReceived":
            ws_101 += int(params.get("response", {}).get("status") == 101)
        elif method in {"Network.webSocketFrameSent", "Network.webSocketFrameReceived"}:
            try:
                payload = json.loads(params["response"]["payloadData"])
            except (KeyError, TypeError, json.JSONDecodeError):
                continue
            if method.endswith("Sent") and payload.get("method") == "commands.catalog":
                catalog_sent += 1
                catalog_ids.add(payload.get("id"))
            if method.endswith("Received") and payload.get("id") in catalog_ids:
                catalog_received += 1
    return responses, catalog_sent, catalog_received, ws_101


def install_cookies(cdp: Any, records: list[dict[str, Any]]) -> None:
    for cookie in records:
        result = cdp.call("Network.setCookie", cookie)
        if result.get("success") is not True:
            raise RuntimeError("Chrome rejected an authentication cookie")


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _cookie_path_matches(request_path: str, cookie_path: str) -> bool:
    return request_path == cookie_path or (
        request_path.startswith(cookie_path)
        and (cookie_path.endswith("/") or request_path[len(cookie_path) :].startswith("/"))
    )


def wait_for_persisted_marker(
    base_url: str,
    session_id: str,
    marker: str,
    cookies: list[dict[str, Any]],
    timeout: float,
    *,
    opener: Any = None,
    sleep: Any = time.sleep,
) -> None:
    endpoint_path = (
        f"/api/sessions/{urllib.parse.quote(session_id, safe='')}/messages"
    )
    pairs: list[str] = []
    for cookie in cookies:
        name = str(cookie.get("name", ""))
        value = str(cookie.get("value", ""))
        if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name) or re.search(
            r"[\x00-\x20\x7f;]", value
        ):
            raise ValueError("cookie is unsafe for persistence probe")
        cookie_path = str(cookie.get("path", ""))
        if _cookie_path_matches(endpoint_path, cookie_path):
            pairs.append(f"{name}={value}")
    if not pairs:
        raise ValueError("no authentication cookie applies to persistence probe")
    endpoint = (
        f"{base_url}{endpoint_path}?limit=500&order=latest"
    )
    open_request = opener or urllib.request.build_opener(NoRedirectHandler()).open
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        request = urllib.request.Request(endpoint, headers={"Cookie": "; ".join(pairs)})
        try:
            with open_request(request, timeout=min(5, timeout)) as response:
                payload = json.loads(response.read(1_000_001))
            if marker in json.dumps(payload, ensure_ascii=False):
                return
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise RuntimeError("persistence probe HTTP failure") from exc
        sleep(0.25)
    raise TimeoutError("persisted smoke marker did not become visible")


def cleanup(cdp: Any, browser: Any, profile: Path) -> None:
    try:
        if cdp is not None:
            with contextlib.suppress(Exception):
                cdp.close()
        if browser is not None:
            with contextlib.suppress(Exception):
                browser.terminate()
            try:
                browser.wait(timeout=5)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(Exception):
                    browser.kill()
                with contextlib.suppress(Exception):
                    browser.wait(timeout=5)
            except Exception:
                pass
    finally:
        shutil.rmtree(profile, ignore_errors=True)


def customer_bootstrap_checks(
    globals_: dict[str, Any], document_title: str, agent_name: str
) -> dict[str, bool]:
    required = (
        "__AIWERK_CUI_LOCALE__",
        "__HERMES_AGENT_DISPLAY_NAME__",
        "__HERMES_USER_DISPLAY_NAME__",
    )
    return {
        "customer_bootstrap_globals_present": all(
            key in globals_ and globals_[key] is not None for key in required
        ),
        "document_title_contains_agent_name": bool(
            agent_name and agent_name in document_title
        ),
    }


def side_isolation_checks(
    parent_session_id: str,
    session_after_reload: Any,
    main_panel_text: Any,
    side_marker: str,
    side_back_parent_id: Any,
) -> dict[str, bool]:
    """Return fail-closed assertions for the real side-session smoke."""
    return {
        "side_message_absent_after_reload": (
            isinstance(main_panel_text, str)
            and bool(main_panel_text.strip())
            and side_marker not in main_panel_text
        ),
        "side_parent_session_preserved": bool(parent_session_id)
        and session_after_reload == parent_session_id,
        "side_back_returned_parent": bool(parent_session_id)
        and side_back_parent_id == parent_session_id,
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    profile = Path(tempfile.mkdtemp(prefix="aiwerk-cui-smoke-"))
    browser: subprocess.Popen[bytes] | None = None
    cdp: CDP | None = None
    report: dict[str, Any] = {"schema_version": 1, "steps": [], "verdict": "FAIL"}
    step_log = StepLog(report["steps"])
    try:
        base_url = normalize_base_url(args.base_url)
        if args.cdp_port != 0:
            raise ValueError("--cdp-port must be 0; dynamic owned CDP is required")
        browser = step_log.run(
            "start owned Chrome",
            lambda: _start_browser(_browser_binary(args.chrome_binary), profile),
        )
        cdp_port = step_log.run(
            "wait Chrome DevTools ready",
            lambda: _wait_debugger(profile, browser, args.timeout),
        )
        tab = step_log.run(
            "open owned Chrome tab", lambda: _new_tab(cdp_port, "about:blank")
        )
        cdp = step_log.run(
            "connect owned Chrome CDP", lambda: CDP(tab["webSocketDebuggerUrl"])
        )
        step_log.run("enable Page domain", lambda: cdp.call("Page.enable"))
        step_log.run("enable Network domain", lambda: cdp.call("Network.enable"))
        step_log.run("enable Runtime domain", lambda: cdp.call("Runtime.enable"))
        cookie_records = step_log.run(
            "load authentication cookies",
            lambda: load_cookie_records(args.cookie_jar, base_url),
        )
        step_log.run(
            "install authentication cookies",
            lambda: install_cookies(cdp, cookie_records),
        )

        step_log.run(
            "navigate to assistant", lambda: cdp.call("Page.navigate", {"url": base_url})
        )
        step_log.run(
            "wait page load",
            lambda: cdp.wait_for(
                "document.readyState === 'complete'", args.timeout, "page load"
            ),
        )
        step_log.run(
            "wait chat input",
            lambda: cdp.wait_for(
                "document.querySelector('textarea') !== null", args.timeout, "chat input"
            ),
        )
        step_log.run(
            "wait connected active session",
            lambda: cdp.wait_for(
                f"Boolean(localStorage.getItem({json.dumps(ACTIVE_SESSION_STORAGE_KEY)}))",
                args.timeout,
                "connected active session",
            ),
        )

        login_has_palette = step_log.run(
            "fetch login appearance",
            lambda: cdp.evaluate(
                f"fetch({json.dumps(base_url + '/login')}, {{credentials:'include'}})"
                f".then(r => r.text()).then(t => t.toLowerCase().includes('{LOGIN_BACKGROUND_TOKEN}'))",
                await_promise=True,
            ),
        )
        model_info = step_log.run(
            "fetch model info",
            lambda: cdp.evaluate(
                "fetch('/api/model/info',{credentials:'include'}).then(r=>r.json())",
                await_promise=True,
            ),
        )
        agent_name = str(model_info.get("agent_name") or "").strip() if isinstance(model_info, dict) else ""
        if not agent_name:
            raise AssertionError("dashboard.agent_name is empty")
        bootstrap_globals = step_log.run(
            "read customer bootstrap globals",
            lambda: cdp.evaluate(
                "({"
                "__AIWERK_CUI_LOCALE__:window.__AIWERK_CUI_LOCALE__,"
                "__HERMES_AGENT_DISPLAY_NAME__:window.__HERMES_AGENT_DISPLAY_NAME__,"
                "__HERMES_USER_DISPLAY_NAME__:window.__HERMES_USER_DISPLAY_NAME__"
                "})"
            ),
        )
        document_title = str(
            step_log.run("read document title", lambda: cdp.evaluate("document.title"))
            or ""
        )
        bootstrap_checks = customer_bootstrap_checks(
            bootstrap_globals if isinstance(bootstrap_globals, dict) else {},
            document_title,
            agent_name,
        )
        header_name = step_log.run(
            "wait assistant header name",
            lambda: cdp.wait_for(
                "(() => [...document.querySelectorAll('aside strong')].map(e=>e.textContent.trim()).find(Boolean) || '')()",
                args.timeout,
                "assistant header name",
            ),
        )

        session_before = step_log.run(
            "read active session before prompt",
            lambda: cdp.evaluate(
                f"localStorage.getItem({json.dumps(ACTIVE_SESSION_STORAGE_KEY)})"
            ),
        )
        if not session_before:
            raise AssertionError("active session storage key is empty")

        marker = f"AIWerk CUI smoke {int(time.time())}"
        main_scope = ".aiwerk-messages"
        main_responses_before = step_log.run(
            "count completed main responses",
            lambda: completed_response_count(cdp, main_scope),
        )
        main_turn_event_index = len(cdp.events)
        step_log.run(
            "enter main smoke prompt",
            lambda: cdp.evaluate(
                "(() => { const e=document.querySelector('textarea');"
                f"const v={json.dumps(marker)}; const s=Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set;"
                "s.call(e,v); e.dispatchEvent(new Event('input',{bubbles:true})); return true; })()"
            ),
        )
        step_log.run(
            "wait enabled main send button",
            lambda: cdp.wait_for(
                "(() => { const b=[...document.querySelectorAll('button')].find(x=>(x.innerText||'').trim()==='Senden');"
                "if(!b||b.disabled)return false;b.click();return true;})()",
                args.timeout,
                "enabled send button",
            ),
        )
        step_log.run(
            "wait rendered main marker",
            lambda: cdp.wait_for(
                f"document.body.innerText.includes({json.dumps(marker)})",
                args.timeout,
                "rendered smoke marker",
            ),
        )
        step_log.run(
            "wait successful main message.complete",
            lambda: wait_for_message_complete(
                cdp,
                marker,
                start_index=main_turn_event_index,
                timeout=args.timeout,
            ),
        )
        step_log.run(
            "wait main turn complete",
            lambda: cdp.wait_for(
                turn_completion_expression(main_scope, main_responses_before),
                args.timeout,
                "completed main response",
            ),
        )
        step_log.run(
            "wait persisted main marker",
            lambda: wait_for_persisted_marker(
                base_url,
                str(session_before),
                marker,
                cookie_records,
                args.timeout,
            ),
        )
        step_log.run(
            "reload after main prompt",
            lambda: cdp.call("Page.reload", {"ignoreCache": True}),
        )
        step_log.run(
            "wait reloaded chat input",
            lambda: cdp.wait_for(
                "document.querySelector('textarea') !== null",
                args.timeout,
                "reloaded chat input",
            ),
        )
        step_log.run(
            "wait rendered persisted main marker",
            lambda: cdp.wait_for(
                f"document.body.innerText.includes({json.dumps(marker)})",
                args.timeout,
                "persisted marker",
            ),
        )
        session_after = step_log.run(
            "read active session after main reload",
            lambda: cdp.evaluate(
                f"localStorage.getItem({json.dumps(ACTIVE_SESSION_STORAGE_KEY)})"
            ),
        )

        side_marker = f"AIWerk CUI side smoke {time.time_ns()}"
        side_start_event_index = len(cdp.events)
        step_log.run(
            "wait Nebenfrage button",
            lambda: cdp.wait_for(
                "(() => { const b=[...document.querySelectorAll('button')].find("
                "x=>(x.innerText||'').trim()==='Nebenfrage');"
                "if(!b||b.disabled)return false;b.click();return true;})()",
                args.timeout,
                "Nebenfrage button",
            ),
        )
        side_start_result = step_log.run(
            "wait session.side.start RPC",
            lambda: wait_for_websocket_rpc(
                cdp,
                "session.side.start",
                start_index=side_start_event_index,
                timeout=args.timeout,
            ),
        )
        side_session_id = str(side_start_result.get("side_session_id") or "")
        if not side_session_id:
            raise AssertionError("session.side.start returned no side_session_id")
        if side_start_result.get("parent_session_id") not in (None, session_before):
            raise AssertionError("session.side.start returned the wrong parent")
        side_scope = 'aside[aria-label="Nebenunterhaltung"][data-open="true"]'
        step_log.run(
            "wait side conversation input",
            lambda: cdp.wait_for(
                "document.querySelector('aside[aria-label=\"Nebenunterhaltung\"]"
                "[data-open=\"true\"] textarea') !== null",
                args.timeout,
                "side conversation input",
            ),
        )
        side_responses_before = step_log.run(
            "count completed side responses",
            lambda: completed_response_count(cdp, side_scope),
        )
        side_turn_event_index = len(cdp.events)
        step_log.run(
            "enter side smoke prompt",
            lambda: cdp.evaluate(
                "(() => { const e=document.querySelector("
                "'aside[aria-label=\"Nebenunterhaltung\"][data-open=\"true\"] textarea');"
                f"const v={json.dumps(side_marker)}; const s=Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set;"
                "s.call(e,v); e.dispatchEvent(new Event('input',{bubbles:true})); return true; })()"
            ),
        )
        step_log.run(
            "wait enabled side send button",
            lambda: cdp.wait_for(
                "(() => { const d=document.querySelector("
                "'aside[aria-label=\"Nebenunterhaltung\"][data-open=\"true\"]');"
                "const b=d&&[...d.querySelectorAll('button')].find("
                "x=>(x.innerText||'').trim()==='Senden');"
                "if(!b||b.disabled)return false;b.click();return true;})()",
                args.timeout,
                "side send button",
            ),
        )
        step_log.run(
            "wait rendered side marker",
            lambda: cdp.wait_for(
                f"document.body.innerText.includes({json.dumps(side_marker)})",
                args.timeout,
                "rendered side marker",
            ),
        )
        step_log.run(
            "wait successful side message.complete",
            lambda: wait_for_message_complete(
                cdp,
                side_marker,
                start_index=side_turn_event_index,
                timeout=args.timeout,
            ),
        )
        step_log.run(
            "wait side turn complete",
            lambda: cdp.wait_for(
                turn_completion_expression(side_scope, side_responses_before),
                args.timeout,
                "completed side response",
            ),
        )
        step_log.run(
            "wait persisted side marker",
            lambda: wait_for_persisted_marker(
                base_url,
                side_session_id,
                side_marker,
                cookie_records,
                args.timeout,
            ),
        )
        side_back_event_index = len(cdp.events)
        step_log.run(
            "wait Schliessen button",
            lambda: cdp.wait_for(
                "(() => { const d=document.querySelector("
                "'aside[aria-label=\"Nebenunterhaltung\"][data-open=\"true\"]');"
                "const b=d&&[...d.querySelectorAll('header button')].find("
                "x=>(x.innerText||'').trim()==='Schliessen');"
                "if(!b||b.disabled)return false;b.click();return true;})()",
                args.timeout,
                "Schliessen button",
            ),
        )
        side_back_result = step_log.run(
            "wait session.side.back RPC",
            lambda: wait_for_websocket_rpc(
                cdp,
                "session.side.back",
                start_index=side_back_event_index,
                timeout=args.timeout,
            ),
        )
        side_back_parent_id = side_back_result.get("parent_session_id")
        step_log.run(
            "wait closed side conversation",
            lambda: cdp.wait_for(
                "document.querySelector('aside[aria-label=\"Nebenunterhaltung\"]')"
                ".getAttribute('data-open') !== 'true'",
                args.timeout,
                "closed side conversation",
            ),
        )
        step_log.run(
            "reload after side prompt",
            lambda: cdp.call("Page.reload", {"ignoreCache": True}),
        )
        step_log.run(
            "wait post-side reload",
            lambda: cdp.wait_for(
                "document.querySelector('textarea') !== null",
                args.timeout,
                "post-side reload",
            ),
        )
        step_log.run(
            "wait main marker after side reload",
            lambda: cdp.wait_for(
                f"document.body.innerText.includes({json.dumps(marker)})",
                args.timeout,
                "main marker after side reload",
            ),
        )
        session_after_side_reload = step_log.run(
            "read active session after side reload",
            lambda: cdp.evaluate(
                f"localStorage.getItem({json.dumps(ACTIVE_SESSION_STORAGE_KEY)})"
            ),
        )
        main_panel_text = step_log.run(
            "read main panel after side reload",
            lambda: cdp.evaluate(
                "document.querySelector('.aiwerk-messages')?.innerText || ''"
            ),
        )
        side_checks = side_isolation_checks(
            str(session_before),
            session_after_side_reload,
            main_panel_text,
            side_marker,
            side_back_parent_id,
        )

        step_log.run(
            "open command palette",
            lambda: cdp.evaluate(
                "(() => { const e=document.querySelector('textarea');"
                "const s=Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set;"
                "s.call(e,'/'); e.dispatchEvent(new Event('input',{bubbles:true})); return true; })()"
            ),
        )
        step_log.run("wait palette render", lambda: time.sleep(0.5))
        palette = step_log.run(
            "read command palette",
            lambda: cdp.evaluate(
                "(() => [...new Set([...document.querySelectorAll('button')].map(b =>"
                "(b.innerText||'').trim().split(/\\s+/)[0]).filter(x=>x&&x.startsWith('/')))].sort())()"
            ),
        )
        overlay_count = step_log.run(
            "read fullscreen overlays",
            lambda: cdp.evaluate(
                "[...document.querySelectorAll('.fixed.inset-0')].filter(e=>{const s=getComputedStyle(e);"
                "const r=e.getBoundingClientRect();return s.display!=='none'&&s.visibility!=='hidden'&&"
                "r.width>=innerWidth*.9&&r.height>=innerHeight*.9}).length"
            ),
        )
        screenshot = step_log.run(
            "capture smoke screenshot",
            lambda: cdp.call(
                "Page.captureScreenshot",
                {"format": "png", "captureBeyondViewport": True},
            ),
        )
        args.screenshot.parent.mkdir(parents=True, exist_ok=True)
        args.screenshot.write_bytes(base64.b64decode(screenshot["data"]))
        step_log.run("wait final network drain", cdp.drain)
        responses, catalog_sent, catalog_received, ws_101 = _network_observations(cdp.events)
        errors_4xx = client_error_responses(responses)

        checks = {
            "active_session_preserved": session_before == session_after,
            "agent_header_matches_status": header_name == agent_name,
            **bootstrap_checks,
            **side_checks,
            "document_title": document_title,
            "catalog_request_count": catalog_sent,
            "catalog_response_count": catalog_received,
            "client_error_count": len(errors_4xx),
            "connected": True,
            "fullscreen_overlay_count": overlay_count,
            "login_background_present": bool(login_has_palette),
            "message_visible_after_reload": True,
            "palette": palette,
            "palette_matches_supported": palette == list(EXPECTED_PALETTE),
            "websocket_101_count": ws_101,
        }
        failures = [
            name
            for name, passed in {
                "active_session_preserved": checks["active_session_preserved"],
                "agent_header_matches_status": checks["agent_header_matches_status"],
                "customer_bootstrap_globals_present": checks[
                    "customer_bootstrap_globals_present"
                ],
                "document_title_contains_agent_name": checks[
                    "document_title_contains_agent_name"
                ],
                "side_message_absent_after_reload": checks[
                    "side_message_absent_after_reload"
                ],
                "side_parent_session_preserved": checks[
                    "side_parent_session_preserved"
                ],
                "side_back_returned_parent": checks[
                    "side_back_returned_parent"
                ],
                "catalog_once_per_connection": (
                    ws_101 >= 1 and catalog_sent == ws_101 and catalog_received == ws_101
                ),
                "no_4xx": not errors_4xx,
                "no_fullscreen_overlay": overlay_count == 0,
                "login_background_present": checks["login_background_present"],
                "palette_matches_supported": checks["palette_matches_supported"],
                "websocket_upgraded": ws_101 >= 1,
            }.items()
            if not passed
        ]
        report.update(
            {
                "base_url": base_url,
                "checks": checks,
                "client_errors": errors_4xx,
                "failures": failures,
                "verdict": "PASS" if not failures else "FAIL",
            }
        )
        return report
    except Exception as exc:
        failed_steps = [
            str(step.get("name") or "")
            for step in report["steps"]
            if step.get("status") == "FAIL"
        ]
        report["error"] = public_error(exc, failed_steps[-1] if failed_steps else None)
        if cdp is not None:
            try:
                screenshot = cdp.call("Page.captureScreenshot", {"format": "png"})
                args.screenshot.parent.mkdir(parents=True, exist_ok=True)
                args.screenshot.write_bytes(base64.b64decode(screenshot["data"]))
            except Exception:
                pass
        return report
    finally:
        cleanup(cdp, browser, profile)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--cookie-jar", type=Path, required=True)
    parser.add_argument("--json-out", type=Path, required=True)
    parser.add_argument("--screenshot", type=Path, required=True)
    parser.add_argument("--chrome-binary")
    parser.add_argument(
        "--cdp-port",
        type=int,
        default=0,
        help="must remain 0; Chrome allocates an owned dynamic debugging port",
    )
    parser.add_argument("--timeout", type=float, default=60.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    report = run(args)
    args.json_out.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    print(f"AIWerk CUI smoke: {report['verdict']}")
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
