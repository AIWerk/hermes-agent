"""Tests for CLI browser CDP auto-launch helpers."""

from contextlib import closing, redirect_stdout
from io import StringIO
import os
from queue import Queue
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from unittest.mock import patch

from cli import HermesCLI
from hermes_cli.browser_connect import (
    _wait_for_browser_debug_ready_or_exit,
    call_local_browser_launcher,
    get_chrome_debug_candidates,
    is_browser_debug_ready,
    launch_chrome_debug,
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
             patch("hermes_cli.browser_connect._wait_for_browser_debug_ready_or_exit", return_value="ready"), \
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
             patch("hermes_cli.browser_connect._wait_for_browser_debug_ready_or_exit", return_value="ready"), \
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
             patch("hermes_cli.browser_connect._wait_for_browser_debug_ready_or_exit", return_value="ready"), \
             patch("subprocess.Popen", side_effect=fake_popen):
            assert HermesCLI._try_launch_chrome_debug(9222, "Linux") is True

        assert attempts == [brave, chrome]

    def test_wait_for_browser_debug_ready_or_exit_detects_early_exit(self, monkeypatch):
        class _Proc:
            def __init__(self):
                self.calls = 0

            def poll(self):
                self.calls += 1
                return 1 if self.calls >= 2 else None

        monkeypatch.setattr("hermes_cli.browser_connect.time.sleep", lambda _seconds: None)
        with patch("hermes_cli.browser_connect.is_browser_debug_ready", return_value=False):
            state = _wait_for_browser_debug_ready_or_exit(_Proc(), 9222, timeout=0.3, interval=0.01)

        assert state == "exited"

    def test_launch_tries_next_browser_when_first_candidate_exits_before_debug_ready(self):
        brave = "/usr/bin/brave-browser"
        chrome = "/usr/bin/google-chrome"
        attempts = []

        class _Proc:
            pass

        def fake_popen(cmd, **kwargs):
            attempts.append(cmd[0])
            return _Proc()

        with patch("hermes_cli.browser_connect.get_chrome_debug_candidates", return_value=[brave, chrome]), \
             patch("hermes_cli.browser_connect._wait_for_browser_debug_ready_or_exit", side_effect=["exited", "ready"]), \
             patch("subprocess.Popen", side_effect=fake_popen):
            assert HermesCLI._try_launch_chrome_debug(9222, "Linux") is True

        assert attempts == [brave, chrome]

    def test_launch_result_hints_singleton_forward_on_clean_exit(self, tmp_path, monkeypatch):
        """A candidate that exits code 0 without opening the port = an existing
        instance absorbed the launch (Chromium single-instance behavior)."""
        chrome = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

        class _Proc:
            pid = 1234
            returncode = 0

            def poll(self):
                return 0

        monkeypatch.setattr(
            "hermes_cli.browser_connect.chrome_debug_data_dir", lambda: str(tmp_path)
        )
        with patch("hermes_cli.browser_connect.get_chrome_debug_candidates", return_value=[chrome]), \
             patch("hermes_cli.browser_connect.is_browser_debug_ready", return_value=False), \
             patch("subprocess.Popen", return_value=_Proc()):
            result = launch_chrome_debug(9222, "Windows")

        assert result.launched is False
        assert result.attempts[0].state == "exited"
        assert result.attempts[0].returncode == 0
        assert result.hint is not None
        assert "already-running" in result.hint
        assert "chrome.exe" in result.hint

    def test_launch_result_surfaces_stderr_tail_on_crash(self, tmp_path, monkeypatch):
        chrome = "/usr/bin/google-chrome"

        class _Proc:
            pid = 4321
            returncode = 127

            def __init__(self, stderr_path):
                # Simulate the browser writing to the redirected stderr file.
                with open(stderr_path, "w", encoding="utf-8") as fh:
                    fh.write("error while loading shared libraries: libnspr4.so\n")

            def poll(self):
                return 127

        monkeypatch.setattr(
            "hermes_cli.browser_connect.chrome_debug_data_dir", lambda: str(tmp_path)
        )
        stderr_path = tmp_path / "launch-stderr.log"
        with patch("hermes_cli.browser_connect.get_chrome_debug_candidates", return_value=[chrome]), \
             patch("hermes_cli.browser_connect.is_browser_debug_ready", return_value=False), \
             patch("subprocess.Popen", side_effect=lambda *a, **k: _Proc(stderr_path)):
            result = launch_chrome_debug(9222, "Linux")

        assert result.launched is False
        assert result.attempts[0].returncode == 127
        assert "libnspr4.so" in result.attempts[0].stderr_tail
        assert result.hint is not None
        assert "libnspr4.so" in result.hint

    def test_launch_result_no_hint_when_no_candidates(self):
        with patch("hermes_cli.browser_connect.get_chrome_debug_candidates", return_value=[]):
            result = launch_chrome_debug(9222, "Linux")

        assert result.launched is False
        assert result.attempts == []
        assert result.hint is None

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

    def test_connect_context_note_keeps_normal_approval_flow(self, monkeypatch):
        """`/browser connect` queues an informational note, not a permission grant.

        The note must describe that the tools are now CDP-backed against a
        possibly-session-bearing debug profile, but it must NOT tell the model to
        skip the normal per-action approval step ("do not wait for separate
        permission"). The attached browser controls a live profile, so
        navigational/destructive actions still go through the usual approval flow.
        """
        cli = HermesCLI.__new__(HermesCLI)
        cli._pending_input = Queue()
        monkeypatch.delenv("BROWSER_CDP_URL", raising=False)

        with patch("cli.is_browser_debug_ready", return_value=True), \
             patch("tools.browser_tool.cleanup_all_browsers"), \
             patch("tools.browser_tool._ensure_cdp_supervisor"), \
             redirect_stdout(StringIO()):
            cli._handle_browser_command("/browser connect")

        note = cli._pending_input.get_nowait()
        # Still informational about what was attached.
        assert "Chromium-family" in note
        assert "dev/debug" in note
        assert "logged-in sessions" in note or "cookies" in note
        # The wait-for-permission step must NOT be removed anymore.
        assert "do not wait for separate permission" not in note
        assert "is expected" not in note
        assert "normal approval flow" in note
        # Still must not misrepresent the profile as the main everyday browser.
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

    def test_local_launcher_config_parses_token(self):
        cfg = load_local_browser_launcher_config({
            "browser": {"local_launcher": {"enabled": True, "launcher_token": " secret-123 "}}
        })
        assert cfg.launcher_token == "secret-123"
        # Absent token stays empty so older launchers keep working unchanged.
        cfg = load_local_browser_launcher_config({"browser": {"local_launcher": {"enabled": True}}})
        assert cfg.launcher_token == ""

    def test_call_local_launcher_sends_bearer_token_when_configured(self):
        class FakeResponse:
            status = 200
            def __enter__(self):
                return self
            def __exit__(self, exc_type, exc, tb):
                return False
            def read(self, limit):
                return b'{"ok": true}'

        cfg = load_local_browser_launcher_config({
            "browser": {"local_launcher": {
                "enabled": True,
                "launcher_url": "http://127.0.0.1:18765",
                "launcher_token": "secret-123",
            }}
        })
        captured = {}

        def fake_urlopen(req, timeout):
            captured["req"] = req
            return FakeResponse()

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            ok, _ = call_local_browser_launcher(cfg, "open", timeout=1.0)

        assert ok is True
        req = captured["req"]
        assert isinstance(req, urllib.request.Request)
        assert req.full_url == "http://127.0.0.1:18765/open"
        assert req.get_header("Authorization") == "Bearer secret-123"

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


def _extract_launcher_server_source():
    """Pull the embedded launcher HTTP server out of rocky-browser.sh."""
    root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    script = os.path.join(
        root, "scripts", "local-browser-connector", "linux", "rocky-browser.sh"
    )
    with open(script, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.strip().endswith("<<'PY' > \"$LOG_DIR/rocky-browser-launcher.log\" 2>&1 &"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "PY")
    return "\n".join(lines[start + 1:end])


def _free_port():
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestLauncherHttpAuth:
    """The embedded launcher HTTP server must require the bearer token."""

    def _run_server(self, tmp_path, token):
        port = _free_port()
        src = _extract_launcher_server_source()
        # A harmless stand-in for the rocky-browser script so /status etc. do not
        # actually launch a browser; it just prints and exits 0.
        stub = tmp_path / "rocky-stub.sh"
        stub.write_text("#!/usr/bin/env bash\necho ok\n")
        stub.chmod(0o755)
        env = dict(os.environ)
        env.update({
            "ROCKY_BROWSER_LAUNCHER_HOST": "127.0.0.1",
            "ROCKY_BROWSER_LAUNCHER_PORT": str(port),
            "ROCKY_BROWSER_SCRIPT": str(stub),
            "ROCKY_BROWSER_LAUNCHER_TOKEN": token,
        })
        proc = subprocess.Popen(
            [sys.executable, "-c", src],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1):
                    break
            except urllib.error.URLError:
                time.sleep(0.1)
        else:
            proc.terminate()
            raise AssertionError("launcher HTTP server did not become ready")
        return proc, port

    @staticmethod
    def _get(port, path, token=None):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
        if token is not None:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status
        except urllib.error.HTTPError as exc:
            return exc.code

    def test_action_endpoints_require_token(self, tmp_path):
        proc, port = self._run_server(tmp_path, token="topsecret")
        try:
            # Health is an open liveness probe.
            assert self._get(port, "/health") == 200
            # Capability endpoints reject missing / wrong tokens.
            assert self._get(port, "/status") == 401
            assert self._get(port, "/status", token="wrong") == 401
            assert self._get(port, "/open") == 401
            assert self._get(port, "/down", token="nope") == 401
            # The correct token is accepted.
            assert self._get(port, "/status", token="topsecret") == 200
        finally:
            proc.terminate()
            proc.wait(timeout=5)
