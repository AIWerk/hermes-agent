import argparse
from types import SimpleNamespace

import pytest

from hermes_cli.subcommands.dashboard import build_dashboard_parser


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    build_dashboard_parser(
        subparsers,
        cmd_dashboard=lambda _args: None,
        cmd_dashboard_register=lambda _args: None,
    )
    return parser


def test_dashboard_and_serve_accept_assistant_mode() -> None:
    parser = _parser()

    assert parser.parse_args(["dashboard", "--assistant"]).assistant is True
    assert parser.parse_args(["serve", "--assistant"]).assistant is True


def test_dashboard_and_serve_default_to_admin_mode() -> None:
    parser = _parser()

    assert parser.parse_args(["dashboard"]).assistant is False
    assert parser.parse_args(["serve"]).assistant is False


def _dashboard_args(*, assistant: bool) -> SimpleNamespace:
    return SimpleNamespace(
        assistant=assistant,
        status=False,
        stop=False,
        host="127.0.0.1",
        port=0,
        no_open=True,
        insecure=False,
        skip_build=False,
        isolated=True,
        open_profile="",
        headless_backend=True,
        ssh_session_token_file=None,
        ssh_owner_nonce=None,
    )


@pytest.mark.parametrize(
    ("assistant", "expected_mode"),
    [(True, "assistant"), (False, "admin")],
)
def test_cmd_dashboard_propagates_explicit_server_mode(
    assistant: bool,
    expected_mode: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hermes_cli.main as main_mod
    import hermes_cli.web_server as web_server

    monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", lambda: "default")
    monkeypatch.setattr("hermes_cli.resource_limits.apply_nofile_soft_limit", lambda: None)
    monkeypatch.setattr(main_mod, "_sync_bundled_skills_quietly", lambda: None)
    monkeypatch.setattr("hermes_cli.config.apply_terminal_config_to_env", lambda: None)
    monkeypatch.setattr("hermes_cli.plugins.discover_plugins", lambda: None)
    monkeypatch.setattr(
        "hermes_cli.mcp_startup.start_background_mcp_discovery", lambda **_kwargs: None
    )
    monkeypatch.setattr(main_mod, "_maybe_setup_dashboard_auth_interactively", lambda _args: None)
    captured: dict[str, object] = {}
    monkeypatch.setattr(web_server, "start_server", lambda **kwargs: captured.update(kwargs))

    main_mod.cmd_dashboard(_dashboard_args(assistant=assistant))

    assert captured["mode"] == expected_mode
