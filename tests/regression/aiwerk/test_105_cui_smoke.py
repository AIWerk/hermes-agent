"""Regression contract for the repository CUI cutover smoke."""

from __future__ import annotations

import importlib.util
import io
import time
import urllib.error
from email.message import Message
from pathlib import Path
from typing import Any

import pytest


_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "aiwerk" / "cui_smoke.py"
_SPEC = importlib.util.spec_from_file_location("aiwerk_cui_smoke", _SCRIPT)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError("Failed to load scripts/aiwerk/cui_smoke.py")
_smoke = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_smoke)


def test_cookie_jar_keeps_http_only_login_cookie(tmp_path: Path) -> None:
    jar = tmp_path / "cookies.txt"
    jar.write_text(
        "# Netscape HTTP Cookie File\n"
        "#HttpOnly_example.test\tTRUE\t/\tTRUE\t0\tsession\tsecret-value\n",
        encoding="utf-8",
    )

    records = _smoke.load_cookie_records(jar, "https://example.test")

    assert records == [
        {
            "domain": ".example.test",
            "httpOnly": True,
            "name": "session",
            "path": "/",
            "secure": True,
            "url": "https://example.test",
            "value": "secret-value",
        }
    ]


def test_network_summary_counts_only_4xx_and_redacts_queries() -> None:
    events = [
        {"status": 200, "url": "https://example.test/api/status?token=secret"},
        {"status": 404, "url": "https://example.test/api/missing?token=secret"},
        {"status": 500, "url": "https://example.test/api/failure"},
    ]

    failures = _smoke.client_error_responses(events)

    assert failures == [{"status": 404, "url": "https://example.test/api/missing"}]


def test_smoke_contract_pins_the_ten_supported_palette_commands() -> None:
    assert _smoke.EXPECTED_PALETTE == (
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
    assert _smoke.ACTIVE_SESSION_STORAGE_KEY == "aiwerk-cui.active-session-id"
    assert _smoke.LOGIN_BACKGROUND_TOKEN == "#f4f1ec"


def test_dynamic_cdp_port_is_read_only_from_the_owned_profile(tmp_path: Path) -> None:
    marker = tmp_path / "DevToolsActivePort"
    marker.write_text("45123\n/devtools/browser/owned\n", encoding="utf-8")

    assert _smoke.read_devtools_port(tmp_path) == 45123
    assert _smoke._parser().parse_args(
        [
            "--base-url",
            "https://example.test",
            "--cookie-jar",
            "cookies.txt",
            "--json-out",
            "result.json",
            "--screenshot",
            "result.png",
        ]
    ).cdp_port == 0


def test_base_url_and_network_output_strip_credentials_and_queries() -> None:
    with pytest.raises(ValueError, match="credentials"):
        _smoke.normalize_base_url("https://user:secret@example.test/chat?token=secret")

    failures = _smoke.client_error_responses(
        [{"status": 401, "url": "https://user:secret@example.test/api/x?token=secret"}]
    )
    assert failures == [{"status": 401, "url": "https://example.test/api/x"}]
    assert _smoke.public_error(RuntimeError("token=secret")) == {
        "type": "RuntimeError",
        "message": "smoke execution failed",
    }


def test_cookie_scope_expiry_and_host_only_semantics(tmp_path: Path) -> None:
    jar = tmp_path / "cookies.txt"
    future = int(time.time()) + 3600
    jar.write_text(
        "# Netscape HTTP Cookie File\n"
        f"example.test\tFALSE\t/\tTRUE\t{future}\thost\tone\n"
        f".example.test\tTRUE\t/\tTRUE\t{future}\tdomain\ttwo\n"
        "evil.test\tFALSE\t/\tTRUE\t0\tevil\tthree\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="target host"):
        _smoke.load_cookie_records(jar, "https://example.test")

    jar.write_text(
        f"example.test\tFALSE\t/\tTRUE\t{future}\thost\tone\n"
        f".example.test\tTRUE\t/\tTRUE\t{future}\tdomain\ttwo\n",
        encoding="utf-8",
    )
    host_only, domain = _smoke.load_cookie_records(jar, "https://example.test")
    assert "domain" not in host_only
    assert host_only["expires"] == future
    assert domain["domain"] == ".example.test"


def test_cookie_rejection_and_close_failure_do_not_skip_cleanup(tmp_path: Path) -> None:
    class FakeCDP:
        def __init__(self) -> None:
            self.closed = False

        def call(self, _method: str, _cookie: dict) -> dict:
            return {"success": False}

        def close(self) -> None:
            self.closed = True
            raise RuntimeError("close failed")

    class FakeBrowser:
        def __init__(self) -> None:
            self.terminated = False

        def terminate(self) -> None:
            self.terminated = True

        def wait(self, timeout: int) -> None:
            assert timeout == 5

    cdp = FakeCDP()
    browser = FakeBrowser()
    profile = tmp_path / "profile"
    profile.mkdir()
    with pytest.raises(RuntimeError, match="rejected"):
        _smoke.install_cookies(cdp, [{"name": "session", "value": "secret"}])

    _smoke.cleanup(cdp, browser, profile)

    assert cdp.closed
    assert browser.terminated
    assert not profile.exists()


def test_persistence_poll_is_outside_browser_network_and_retries_404() -> None:
    attempts: list[Any] = [
        urllib.error.HTTPError("https://example.test/api/x", 404, "missing", Message(), None),
        io.BytesIO(b'{"messages":[{"content":"marker"}]}'),
    ]
    requests = []

    def opener(request, timeout):
        requests.append((request, timeout))
        result = attempts.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    _smoke.wait_for_persisted_marker(
        "https://example.test",
        "session-id",
        "marker",
        [
            {"name": "session", "value": "secret", "path": "/"},
            {"name": "login_only", "value": "do-not-send", "path": "/login"},
        ],
        1,
        opener=opener,
        sleep=lambda _seconds: None,
    )

    assert len(requests) == 2
    assert requests[0][0].get_header("Cookie") == "session=secret"


def test_persistence_probe_disables_redirects() -> None:
    handler = _smoke.NoRedirectHandler()
    assert (
        handler.redirect_request(
            None,
            None,
            302,
            "Found",
            {},
            "https://evil.test/capture",
        )
        is None
    )
