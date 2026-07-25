"""Tests for ${ENV_VAR} substitution and config cache invalidation."""

import os

import pytest
from hermes_cli.config import _expand_env_vars, load_config, read_raw_config


class TestExpandEnvVars:
    def test_simple_substitution(self):
        with pytest.MonkeyPatch().context() as mp:
            mp.setenv("MY_KEY", "secret123")
            assert _expand_env_vars("${MY_KEY}") == "secret123"

    def test_missing_var_kept_verbatim(self):
        with pytest.MonkeyPatch().context() as mp:
            mp.delenv("UNDEFINED_VAR_XYZ", raising=False)
            assert _expand_env_vars("${UNDEFINED_VAR_XYZ}") == "${UNDEFINED_VAR_XYZ}"

    def test_no_placeholder_unchanged(self):
        assert _expand_env_vars("plain-value") == "plain-value"

    def test_dict_recursive(self):
        with pytest.MonkeyPatch().context() as mp:
            mp.setenv("TOKEN", "tok-abc")
            result = _expand_env_vars({"key": "${TOKEN}", "other": "literal"})
            assert result == {"key": "tok-abc", "other": "literal"}

    def test_nested_dict(self):
        with pytest.MonkeyPatch().context() as mp:
            mp.setenv("API_KEY", "sk-xyz")
            result = _expand_env_vars({"model": {"api_key": "${API_KEY}"}})
            assert result["model"]["api_key"] == "sk-xyz"

    def test_list_items(self):
        with pytest.MonkeyPatch().context() as mp:
            mp.setenv("VAL", "hello")
            result = _expand_env_vars(["${VAL}", "literal", 42])
            assert result == ["hello", "literal", 42]

    def test_non_string_values_untouched(self):
        assert _expand_env_vars(42) == 42
        assert _expand_env_vars(3.14) == 3.14
        assert _expand_env_vars(True) is True
        assert _expand_env_vars(None) is None

    def test_multiple_placeholders_in_one_string(self):
        with pytest.MonkeyPatch().context() as mp:
            mp.setenv("HOST", "localhost")
            mp.setenv("PORT", "5432")
            assert _expand_env_vars("${HOST}:${PORT}") == "localhost:5432"

    def test_dict_keys_not_expanded(self):
        with pytest.MonkeyPatch().context() as mp:
            mp.setenv("KEY", "value")
            result = _expand_env_vars({"${KEY}": "no-expand-key"})
            assert "${KEY}" in result


class TestLoadConfigExpansion:
    def test_load_config_expands_env_vars(self, tmp_path, monkeypatch):
        config_yaml = (
            "model:\n"
            "  api_key: ${GOOGLE_API_KEY}\n"
            "platforms:\n"
            "  telegram:\n"
            "    token: ${TELEGRAM_BOT_TOKEN}\n"
            "plain: no-substitution\n"
        )
        config_file = tmp_path / "config.yaml"
        config_file.write_text(config_yaml)

        monkeypatch.setenv("GOOGLE_API_KEY", "gsk-test-key")
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1234567:ABC-token")
        # Patch the imported function's own globals. Other tests may reload
        # hermes_cli.config, making string-target monkeypatches hit a different
        # module object than this collection-time imported load_config().
        monkeypatch.setitem(load_config.__globals__, "get_config_path", lambda: config_file)

        config = load_config()

        assert config["model"]["api_key"] == "gsk-test-key"
        assert config["platforms"]["telegram"]["token"] == "1234567:ABC-token"
        assert config["plain"] == "no-substitution"

    def test_load_config_unresolved_kept_verbatim(self, tmp_path, monkeypatch):
        config_yaml = "model:\n  api_key: ${NOT_SET_XYZ_123}\n"
        config_file = tmp_path / "config.yaml"
        config_file.write_text(config_yaml)

        monkeypatch.delenv("NOT_SET_XYZ_123", raising=False)
        monkeypatch.setitem(load_config.__globals__, "get_config_path", lambda: config_file)

        config = load_config()

        assert config["model"]["api_key"] == "${NOT_SET_XYZ_123}"


