"""Multiplexed gateways discover and reload MCP servers per profile (#95518)."""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource
from hermes_constants import get_hermes_home, hermes_home_key


def test_registered_handlers_keep_immutable_profile_state_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tools import mcp_tool_handlers as handlers

    key_a = (hermes_home_key(tmp_path / "profiles" / "a"), "shared")
    key_b = (hermes_home_key(tmp_path / "profiles" / "b"), "shared")
    seen = []

    def capture(server_name, _timeout):
        from tools import mcp_tool

        seen.append(mcp_tool._server_state_key(server_name))
        return None, "not-connected"

    monkeypatch.setattr(handlers, "_acquire_call_server", capture)
    handler_a = handlers._make_tool_handler(
        "shared", "read", 1.0, state_key=key_a
    )
    handler_b = handlers._make_tool_handler(
        "shared", "read", 1.0, state_key=key_b
    )

    assert handler_a({}) == "not-connected"
    assert handler_b({}) == "not-connected"
    assert seen == [key_a, key_b]


def test_profile_secondary_mcp_files_are_not_first_profile_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools import mcp_tool_config as config
    from tools import mcp_tool_loop as loop

    homes = [tmp_path / "a", tmp_path / "b"]
    for home in homes:
        home.mkdir()
    monkeypatch.setattr(config, "_mcp_stderr_log_fhs", {})
    monkeypatch.setattr(config, "_mcp_stderr_log_fh", None)

    log_paths = []
    lock_paths = []
    for home in homes:
        token = set_hermes_home_override(home)
        try:
            log_paths.append(Path(config._get_mcp_stderr_log().name))
            cookie = loop._try_acquire_mcp_discovery_lock()
            assert hasattr(cookie, "_fh")
            lock_paths.append(Path(cookie._fh.name))
            cookie.release()
        finally:
            reset_hermes_home_override(token)

    assert log_paths == [home / "logs" / "mcp-stderr.log" for home in homes]
    assert lock_paths == [home / ".mcp-discovery.lock" for home in homes]
    assert log_paths[0] != log_paths[1]
    assert lock_paths[0] != lock_paths[1]
    for handle in config._mcp_stderr_log_fhs.values():
        handle.close()


def test_scoped_shutdown_deregisters_cache_only_lazy_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tools import mcp_tool
    from tools import mcp_tool_lifecycle as lifecycle
    from tools import registry as registry_module
    from tools.registry import ToolRegistry

    scope = hermes_home_key(tmp_path / "profiles" / "a")
    key = (scope, "shared")
    tool_name = "mcp__shared__read"
    reg = ToolRegistry()
    reg.register(tool_name, "mcp-shared", {"name": tool_name}, lambda: None, scope=scope)
    monkeypatch.setattr(registry_module, "registry", reg)
    monkeypatch.setattr(mcp_tool, "_servers", {})
    monkeypatch.setattr(mcp_tool, "_server_scope_keys", {key: scope})
    monkeypatch.setattr(mcp_tool, "_lazy_server_configs", {key: {"command": "srv"}})
    monkeypatch.setattr(mcp_tool, "_lazy_server_fingerprints", {key: "fp"})
    monkeypatch.setattr(mcp_tool, "_lazy_server_tool_names", {key: [tool_name]})
    monkeypatch.setattr(
        mcp_tool, "_mcp_tool_server_names", {(scope, tool_name): key}
    )
    monkeypatch.setattr(lifecycle._loop, "_stop_mcp_loop", lambda **_kwargs: True)

    lifecycle.shutdown_mcp_servers(scope=scope)

    assert reg.get_entry(tool_name, scope=scope) is None
    assert key not in mcp_tool._lazy_server_configs
    assert (scope, tool_name) not in mcp_tool._mcp_tool_server_names


