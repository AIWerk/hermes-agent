from queue import Queue
from unittest.mock import patch

from cli import HermesCLI
from hermes_cli.browser_connect import (
    DEFAULT_LOCAL_BROWSER_LAUNCHER_URL,
    LocalBrowserLauncherConfig,
    load_local_browser_launcher_config,
)


def test_local_launcher_config_defaults_and_tenant_overrides():
    cfg = load_local_browser_launcher_config({"browser": {"local_launcher": {"enabled": True}}})
    assert cfg.configured is True
    assert cfg.launcher_url == DEFAULT_LOCAL_BROWSER_LAUNCHER_URL


def test_local_launcher_rejects_non_loopback_urls():
    cfg = load_local_browser_launcher_config(
        {"browser": {"local_launcher": {"enabled": True, "launcher_url": "https://example.com"}}}
    )
    assert cfg.configured is False
    assert "loopback" in cfg.validation_error


def test_launcher_config_token_is_preserved():
    cfg = load_local_browser_launcher_config(
        {"browser": {"local_launcher": {"enabled": True, "launcher_token": "sentinel"}}}
    )
    assert cfg.launcher_token == "sentinel"


def test_browser_connect_uses_configured_local_launcher(monkeypatch):
    cli = HermesCLI.__new__(HermesCLI)
    cli._pending_input = Queue()
    cfg = LocalBrowserLauncherConfig(enabled=True)
    monkeypatch.delenv("BROWSER_CDP_URL", raising=False)

    with (
        patch("hermes_cli.cli_commands_mixin.load_local_browser_launcher_config", return_value=cfg),
        patch("hermes_cli.cli_commands_mixin.is_browser_debug_ready", return_value=False),
        patch("hermes_cli.cli_commands_mixin.discover_local_cdp_url", return_value=None),
        patch("hermes_cli.cli_commands_mixin.call_local_browser_launcher", return_value=(True, "ok")) as launch,
        patch("hermes_cli.cli_commands_mixin.wait_for_browser_debug_ready", return_value=True),
    ):
        cli._handle_browser_command("/browser connect")

    launch.assert_called_once_with(cfg, "open", timeout=15.0)
    assert __import__("os").environ["BROWSER_CDP_URL"] == cfg.cdp_url
    assert "normal approval flow" in cli._pending_input.get_nowait()