class TestLoadConfigCacheEnvStaleness:
    """The load_config() cache must not pin expansions made against a stale
    environment (#58514): a load before load_hermes_dotenv() runs, or an env
    var rotated in-process, must not keep serving the old expansion."""

    def test_env_var_appearing_after_first_load_invalidates_cache(self, tmp_path, monkeypatch):
        config_yaml = "auxiliary:\n  vision:\n    api_key: ${LATE_DOTENV_KEY_58514}\n"
        config_file = tmp_path / "config.yaml"
        config_file.write_text(config_yaml)

        monkeypatch.delenv("LATE_DOTENV_KEY_58514", raising=False)
        monkeypatch.setitem(load_config.__globals__, "get_config_path", lambda: config_file)

        # First load happens before the var exists (pre-dotenv): literal kept.
        assert load_config()["auxiliary"]["vision"]["api_key"] == "${LATE_DOTENV_KEY_58514}"

        # .env load brings the var in — same file mtime/size, env changed.
        monkeypatch.setenv("LATE_DOTENV_KEY_58514", "nvapi-real")
        assert load_config()["auxiliary"]["vision"]["api_key"] == "nvapi-real"

    def test_env_var_rotation_invalidates_cache(self, tmp_path, monkeypatch):
        config_yaml = "providers:\n  mistral:\n    api_key: ${ROTATED_KEY_58514}\n"
        config_file = tmp_path / "config.yaml"
        config_file.write_text(config_yaml)

        monkeypatch.setenv("ROTATED_KEY_58514", "key-v1")
        monkeypatch.setitem(load_config.__globals__, "get_config_path", lambda: config_file)

        assert load_config()["providers"]["mistral"]["api_key"] == "key-v1"

        monkeypatch.setenv("ROTATED_KEY_58514", "key-v2")
        assert load_config()["providers"]["mistral"]["api_key"] == "key-v2"

    def test_unchanged_env_still_serves_cache(self, tmp_path, monkeypatch):
        config_yaml = "providers:\n  mistral:\n    api_key: ${STABLE_KEY_58514}\n"
        config_file = tmp_path / "config.yaml"
        config_file.write_text(config_yaml)

        monkeypatch.setenv("STABLE_KEY_58514", "key-stable")
        monkeypatch.setitem(load_config.__globals__, "get_config_path", lambda: config_file)

        load_config()
        # load_config_readonly() returns the cached object itself, so object
        # identity across calls proves the cache-hit path was taken (a rebuild
        # would produce a fresh dict).
        readonly = load_config.__globals__["load_config_readonly"]
        first = readonly()
        second = readonly()

        assert first is second
        assert first["providers"]["mistral"]["api_key"] == "key-stable"


