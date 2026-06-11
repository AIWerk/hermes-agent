"""Tests for CLI browser CDP auto-launch helpers."""

from contextlib import redirect_stdout
from io import StringIO
import os
from queue import Queue
import subprocess
from unittest.mock import patch

from cli import HermesCLI
from hermes_cli.browser_connect import (
    call_local_browser_launcher,
    get_chrome_debug_candidates,
    is_browser_debug_ready,
    load_local_browser_launcher_config,
    manual_chrome_debug_command,
    wait_for_browser_debug_ready,
)


def _assert_chrome_debug_cmd(cmd, expected_chrome, expected_port):
    """Verify the auto-launch command has all required flags."""
    assert cmd[0] == expected_chrome
    assert f"--remote-debugging-port={expected_port}" in cmd
    assert "--no-first-run" in cmd
    assert "--no-default-browser-check" in cmd
    user_data_args = [a for a in cmd if a.startswith("--user-data-dir=")]
    assert len(user_data_args) == 1, "Expected exactly one --user-data-dir flag"
    assert "chrome-debug" in user_data_args[0]


class _FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class TestChromeDebugLaunch:
    def test_browser_debug_ready_requires_http_cdp_endpoint(self):
        requested = []

        def fake_urlopen(url, timeout):
            requested.append(url)
            if url.endswith("/json/version"):
                return _FakeResponse()
            raise OSError("unexpected probe")

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            assert is_browser_debug_ready("http://127.0.0.1:9222", timeout=0.1) is True

        assert requested == ["http://127.0.0.1:9222/json/version"]

    def test_browser_debug_ready_rejects_non_cdp_listener(self):
        with patch("urllib.request.urlopen", side_effect=OSError("not cdp")):
            assert is_browser_debug_ready("http://127.0.0.1:9222", timeout=0.1) is False

    def test_windows_launch_uses_browser_found_on_path(self):
        captured = {}

        def fake_popen(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return object()

        with patch("hermes_cli.browser_connect.shutil.which", side_effect=lambda name: r"C:\Chrome\chrome.exe" if name == "chrome.exe" else None), \
             patch("hermes_cli.browser_connect.os.path.isfile", side_effect=lambda path: path == r"C:\Chrome\chrome.exe"), \
             patch("subprocess.Popen", side_effect=fake_popen):
            assert HermesCLI._try_launch_chrome_debug(9333, "Windows") is True

        _assert_chrome_debug_cmd(captured["cmd"], r"C:\Chrome\chrome.exe", 9333)
        # Windows uses creationflags (POSIX-only start_new_session would raise).
        assert "start_new_session" not in captured["kwargs"]
        flags = captured["kwargs"].get("creationflags", 0)
        expected = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
        assert flags == expected

    def test_windows_launch_falls_back_to_common_install_dirs(self, monkeypatch):
        captured = {}
        program_files = r"C:\Program Files"
        # Use os.path.join so path separators match cross-platform
        installed = os.path.join(program_files, "Google", "Chrome", "Application", "chrome.exe")

        def fake_popen(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return object()

        monkeypatch.setenv("ProgramFiles", program_files)
        monkeypatch.delenv("ProgramFiles(x86)", raising=False)
        monkeypatch.delenv("LOCALAPPDATA", raising=False)

        with patch("hermes_cli.browser_connect.shutil.which", return_value=None), \
             patch("hermes_cli.browser_connect.os.path.isfile", side_effect=lambda path: path == installed), \
             patch("subprocess.Popen", side_effect=fake_popen):
            assert HermesCLI._try_launch_chrome_debug(9222, "Windows") is True

        _assert_chrome_debug_cmd(captured["cmd"], installed, 9222)

    def test_manual_command_uses_detected_linux_browser(self):
        with patch("hermes_cli.browser_connect.shutil.which", side_effect=lambda name: "/usr/bin/chromium" if name == "chromium" else None), \
             patch("hermes_cli.browser_connect.os.path.isfile", side_effect=lambda path: path == "/usr/bin/chromium"):
            command = manual_chrome_debug_command(9222, "Linux")

        assert command is not None
        assert command.startswith("/usr/bin/chromium --remote-debugging-port=9222")

    def test_linux_candidates_prefer_chrome_before_brave_when_both_exist(self):
        chrome = "/usr/bin/google-chrome"
        brave = "/usr/bin/brave-browser"

        def fake_which(name):
            return {"google-chrome": chrome, "brave-browser": brave}.get(name)

        with patch("hermes_cli.browser_connect.shutil.which", side_effect=fake_which), \
             patch("hermes_cli.browser_connect.os.path.isfile", side_effect=lambda path: path in {chrome, brave}):
            candidates = get_chrome_debug_candidates("Linux")
            command = manual_chrome_debug_command(9222, "Linux")

        assert candidates[:2] == [chrome, brave]
        assert command is not None
        assert command.startswith(f"{chrome} --remote-debugging-port=9222")

    def test_linux_candidates_prefer_chrome_install_path_before_brave_on_path(self):
        chrome = "/opt/google/chrome/chrome"
        brave = "/usr/bin/brave-browser"

        with patch("hermes_cli.browser_connect.shutil.which", side_effect=lambda name: brave if name == "brave-browser" else None), \
             patch("hermes_cli.browser_connect.os.path.isfile", side_effect=lambda path: path in {chrome, brave}):
            candidates = get_chrome_debug_candidates("Linux")

        assert candidates[:2] == [chrome, brave]

    def test_windows_candidates_prefer_chrome_install_path_before_brave_on_path(self, monkeypatch):
        program_files = r"C:\Program Files"
        chrome = os.path.join(program_files, "Google", "Chrome", "Application", "chrome.exe")
        brave = r"C:\Brave\brave.exe"

        monkeypatch.setenv("ProgramFiles", program_files)
        monkeypatch.delenv("ProgramFiles(x86)", raising=False)
        monkeypatch.delenv("LOCALAPPDATA", raising=False)

        with patch("hermes_cli.browser_connect.shutil.which", side_effect=lambda name: brave if name == "brave.exe" else None), \
             patch("hermes_cli.browser_connect.os.path.isfile", side_effect=lambda path: path in {chrome, brave}):
            candidates = get_chrome_debug_candidates("Windows")

        assert candidates[:2] == [chrome, brave]

    def test_linux_candidates_include_arch_brave_install_path(self):
        brave = "/opt/brave-bin/brave"

        with patch("hermes_cli.browser_connect.shutil.which", return_value=None), \
             patch("hermes_cli.browser_connect.os.path.isfile", side_effect=lambda path: path == brave):
            candidates = get_chrome_debug_candidates("Linux")
            command = manual_chrome_debug_command(9222, "Linux")

        assert candidates == [brave]
        assert command is not None
        assert command.startswith(f"{brave} --remote-debugging-port=9222")

    def test_linux_candidates_include_brave_binary_name(self):
        brave = "/usr/bin/brave"

        with patch("hermes_cli.browser_connect.shutil.which", side_effect=lambda name: brave if name == "brave" else None), \
             patch("hermes_cli.browser_connect.os.path.isfile", side_effect=lambda path: path == brave):
            candidates = get_chrome_debug_candidates("Linux")
            command = manual_chrome_debug_command(9222, "Linux")

        assert candidates == [brave]
        assert command is not None
        assert command.startswith(f"{brave} --remote-debugging-port=9222")

    def test_linux_candidates_include_official_brave_and_edge_stable_paths(self):
        brave = "/usr/bin/brave-browser-stable"
        edge = "/usr/bin/microsoft-edge-stable"

        with patch("hermes_cli.browser_connect.shutil.which", return_value=None), \
             patch("hermes_cli.browser_connect.os.path.isfile", side_effect=lambda path: path in {brave, edge}):
            candidates = get_chrome_debug_candidates("Linux")

        assert candidates == [brave, edge]

    def test_launch_tries_next_browser_when_first_candidate_fails(self):
        brave = "/usr/bin/brave-browser"
        chrome = "/usr/bin/google-chrome"
        attempts = []

        def fake_popen(cmd, **kwargs):
            attempts.append(cmd[0])
            if cmd[0] == brave:
                raise OSError("broken brave install")
            return object()

        with patch("hermes_cli.browser_connect.get_chrome_debug_candidates", return_value=[brave, chrome]), \
             patch("subprocess.Popen", side_effect=fake_popen):
            assert HermesCLI._try_launch_chrome_debug(9222, "Linux") is True

        assert attempts == [brave, chrome]

    def test_manual_command_uses_wsl_windows_chrome_when_available(self):
        chrome = "/mnt/c/Program Files/Google/Chrome/Application/chrome.exe"

        with patch("hermes_cli.browser_connect.shutil.which", return_value=None), \
             patch("hermes_cli.browser_connect.os.path.isfile", side_effect=lambda path: path == chrome):
            command = manual_chrome_debug_command(9222, "Linux")

        assert command is not None
        # Linux/WSL uses POSIX shell quoting (single quotes around paths with spaces).
        assert command.startswith(f"'{chrome}' --remote-debugging-port=9222")

    def test_manual_command_uses_windows_quoting_on_windows(self):
        chrome = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

        with patch("hermes_cli.browser_connect.shutil.which", side_effect=lambda name: chrome if name == "chrome.exe" else None), \
             patch("hermes_cli.browser_connect.os.path.isfile", side_effect=lambda path: path == chrome):
            command = manual_chrome_debug_command(9222, "Windows")

        assert command is not None
        # Windows uses cmd.exe-compatible quoting via subprocess.list2cmdline.
        assert command.startswith(f'"{chrome}" --remote-debugging-port=9222')
        assert "'" not in command

    def test_manual_command_returns_none_when_linux_browser_missing(self):
        with patch("hermes_cli.browser_connect.shutil.which", return_value=None), \
             patch("hermes_cli.browser_connect.os.path.isfile", return_value=False):
            assert manual_chrome_debug_command(9222, "Linux") is None

    def test_connect_context_note_allows_expected_browser_use(self, monkeypatch):
        """`/browser connect` is an instruction to use the CDP browser.

        The queued context note must not tell the model to wait for a second
        permission step or imply that the attached browser is the user's main
        everyday Chrome profile.
        """
        cli = HermesCLI.__new__(HermesCLI)
        cli._pending_input = Queue()
        monkeypatch.delenv("BROWSER_CDP_URL", raising=False)

        with patch("hermes_cli.cli_commands_mixin.is_browser_debug_ready", return_value=True), \
             patch("tools.browser_tool.cleanup_all_browsers"), \
             patch("tools.browser_tool._ensure_cdp_supervisor"), \
             redirect_stdout(StringIO()):
            cli._handle_browser_command("/browser connect")

        note = cli._pending_input.get_nowait()
        assert "Chromium-family" in note
        assert "dev/debug" in note
        assert "using browser tools for their current browser-related request is expected" in note
        assert "live Chrome browser" not in note
        assert "real browser" not in note
        assert "Please await their instruction" not in note

    def test_local_launcher_config_defaults_and_tenant_overrides(self):
        cfg = load_local_browser_launcher_config({"browser": {}})
        assert cfg.enabled is False
        assert cfg.launcher_port == 18765
        assert cfg.cdp_port == 9222
        assert cfg.launcher_url == "http://127.0.0.1:18765"
        assert cfg.cdp_url == "http://127.0.0.1:9222"

        cfg = load_local_browser_launcher_config({
            "browser": {
                "local_launcher": {
                    "enabled": True,
                    "launcher_port": 18766,
                    "cdp_port": 9333,
                    "ssh_target": "tenant-user@example.invalid",
                    "ssh_port": 22222,
                    "ssh_identity_file": "~/.ssh/tenant_key",
                    "browser_profile_dir": "~/.hermes/tenant-browser",
                    "browser_binary": "/usr/bin/chromium",
                    "start_url": "https://example.invalid/start",
                }
            }
        })
        assert cfg.enabled is True
        assert cfg.launcher_url == "http://127.0.0.1:18766"
        assert cfg.cdp_url == "http://127.0.0.1:9333"
        assert cfg.ssh_target == "tenant-user@example.invalid"
        assert cfg.ssh_port == 22222
        assert cfg.ssh_identity_file == "~/.ssh/tenant_key"
        assert cfg.browser_profile_dir == "~/.hermes/tenant-browser"
        assert cfg.browser_binary == "/usr/bin/chromium"
        assert cfg.start_url == "https://example.invalid/start"

    def test_local_launcher_rejects_non_loopback_urls(self):
        cfg = load_local_browser_launcher_config({
            "browser": {"local_launcher": {"enabled": True, "launcher_url": "http://192.0.2.1:18765"}}
        })
        assert cfg.enabled is False
        assert "loopback" in cfg.validation_error
        ok, detail = call_local_browser_launcher(cfg, "open", timeout=0.1)
        assert ok is False
        assert "loopback" in detail

    def test_local_launcher_rejects_non_http_schemes(self):
        cfg = load_local_browser_launcher_config({
            "browser": {"local_launcher": {"enabled": True, "launcher_url": "file://127.0.0.1/tmp/socket"}}
        })
        assert cfg.enabled is False
        assert "http/https" in getattr(cfg, "validation_error", "")

    def test_connect_falls_back_when_no_launcher_config(self, monkeypatch):
        cli = HermesCLI.__new__(HermesCLI)
        monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
        calls = []

        def fake_ready(url, timeout=1.0):
            calls.append(url)
            return len(calls) > 1

        with patch("cli.load_local_browser_launcher_config", return_value=load_local_browser_launcher_config({"browser": {}})), \
             patch("cli.is_browser_debug_ready", side_effect=fake_ready), \
             patch("cli.wait_for_browser_debug_ready", return_value=True), \
             patch.object(HermesCLI, "_try_launch_chrome_debug", return_value=True) as launch, \
             patch("tools.browser_tool.cleanup_all_browsers"), \
             patch("tools.browser_tool._ensure_cdp_supervisor"), \
             redirect_stdout(StringIO()):
            cli._handle_browser_command("/browser connect")

        launch.assert_called_once()
        assert os.environ["BROWSER_CDP_URL"] == "http://127.0.0.1:9222"

    def test_connect_falls_back_to_builtin_launch_when_configured_launcher_unreachable(self, monkeypatch):
        cli = HermesCLI.__new__(HermesCLI)
        cfg = load_local_browser_launcher_config({
            "browser": {"local_launcher": {"enabled": True, "launcher_url": "http://127.0.0.1:18765"}}
        })
        monkeypatch.delenv("BROWSER_CDP_URL", raising=False)

        with patch("cli.load_local_browser_launcher_config", return_value=cfg), \
             patch("cli.is_browser_debug_ready", return_value=False), \
             patch("cli.call_local_browser_launcher", return_value=(False, "connection refused")) as launcher, \
             patch("cli.wait_for_browser_debug_ready", return_value=True), \
             patch.object(HermesCLI, "_try_launch_chrome_debug", return_value=True) as launch, \
             patch("tools.browser_tool.cleanup_all_browsers"), \
             patch("tools.browser_tool._ensure_cdp_supervisor"), \
             redirect_stdout(StringIO()) as out:
            cli._handle_browser_command("/browser connect")

        launcher.assert_called_once()
        launch.assert_called_once()
        assert "Falling back to the normal local Chromium launch path" in out.getvalue()
        assert os.environ["BROWSER_CDP_URL"] == "http://127.0.0.1:9222"

    def test_browser_launch_calls_local_launcher_and_connects(self, monkeypatch):
        cli = HermesCLI.__new__(HermesCLI)
        cfg = load_local_browser_launcher_config({
            "browser": {"local_launcher": {"enabled": True, "launcher_url": "http://127.0.0.1:18765"}}
        })
        monkeypatch.delenv("BROWSER_CDP_URL", raising=False)

        with patch("cli.load_local_browser_launcher_config", return_value=cfg), \
             patch("cli.call_local_browser_launcher", return_value=(True, "{}")) as launcher, \
             patch("cli.wait_for_browser_debug_ready", return_value=True), \
             patch("tools.browser_tool.cleanup_all_browsers"), \
             patch("tools.browser_tool._ensure_cdp_supervisor"), \
             redirect_stdout(StringIO()) as out:
            cli._handle_browser_command("/browser launch")

        launcher.assert_called_once_with(cfg, "open", timeout=15.0)
        assert "Local browser launcher accepted /open" in out.getvalue()
        assert os.environ["BROWSER_CDP_URL"] == "http://127.0.0.1:9222"

    def test_cdp_readiness_polling_retries_until_ready(self):
        attempts = []

        def fake_ready(url, timeout=1.0):
            attempts.append((url, timeout))
            return len(attempts) == 3

        with patch("hermes_cli.browser_connect.is_browser_debug_ready", side_effect=fake_ready), \
             patch("hermes_cli.browser_connect.time.sleep", return_value=None):
            assert wait_for_browser_debug_ready("http://127.0.0.1:9222", timeout_s=2, interval_s=0.1)
        assert len(attempts) == 3

    def test_call_local_launcher_open_success_is_mockable(self):
        class FakeResponse:
            status = 200
            def __enter__(self):
                return self
            def __exit__(self, exc_type, exc, tb):
                return False
            def read(self, limit):
                return b'{"ok": true}'

        cfg = load_local_browser_launcher_config({
            "browser": {"local_launcher": {"enabled": True, "launcher_url": "http://127.0.0.1:18765"}}
        })
        with patch("urllib.request.urlopen", return_value=FakeResponse()) as urlopen:
            ok, detail = call_local_browser_launcher(cfg, "open", timeout=1.0)
        assert ok is True
        assert detail == '{"ok": true}'
        urlopen.assert_called_once_with("http://127.0.0.1:18765/open", timeout=1.0)

    def test_generic_browser_code_has_no_attilla_specific_hardcodes(self):
        root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        paths = [
            os.path.join(root, "hermes_cli", "browser_connect.py"),
            os.path.join(root, "cli.py"),
        ]
        forbidden = ["agent" + ".aiwerk.ch", "hermes" + "-attila", "id_agent" + "_aiwerk_hermes"]
        for path in paths:
            with open(path, encoding="utf-8") as fh:
                content = fh.read()
            for marker in forbidden:
                assert marker not in content
