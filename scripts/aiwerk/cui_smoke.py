#!/usr/bin/env python3
"""Authenticated headless Chrome/CDP cutover smoke for the AIWerk CUI.

Requires ``websocket-client`` and a Netscape-format authenticated cookie jar.
Writes one canonical JSON report and one PNG screenshot. Secrets and query
strings are never written to the report.
"""

from __future__ import annotations

import argparse
import base64
import json
import shutil
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

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


def load_cookie_records(path: Path, base_url: str) -> list[dict[str, Any]]:
    """Parse curl/Netscape cookies without logging their values."""
    parsed = urllib.parse.urlsplit(base_url)
    records: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        http_only = raw.startswith("#HttpOnly_")
        line = raw.removeprefix("#HttpOnly_") if http_only else raw
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 7:
            raise ValueError("invalid Netscape cookie record")
        domain, _include_subdomains, cookie_path, secure, _expires, name, value = parts
        records.append(
            {
                "domain": domain.lstrip("."),
                "httpOnly": http_only,
                "name": name,
                "path": cookie_path or "/",
                "secure": secure.upper() == "TRUE",
                "url": f"{parsed.scheme}://{parsed.netloc}",
                "value": value,
            }
        )
    if not records:
        raise ValueError("cookie jar contains no cookies")
    return records


def client_error_responses(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for event in events:
        status = int(event.get("status", 0))
        if 400 <= status < 500:
            split = urllib.parse.urlsplit(str(event.get("url", "")))
            failures.append(
                {
                    "status": status,
                    "url": urllib.parse.urlunsplit((split.scheme, split.netloc, split.path, "", "")),
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


def run(args: argparse.Namespace) -> dict[str, Any]:
    profile = Path(tempfile.mkdtemp(prefix="aiwerk-cui-smoke-"))
    browser: subprocess.Popen[bytes] | None = None
    cdp: CDP | None = None
    report: dict[str, Any] = {"schema_version": 1, "verdict": "FAIL"}
    try:
        if args.cdp_port != 0:
            raise ValueError("--cdp-port must be 0; dynamic owned CDP is required")
        browser = _start_browser(_browser_binary(args.chrome_binary), profile)
        cdp_port = _wait_debugger(profile, browser, args.timeout)
        tab = _new_tab(cdp_port, "about:blank")
        cdp = CDP(tab["webSocketDebuggerUrl"])
        cdp.call("Page.enable")
        cdp.call("Network.enable")
        cdp.call("Runtime.enable")
        for cookie in load_cookie_records(args.cookie_jar, args.base_url):
            cdp.call("Network.setCookie", cookie)

        cdp.call("Page.navigate", {"url": args.base_url})
        cdp.wait_for("document.readyState === 'complete'", args.timeout, "page load")
        cdp.wait_for("document.querySelector('textarea') !== null", args.timeout, "chat input")
        cdp.wait_for(
            f"Boolean(localStorage.getItem({json.dumps(ACTIVE_SESSION_STORAGE_KEY)}))",
            args.timeout,
            "connected active session",
        )

        login_has_palette = cdp.evaluate(
            f"fetch({json.dumps(args.base_url.rstrip('/') + '/login')}, {{credentials:'include'}})"
            f".then(r => r.text()).then(t => t.toLowerCase().includes('{LOGIN_BACKGROUND_TOKEN}'))",
            await_promise=True,
        )
        model_info = cdp.evaluate(
            "fetch('/api/model/info',{credentials:'include'}).then(r=>r.json())",
            await_promise=True,
        )
        agent_name = str(model_info.get("agent_name") or "").strip() if isinstance(model_info, dict) else ""
        if not agent_name:
            raise AssertionError("dashboard.agent_name is empty")
        header_name = cdp.wait_for(
            "(() => [...document.querySelectorAll('aside strong')].map(e=>e.textContent.trim()).find(Boolean) || '')()",
            args.timeout,
            "assistant header name",
        )

        session_before = cdp.evaluate(
            f"localStorage.getItem({json.dumps(ACTIVE_SESSION_STORAGE_KEY)})"
        )
        if not session_before:
            raise AssertionError("active session storage key is empty")

        marker = f"AIWerk CUI smoke {int(time.time())}"
        cdp.evaluate(
            "(() => { const e=document.querySelector('textarea');"
            f"const v={json.dumps(marker)}; const s=Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set;"
            "s.call(e,v); e.dispatchEvent(new Event('input',{bubbles:true})); return true; })()"
        )
        cdp.wait_for(
            "(() => { const b=[...document.querySelectorAll('button')].find(x=>(x.innerText||'').trim()==='Senden');"
            "if(!b||b.disabled)return false;b.click();return true;})()",
            args.timeout,
            "enabled send button",
        )
        cdp.wait_for(
            f"document.body.innerText.includes({json.dumps(marker)})",
            args.timeout,
            "rendered smoke marker",
        )
        time.sleep(1.0)
        cdp.call("Page.reload", {"ignoreCache": True})
        cdp.wait_for("document.querySelector('textarea') !== null", args.timeout, "reloaded chat input")
        cdp.wait_for(f"document.body.innerText.includes({json.dumps(marker)})", args.timeout, "persisted marker")
        session_after = cdp.evaluate(
            f"localStorage.getItem({json.dumps(ACTIVE_SESSION_STORAGE_KEY)})"
        )

        cdp.evaluate(
            "(() => { const e=document.querySelector('textarea');"
            "const s=Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set;"
            "s.call(e,'/'); e.dispatchEvent(new Event('input',{bubbles:true})); return true; })()"
        )
        time.sleep(0.5)
        palette = cdp.evaluate(
            "(() => [...new Set([...document.querySelectorAll('button')].map(b =>"
            "(b.innerText||'').trim().split(/\\s+/)[0]).filter(x=>x&&x.startsWith('/')))].sort())()"
        )
        overlay_count = cdp.evaluate(
            "[...document.querySelectorAll('.fixed.inset-0')].filter(e=>{const s=getComputedStyle(e);"
            "const r=e.getBoundingClientRect();return s.display!=='none'&&s.visibility!=='hidden'&&"
            "r.width>=innerWidth*.9&&r.height>=innerHeight*.9}).length"
        )
        screenshot = cdp.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True})
        args.screenshot.parent.mkdir(parents=True, exist_ok=True)
        args.screenshot.write_bytes(base64.b64decode(screenshot["data"]))
        cdp.drain()
        responses, catalog_sent, catalog_received, ws_101 = _network_observations(cdp.events)
        errors_4xx = client_error_responses(responses)

        checks = {
            "active_session_preserved": session_before == session_after,
            "agent_header_matches_status": header_name == agent_name,
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
                "base_url": urllib.parse.urlunsplit(
                    (*urllib.parse.urlsplit(args.base_url)[:3], "", "")
                ),
                "checks": checks,
                "client_errors": errors_4xx,
                "failures": failures,
                "verdict": "PASS" if not failures else "FAIL",
            }
        )
        return report
    except Exception as exc:
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
        if cdp is not None:
            try:
                screenshot = cdp.call("Page.captureScreenshot", {"format": "png"})
                args.screenshot.parent.mkdir(parents=True, exist_ok=True)
                args.screenshot.write_bytes(base64.b64decode(screenshot["data"]))
            except Exception:
                pass
        return report
    finally:
        if cdp is not None:
            cdp.close()
        if browser is not None:
            browser.terminate()
            try:
                browser.wait(timeout=5)
            except subprocess.TimeoutExpired:
                browser.kill()
                browser.wait(timeout=5)
        shutil.rmtree(profile, ignore_errors=True)


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