def test_partial_config_removal_retires_cache_only_lazy_server(
    monkeypatch, tmp_path: Path
) -> None:
    from tools import mcp_tool
    from tools import mcp_tool_discovery as discovery
    from tools import registry as registry_module
    from tools.registry import ToolRegistry

    scope = hermes_home_key(tmp_path / "profiles" / "a")
    removed_key = (scope, "removed")
    tool_name = "mcp__removed__read"
    reg = ToolRegistry()
    reg.register(tool_name, "mcp-removed", {"name": tool_name}, lambda: None, scope=scope)
    monkeypatch.setattr(registry_module, "registry", reg)
    monkeypatch.setattr(mcp_tool, "_mcp_registry_scope", lambda: scope)
    monkeypatch.setattr(mcp_tool, "_servers", {})
    monkeypatch.setattr(mcp_tool, "_lazy_server_configs", {removed_key: {"command": "old"}})
    monkeypatch.setattr(mcp_tool, "_lazy_server_fingerprints", {removed_key: "fp"})
    monkeypatch.setattr(mcp_tool, "_lazy_server_tool_names", {removed_key: [tool_name]})
    monkeypatch.setattr(
        mcp_tool, "_mcp_tool_server_names", {(scope, tool_name): removed_key}
    )
    monkeypatch.setattr(
        discovery._config,
        "_load_mcp_config",
        lambda: {"kept": {"command": "kept", "enabled": False}},
    )
    shutdown_calls = []
    monkeypatch.setattr(
        discovery._lifecycle,
        "shutdown_mcp_servers",
        lambda *, scope=None, server_names=None: shutdown_calls.append(
            (scope, server_names)
        ),
    )
    monkeypatch.setattr(mcp_tool, "_ensure_mcp_sdk", lambda: False)

    assert discovery.discover_mcp_tools() == []
    assert shutdown_calls == [(scope, {"removed"})]


def test_disabling_live_server_triggers_targeted_shutdown(
    monkeypatch, tmp_path: Path
) -> None:
    from tools import mcp_tool
    from tools import mcp_tool_discovery as discovery

    scope = hermes_home_key(tmp_path / "profiles" / "a")
    key = (scope, "disabled")
    monkeypatch.setattr(mcp_tool, "_mcp_registry_scope", lambda: scope)
    monkeypatch.setattr(mcp_tool, "_servers", {key: SimpleNamespace(session=object())})
    monkeypatch.setattr(
        discovery._config,
        "_load_mcp_config",
        lambda: {"disabled": {"command": "srv", "enabled": False}},
    )
    calls = []
    monkeypatch.setattr(
        discovery._lifecycle,
        "shutdown_mcp_servers",
        lambda *, scope=None, server_names=None: calls.append((scope, server_names)),
    )
    monkeypatch.setattr(mcp_tool, "_ensure_mcp_sdk", lambda: False)

    assert discovery.discover_mcp_tools() == []
    assert calls == [(scope, {"disabled"})]


def test_empty_config_discovery_tears_down_current_profile(monkeypatch) -> None:
    from tools import mcp_tool
    from tools import mcp_tool_discovery as discovery

    calls = []
    scope = hermes_home_key()
    key = (scope, "removed")
    monkeypatch.setattr(mcp_tool, "_mcp_registry_scope", lambda: scope)
    monkeypatch.setattr(discovery._config, "_load_mcp_config", lambda: {})
    monkeypatch.setattr(mcp_tool, "_lazy_server_configs", {key: {"command": "srv"}})
    monkeypatch.setattr(
        discovery._lifecycle,
        "shutdown_mcp_servers",
        lambda *, scope=None, server_names=None: calls.append((scope, server_names)),
    )

    assert discovery.discover_mcp_tools() == []
    assert calls == [(hermes_home_key(), {"removed"})]


def test_removed_pre_adoption_claim_cannot_resurrect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tools import mcp_tool
    from tools import mcp_tool_discovery as discovery
    from tools import mcp_tool_lifecycle as lifecycle

    scope = hermes_home_key(tmp_path / "profiles" / "a")
    key = (scope, "removed")
    server = SimpleNamespace(
        name="removed",
        registry_scope=scope,
        state_key=key,
        _discovery_claimed=True,
    )
    monkeypatch.setattr(mcp_tool, "_servers", {})
    monkeypatch.setattr(mcp_tool, "_server_scope_keys", {key: scope})
    monkeypatch.setattr(mcp_tool, "_server_connect_claims", {key: server}, raising=False)
    monkeypatch.setattr(mcp_tool, "_mcp_loop", None)
    monkeypatch.setattr(lifecycle._loop, "_stop_mcp_loop", lambda **_kwargs: True)

    lifecycle.shutdown_mcp_servers(scope=scope, server_names={"removed"})

    assert key not in mcp_tool._server_connect_claims
    assert discovery._adopt_server("removed", server) is False
    assert key not in mcp_tool._servers


@pytest.mark.asyncio
async def test_retired_inflight_connect_does_not_recreate_failure_cooldown(
    monkeypatch,
) -> None:
    from tools import mcp_tool_discovery as discovery

    async def retired(_name, _config):
        raise discovery._MCPConfigRetiredDuringConnect("removed")

    failures = []
    monkeypatch.setattr(discovery, "_discover_and_register_server", retired)
    monkeypatch.setattr(
        discovery, "_note_connect_failure", lambda *args: failures.append(args)
    )

    await discovery._discover_all({"removed": {"command": "srv"}})

    assert failures == []


