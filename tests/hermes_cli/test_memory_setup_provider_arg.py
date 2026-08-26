"""Tests for `hermes memory setup [provider]` routing.

The `memory setup` subcommand accepts an optional positional ``provider`` so a
fresh install can configure a specific provider directly (e.g.
``hermes memory setup honcho``) without the interactive picker — which matters
because the per-provider ``hermes <provider>`` subcommand is only registered
once that provider is active.
"""

from types import SimpleNamespace
from unittest.mock import patch

from hermes_cli import memory_setup


class TestMemorySetupProviderRouting:
    def test_setup_with_provider_arg_skips_picker(self):
        """`memory setup honcho` routes straight to cmd_setup_provider."""
        args = SimpleNamespace(memory_command="setup", provider="honcho")
        with patch.object(memory_setup, "cmd_setup_provider") as direct, \
             patch.object(memory_setup, "cmd_setup") as picker:
            memory_setup.memory_command(args)
        direct.assert_called_once_with("honcho")
        picker.assert_not_called()


    def test_unknown_provider_reports_and_returns_early(self, capsys):
        """An unknown provider name surfaces a helpful message and returns
        before any config load/save (the not-found guard precedes those imports)."""
        memory_setup.cmd_setup_provider("notaprovider")
        out = capsys.readouterr().out
        assert "not found" in out
        assert "hermes memory setup" in out


class TestInstallDependenciesRunner:
    """Provider installs must use the canonical environment-aware pipeline."""

    def _run_with_outcome(self, tmp_path, outcome):
        from tools.lazy_deps import InstallSpecsResult

        (tmp_path / "plugin.yaml").write_text(
            "pip_dependencies:\n  - definitely-not-installed-xyz\n", encoding="utf-8"
        )
        result = outcome if isinstance(outcome, InstallSpecsResult) else InstallSpecsResult(**outcome)
        with patch("plugins.memory.find_provider_dir", return_value=tmp_path), \
             patch("tools.lazy_deps.install_specs", return_value=result) as install:
            memory_setup._install_dependencies("x")
        return install

    def test_routes_through_install_specs(self, tmp_path):
        install = self._run_with_outcome(tmp_path, {"ok": True})
        install.assert_called_once_with(["definitely-not-installed-xyz"], timeout=120)

    def test_surfaces_blocked_install(self, tmp_path, capsys):
        self._run_with_outcome(
            tmp_path, {"ok": False, "blocked": True, "reason": "policy disabled"}
        )
        out = capsys.readouterr().out
        assert "Cannot install" in out
        assert "policy disabled" in out

    def test_surfaces_failed_install(self, tmp_path, capsys):
        self._run_with_outcome(
            tmp_path, {"ok": False, "stderr": "installer failed"}
        )
        out = capsys.readouterr().out
        assert "Failed to install" in out
        assert "installer failed" in out
        assert "Run manually" in out
