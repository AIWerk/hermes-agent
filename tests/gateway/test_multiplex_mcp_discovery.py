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


def test_registered_handlers_resolve_the_invoking_profile_state_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools import mcp_tool_handlers as handlers
    from tools.mcp_tool_scope import _server_key

    homes = [tmp_path / "profiles" / name for name in ("a", "b")]
    for home in homes:
        home.mkdir(parents=True)
    monkeypatch.setattr("agent.secret_scope.is_multiplex_active", lambda: True)
    seen = []

    def capture(server_name, _timeout):
        seen.append(_server_key(server_name))
        return None, "not-connected"

    monkeypatch.setattr(handlers, "_acquire_call_server", capture)
    handler = handlers._make_tool_handler("shared", "read", 1.0)
    expected = []
    for home in homes:
        token = set_hermes_home_override(home)
        try:
            expected.append((hermes_home_key(home), "shared"))
            assert handler({}) == "not-connected"
        finally:
            reset_hermes_home_override(token)
    assert seen == expected


def test_profile_secondary_mcp_files_are_not_first_profile_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools import mcp_tool_config as config
    from tools import mcp_tool_loop as loop

    homes = [tmp_path / "a", tmp_path / "b"]
    for home in homes:
        home.mkdir()
    monkeypatch.setattr(config, "_mcp_stderr_log_fh", {})

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
    for handle in config._mcp_stderr_log_fh.values():
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
        lambda *, scope=None, names=None: shutdown_calls.append(
            (scope, names)
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
        lambda *, scope=None, names=None: calls.append((scope, names)),
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
        lambda *, scope=None, names=None: calls.append((scope, names)),
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

    lifecycle.shutdown_mcp_servers(scope=scope, names={"removed"})

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

    lifecycle.shutdown_mcp_servers(scope=scope, names={"removed"})

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
        {"mcp__shared__a": "shared", "mcp__shared__b": "shared"},
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
    # Provenance is plain tool->server in the upstream registry contract and remains
    # while another scoped connection with the same server name is live.
    assert set(mcp_tool._mcp_tool_server_names) == {
        "mcp__shared__a", "mcp__shared__b"
    }


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
        lambda multiplex: homes,
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

@pytest.mark.asyncio
async def test_reload_mcp_formats_scoped_connection_keys_before_refreshing_cached_agents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Connection-ledger tuple keys are internal; reload reports server names and completes refresh."""
    from gateway.run import GatewayRunner
    from tools import mcp_tool
    from tools import mcp_tool_discovery as _mcp_discovery
    from tools import mcp_tool_lifecycle as _mcp_lifecycle

    launch_scope = hermes_home_key(tmp_path / "default")
    worker_home = tmp_path / "profiles" / "worker"
    worker_home.mkdir(parents=True)
    worker_scope = hermes_home_key(worker_home)
    launch_key = (launch_scope, "default-srv")
    worker_key = (worker_scope, "worker-srv")

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner._resolve_profile_home_for_source = MagicMock(return_value=worker_home)
    runner._mcp_reload_refresh_cached_agents = MagicMock()
    runner._async_session_store = SimpleNamespace(
        get_or_create_session=MagicMock(side_effect=RuntimeError("skip transcript")),
    )

    monkeypatch.setattr(mcp_tool, "_servers", {launch_key: object(), worker_key: object()})
    monkeypatch.setattr(
        mcp_tool, "_server_scope_keys",
        {launch_key: launch_scope, worker_key: worker_scope},
    )
    monkeypatch.setattr(_mcp_lifecycle, "shutdown_mcp_servers", lambda **_kwargs: None)
    monkeypatch.setattr(_mcp_discovery, "discover_mcp_tools", lambda: [])

    event = MessageEvent(
        text="/reload-mcp", message_id="m1",
        source=SessionSource(
            platform=Platform.TELEGRAM, user_id="u1", chat_id="c1",
            chat_type="dm", profile="worker",
        ),
    )
    result = await runner._execute_mcp_reload(event)

    assert "MCP reload failed" not in result
    assert "worker-srv" in result
    assert "default-srv" not in result
    runner._mcp_reload_refresh_cached_agents.assert_called_once_with(True, "worker")


@pytest.mark.asyncio
async def test_reload_mcp_reports_a_shared_server_to_a_non_owner_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shared connection remains visible when its peer profile reloads MCP."""
    from gateway.run import GatewayRunner
    from tools import mcp_tool
    from tools import mcp_tool_discovery as _mcp_discovery
    from tools import mcp_tool_lifecycle as _mcp_lifecycle

    worker_home = tmp_path / "profiles" / "worker"
    worker_home.mkdir(parents=True)
    worker_scope = hermes_home_key(worker_home)
    launch_scope = hermes_home_key(tmp_path / "default")

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner._resolve_profile_home_for_source = MagicMock(return_value=worker_home)
    runner._agent_cache = {}
    runner._agent_cache_lock = None
    runner._async_session_store = SimpleNamespace(
        get_or_create_session=MagicMock(side_effect=RuntimeError("skip transcript")),
    )

    live_server = SimpleNamespace(session=object(), _config={}, _tools=[], tool_timeout=30,
                                  initialize_result=None, _registered_tool_names=[])
    monkeypatch.setattr(mcp_tool, "_servers", {"shared": live_server})
    monkeypatch.setattr(mcp_tool, "_server_scope_keys", {"shared": launch_scope})
    monkeypatch.setattr(mcp_tool, "_server_tool_scopes", {"shared": {launch_scope}}, raising=False)
    monkeypatch.setattr(mcp_tool, "_server_connecting", set())
    monkeypatch.setattr(mcp_tool, "_server_connect_errors", {})
    monkeypatch.setattr(mcp_tool, "_lazy_server_configs", {})
    monkeypatch.setattr(mcp_tool, "_mcp_registry_scope", lambda: worker_scope)

    def fake_discover() -> list[str]:
        from tools import mcp_tool_registration as _mcp_registration
        _mcp_registration.register_connected_into_current_scope({"shared": {}})
        return ["mcp__shared__tool"]

    monkeypatch.setattr(_mcp_lifecycle, "shutdown_mcp_servers", lambda **_kwargs: None)
    monkeypatch.setattr(_mcp_discovery, "discover_mcp_tools", fake_discover)

    event = MessageEvent(
        text="/reload-mcp", message_id="m1",
        source=SessionSource(
            platform=Platform.TELEGRAM, user_id="u1", chat_id="c1",
            chat_type="dm", profile="worker",
        ),
    )
    result = await runner._execute_mcp_reload(event)

    assert "No MCP servers connected." not in result
    assert "shared" in result
    assert mcp_tool._server_scope_keys["shared"] == launch_scope
    assert mcp_tool._server_tool_scopes["shared"] == {launch_scope, worker_scope}


@pytest.mark.parametrize("worker_cfg", [
    {"url": "https://worker.example/mcp"},                                   # different route
    {"url": "https://default.example/mcp", "headers": {"Authorization": "Bearer worker"}},  # same route, other credentials
    {"url": "https://default.example/mcp", "env": {"API_TOKEN": "worker"}},
])
def test_scope_visibility_rejects_a_foreign_or_differently_authenticated_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, worker_cfg: dict
) -> None:
    """A profile may only see a live connection whose route AND credentials match its own config;
    otherwise it would call tools as the owning profile's identity."""
    from tools import mcp_tool
    from tools import mcp_tool_registration as _mcp_registration

    worker_scope = hermes_home_key(tmp_path / "worker")
    launch_scope = hermes_home_key(tmp_path / "default")
    live_server = SimpleNamespace(session=object(), _config={"url": "https://default.example/mcp"})
    monkeypatch.setattr(mcp_tool, "_servers", {"shared": live_server})
    monkeypatch.setattr(mcp_tool, "_server_scope_keys", {"shared": launch_scope})
    monkeypatch.setattr(mcp_tool, "_server_tool_scopes", {"shared": {launch_scope}}, raising=False)
    monkeypatch.setattr(mcp_tool, "_mcp_registry_scope", lambda: worker_scope)

    assert _mcp_registration.register_connected_into_current_scope({"shared": worker_cfg}) == 0
    assert mcp_tool._server_tool_scopes["shared"] == {launch_scope}