def test_named_shutdown_clears_removed_failed_state_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tools import mcp_tool
    from tools import mcp_tool_lifecycle as lifecycle

    scope = hermes_home_key(tmp_path / "profiles" / "a")
    removed = (scope, "removed")
    kept = (scope, "kept")
    monkeypatch.setattr(mcp_tool, "_servers", {})
    monkeypatch.setattr(mcp_tool, "_server_scope_keys", {removed: scope, kept: scope})
    monkeypatch.setattr(mcp_tool, "_server_connect_errors", {removed: "bad", kept: "bad"})
    monkeypatch.setattr(mcp_tool, "_server_connect_retry_after", {removed: 1.0, kept: 2.0})
    monkeypatch.setattr(mcp_tool, "_server_connect_failures", {removed: 1, kept: 2})
    monkeypatch.setattr(lifecycle._loop, "_stop_mcp_loop", lambda **_kwargs: True)

    lifecycle.shutdown_mcp_servers(scope=scope, server_names={"removed"})

    assert removed not in mcp_tool._server_connect_errors
    assert removed not in mcp_tool._server_connect_retry_after
    assert removed not in mcp_tool._server_connect_failures
    assert mcp_tool._server_connect_errors == {kept: "bad"}
    assert mcp_tool._server_connect_retry_after == {kept: 2.0}
    assert mcp_tool._server_connect_failures == {kept: 2}


def test_scoped_shutdown_clears_only_own_same_named_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tools import mcp_tool
    from tools import mcp_tool_lifecycle as lifecycle

    scope_a = hermes_home_key(tmp_path / "profiles" / "a")
    scope_b = hermes_home_key(tmp_path / "profiles" / "b")
    key_a = (scope_a, "shared")
    key_b = (scope_b, "shared")
    monkeypatch.setattr(mcp_tool, "_servers", {})
    monkeypatch.setattr(mcp_tool, "_server_scope_keys", {})
    monkeypatch.setattr(mcp_tool, "_server_connecting", {key_a, key_b})
    monkeypatch.setattr(mcp_tool, "_server_connect_errors", {key_a: "a", key_b: "b"})
    monkeypatch.setattr(mcp_tool, "_server_connect_retry_after", {key_a: 1.0, key_b: 2.0})
    monkeypatch.setattr(mcp_tool, "_server_connect_failures", {key_a: 1, key_b: 2})
    monkeypatch.setattr(mcp_tool, "_lazy_server_configs", {key_a: {}, key_b: {}})
    monkeypatch.setattr(mcp_tool, "_lazy_server_fingerprints", {key_a: "a", key_b: "b"})
    monkeypatch.setattr(mcp_tool, "_lazy_server_tool_names", {key_a: ["a"], key_b: ["b"]})
    monkeypatch.setattr(mcp_tool, "_server_error_counts", {key_a: 1, key_b: 2})
    monkeypatch.setattr(mcp_tool, "_server_breaker_opened_at", {key_a: 1.0, key_b: 2.0})
    monkeypatch.setattr(mcp_tool, "_server_trust_levels", {key_a: "full", key_b: "untrusted"})
    monkeypatch.setattr(mcp_tool, "_tool_read_only_hints", {key_a: {}, key_b: {}})
    monkeypatch.setattr(mcp_tool, "_parallel_safe_servers", {key_a, key_b})
    monkeypatch.setattr(
        mcp_tool,
        "_mcp_tool_server_names",
        {(scope_a, "mcp__shared__a"): key_a, (scope_b, "mcp__shared__b"): key_b},
    )
    monkeypatch.setattr(lifecycle._loop, "_stop_mcp_loop", lambda **_kwargs: True)

    lifecycle.shutdown_mcp_servers(scope=scope_a)

    for mapping_name in (
        "_server_connect_errors",
        "_server_connect_retry_after",
        "_server_connect_failures",
        "_lazy_server_configs",
        "_lazy_server_fingerprints",
        "_lazy_server_tool_names",
        "_server_error_counts",
        "_server_breaker_opened_at",
        "_server_trust_levels",
        "_tool_read_only_hints",
    ):
        mapping = getattr(mcp_tool, mapping_name)
        assert key_a not in mapping
        assert key_b in mapping
    assert mcp_tool._server_connecting == {key_b}
    assert mcp_tool._parallel_safe_servers == {key_b}
    assert set(mcp_tool._mcp_tool_server_names) == {(scope_b, "mcp__shared__b")}


