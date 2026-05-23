import os
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest

from cli import HermesCLI
from hermes_cli.skin_engine import get_active_skin_name, set_active_skin


@pytest.fixture(autouse=True)
def restore_language_env():
    original = os.environ.get("HERMES_LANGUAGE")
    yield
    if original is None:
        os.environ.pop("HERMES_LANGUAGE", None)
    else:
        os.environ["HERMES_LANGUAGE"] = original


def _make_cli_stub():
    cli = HermesCLI.__new__(HermesCLI)
    cli._tui_style_base = {
        "prompt": "#fff",
        "input-area": "#fff",
        "input-rule": "#aaa",
        "prompt-working": "#888 italic",
    }
    cli._app = SimpleNamespace(style=None)
    cli._invalidate = MagicMock()
    return cli


def test_language_command_sets_language_and_matching_skin(monkeypatch, capsys):
    cli = _make_cli_stub()
    set_active_skin("default")
    monkeypatch.delenv("HERMES_LANGUAGE", raising=False)

    skins = [
        {"name": "default", "description": "English", "source": "builtin"},
        {"name": "default-de", "description": "German", "source": "user"},
    ]

    with patch("hermes_cli.config.load_config", return_value={"display": {"language": "en", "skin": "default"}}), \
         patch("hermes_cli.skin_engine.list_skins", return_value=skins), \
         patch("cli.save_config_value", return_value=True) as save:
        cli._handle_language_command("/language de")

    output = capsys.readouterr().out
    assert "Language set to: de (saved)" in output
    assert "Skin set to: default-de (saved)" in output
    assert get_active_skin_name() == "default-de"
    assert save.call_args_list == [
        call("display.language", "de"),
        call("display.skin", "default-de"),
    ]
    assert os.environ["HERMES_LANGUAGE"] == "de"


def test_language_command_english_uses_default_skin(monkeypatch, capsys):
    cli = _make_cli_stub()
    set_active_skin("default-de")
    monkeypatch.delenv("HERMES_LANGUAGE", raising=False)

    skins = [
        {"name": "default", "description": "English", "source": "builtin"},
        {"name": "default-de", "description": "German", "source": "user"},
    ]

    with patch("hermes_cli.config.load_config", return_value={"display": {"language": "de", "skin": "default-de"}}), \
         patch("hermes_cli.skin_engine.list_skins", return_value=skins), \
         patch("cli.save_config_value", return_value=True) as save:
        cli._handle_language_command("/lang english")

    output = capsys.readouterr().out
    assert "Language set to: en (saved)" in output
    assert "Skin set to: default (saved)" in output
    assert get_active_skin_name() == "default"
    assert save.call_args_list == [
        call("display.language", "en"),
        call("display.skin", "default"),
    ]


def test_language_command_keeps_skin_when_matching_skin_missing(monkeypatch, capsys):
    cli = _make_cli_stub()
    set_active_skin("default")
    monkeypatch.delenv("HERMES_LANGUAGE", raising=False)

    skins = [{"name": "default", "description": "English", "source": "builtin"}]

    with patch("hermes_cli.config.load_config", return_value={"display": {"language": "en", "skin": "default"}}), \
         patch("hermes_cli.skin_engine.list_skins", return_value=skins), \
         patch("cli.save_config_value", return_value=True) as save:
        cli._handle_language_command("/language hu")

    output = capsys.readouterr().out
    assert "Language set to: hu (saved)" in output
    assert "No matching skin found for hu: default-hu" in output
    assert save.call_args_list == [call("display.language", "hu")]


def test_language_aliases_are_registered():
    from hermes_cli.commands import resolve_command

    assert resolve_command("lang").name == "language"
    assert resolve_command("locale").name == "language"


def test_localized_default_skins_are_built_in():
    from hermes_cli.skin_engine import list_skins, load_skin

    skin_names = {skin["name"] for skin in list_skins()}
    assert "default-hu" in skin_names
    assert "default-de" in skin_names
    assert "Üdv a Hermes Agentben" in load_skin("default-hu").get_branding("welcome")
    assert "Willkommen bei Hermes Agent" in load_skin("default-de").get_branding("welcome")
