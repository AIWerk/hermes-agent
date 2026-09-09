"""Unit tests for the on-disk MCP schema cache (tools/mcp_schema_cache.py).

The module landed in #56832's extraction without its tests; these cover the
fingerprint keying, read/write round-trip, and invalidation behavior.
"""

import tools.mcp_schema_cache as msc
from tools import mcp_tool_registration as _mcp_registration


class TestConfigFingerprint:
    def test_stable_for_same_config(self):
        cfg = {"command": "npx", "args": ["-y", "@playwright/mcp"]}
        assert msc.config_fingerprint(cfg) == msc.config_fingerprint(dict(cfg))

    def test_changes_when_connection_config_changes(self):
        base = {"command": "npx", "args": ["-y", "@playwright/mcp"]}
        assert msc.config_fingerprint(base) != msc.config_fingerprint(
            {**base, "args": ["-y", "@playwright/mcp", "--headless"]}
        )
        assert msc.config_fingerprint(base) != msc.config_fingerprint(
            {**base, "command": "uvx"}
        )
        assert msc.config_fingerprint(base) != msc.config_fingerprint(
            {**base, "tools": {"include": ["a"]}}
        )
        assert msc.config_fingerprint(base) != msc.config_fingerprint(
            {**base, "tools": {"resources": False}}
        )
        assert msc.config_fingerprint(base) != msc.config_fingerprint(
            {**base, "tools": {"prompts": False}}
        )

    def test_changes_when_connection_authority_changes(self, tmp_path):
        base = {"url": "https://example.test/mcp"}
        variants = (
            {"headers": {"Authorization": "Bearer token-a"}},
            {"env": {"API_TOKEN": "token-a"}},
            {"auth": "oauth"},
            {"oauth": {"client_id": "client-a", "client_secret": "secret-a"}},
            {"identity_header": {"name": "X-User", "value": "user-a"}},
        )
        for authority in variants:
            changed = {
                key: (
                    {nested_key: f"{nested_value}-changed" for nested_key, nested_value in value.items()}
                    if isinstance(value, dict)
                    else "none"
                )
                for key, value in authority.items()
            }
            assert msc.config_fingerprint({**base, **authority}) != msc.config_fingerprint(
                {**base, **changed}
            )

        cert = tmp_path / "client.pem"
        key = tmp_path / "client.key"
        cert.write_text("certificate-a", encoding="utf-8")
        key.write_text("private-key-a", encoding="utf-8")
        cert_config = {**base, "client_cert": str(cert), "client_key": str(key)}
        first = msc.config_fingerprint(cert_config)
        cert.write_text("certificate-b", encoding="utf-8")
        second = msc.config_fingerprint(cert_config)
        assert first != second
        key.write_text("private-key-b", encoding="utf-8")
        assert second != msc.config_fingerprint(cert_config)

        spaced_config = {
            **base,
            "client_cert": f"  {cert}  ",
            "client_key": f"  {key}  ",
        }
        spaced_first = msc.config_fingerprint(spaced_config)
        cert.write_text("certificate-c", encoding="utf-8")
        assert spaced_first != msc.config_fingerprint(spaced_config)

    def test_effective_runtime_authority_changes_fingerprint(self, monkeypatch):
        stdio = {"command": "srv"}
        monkeypatch.setenv("PATH", "/authority/a")
        first = msc.config_fingerprint(stdio)
        monkeypatch.setenv("PATH", "/authority/b")
        assert first != msc.config_fingerprint(stdio)

        from hermes_cli import profiles

        http = {
            "url": "https://example.test/mcp",
            "identity_header": {"name": "X-Profile", "value_from": "profile"},
        }
        monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "profile-a")
        first = msc.config_fingerprint(http)
        monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "profile-b")
        assert first != msc.config_fingerprint(http)

    def test_ignores_non_connection_keys(self):
        base = {"command": "npx", "args": []}
        assert msc.config_fingerprint(base) == msc.config_fingerprint(
            {**base, "timeout": 5, "enabled": True, "lazy": True}
        )