def test_same_named_server_policy_state_is_profile_scoped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tools import mcp_tool
    from tools import mcp_tool_discovery as discovery
    from tools import mcp_tool_registration as registration

    scope_a = hermes_home_key(tmp_path / "profiles" / "a")
    scope_b = hermes_home_key(tmp_path / "profiles" / "b")
    active_scope = {"value": scope_a}
    monkeypatch.setattr(mcp_tool, "_mcp_registry_scope", lambda: active_scope["value"])
    for name, value in (
        ("_server_trust_levels", {}),
        ("_tool_read_only_hints", {}),
        ("_server_error_counts", {}),
        ("_server_breaker_opened_at", {}),
        ("_parallel_safe_servers", set()),
        ("_servers", {}),
        ("_server_scope_keys", {}),
        ("_server_connecting", set()),
        ("_server_connect_errors", {}),
        ("_server_connect_retry_after", {}),
        ("_server_connect_failures", {}),
        ("_lazy_server_configs", {}),
    ):
        monkeypatch.setattr(mcp_tool, name, value)

    tool = SimpleNamespace(name="write", annotations=SimpleNamespace(readOnlyHint=False))
    registration._record_tool_trust_metadata("shared", {"trust": "full"}, [tool])
    mcp_tool._bump_server_error("shared")
    discovery._select_new_servers(
        {"shared": {"url": "https://a.test/mcp", "supports_parallel_tool_calls": True}}
    )

    active_scope["value"] = scope_b
    registration._record_tool_trust_metadata("shared", {"trust": "untrusted"}, [tool])
    mcp_tool._bump_server_error("shared")
    mcp_tool._bump_server_error("shared")
    discovery._select_new_servers(
        {"shared": {"url": "https://b.test/mcp", "supports_parallel_tool_calls": False}}
    )

    key_a = (scope_a, "shared")
    key_b = (scope_b, "shared")
    assert mcp_tool._server_trust_levels[key_a] == "full"
    assert mcp_tool._server_trust_levels[key_b] == "untrusted"
    assert mcp_tool._server_error_counts[key_a] == 1
    assert mcp_tool._server_error_counts[key_b] == 2
    assert key_a in mcp_tool._parallel_safe_servers
    assert key_b not in mcp_tool._parallel_safe_servers


def test_same_named_servers_are_isolated_by_profile_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tools import mcp_tool
    from tools import mcp_tool_discovery as discovery

    scope_a = hermes_home_key(tmp_path / "profiles" / "a")
    scope_b = hermes_home_key(tmp_path / "profiles" / "b")
    active_scope = {"value": scope_a}
    monkeypatch.setattr(mcp_tool, "_mcp_registry_scope", lambda: active_scope["value"])
    for name, value in (
        ("_servers", {}),
        ("_server_scope_keys", {}),
        ("_server_connect_errors", {}),
        ("_server_connect_retry_after", {}),
        ("_server_connect_failures", {}),
        ("_lazy_server_configs", {}),
        ("_parallel_safe_servers", set()),
        ("_server_connecting", set()),
    ):
        monkeypatch.setattr(mcp_tool, name, value)

    cfg_a = {"url": "https://example.test/mcp", "headers": {"Authorization": "Bearer a"}}
    cfg_b = {"url": "https://example.test/mcp", "headers": {"Authorization": "Bearer b"}}
    server_a = SimpleNamespace(session=object())
    server_b = SimpleNamespace(session=object())

    assert discovery._select_new_servers({"shared": cfg_a}) == {"shared": cfg_a}
    discovery._adopt_server("shared", server_a)
    discovery._note_connect_success("shared")

    active_scope["value"] = scope_b
    assert discovery._select_new_servers({"shared": cfg_b}) == {"shared": cfg_b}
    discovery._adopt_server("shared", server_b)
    discovery._note_connect_success("shared")

    active_scope["value"] = scope_a
    assert discovery._get_connected_server_for_call("shared") is server_a
    active_scope["value"] = scope_b
    assert discovery._get_connected_server_for_call("shared") is server_b
    assert len(mcp_tool._servers) == 2


