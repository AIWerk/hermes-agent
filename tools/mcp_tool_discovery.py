"""Connecting and discovery for tools.mcp_tool: per-server connect cooldown, connect /
lazy-start / recycled-stdio wake-up, ``register_mcp_servers`` / ``discover_mcp_tools`` and
the status / probe public API. Origin state (``_servers``, ``_lock``, the loop, patchable
helpers) is read through ``_core`` so ``mock.patch("tools.mcp_tool.X")`` keeps working."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Tuple
from tools.mcp_tool_common import _core, _parse_boolish
from tools import mcp_tool_config as _config
from tools import mcp_tool_errors as _errors
from tools import mcp_tool_lifecycle as _lifecycle
from tools import mcp_tool_loop as _loop
from tools import mcp_tool_registration as _registration
from tools.mcp_tool_schema import MCP_TOOL_NAME_PREFIX

logger = logging.getLogger("tools.mcp_tool")


class _MCPConfigRetiredDuringConnect(RuntimeError):
    """The config authority disappeared while its connection was in flight."""


def _record_connect_failure(server_name: str) -> None:
    """Stamp a geometric, capped retry cooldown after a failed connect (under ``_lock``)."""
    key = _core._server_state_key(server_name)
    n = _core._server_connect_failures.get(key, 0) + 1
    _core._server_connect_failures[key] = n
    backoff = min(_core._CONNECT_RETRY_BASE_BACKOFF_SEC * (2 ** (n - 1)), _core._CONNECT_RETRY_MAX_BACKOFF_SEC)
    _core._server_connect_retry_after[key] = time.monotonic() + backoff


def _clear_connect_failure(server_name: str) -> None:
    """Clear the connect-cooldown state after a successful connection."""
    key = _core._server_state_key(server_name)
    _core._server_connect_failures.pop(key, None)
    _core._server_connect_retry_after.pop(key, None)


def _connect_cooldown_active(server_name: str) -> bool:
    """True if ``server_name`` is still within its retry cooldown."""
    deadline = _core._server_connect_retry_after.get(_core._server_state_key(server_name))
    return deadline is not None and time.monotonic() < deadline


def _enabled(cfg: dict) -> bool:
    return _parse_boolish(cfg.get("enabled", True), default=True)


async def _connect_server(name: str, config: dict) -> _core.MCPServerTask:
    """Create an MCPServerTask, start it, return once ready (tear down with ``server.shutdown()``
    on the same loop). Raises on bad config, missing HTTP support or connect failure."""
    scope = _core._mcp_registry_scope()
    server = _core.MCPServerTask(name)
    server.registry_scope = scope
    server.state_key = _core._server_state_key(name, scope)
    claim = _core._connect_server_claim.get()
    server._discovery_claimed = claim is not None
    if claim is not None:
        with _core._lock:
            _core._server_connect_claims[server.state_key] = server
        claim(server)
    # The run task copies this context: don't retain the discovery closure for its life.
    claim_token = _core._connect_server_claim.set(None) if claim is not None else None
    try:
        await server.start(config)
    except asyncio.CancelledError:
        raise  # start() already reaps server._task; shutdown() here could swallow the cancel
    except BaseException:
        # Discovery owns claimed tasks (recoverable park); standalone probes must reap locally.
        if claim is None:
            try:
                await server.shutdown()
            except Exception as shutdown_exc:  # noqa: BLE001 -- best-effort reap, don't mask the real error
                logger.debug("MCP server '%s' shutdown during orphan-reap failed: %s", name, shutdown_exc)
        raise
    finally:
        if claim_token is not None:
            _core._connect_server_claim.reset(claim_token)
    return server


def _request_lazy_reconnect(server_name: str, server: _core.MCPServerTask) -> bool:
    """Wake a recycled stdio server and wait briefly for a fresh session."""
    loop = _loop._running_loop() if server._is_recycled_stdio() else None
    if loop is None:
        return False

    def _wake() -> None:
        server._ready.clear()
        server._reconnect_event.set()

    loop.call_soon_threadsafe(_wake)

    async def _await_ready() -> bool:
        deadline = time.monotonic() + _core._RECYCLED_RECONNECT_TIMEOUT
        while time.monotonic() < deadline:
            if server.session is not None and server._ready.is_set():
                return True
            await asyncio.sleep(0.05)
        return False

    try:
        return bool(_loop._run_on_mcp_loop(_await_ready, timeout=_core._RECYCLED_RECONNECT_TIMEOUT))
    except Exception as exc:
        logger.warning("MCP server '%s': lazy reconnect after stdio recycle failed: %s", server_name, exc)
        return False


def _resolve_server_lazy(name: str, config: dict) -> bool:
    """True when ``mcp_servers.<name>.lazy`` defers connect to first tool use (default off).

    Gated per-server by ``mcp_servers.<name>.lazy`` in config (default OFF), following the same per-server
    key pattern as ``idle_timeout_seconds``. Design from #56832 (Vansh5632).
    """
    return _parse_boolish(config.get("lazy", False), default=False)


def _note_connect_failure(name: str, exc: BaseException) -> str:
    """Record a failed connect (under ``_lock``): error text for status, cooldown stamp."""
    message = _errors._format_connect_error(exc)
    key = _core._server_state_key(name)
    with _core._lock:
        _core._server_connecting.discard(key)
        _core._server_connect_errors[key] = message
        _record_connect_failure(name)
    return message


def _note_connect_success(name: str) -> None:
    """Clear connecting/error/cooldown state after a successful connect (under ``_lock``)."""
    key = _core._server_state_key(name)
    with _core._lock:
        _core._server_connecting.discard(key)
        _core._server_connect_errors.pop(key, None)
        _clear_connect_failure(name)


def _adopt_server(name: str, server: Any) -> bool:
    """Publish *server* into ``_servers`` with its owning registry scope (under ``_lock``)."""
    scope = getattr(server, "registry_scope", _core._mcp_registry_scope())
    key = getattr(server, "state_key", _core._server_state_key(name, scope))
    with _core._lock:
        if (
            getattr(server, "_discovery_claimed", False)
            and _core._server_connect_claims.get(key) is not server
        ):
            return False
        _core._server_connect_claims.pop(key, None)
        _core._servers[key] = server
        _core._server_scope_keys[key] = scope
    return True


def _ensure_lazy_server_connected(server_name: str) -> bool:
    """Connect a lazily-registered server on demand (sync; blocks). Honours the cooldown and the
    ``_server_connecting`` dedup set; routes through ``_discover_and_register_server`` so
    park/recycle/cooldown bookkeeping stays in one place. True when a live session exists.

    See #50394.
    """
    key = _core._server_state_key(server_name)
    with _core._lock:
        server = _core._servers.get(key)
        if server is not None and server.session is not None:
            return True
        config = _core._lazy_server_configs.get(key)
        if (not config or _connect_cooldown_active(server_name)
                or key in _core._server_connecting):
            return False
        _core._server_connecting.add(key)
        _core._server_connect_errors.pop(key, None)
    logger.info("MCP server '%s': lazy start on first use", server_name)
    _loop._ensure_mcp_loop()
    connect_timeout = config.get("connect_timeout", _core._DEFAULT_CONNECT_TIMEOUT)
    try:
        _loop._run_on_mcp_loop(lambda: _discover_and_register_server(server_name, config),
                               timeout=float(connect_timeout) + 30.0)
    except BaseException as exc:
        if isinstance(exc, _MCPConfigRetiredDuringConnect):
            with _core._lock:
                _core._server_connecting.discard(key)
                _core._server_connect_errors.pop(key, None)
            return False
        logger.warning("Lazy MCP connect failed for '%s': %s", server_name, _note_connect_failure(server_name, exc))
        return False
    _note_connect_success(server_name)
    with _core._lock:
        _core._lazy_server_configs.pop(key, None)
        stale_fingerprint = _core._lazy_server_fingerprints.pop(key, None)
        cached_names = _core._lazy_server_tool_names.pop(key, None) or []
        server = _core._servers.get(key)
        live_names = set(getattr(server, "_registered_tool_names", []) or [])
    # The cached manifest may advertise tools the live server no longer serves.
    phantom_names = [n for n in cached_names if n not in live_names]
    if phantom_names:
        from tools.registry import registry
        for tool_name in phantom_names:
            registry.deregister(
                tool_name,
                scope=getattr(server, "registry_scope", _core._server_registry_scope(server_name)),
            )
            _registration._forget_mcp_tool_server(
                tool_name, state_key=getattr(server, "state_key", key)
            )
        logger.info("MCP server '%s': deregistered %d phantom cached tool(s) not served live (stale schema-cache "
                    "fingerprint %s): %s", server_name, len(phantom_names), stale_fingerprint, ", ".join(phantom_names))
    return server is not None and server.session is not None


def _get_connected_server_for_call(server_name: str) -> Optional[_core.MCPServerTask]:
    """Return a connected server; the single first-use connect point for lazy servers and
    the wake-up point for recycled stdio ones.

    Also the single first-use connect point for lazy (schema-cache registered) servers, so raw tool calls
    AND the resource/prompt utility handlers all trigger the deferred spawn (#56832).
    """
    key = _core._server_state_key(server_name)
    with _core._lock:
        server = _core._servers.get(key)
        is_lazy = key in _core._lazy_server_configs
    if is_lazy and (server is None or server.session is None):
        _ensure_lazy_server_connected(server_name)
    elif server is not None and server.session is None and server._is_recycled_stdio():
        _request_lazy_reconnect(server_name, server)
    else:
        return server
    with _core._lock:
        return _core._servers.get(key)


async def _discover_and_register_server_scoped(name: str, config: dict) -> List[str]:
    """Connect one server, register its tools; return the registered names."""
    # The claim fires inside _connect_server while this frame is suspended (list, not nonlocal).
    claimed: List[_core.MCPServerTask] = []
    claim_token = _core._connect_server_claim.set(claimed.append)
    try:
        server = await asyncio.wait_for(_connect_server(name, config),
                                        timeout=config.get("connect_timeout", _core._DEFAULT_CONNECT_TIMEOUT))
    except BaseException:
        server = claimed[0] if claimed else None
        task = server._task if server is not None else None
        task_cancelling = task.cancelling() if task is not None and hasattr(task, "cancelling") else 0
        if (server is not None and server._error is not None and task is not None
                and not task.done() and not task_cancelling):
            # Recoverable park: the run task self-probes, so adopt it for shutdown/revival.
            if not _adopt_server(name, server):
                await server.shutdown()
                raise _MCPConfigRetiredDuringConnect(
                    f"MCP server '{name}' configuration was removed while connecting"
                ) from None
        elif server is not None:
            key = getattr(server, "state_key", _core._server_state_key(name))
            with _core._lock:
                if _core._server_connect_claims.get(key) is server:
                    _core._server_connect_claims.pop(key, None)
            await server.shutdown()
        raise
    finally:
        _core._connect_server_claim.reset(claim_token)
    key = _core._server_state_key(name)
    with _core._lock:
        _core._server_connecting.discard(key)
        _core._server_connect_errors.pop(key, None)
    if not _adopt_server(name, server):
        await server.shutdown()
        raise _MCPConfigRetiredDuringConnect(
            f"MCP server '{name}' configuration was removed while connecting"
        )
    registered_names = _registration._register_server_tools(name, server, config)
    server._registered_tool_names = list(registered_names)
    logger.info("MCP server '%s' (%s): registered %d tool(s): %s", name,
                "HTTP" if "url" in config else "stdio", len(registered_names), ", ".join(registered_names))
    return registered_names


async def _discover_and_register_server(name: str, config: dict) -> List[str]:
    """Bind the immutable profile state key across the complete async discovery task."""
    key = _core._server_state_key(name)
    token = _core._server_state_key_override.set((name, key))
    try:
        return await _discover_and_register_server_scoped(name, config)
    finally:
        _core._server_state_key_override.reset(token)


def _select_new_servers(servers: Dict[str, dict]) -> Dict[str, dict]:
    """Pick connect candidates (enabled, not connected/connecting/lazy, not in backoff) and
    refresh per-server bookkeeping. Known servers without a live session are parked or
    mid-reconnect with tools deregistered, so nothing else can nudge them: signal a reconnect."""
    scope = _core._mcp_registry_scope()
    keys = {name: _core._server_state_key(name, scope) for name in servers}
    from tools.mcp_schema_cache import config_fingerprint
    current_fingerprints = {name: config_fingerprint(cfg) for name, cfg in servers.items()}
    stale_lazy_tools: list[tuple[str, Any, list[str]]] = []
    with _core._lock:
        for name, key in keys.items():
            if (
                key in _core._lazy_server_configs
                and (
                    not _enabled(servers[name])
                    or _core._lazy_server_fingerprints.get(key) != current_fingerprints[name]
                )
            ):
                stale_lazy_tools.append(
                    (name, key, list(_core._lazy_server_tool_names.get(key, [])))
                )
                _core._lazy_server_configs.pop(key, None)
                _core._lazy_server_fingerprints.pop(key, None)
                _core._lazy_server_tool_names.pop(key, None)
                _core._server_connect_errors.pop(key, None)
                _core._server_connect_retry_after.pop(key, None)
                _core._server_connect_failures.pop(key, None)
                _core._server_error_counts.pop(key, None)
                _core._server_breaker_opened_at.pop(key, None)
                _core._server_trust_levels.pop(key, None)
                _core._tool_read_only_hints.pop(key, None)
        connecting = set(_core._server_connecting)
        # Only deduplicate inside the current profile scope. Two profiles may
        # intentionally use the same logical server name with different authority.
        new_servers = {
            name: cfg
            for name, cfg in servers.items()
            if keys[name] not in _core._servers
            and keys[name] not in connecting
            and keys[name] not in _core._lazy_server_configs
            and _enabled(cfg)
            and not _connect_cooldown_active(name)
        }
        stale_cached = [
            _core._servers[keys[name]]
            for name in servers
            if keys[name] in _core._servers
            and getattr(_core._servers[keys[name]], "session", None) is None
        ]
        _core._server_connecting.update(keys[name] for name in new_servers)
        for name in new_servers:
            key = keys[name]
            _core._server_scope_keys[key] = scope
            _core._server_connect_errors.pop(key, None)
        # Track which servers opt-in to parallel tool calls (idempotent).
        for name, cfg in servers.items():
            key = keys[name]
            if _parse_boolish(cfg.get("supports_parallel_tool_calls", False), default=False):
                _core._parallel_safe_servers.add(key)
            else:
                _core._parallel_safe_servers.discard(key)
    if stale_lazy_tools:
        from tools.registry import registry
        for name, key, tool_names in stale_lazy_tools:
            for tool_name in tool_names:
                entry = registry.get_entry(tool_name, scope=scope)
                if entry is not None and entry.toolset == f"mcp-{name}":
                    registry.deregister(tool_name, scope=scope)
                _registration._forget_mcp_tool_server(tool_name, state_key=key)
    for srv in stale_cached:
        _loop._signal_reconnect(srv)
    return new_servers


def _retire_removed_runtime_servers(configured_names: set[str]) -> None:
    """Reconcile every current-profile runtime key against the complete config."""
    scope = _core._mcp_registry_scope()
    with _core._lock:
        state_keys = (
            set(_core._servers)
            | set(_core._server_scope_keys)
            | set(_core._server_connecting)
            | set(_core._server_connect_errors)
            | set(_core._server_connect_retry_after)
            | set(_core._server_connect_failures)
            | set(_core._lazy_server_configs)
            | set(_core._lazy_server_fingerprints)
            | set(_core._lazy_server_tool_names)
            | set(_core._server_error_counts)
            | set(_core._server_breaker_opened_at)
            | set(_core._server_trust_levels)
            | set(_core._tool_read_only_hints)
            | set(_core._parallel_safe_servers)
            | set(_core._server_connect_claims)
            | set(_core._mcp_tool_server_names.values())
        )
        removed_names = {
            _core._server_name_from_state_key(key)
            for key in state_keys
            if _core._server_scope_from_state_key(key) == scope
            and _core._server_name_from_state_key(key) not in configured_names
        }
    if removed_names:
        _lifecycle.shutdown_mcp_servers(scope=scope, server_names=removed_names)


def _register_lazy_from_cache(new_servers: Dict[str, dict]) -> Tuple[Dict[str, dict], int, int]:
    """Register ``lazy: true`` servers from a valid schema-cache entry without connecting
    (missing/stale entry or failed registration -> eager). Returns (eager servers, lazy tool
    count, lazy server count)."""
    # A missing or stale cache entry falls back to the normal eager connect below (which write-through
    # refreshes the cache for next time). See #56832.
    eager_servers: Dict[str, dict] = dict(new_servers)
    lazy_registered = 0
    lazy_server_count = 0
    try:
        from tools.mcp_schema_cache import config_fingerprint, get_cached_entry
    except Exception:  # pragma: no cover - cache module missing
        return eager_servers, 0, 0
    for name, cfg in new_servers.items():
        if not _resolve_server_lazy(name, cfg):
            continue
        entry = get_cached_entry(name, config_fingerprint(cfg))
        if not entry:
            continue
        key = _core._server_state_key(name)
        with _core._lock:
            _core._server_connecting.discard(key)
        try:
            names = _registration._register_from_cache_sync(name, cfg, entry)
        except Exception as exc:
            logger.warning("Failed lazy MCP registration for '%s': %s", name, exc)
            with _core._lock:
                _core._server_connecting.add(key)
            continue
        if not names:
            # A valid cache entry can still become empty under the current
            # filters/policy. Keep the server eager so a resource-only or
            # prompt-only server is not silently parked forever.
            with _core._lock:
                _core._server_connecting.add(key)
            continue
        eager_servers.pop(name, None)
        lazy_registered += len(names)
        lazy_server_count += 1
    return eager_servers, lazy_registered, lazy_server_count


async def _discover_all(new_servers: Dict[str, dict]) -> None:
    """Connect every candidate concurrently; record per-server outcome."""
    results = await asyncio.gather(
        *(_discover_and_register_server(name, cfg) for name, cfg in new_servers.items()),
        return_exceptions=True)
    for name, result in zip(new_servers, results):
        if isinstance(result, BaseException):
            if isinstance(result, _MCPConfigRetiredDuringConnect):
                key = _core._server_state_key(name)
                with _core._lock:
                    _core._server_connecting.discard(key)
                    _core._server_connect_errors.pop(key, None)
                continue
            command = new_servers.get(name, {}).get("command")
            message = _note_connect_failure(name, result)
            logger.warning("Failed to connect to MCP server '%s'%s: %s",
                           name, f" (command={command})" if command else "", message)
        else:
            _note_connect_success(name)


def _run_discovery_pass(new_servers: Dict[str, dict]) -> None:
    """Run ``_discover_all`` on the MCP loop with the interrupt flag parked; clean up
    ``_server_connecting`` when the pass dies early."""
    # Executor threads are reused: a prior session's stale interrupt must not cancel this pass.
    from tools.interrupt import is_interrupted as _is_interrupted, set_interrupt as _set_interrupt
    _was_interrupted = _is_interrupted()
    if _was_interrupted:
        _set_interrupt(False)
    try:
        _loop._run_on_mcp_loop(lambda: _discover_all(new_servers), timeout=120)
    except (TimeoutError, InterruptedError) as _e:
        # Stranded _server_connecting entries would block future reconnects.
        how = "timed out" if isinstance(_e, TimeoutError) else "interrupted"
        keys = {name: _core._server_state_key(name) for name in new_servers}
        with _core._lock:
            stale = [name for name, key in keys.items() if key in _core._server_connecting]
            if stale:
                logger.warning("MCP discovery %s while %d server(s) were still connecting; clearing stale "
                               "connecting set: %s", how, len(stale), ", ".join(stale))
                _core._server_connecting.difference_update(keys[name] for name in stale)
                for name in stale:
                    _core._server_connect_errors.setdefault(
                        keys[name], f"Connection attempt {how} during discovery"
                    )
        raise
    finally:
        if _was_interrupted:
            _set_interrupt(True)


def _connected_summary(names, *, lazy_tools: int = 0, lazy_servers: int = 0) -> Tuple[int, int, int]:
    """(tool count, connected count, failed count) for candidate names, plus lazy servers."""
    keys = {name: _core._server_state_key(name) for name in names}
    with _core._lock:
        connected = [
            name for name, key in keys.items()
            if key in _core._servers and key not in _core._server_connect_errors
        ]
        tool_count = sum(
            len(getattr(_core._servers[keys[name]], "_registered_tool_names", []))
            for name in connected
        )
    failed = len(names) - len(connected)
    return tool_count + lazy_tools, len(connected) + lazy_servers, failed


def _log_summary(prefix: str, names, **lazy) -> None:
    """Log ``<prefix> N tool(s) from M server(s) (K failed)`` when anything happened."""
    new_tool_count, connected_count, failed = _connected_summary(names, **lazy)
    if new_tool_count or failed:
        summary = f"{prefix} {new_tool_count} tool(s) from {connected_count} server(s)"
        if failed:
            summary += f" ({failed} failed)"
        logger.info(summary)


def register_mcp_servers(servers: Dict[str, dict]) -> List[str]:
    """Connect ``{name: config}`` servers and register their tools; idempotent for connected
    names, ``enabled: false`` skipped without disconnecting. Returns every MCP tool name."""
    if not _core._ensure_mcp_sdk():
        logger.debug("MCP SDK not available -- skipping explicit MCP registration")
        return []
    servers = _config._filter_suspicious_mcp_servers(servers)
    if not servers:
        logger.debug("No explicit MCP servers provided")
        return []
    new_servers = _select_new_servers(servers)
    if not new_servers:
        return _registration._existing_tool_names()
    new_servers, lazy_registered, lazy_server_count = _register_lazy_from_cache(new_servers)
    if not new_servers:
        if lazy_registered:
            logger.info("MCP: registered %d lazy tool(s) from schema cache (no processes spawned)",
                        lazy_registered)
        return _registration._existing_tool_names()
    _loop._ensure_mcp_loop()
    _run_discovery_pass(new_servers)
    _log_summary("MCP: registered", new_servers, lazy_tools=lazy_registered, lazy_servers=lazy_server_count)
    return _registration._existing_tool_names()


def _acquire_discovery_lock_with_retry():
    """Cross-process guard: a lock loser waits for the holder then discovers itself; unavailable
    locking or an expired wait runs unguarded (fail-soft). None / _LOCK_UNAVAILABLE = unguarded."""
    cookie = _loop._try_acquire_mcp_discovery_lock()
    if cookie is not None:
        return cookie
    logger.debug("Another process holds MCP discovery lock -- retrying with backoff")
    for _ in range(_core._MCP_DISCOVERY_LOCK_MAX_RETRIES):
        time.sleep(_core._MCP_DISCOVERY_LOCK_RETRY_DELAY_S)
        cookie = _loop._try_acquire_mcp_discovery_lock()
        if cookie is not None:
            break
    # Cross-process discovery guard (#62771). A lock loser waits for the holder, then performs its own
    # process-local discovery. If locking is unavailable or the bounded wait expires, preserve the previous
    # fail-soft behavior by running discovery unguarded.
    if cookie is None:
        logger.warning("MCP discovery lock still held after %d retries -- running discovery unguarded",
                       _core._MCP_DISCOVERY_LOCK_MAX_RETRIES)
    elif cookie is not _core._LOCK_UNAVAILABLE:
        logger.debug("Retry succeeded -- acquired MCP discovery lock")
    return cookie


def discover_mcp_tools(allowed_mcp_names: Optional[List[str]] = None) -> List[str]:
    """Entry point: load config, connect servers, register tools. [] without the ``mcp``
    package; idempotent (only servers missing from a previous call are retried).

    ``allowed_mcp_names``: spawn only the MCP servers named in it (built-in toolset names in the
    list simply don't match); ``None`` spawns every configured server. Used by
    ``hermes -z -t <toolsets>`` to skip cold-starting servers the caller doesn't need (10-60s
    each); it only affects which servers start, not which names ``-t`` validation can see."""
    servers = _config._load_mcp_config()
    _retire_removed_runtime_servers({name for name, cfg in servers.items() if _enabled(cfg)})
    if not servers:
        logger.debug("No MCP servers configured")
        return []
    if allowed_mcp_names is not None:
        allowed_set = {str(n) for n in allowed_mcp_names}
        filtered = {name: cfg for name, cfg in servers.items() if name in allowed_set}
        if len(filtered) != len(servers):
            logger.debug("MCP discovery filter: spawning %d/%d configured server(s) per --toolsets filter "
                         "(skipped: %s)", len(filtered), len(servers), ",".join(sorted(set(servers) - set(filtered))))
        servers = filtered
        if not servers:
            logger.debug("No MCP servers in --toolsets filter; skipping MCP load entirely")
            return []
    # SDK import deferred to here so a config without servers — or a -t filter that keeps
    # none — never pays it.
    if not _core._ensure_mcp_sdk():
        logger.debug("MCP SDK not available -- skipping MCP tool discovery")
        return []
    cookie = _acquire_discovery_lock_with_retry()
    try:
        keys = {name: _core._server_state_key(name) for name in servers}
        with _core._lock:
            connecting = set(_core._server_connecting)
            new_server_names = [name for name, cfg in servers.items()
                                if keys[name] not in _core._servers
                                and keys[name] not in connecting and _enabled(cfg)]
        tool_names = register_mcp_servers(servers)
        if new_server_names:
            _log_summary("  MCP:", new_server_names)
        return tool_names
    finally:
        if cookie not in (None, _core._LOCK_UNAVAILABLE):
            cookie.release()


def is_mcp_tool_parallel_safe(tool_name: str) -> bool:
    """True when the tool's server opted into ``supports_parallel_tool_calls`` (provenance
    captured at registration, never the ambiguous ``mcp__{server}__{tool}`` shape)."""
    if not tool_name.startswith(MCP_TOOL_NAME_PREFIX):
        return False
    tool_key = _core._server_state_key(tool_name)
    with _core._lock:
        server_key = _core._mcp_tool_server_names.get(tool_key)
        return bool(server_key and server_key in _core._parallel_safe_servers)


def get_mcp_status(configured: Optional[Dict[str, dict]] = None, *, include_runtime: bool = True) -> List[dict]:
    """Per-server status dicts for banner/TUI: name, transport, tools, connected, disabled,
    status (connected / disabled / connecting / failed / configured) and error for failed.
    Reads cached runtime state only; never connects."""
    configured = _config._load_mcp_config() if configured is None else dict(configured)
    if not configured:
        return []
    current_scope = _core._mcp_registry_scope()
    keys = {name: _core._server_state_key(name, current_scope) for name in configured}
    with _core._lock:
        active_servers = {
            name: _core._servers[key]
            for name, key in keys.items()
            if include_runtime and key in _core._servers
        }
        connecting = {
            name for name, key in keys.items()
            if include_runtime and key in _core._server_connecting
        }
        connect_errors = {
            name: _core._server_connect_errors[key]
            for name, key in keys.items()
            if include_runtime and key in _core._server_connect_errors
        }

    result: List[dict] = []
    for name, cfg in configured.items():
        enabled = _enabled(cfg)  # evaluated unconditionally: malformed values warn even when connected
        server = active_servers.get(name)
        live = server is not None and server.session is not None
        status = ("connected" if live else "disabled" if not enabled else "connecting" if name in connecting
                  else "failed" if name in connect_errors else "configured")
        entry = {"name": name, "transport": cfg.get("transport", "http") if "url" in cfg else "stdio",
                 "tools": 0, "connected": False, "disabled": status == "disabled", "status": status}
        if live:
            entry["connected"] = True
            entry["tools"] = (len(server._registered_tool_names) if hasattr(server, "_registered_tool_names")
                              else len(server._tools))
            if server._sampling:
                entry["sampling"] = dict(server._sampling.metrics)
        elif status == "failed":
            entry["error"] = connect_errors[name]
        result.append(entry)
    return result


def probe_mcp_server_tools() -> Dict[str, List[tuple]]:
    """Connect each enabled server, list ``(tool_name, description)``, disconnect; nothing is
    registered and failed servers are omitted."""
    if not _core._ensure_mcp_sdk():
        return {}
    enabled = {k: v for k, v in (_config._load_mcp_config() or {}).items() if _enabled(v)}
    if not enabled:
        return {}
    _loop._ensure_mcp_loop()
    result: Dict[str, List[tuple]] = {}
    probed_servers: List[_core.MCPServerTask] = []

    async def _probe_all():
        coros = [asyncio.wait_for(_connect_server(name, cfg),
                                  timeout=cfg.get("connect_timeout", _core._DEFAULT_CONNECT_TIMEOUT))
                 for name, cfg in enabled.items()]
        outcomes = await asyncio.gather(*coros, return_exceptions=True)
        for name, outcome in zip(enabled, outcomes):
            if isinstance(outcome, Exception):
                logger.debug("Probe: failed to connect to '%s': %s", name, outcome)
                continue
            probed_servers.append(outcome)
            result[name] = [(t.name, getattr(t, "description", "") or "") for t in outcome._tools]
        await asyncio.gather(*(s.shutdown() for s in probed_servers), return_exceptions=True)

    try:
        _loop._run_on_mcp_loop(_probe_all, timeout=120)
    except Exception as exc:
        logger.debug("MCP probe failed: %s", exc)
    finally:
        _lifecycle._stop_mcp_loop_if_idle()
    return result


def has_registered_mcp_tools() -> bool:
    """True if this profile scope has registered MCP tools."""
    scope = _core._mcp_registry_scope()
    with _core._lock:
        return any(
            _core._server_scope_from_state_key(key) == scope
            for key in _core._mcp_tool_server_names
        )


def get_registered_mcp_server_names() -> set:
    """Logical server names that registered tools in the active profile scope."""
    scope = _core._mcp_registry_scope()
    with _core._lock:
        return {
            _core._server_name_from_state_key(server_key)
            for key, server_key in _core._mcp_tool_server_names.items()
            if _core._server_scope_from_state_key(key) == scope
        }
