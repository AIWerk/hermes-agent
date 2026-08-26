"""Tests for gated Chromium-binary auto-install on local cold start."""

from types import SimpleNamespace

import pytest

import tools.browser_tool as bt


class _AllowedLease:
    allowed = True

    def validate(self):
        return True


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_security_policy_bool_strict",
        lambda *a, **k: _AllowedLease(),
    )
    bt._chromium_autoinstall_attempted = False
    bt._cached_chromium_installed = None
    yield
    bt._chromium_autoinstall_attempted = False
    bt._cached_chromium_installed = None


def _no_subprocess(monkeypatch):
    calls = []
    monkeypatch.setattr(bt.subprocess, "run", lambda *a, **k: calls.append((a, k)))
    return calls


class TestGating:
    def test_disabled_lazy_installs_skips(self, monkeypatch):
        monkeypatch.setattr(bt, "_running_in_docker", lambda: False)
        class DisabledLease:
            allowed = False

            def validate(self):
                return True

        monkeypatch.setattr(
            "hermes_cli.config.load_security_policy_bool_strict",
            lambda *a, **k: DisabledLease(),
        )
        calls = _no_subprocess(monkeypatch)
        assert bt._maybe_autoinstall_chromium() is False
        assert calls == []

    def test_docker_skips(self, monkeypatch):
        monkeypatch.setattr(bt, "_running_in_docker", lambda: True)
        calls = _no_subprocess(monkeypatch)
        assert bt._maybe_autoinstall_chromium() is False
        assert calls == []

    def test_policy_switch_before_subprocess_skips(self, monkeypatch):
        class SwitchedLease:
            allowed = True

            def validate(self):
                return False

        monkeypatch.setattr(bt, "_running_in_docker", lambda: False)
        monkeypatch.setattr(
            "hermes_cli.config.load_security_policy_bool_strict",
            lambda *a, **k: SwitchedLease(),
        )
        monkeypatch.setattr(bt, "_find_agent_browser", lambda: "/x/agent-browser")
        calls = _no_subprocess(monkeypatch)
        assert bt._maybe_autoinstall_chromium() is False
        assert calls == []


class TestInstall:
    def test_success_installs_binary_only_and_rechecks(self, monkeypatch):
        monkeypatch.setattr(bt, "_running_in_docker", lambda: False)
        monkeypatch.setattr("tools.lazy_deps._allow_lazy_installs", lambda: True)
        monkeypatch.setattr(bt, "_find_agent_browser", lambda: "/x/agent-browser")
        monkeypatch.setattr(bt, "_build_browser_env", lambda: {})
        monkeypatch.setattr(bt, "_chromium_installed", lambda: True)

        captured = {}

        def fake_run(cmd, **kw):
            captured["cmd"] = cmd
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(bt.subprocess, "run", fake_run)

        assert bt._maybe_autoinstall_chromium() is True
        assert captured["cmd"] == ["/x/agent-browser", "install"]
        assert "--with-deps" not in captured["cmd"]

    def test_npx_form_is_binary_only(self, monkeypatch):
        monkeypatch.setattr(bt, "_running_in_docker", lambda: False)
        monkeypatch.setattr("tools.lazy_deps._allow_lazy_installs", lambda: True)
        monkeypatch.setattr(bt, "_find_agent_browser", lambda: "npx agent-browser")
        monkeypatch.setattr(bt, "_build_browser_env", lambda: {})
        monkeypatch.setattr(bt, "_chromium_installed", lambda: True)
        monkeypatch.setattr(bt.shutil, "which", lambda _, path=None: "/usr/bin/npx")
        monkeypatch.setattr(bt, "node_tool_runnable", lambda p: True)

        captured = {}
        monkeypatch.setattr(
            bt.subprocess, "run",
            lambda cmd, **kw: captured.update(cmd=cmd) or SimpleNamespace(returncode=0, stdout="", stderr=""),
        )

        assert bt._maybe_autoinstall_chromium() is True
        assert captured["cmd"] == [
            "/usr/bin/npx", "--ignore-scripts", "-y", bt.AGENT_BROWSER_NPX_SPEC, "install",
        ]
        assert "--with-deps" not in captured["cmd"]

    def test_nonzero_exit_returns_false(self, monkeypatch):
        monkeypatch.setattr(bt, "_running_in_docker", lambda: False)
        monkeypatch.setattr("tools.lazy_deps._allow_lazy_installs", lambda: True)
        monkeypatch.setattr(bt, "_find_agent_browser", lambda: "/x/agent-browser")
        monkeypatch.setattr(bt, "_build_browser_env", lambda: {})
        monkeypatch.setattr(
            bt.subprocess, "run",
            lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="boom"),
        )
        assert bt._maybe_autoinstall_chromium() is False


class TestOneShot:
    def test_second_call_does_not_reinstall(self, monkeypatch):
        monkeypatch.setattr(bt, "_running_in_docker", lambda: False)
        monkeypatch.setattr("tools.lazy_deps._allow_lazy_installs", lambda: True)
        monkeypatch.setattr(bt, "_find_agent_browser", lambda: "/x/agent-browser")
        monkeypatch.setattr(bt, "_build_browser_env", lambda: {})
        monkeypatch.setattr(bt, "_chromium_installed", lambda: True)

        runs = []
        monkeypatch.setattr(
            bt.subprocess, "run",
            lambda *a, **k: runs.append(1) or SimpleNamespace(returncode=0, stdout="", stderr=""),
        )

        assert bt._maybe_autoinstall_chromium() is True
        assert bt._maybe_autoinstall_chromium() is True
        assert len(runs) == 1