@pytest.mark.asyncio
async def test_gateway_boot_discovers_mcp_under_every_profile_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import gateway.run as gateway_run
    from tools import mcp_tool_discovery as _mcp_discovery

    homes = [("default", tmp_path / "default"), ("worker", tmp_path / "worker")]
    for _name, home in homes:
        home.mkdir()
    seen: list[tuple[Path, str]] = []

    def fake_discover() -> list[str]:
        seen.append((get_hermes_home(), threading.current_thread().name))
        return []

    monkeypatch.setattr(
        "hermes_cli.profiles.profiles_to_serve",
        lambda multiplex, profile_allowlist=None: homes,
    )
    monkeypatch.setattr(_mcp_discovery, "discover_mcp_tools", fake_discover)

    await gateway_run._discover_gateway_mcp_tools(GatewayConfig(multiplex_profiles=True))

    # Ran once per profile, under that profile's home, off the loop thread.
    assert [home for home, _ in seen] == [home for _, home in homes]
    assert all(thread != threading.current_thread().name for _, thread in seen)


@pytest.mark.asyncio
async def test_reload_mcp_only_touches_requesting_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gateway.run import GatewayRunner
    from tools import mcp_tool
    from tools import mcp_tool_discovery as _mcp_discovery
    from tools import mcp_tool_lifecycle as _mcp_lifecycle

    worker_home = tmp_path / "profiles" / "worker"
    worker_home.mkdir(parents=True)
    worker_scope = hermes_home_key(worker_home)

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner._resolve_profile_home_for_source = MagicMock(return_value=worker_home)
    runner._agent_cache = {}
    runner._agent_cache_lock = None
    runner._async_session_store = SimpleNamespace(
        get_or_create_session=MagicMock(side_effect=RuntimeError("skip transcript")),
    )

    monkeypatch.setattr(mcp_tool, "_servers", {"default-srv": object(), "worker-srv": object()})
    monkeypatch.setattr(
        mcp_tool, "_server_scope_keys",
        {"default-srv": hermes_home_key(tmp_path), "worker-srv": worker_scope},
    )
    seen: list[tuple] = []

    def fake_shutdown(*, scope=None) -> None:
        seen.append(("shutdown", scope, get_hermes_home()))

    def fake_discover() -> list[str]:
        seen.append(("discover", get_hermes_home()))
        return []

    monkeypatch.setattr(_mcp_lifecycle, "shutdown_mcp_servers", fake_shutdown)
    monkeypatch.setattr(_mcp_discovery, "discover_mcp_tools", fake_discover)

    event = MessageEvent(
        text="/reload-mcp", message_id="m1",
        source=SessionSource(
            platform=Platform.TELEGRAM, user_id="u1", chat_id="c1",
            chat_type="dm", profile="worker",
        ),
    )
    result = await runner._execute_mcp_reload(event)

    # Entered worker's scope itself, shut down only worker's servers, and
    # reported only worker's servers (default's untouched connection is not
    # "removed").
    assert seen == [
        ("shutdown", worker_scope, worker_home),
        ("discover", worker_home),
    ]
    assert "default-srv" not in result


def test_scoped_mcp_deregister_keeps_alias_owned_by_other_profile() -> None:
    from tools.registry import ToolRegistry

    reg = ToolRegistry()
    schema = {"name": "mcp__shared__echo", "description": "d"}
    for scope in ("/home/p1", "/home/p2"):
        reg.register(
            "mcp__shared__echo",
            "mcp-shared",
            schema,
            lambda **_kwargs: None,
            scope=scope,
        )
    reg.register_toolset_alias("shared", "mcp-shared")

    reg.deregister("mcp__shared__echo", scope="/home/p1")

    assert reg.get_toolset_alias_target("shared") == "mcp-shared"
    assert reg.snapshot_registration("mcp__shared__echo", scope="/home/p2") is not None


def test_deregister_scope_kwarg_targets_overlay_and_keeps_plugin_confinement() -> None:
    from tools.registry import ToolRegistry

    reg = ToolRegistry()
    reg.register("mcp__s__t", "mcp-s", {"name": "mcp__s__t", "description": "d"},
                 lambda **kw: None, scope="/home/p1")
    assert reg.snapshot_registration("mcp__s__t", scope="/home/p1") is not None

    reg.deregister("mcp__s__t")  # unscoped: global slot only, overlay untouched
    assert reg.snapshot_registration("mcp__s__t", scope="/home/p1") is not None

    reg.deregister("mcp__s__t", scope="/home/p1")
    assert reg.snapshot_registration("mcp__s__t", scope="/home/p1") is None

    # A plugin module may not name another profile's overlay.
    reg._plugin_module_scopes["hermes_plugins.p"] = {"/home/p1"}
    reg._caller_module = staticmethod(lambda: "hermes_plugins.p")
    with pytest.raises(PermissionError):
        reg.deregister("anything", scope="/home/p2")