class TestCacheRoundTrip:
    def _isolate(self, monkeypatch, tmp_path):
        monkeypatch.setattr(msc, "_cache_path", lambda: tmp_path / "cache.json")

    def test_write_then_read_with_matching_fingerprint(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        tools = [{"name": "t1", "description": "d", "inputSchema": {"type": "object"}}]
        msc.write_cache_entry("srv", "fp1", tools=tools, utility_tools=[])
        entry = msc.get_cached_entry("srv", "fp1")
        assert entry is not None
        assert msc.tools_from_cache_entry(entry) == tools
        assert msc.utility_tools_from_cache_entry(entry) == []

    def test_persisted_entry_never_contains_authority_secrets(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        secret = "top-secret-client-passphrase"
        config = {
            "url": "https://example.test/mcp",
            "oauth": {"client_id": "client", "client_secret": secret},
            "identity_header": {"name": "X-User", "value": secret},
        }
        msc.write_cache_entry(
            "srv", msc.config_fingerprint(config), tools=[], utility_tools=[]
        )

        assert secret not in (tmp_path / "cache.json").read_text(encoding="utf-8")

    def test_fingerprint_mismatch_returns_none(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        msc.write_cache_entry("srv", "fp1", tools=[], utility_tools=[])
        assert msc.get_cached_entry("srv", "OTHER") is None

    def test_missing_server_returns_none(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        assert msc.get_cached_entry("nope", "fp") is None

    def test_corrupt_cache_file_is_tolerated(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        (tmp_path / "cache.json").write_text("{not json", encoding="utf-8")
        assert msc.get_cached_entry("srv", "fp") is None
        # And writes recover the file.
        msc.write_cache_entry("srv", "fp", tools=[], utility_tools=[])
        assert msc.get_cached_entry("srv", "fp") is not None

    def test_malformed_entry_shapes_are_tolerated(self):
        assert msc.tools_from_cache_entry({"tools": "nope"}) == []
        assert msc.utility_tools_from_cache_entry({}) == []


def test_in_process_lazy_authority_rotation_becomes_eager(monkeypatch):
    from types import SimpleNamespace

    from tools import mcp_tool
    from tools import mcp_tool_discovery as discovery
    from tools import registry as registry_module

    old = {"command": "srv", "lazy": True, "env": {"TOKEN": "old"}}
    new = {"command": "srv", "lazy": True, "env": {"TOKEN": "new"}}
    key = mcp_tool._server_state_key("srv")
    monkeypatch.setattr(mcp_tool, "_servers", {})
    monkeypatch.setattr(mcp_tool, "_server_connecting", set())
    monkeypatch.setattr(mcp_tool, "_server_scope_keys", {key: mcp_tool._mcp_registry_scope()})
    monkeypatch.setattr(mcp_tool, "_lazy_server_configs", {key: old})
    monkeypatch.setattr(
        mcp_tool, "_lazy_server_fingerprints", {key: msc.config_fingerprint(old)}
    )
    monkeypatch.setattr(
        mcp_tool, "_lazy_server_tool_names", {key: ["mcp__srv__read"]}
    )
    monkeypatch.setattr(mcp_tool, "_server_connect_retry_after", {key: 999999999.0})
    monkeypatch.setattr(mcp_tool, "_server_connect_failures", {key: 3})
    deregistered = []
    fake_registry = SimpleNamespace(
        get_entry=lambda name, scope=None: SimpleNamespace(toolset="mcp-srv"),
        deregister=lambda name, scope=None: deregistered.append((name, scope)),
    )
    monkeypatch.setattr(registry_module, "registry", fake_registry)

    selected = discovery._select_new_servers({"srv": new})

    assert selected == {"srv": new}
    assert key in mcp_tool._server_connecting
    assert key not in mcp_tool._lazy_server_configs
    assert key not in mcp_tool._lazy_server_fingerprints
    assert key not in mcp_tool._lazy_server_tool_names
    assert key not in mcp_tool._server_connect_retry_after
    assert key not in mcp_tool._server_connect_failures
    assert deregistered == [("mcp__srv__read", mcp_tool._mcp_registry_scope())]


def test_disabling_in_process_lazy_server_removes_cached_state(monkeypatch):
    from tools import mcp_tool
    from tools import mcp_tool_discovery as discovery

    old = {"command": "srv", "lazy": True}
    disabled = {**old, "enabled": False}
    key = mcp_tool._server_state_key("srv")
    monkeypatch.setattr(mcp_tool, "_servers", {})
    monkeypatch.setattr(mcp_tool, "_server_connecting", set())
    monkeypatch.setattr(mcp_tool, "_lazy_server_configs", {key: old})
    monkeypatch.setattr(
        mcp_tool, "_lazy_server_fingerprints", {key: msc.config_fingerprint(old)}
    )
    monkeypatch.setattr(mcp_tool, "_lazy_server_tool_names", {key: []})

    assert discovery._select_new_servers({"srv": disabled}) == {}
    assert key not in mcp_tool._lazy_server_configs
    assert key not in mcp_tool._lazy_server_fingerprints
    assert key not in mcp_tool._lazy_server_tool_names


def test_zero_name_lazy_cache_registration_falls_back_to_eager(monkeypatch):
    from tools import mcp_tool
    from tools import mcp_tool_discovery as discovery

    config = {"srv": {"command": "srv", "lazy": True}}
    key = mcp_tool._server_state_key("srv")
    monkeypatch.setattr(discovery, "_resolve_server_lazy", lambda *_args: True)
    monkeypatch.setattr(msc, "get_cached_entry", lambda *_args: {"tools": []})
    monkeypatch.setattr(
        _mcp_registration, "_register_from_cache_sync", lambda *_args: []
    )
    monkeypatch.setattr(mcp_tool, "_server_connecting", {key})

    eager, tool_count, server_count = discovery._register_lazy_from_cache(config)

    assert eager == config
    assert tool_count == 0
    assert server_count == 0
    assert key in mcp_tool._server_connecting


class TestCacheFileLocation:
    def test_cache_lives_under_hermes_home_cache_dir_with_0600(
        self, monkeypatch, tmp_path
    ):
        # Real path (no _cache_path monkeypatch): HERMES_HOME/cache/…, 0o600,
        # matching the discovery-cache precedent in tools/registry.py.
        import hermes_constants

        monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
        path = msc._cache_path()
        assert path == tmp_path / "cache" / "mcp_schema_cache.json"
        msc.write_cache_entry("srv", "fp", tools=[], utility_tools=[])
        assert path.exists()
        assert (path.stat().st_mode & 0o777) == 0o600


class TestWriteSkip:
    def test_identical_payload_skips_rewrite(self, monkeypatch, tmp_path):
        monkeypatch.setattr(msc, "_cache_path", lambda: tmp_path / "cache.json")
        saves = []
        real_save = msc._save_all

        def _counting_save(data):
            saves.append(1)
            real_save(data)

        monkeypatch.setattr(msc, "_save_all", _counting_save)
        tools = [{"name": "t1", "description": "d", "inputSchema": {}}]
        msc.write_cache_entry("srv", "fp1", tools=tools, utility_tools=[])
        assert len(saves) == 1
        # Identical payload (reconnect / list_changed refresh) → no rewrite.
        msc.write_cache_entry("srv", "fp1", tools=list(tools), utility_tools=[])
        assert len(saves) == 1
        # Changed payload → rewrite.
        msc.write_cache_entry("srv", "fp2", tools=tools, utility_tools=[])
        assert len(saves) == 2


class TestWriteThroughPreservesSchema:
    """Regression: the write-through path must persist real tool parameters.

    ``mcp`` 2.0 renamed ``Tool.inputSchema`` to ``input_schema``, keeping the
    camelCase spelling only as a *serialization* alias — pydantic aliases do
    not apply to attribute access, so ``getattr(tool, "inputSchema")`` returns
    None on 2.x instead of raising. The cache-write path used exactly that
    bare read, so every entry landed on disk with ``"inputSchema": {}``. A
    server later registered from that cache (``lazy: true``) was advertised to
    the model with every parameter stripped, which makes required-argument
    tools such as zhihu's ``zhida`` (``query`` + ``model`` both required)
    uncallable.

    These tests drive the live ``_register_server_tools`` write-through with a
    genuine SDK ``Tool`` so the field-rename is actually exercised — the mock
    fixtures elsewhere build ``SimpleNamespace`` objects and cannot catch it.
    (Salvaged from #91451 / #102129.)
    """

    _SCHEMA = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "model": {"type": "string"},
        },
        "required": ["query", "model"],
    }

    def _cache_write_through(self, tmp_path, monkeypatch):
        import json
        from unittest.mock import MagicMock, patch

        from mcp.types import Tool

        import tools.mcp_tool as mt
        from tools.registry import ToolRegistry

        monkeypatch.setattr(msc, "_cache_path", lambda: tmp_path / "cache.json")
        # Registration records per-server state in module globals (lazy tool names, trust
        # levels, read-only hints...); isolate them so the probe server never leaks into
        # later tests such as ``discover_mcp_tools() == []`` assertions.
        for attr in ("_lazy_server_tool_names", "_lazy_server_configs", "_lazy_server_fingerprints",
                     "_mcp_tool_server_names", "_server_trust_levels", "_tool_read_only_hints"):
            monkeypatch.setattr(mt, attr, {})
        server = mt.MCPServerTask("probe_srv")
        server._tools = [
            Tool(name="zhida", description="知乎直答", inputSchema=self._SCHEMA)
        ]
        server.session = MagicMock()

        with patch("tools.registry.registry", ToolRegistry()):
            registered = _mcp_registration._register_server_tools("probe_srv", server, {})
        assert registered, "tool was not registered; write-through never fired"
        entry = json.loads((tmp_path / "cache.json").read_text(encoding="utf-8"))["probe_srv"]
        return entry

    def test_cached_schema_keeps_properties(self, tmp_path, monkeypatch):
        cached = self._cache_write_through(tmp_path, monkeypatch)["tools"][0]["inputSchema"]
        assert set(cached.get("properties", {})) == {"query", "model"}, (
            "write-through persisted an empty schema — the SDK field rename "
            "was read with a bare camelCase getattr"
        )

    def test_cached_schema_keeps_required(self, tmp_path, monkeypatch):
        cached = self._cache_write_through(tmp_path, monkeypatch)["tools"][0]["inputSchema"]
        assert cached.get("required") == ["query", "model"]

    def test_cache_round_trip_reaches_agent_schema(self, tmp_path, monkeypatch):
        """The whole point of the cache: a lazy server re-advertises params."""
        from unittest.mock import patch

        import tools.mcp_tool as mt
        from tools import mcp_tool_registration as _mcp_registration
        from tools.registry import ToolRegistry

        entry = self._cache_write_through(tmp_path, monkeypatch)
        lazy_reg = ToolRegistry()
        with patch("tools.registry.registry", lazy_reg):
            names = _mcp_registration._register_from_cache_sync("probe_srv", {}, entry)
        assert names, "lazy registration produced no tools"
        schema = lazy_reg.get_schema("mcp__probe_srv__zhida")
        assert schema is not None, "lazy path did not register the tool"
        assert set(schema["parameters"].get("properties", {})) == {"query", "model"}
        assert schema["parameters"].get("required") == ["query", "model"]
