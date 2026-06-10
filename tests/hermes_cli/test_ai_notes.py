from __future__ import annotations

import json
from datetime import date

import pytest

from hermes_cli.config import DEFAULT_CONFIG
from hermes_cli.ai_notes import publish_html_note, sanitize_slug
from tools.ai_notes_tool import check_ai_notes_requirements, publish_ai_note_tool


def test_default_config_contains_disabled_ai_notes_section():
    assert DEFAULT_CONFIG["ai_notes"] == {
        "enabled": False,
        "agent_name": "",
        "public_base_url": "",
        "publish_root": "",
        "visibility": "disabled",
    }


def test_sanitize_slug_normalizes_to_safe_filename():
    assert sanitize_slug("  Rocky Demo: Umsatz & Termine! ") == "rocky-demo-umsatz-termine"
    assert sanitize_slug("../../etc/passwd") == "etc-passwd"


def test_publish_html_note_writes_dated_html_and_returns_public_url(tmp_path):
    result = publish_html_note(
        html="<html><body><h1>Hallo</h1></body></html>",
        title="Rocky Demo",
        slug="Rocky Demo",
        config={
            "enabled": True,
            "agent_name": "rocky",
            "public_base_url": "https://rocky.ainotes.ch/",
            "publish_root": str(tmp_path / "public"),
            "visibility": "public_static_html",
        },
        today=date(2026, 6, 10),
    )

    assert result["ok"] is True
    assert result["url"] == "https://rocky.ainotes.ch/2026-06-10/rocky-demo.html"
    assert result["path"].endswith("/2026-06-10/rocky-demo.html")
    assert (tmp_path / "public" / "2026-06-10" / "rocky-demo.html").read_text() == "<html><body><h1>Hallo</h1></body></html>"


@pytest.mark.parametrize(
    "html",
    [
        '<a href="file:///home/customer/secret.txt">secret</a>',
        '<img src="http://127.0.0.1:9120/debug.png">',
        '<p>MEDIA:/home/customer/private.png</p>',
        '<script>const token = "' + "sk-123" + 'abcdefghi"</script>',
        '<img src="http://169.254.169.254/latest/meta-data/">',
        '<a href="http://[fd00::1]/internal">internal</a>',
        '<img src="//169.254.169.254/latest/meta-data/">',
        '<a href="//[fd00::1]/internal">internal</a>',
        '<style>body{background:url(//169.254.169.254/x)}</style>',
        '<div style="background:url(//[fd00::1]/x)"></div>',
        '<img srcset="//169.254.169.254/a 1x">',
    ],
)
def test_publish_html_note_rejects_private_paths_and_secret_like_content(tmp_path, html):
    with pytest.raises(ValueError):
        publish_html_note(
            html=html,
            title="bad",
            slug="bad",
            config={
                "enabled": True,
                "public_base_url": "https://rocky.ainotes.ch",
                "publish_root": str(tmp_path / "public"),
            },
            today=date(2026, 6, 10),
        )


def test_publish_html_note_requires_enabled_config(tmp_path):
    with pytest.raises(ValueError, match="disabled"):
        publish_html_note(
            html="<html></html>",
            title="disabled",
            slug="disabled",
            config={"enabled": False, "publish_root": str(tmp_path)},
        )


def test_publish_html_note_rejects_local_public_base_url_without_local_visibility(tmp_path):
    with pytest.raises(ValueError, match="public/global host"):
        publish_html_note(
            html="<html></html>",
            title="local",
            slug="local",
            config={
                "enabled": True,
                "public_base_url": "http://127.0.0.1:18180",
                "publish_root": str(tmp_path),
            },
        )


def test_publish_html_note_allows_local_base_url_for_explicit_local_only_visibility(tmp_path):
    result = publish_html_note(
        html="<html></html>",
        title="local",
        slug="local",
        config={
            "enabled": True,
            "public_base_url": "http://127.0.0.1:18180",
            "publish_root": str(tmp_path),
            "visibility": "local_only_static_html",
        },
        today=date(2026, 6, 10),
    )

    assert result["url"] == "http://127.0.0.1:18180/2026-06-10/local.html"
    assert result["visibility"] == "local_only_static_html"


def test_publish_html_note_requires_publish_root_when_enabled():
    with pytest.raises(ValueError, match="publish_root"):
        publish_html_note(
            html="<html></html>",
            title="missing root",
            slug="missing-root",
            config={
                "enabled": True,
                "public_base_url": "https://rocky.ainotes.ch",
            },
        )


def test_ai_notes_tool_requirements_follow_enabled_config(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "tools.ai_notes_tool.load_config",
        lambda: {"ai_notes": {"enabled": False}},
    )
    assert check_ai_notes_requirements() is False

    monkeypatch.setattr(
        "tools.ai_notes_tool.load_config",
        lambda: {
            "ai_notes": {
                "enabled": True,
                "public_base_url": "https://rocky.ainotes.ch",
                "publish_root": str(tmp_path),
            }
        },
    )
    assert check_ai_notes_requirements() is True


def test_publish_ai_note_tool_returns_json_success(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "tools.ai_notes_tool.load_config",
        lambda: {
            "ai_notes": {
                "enabled": True,
                "public_base_url": "https://rocky.ainotes.ch",
                "publish_root": str(tmp_path / "public"),
            }
        },
    )

    payload = json.loads(
        publish_ai_note_tool(
            html="<html><body>OK</body></html>",
            title="Tool Note",
            slug="tool-note",
            today=date(2026, 6, 10),
        )
    )

    assert payload["ok"] is True
    assert payload["url"] == "https://rocky.ainotes.ch/2026-06-10/tool-note.html"