def test_shared_server_tools_are_callable_and_removed_on_non_owner_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent.secret_scope import set_multiplex_active
    from hermes_constants import (
        hermes_home_key,
        reset_hermes_home_override,
        set_hermes_home_override,
    )
    from tools import mcp_tool
    from tools import mcp_tool_config as _mcp_config
    from tools import mcp_tool_discovery as _mcp_discovery
    from tools.registry import registry

    worker_home = tmp_path / "profiles" / "worker"
    launch_home = tmp_path / "default"
    worker_home.mkdir(parents=True)
    launch_home.mkdir()
    worker_token = set_hermes_home_override(worker_home)
    previous_multiplex = set_multiplex_active(True)
    worker_scope = hermes_home_key()
    launch_scope = hermes_home_key(launch_home)
    tool = SimpleNamespace(
        name="echo",
        description="Echo a value",
        inputSchema={"type": "object", "properties": {}},
        annotations=None,
    )
    server = SimpleNamespace(
        name="shared",
        session=object(),
        _tools=[tool],
        tool_timeout=30,
        _registered_tool_names=[],
        _config={},
        initialize_result=None,
    )
    owner_tool_name = "mcp__shared__echo"
    registry.register(
        owner_tool_name,
        "mcp-shared",
        {"name": owner_tool_name, "description": "Echo a value", "type": "object"},
        lambda **_kwargs: None,
        scope=launch_scope,
    )
    registry.register_toolset_alias("shared", "mcp-shared")
    server._registered_tool_names = [owner_tool_name]
    with mcp_tool._lock:
        saved = {
            "_servers": dict(mcp_tool._servers),
            "_server_scope_keys": dict(mcp_tool._server_scope_keys),
            "_server_tool_scopes": dict(mcp_tool._server_tool_scopes),
            "_mcp_tool_server_names": dict(mcp_tool._mcp_tool_server_names),
        }
        mcp_tool._servers.clear()
        mcp_tool._server_scope_keys.clear()
        mcp_tool._server_tool_scopes.clear()
        mcp_tool._mcp_tool_server_names.clear()
        mcp_tool._servers["shared"] = server
        mcp_tool._server_scope_keys["shared"] = launch_scope
        mcp_tool._server_tool_scopes["shared"] = {launch_scope}

    try:
        monkeypatch.setattr(mcp_tool, "_ensure_mcp_sdk", lambda: True)
        monkeypatch.setattr(_mcp_config, "_filter_suspicious_mcp_servers", lambda servers: servers)
        assert _mcp_discovery.register_mcp_servers({"shared": {}})
        tool_names = registry.get_tool_names_for_toolset("mcp-shared")
        assert tool_names
        assert callable(registry.get_entry(tool_names[0]).handler)

        # Changing the worker route removes only the worker overlay; the shared
        # live connection and launch owner remain intact.
        assert _mcp_discovery.register_mcp_servers(
            {"shared": {"url": "https://worker.example/mcp"}}
        ) == []
        assert registry.get_tool_names_for_toolset("mcp-shared") == []
        with mcp_tool._lock:
            assert mcp_tool._server_scope_keys["shared"] == launch_scope
            assert mcp_tool._server_tool_scopes["shared"] == {launch_scope}
            assert mcp_tool._servers["shared"] is server
        assert registry.snapshot_registration(owner_tool_name, scope=launch_scope) is not None
        assert registry.get_toolset_alias_target("shared") == "mcp-shared"

        # Removing the server from the worker config has the same scoped cleanup.
        assert _mcp_discovery.register_mcp_servers({}) == []
        assert registry.get_tool_names_for_toolset("mcp-shared") == []
        with mcp_tool._lock:
            assert mcp_tool._server_scope_keys["shared"] == launch_scope
            assert mcp_tool._server_tool_scopes["shared"] == {launch_scope}
            assert mcp_tool._servers["shared"] is server
    finally:
        for tool_name in list(registry.get_tool_names_for_toolset("mcp-shared")):
            registry.deregister(tool_name, scope=worker_scope)
        registry.deregister(owner_tool_name, scope=launch_scope)
        with mcp_tool._lock:
            for name, value in saved.items():
                target = getattr(mcp_tool, name)
                target.clear()
                target.update(value)
        set_multiplex_active(previous_multiplex)
        reset_hermes_home_override(worker_token)


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
