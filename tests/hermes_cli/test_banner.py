"""Tests for banner toolset name normalization and skin color usage."""

from unittest.mock import patch

from rich.console import Console
import pytest

import hermes_cli.banner as banner
import model_tools
import tools.mcp_tool


def test_cprint_falls_back_to_plain_print_when_prompt_toolkit_has_no_console(capsys):
    with patch(
        "prompt_toolkit.print_formatted_text",
        side_effect=RuntimeError("no console screen buffer"),
    ):
        banner.cprint("fallback text")

    assert capsys.readouterr().out == "fallback text\n"



def test_build_welcome_banner_uses_active_language_for_static_labels(monkeypatch):
    """Startup banner static prose should honor display language."""
    from agent.i18n import reset_language_cache

    monkeypatch.setenv("HERMES_LANGUAGE", "hu")
    reset_language_cache()
    try:
        with (
            patch.object(model_tools, "check_tool_availability", return_value=(["web"], [])),
            patch.object(banner, "get_available_skills", return_value={}),
            patch.object(banner, "get_update_result", return_value=None),
            patch.object(tools.mcp_tool, "get_mcp_status", return_value=[]),
            patch.object(banner, "get_latest_release_tag", return_value=None),
        ):
            console = Console(record=True, force_terminal=False, color_system=None, width=160)
            banner.build_welcome_banner(
                console=console,
                model="anthropic/test-model",
                cwd="/tmp/project",
                session_id="abc123",
                tools=[{"function": {"name": "read_file"}}],
                get_toolset_for_tool=lambda name: "file",
                context_length=128000,
            )

        output = console.export_text()
        assert "Elérhető eszközök" in output
        assert "Elérhető készségek" in output
        assert "Munkamenet: abc123" in output
        assert "128K kontextus" in output
        assert "/help a parancsokhoz" in output
        assert "Available Tools" not in output
    finally:
        reset_language_cache()


@pytest.mark.parametrize("model", ["", "unknown", " Unknown "])
def test_build_welcome_banner_localizes_missing_model_warning(monkeypatch, model):
    """Blank/unknown models must not punch hard-coded English through i18n."""
    from agent.i18n import reset_language_cache

    monkeypatch.setenv("HERMES_LANGUAGE", "hu")
    reset_language_cache()
    try:
        with (
            patch.object(model_tools, "check_tool_availability", return_value=([], [])),
            patch.object(banner, "get_available_skills", return_value={}),
            patch.object(banner, "get_update_result", return_value=None),
            patch.object(tools.mcp_tool, "get_mcp_status", return_value=[]),
            patch.object(banner, "get_latest_release_tag", return_value=None),
        ):
            console = Console(record=True, force_terminal=False, color_system=None, width=160)
            banner.build_welcome_banner(
                console=console,
                model=model,
                cwd="/tmp/project",
                tools=[],
                enabled_toolsets=[],
            )

        output = console.export_text()
        assert "nincs modell beállítva" in output
        assert "futtasd a /model parancsot vagy a hermes setupot" in output
        assert "no model configured" not in output
    finally:
        reset_language_cache()


def test_build_welcome_banner_uses_german_static_labels(monkeypatch):
    """Startup banner static prose should honor German display language."""
    from agent.i18n import reset_language_cache

    monkeypatch.setenv("HERMES_LANGUAGE", "de")
    reset_language_cache()
    try:
        with (
            patch.object(model_tools, "check_tool_availability", return_value=(["web"], [])),
            patch.object(banner, "get_available_skills", return_value={}),
            patch.object(banner, "get_update_result", return_value=None),
            patch.object(tools.mcp_tool, "get_mcp_status", return_value=[]),
            patch.object(banner, "get_latest_release_tag", return_value=None),
        ):
            console = Console(record=True, force_terminal=False, color_system=None, width=160)
            banner.build_welcome_banner(
                console=console,
                model="anthropic/test-model",
                cwd="/tmp/project",
                session_id="abc123",
                tools=[{"function": {"name": "read_file"}}],
                get_toolset_for_tool=lambda name: "file",
                context_length=128000,
            )

        output = console.export_text()
        assert "Verfügbare Werkzeuge" in output
        assert "Verfügbare Skills" in output
        assert "Sitzung: abc123" in output
        assert "128K Kontext" in output
        assert "/help für Befehle" in output
        assert "Available Tools" not in output
    finally:
        reset_language_cache()


def test_build_welcome_banner_title_is_hyperlinked_to_release():
    """Panel title (version label) is wrapped in an OSC-8 hyperlink to the GitHub release."""
    import io
    from unittest.mock import patch as _patch
    import hermes_cli.banner as _banner
    import model_tools as _mt
    import tools.mcp_tool as _mcp





def test_build_welcome_banner_title_falls_back_when_no_tag():
    """Without a resolvable tag, the panel title renders as plain text (no hyperlink escape)."""
    import io
    from unittest.mock import patch as _patch
    import hermes_cli.banner as _banner
    import model_tools as _mt
    import tools.mcp_tool as _mcp

    _banner._latest_release_cache = None
    buf = io.StringIO()
    with (
        _patch.object(_mt, "check_tool_availability", return_value=(["web"], [])),
        _patch.object(_banner, "get_available_skills", return_value={}),
        _patch.object(_banner, "get_update_result", return_value=None),
        _patch.object(_mcp, "get_mcp_status", return_value=[]),
        _patch.object(_banner, "get_latest_release_tag", return_value=None),
    ):
        console = Console(file=buf, force_terminal=True, color_system="truecolor", width=160)
        _banner.build_welcome_banner(
            console=console, model="x", cwd="/tmp",
            session_id="abc123",
            tools=[{"function": {"name": "read_file"}}],
            get_toolset_for_tool=lambda n: "file",
        )

    raw = buf.getvalue()
    assert "Hermes Agent v" in raw, "Version label missing from title"
    assert "\x1b]8;" not in raw, "OSC-8 hyperlink should not be emitted without a tag"






def test_build_welcome_banner_non_moa_unchanged(tmp_path, monkeypatch):
    """A normal provider still renders the bare model slug, no MoA prefix."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    (tmp_path / ".hermes").mkdir()

    with (
        patch.object(model_tools, "check_tool_availability", return_value=([], [])),
        patch.object(banner, "get_available_skills", return_value={}),
        patch.object(banner, "get_update_result", return_value=None),
        patch.object(tools.mcp_tool, "get_mcp_status", return_value=[]),
    ):
        console = Console(record=True, force_terminal=False, color_system=None, width=160)
        banner.build_welcome_banner(
            console=console,
            model="anthropic/claude-opus-4.8",
            cwd="/tmp/project",
            tools=[],
            enabled_toolsets=[],
            provider="openrouter",
        )

    out = console.export_text()
    assert "claude-opus-4.8" in out
    assert "MoA:" not in out
