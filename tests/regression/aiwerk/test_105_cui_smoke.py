"""Regression contract for the repository CUI cutover smoke."""

from __future__ import annotations

import importlib.util
from pathlib import Path


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
            "domain": "example.test",
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