class TestConfigCacheContentStaleness:
    def test_raw_config_same_size_same_mtime_edit_invalidates_cache(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("marker: alpha\n")
        monkeypatch.setitem(read_raw_config.__globals__, "get_config_path", lambda: config_file)
        read_raw_config.__globals__["_RAW_CONFIG_CACHE"].clear()

        assert read_raw_config()["marker"] == "alpha"
        before = config_file.stat()
        config_file.write_text("marker: bravo\n")
        os.utime(config_file, ns=(before.st_atime_ns, before.st_mtime_ns))

        assert read_raw_config()["marker"] == "bravo"

    def test_load_config_same_size_same_mtime_edit_invalidates_cache(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("marker: alpha\n")
        monkeypatch.setitem(load_config.__globals__, "get_config_path", lambda: config_file)
        load_config.__globals__["_LOAD_CONFIG_CACHE"].clear()

        assert load_config()["marker"] == "alpha"
        before = config_file.stat()
        config_file.write_text("marker: bravo\n")
        os.utime(config_file, ns=(before.st_atime_ns, before.st_mtime_ns))

        assert load_config()["marker"] == "bravo"

    def test_managed_same_size_same_mtime_edit_invalidates_cache(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.yaml"
        managed_dir = tmp_path / "managed"
        managed_dir.mkdir()
        managed_file = managed_dir / "config.yaml"
        managed_file.write_text("marker: alpha\n")
        monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed_dir))
        monkeypatch.setitem(load_config.__globals__, "get_config_path", lambda: config_file)
        load_config.__globals__["_LOAD_CONFIG_CACHE"].clear()

        assert load_config()["marker"] == "alpha"
        before = managed_file.stat()
        managed_file.write_text("marker: bravo\n")
        os.utime(managed_file, ns=(before.st_atime_ns, before.st_mtime_ns))

        assert load_config()["marker"] == "bravo"

    def test_raw_config_parses_the_exact_bytes_it_fingerprints(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("marker: alpha\n")
        before = config_file.stat()
        monkeypatch.setitem(read_raw_config.__globals__, "get_config_path", lambda: config_file)
        read_raw_config.__globals__["_RAW_CONFIG_CACHE"].clear()
        path_type = type(config_file)
        original_read_bytes = path_type.read_bytes
        raced = False

        def racing_read_bytes(path):
            nonlocal raced
            data = original_read_bytes(path)
            if path == config_file and not raced:
                raced = True
                path.write_text("marker: bravo\n")
                os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            return data

        monkeypatch.setattr(path_type, "read_bytes", racing_read_bytes)

        assert read_raw_config()["marker"] == "alpha"

    def test_load_config_parses_the_exact_bytes_it_fingerprints(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("marker: alpha\n")
        before = config_file.stat()
        monkeypatch.setitem(load_config.__globals__, "get_config_path", lambda: config_file)
        load_config.__globals__["_LOAD_CONFIG_CACHE"].clear()
        path_type = type(config_file)
        original_read_bytes = path_type.read_bytes
        raced = False

        def racing_read_bytes(path):
            nonlocal raced
            data = original_read_bytes(path)
            if path == config_file and not raced:
                raced = True
                path.write_text("marker: bravo\n")
                os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            return data

        monkeypatch.setattr(path_type, "read_bytes", racing_read_bytes)

        assert load_config()["marker"] == "alpha"

    def test_managed_config_parses_the_exact_bytes_it_fingerprints(self, tmp_path, monkeypatch):
        from hermes_cli import managed_scope

        managed_dir = tmp_path / "managed"
        managed_dir.mkdir()
        managed_file = managed_dir / "config.yaml"
        managed_file.write_text("marker: alpha\n")
        before = managed_file.stat()
        monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed_dir))
        managed_scope.invalidate_managed_cache()
        path_type = type(managed_file)
        original_read_bytes = path_type.read_bytes
        raced = False

        def racing_read_bytes(path):
            nonlocal raced
            data = original_read_bytes(path)
            if path == managed_file and not raced:
                raced = True
                path.write_text("marker: bravo\n")
                os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            return data

        monkeypatch.setattr(path_type, "read_bytes", racing_read_bytes)

        assert managed_scope.load_managed_config()["marker"] == "alpha"


class TestLoadCliConfigExpansion:
    """Verify that load_cli_config() also expands ${VAR} references."""

    def test_cli_config_ignores_empty_terminal_section(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("terminal:\n")

        monkeypatch.setattr("cli._hermes_home", tmp_path)

        from cli import load_cli_config
        config = load_cli_config()

        assert isinstance(config["terminal"], dict)
        assert config["terminal"]["env_type"] == "local"

    def test_cli_config_expands_auxiliary_api_key(self, tmp_path, monkeypatch):
        config_yaml = (
            "auxiliary:\n"
            "  vision:\n"
            "    api_key: ${TEST_VISION_KEY_XYZ}\n"
        )
        config_file = tmp_path / "config.yaml"
        config_file.write_text(config_yaml)

        monkeypatch.setenv("TEST_VISION_KEY_XYZ", "vis-key-123")
        # Patch the hermes home so load_cli_config finds our test config
        monkeypatch.setattr("cli._hermes_home", tmp_path)

        from cli import load_cli_config
        config = load_cli_config()

        assert config["auxiliary"]["vision"]["api_key"] == "vis-key-123"

    def test_cli_config_unresolved_kept_verbatim(self, tmp_path, monkeypatch):
        config_yaml = (
            "auxiliary:\n"
            "  vision:\n"
            "    api_key: ${UNSET_CLI_VAR_ABC}\n"
        )
        config_file = tmp_path / "config.yaml"
        config_file.write_text(config_yaml)

        monkeypatch.delenv("UNSET_CLI_VAR_ABC", raising=False)
        monkeypatch.setattr("cli._hermes_home", tmp_path)

        from cli import load_cli_config
        config = load_cli_config()

        assert config["auxiliary"]["vision"]["api_key"] == "${UNSET_CLI_VAR_ABC}"
