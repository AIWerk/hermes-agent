"""MCP process lifecycle: stdio child PID tracking and orphan cleanup, graceful
server shutdown and draining of the background MCP loop."""

import logging
import asyncio
import os
import time
from typing import Any, Dict, Optional
from tools.mcp_tool_common import _core
from tools import mcp_tool_loop as _loop

logger = logging.getLogger("tools.mcp_tool")

# Live stdio MCP children (pid -> server_name), added after connection and removed on normal
# shutdown, so they can be force-killed if SDK teardown fails.
_stdio_pids: Dict[int, Any] = {}
# PIDs that survived their session context exit (detected in _run_stdio's finally, reaped by
# _kill_orphaned_mcp_children). Separate from _stdio_pids so sweeps never race active sessions.
_orphan_stdio_pids: set = set()
_orphan_stdio_pid_servers: Dict[int, Any] = {}
# pid -> pgid captured at spawn. The SDK spawns with start_new_session=True (PGID == PID);
# grandchildren keep that PGID after the direct child exits, so killpg still reaches them.
# Separate from _stdio_pids so the PGID survives the child's removal. Empty on Windows.
_stdio_pgids: Dict[int, int] = {}


def _snapshot_child_pids() -> set:
    """Current direct-child PIDs: /proc on Linux, else psutil, else empty set."""
    my_pid = os.getpid()
    # /proc/<pid>/task/<tid>/children is per-THREAD, and stdio_client() spawns from the MCP
    # loop thread, so union every task's children — reading only the main thread's file
    # returns an empty set on every Linux install.
    try:
        # ``/proc/<pid>/task/<tid>/children`` is per-THREAD — a child forked from thread T is listed only
        # under T's task dir. stdio_client() spawns from the background MCP loop thread, so reading only the
        # main thread's file (``task/<pid>/children``) returned an empty set on every Linux install and left
        # ``_stdio_child_pids`` / ``_stdio_pids`` empty: the #81995 dead-child fast-fail, the #96452 respawn
        # signal, and the killpg shutdown sweep never saw the subprocess.
        task_dir = f"/proc/{my_pid}/task"
        found: set = set()
        for tid in os.listdir(task_dir):
            try:
                with open(f"{task_dir}/{tid}/children", encoding="utf-8") as f:
                    found.update(int(p) for p in f.read().split() if p.strip())
            except (FileNotFoundError, OSError, ValueError):
                continue  # thread exited between listdir and open
        return found
    except (FileNotFoundError, OSError, ValueError):
        pass
    try:
        import psutil
        return {c.pid for c in psutil.Process(my_pid).children()}
    except Exception:
        return set()


# argv markers of non-MCP gateway children that can race into the snapshot delta during an
# MCP spawn (defense-in-depth; LSP/slash_worker already use start_new_session). Matched against
# argv[1:] because Python/Java children start with the interpreter path.
_NON_MCP_CHILD_CMDLINE_MARKERS: tuple[str, ...] = (
    "tui_gateway.slash_worker", "tui_gateway.entry",
    "-dorg.eclipse.equinox.launcher", "eclipse.jdt.ls", "org.eclipse.equinox.launcher_",  # jdtls
)


def _filter_mcp_children(pids: set) -> set:
    """Drop non-MCP children from a PID snapshot delta. Tracking a stray child in _stdio_pgids
    is catastrophic if it lacks start_new_session: its pgid can be the TUI parent's, so the
    shutdown killpg() would kill the TUI itself."""
    if not pids:
        return pids
    try:
        import psutil
    except ImportError:
        return pids  # keep all PIDs (prior behavior)
    kept = set()
    for pid in pids:
        try:
            argv = psutil.Process(pid).cmdline()
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue  # raced away or zombie — cannot be our fresh server, unsafe to track
        if not any(marker in arg for arg in argv[1:] for marker in _NON_MCP_CHILD_CMDLINE_MARKERS):
            kept.add(pid)
    return kept


def _clear_connect_cooldowns(keys=None) -> None:
    """Drop connect-retry cooldowns: a restart must re-attempt every server immediately, not
    honour a stale per-server backoff. Caller holds ``_core._lock``."""
    if keys is None:
        _core._server_connect_retry_after.clear()
        _core._server_connect_failures.clear()
    else:
        for key in keys:
            _core._server_connect_retry_after.pop(key, None)
            _core._server_connect_failures.pop(key, None)


def _reregister_orphaned_adopters() -> None:
    """Re-run MCP registration for profiles whose ADOPTED shared connection an owner's
    ``/reload-mcp`` just tore down. Their tools vanished with the owner's teardown and nothing
    re-runs their discovery until THEY reload, so they sat tool-less behind a healthy-looking
    status (#106005). Runs after the owner's rediscovery, under each adopter's own home + secret
    scope (its ``${VAR}`` refs must resolve to ITS credentials): the adopter re-adopts the owner's
    new identical connection or connects its own."""
    with _core._lock:
        pending = dict(_core._orphaned_adopters)
        _core._orphaned_adopters.clear()
    if not pending:
        return
    from pathlib import Path
    from agent.secret_scope import build_profile_secret_scope, reset_secret_scope, set_secret_scope
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools import mcp_tool_discovery as _discovery
    from tools.mcp_tool_config import _load_mcp_config
    for adopter, names in pending.items():
        home_token = secret_token = None
        try:
            home_token = set_hermes_home_override(adopter)
            secret_token = set_secret_scope(build_profile_secret_scope(Path(adopter)), profile_home=adopter)
            servers = {n: c for n, c in (_load_mcp_config() or {}).items() if n in names}
            if servers:
                _discovery.register_mcp_servers(servers)
        except Exception:
            logger.debug("MCP: re-registration for profile scope %s failed", adopter, exc_info=True)
        finally:
            if secret_token is not None:
                reset_secret_scope(secret_token)
            if home_token is not None:
                reset_hermes_home_override(home_token)


def shutdown_mcp_servers(*, scope: Optional[str] = None, names: Optional[set] = None,
                         timeout: float = 15.0):
    """Close MCP server connections (in parallel) and stop the background loop. Each server
    Task is signalled to exit its own ``async with`` so the anyio cancel-scope cleanup runs in
    the Task that opened it. ``scope`` restricts teardown to one multiplexed profile's servers
    (its ``/reload-mcp`` must not kill other profiles') and leaves the shared loop running if
    anything else is still connected. ``names`` restricts it further to those server names
    (dropped-from-config pruning); other servers' bookkeeping is untouched. Only the bare call
    (no ``scope``, no ``names``) is the process-wide wildcard: the launch profile's registry
    scope IS ``None``, so ``scope=None, names={...}`` prunes that unscoped owner's servers and
    must leave a served profile's same-named ``(B, name)`` connection alone. ``timeout`` bounds
    the wait for the close to land on the MCP loop — a caller running one pass per served
    profile under a total budget divides it, or N profiles × 15s starve the wildcard pass that
    actually stops the loop."""
    from tools.mcp_tool_scope import _key_name, _key_scope
    wildcard = scope is None and names is None
    with _core._lock:
        def selected_key(key) -> bool:
            owner = _core._server_scope_keys.get(key, _key_scope(key))
            return (wildcard or owner == scope) and (names is None or _key_name(key) in names)

        all_state_keys = (
            set(_core._servers) | set(_core._server_connect_claims)
            | set(_core._server_scope_keys) | set(_core._server_tool_scopes)
            | set(_core._server_connecting) | set(_core._server_connect_errors)
            | set(_core._server_connect_retry_after) | set(_core._server_connect_failures)
            | set(_core._lazy_server_configs) | set(_core._lazy_server_fingerprints)
            | set(_core._lazy_server_tool_names) | set(_core._server_error_counts)
            | set(_core._server_breaker_opened_at) | set(_core._server_errors_all_application)
            | set(_core._server_trust_levels) | set(_core._tool_read_only_hints)
            | set(_core._parallel_safe_servers) | set(_core._mcp_tool_server_names.values())
        )
        selected_status = {key for key in all_state_keys if selected_key(key)}
        selected = [key for key in _core._servers if selected_key(key)]
        claimed = [key for key in _core._server_connect_claims if selected_key(key)]
        servers_snapshot = []
        seen = set()
        for server in ([_core._servers[key] for key in selected]
                       + [_core._server_connect_claims[key] for key in claimed]):
            if id(server) not in seen:
                seen.add(id(server))
                servers_snapshot.append(server)
        selected_lazy_tools = [
            (key, list(_core._lazy_server_tool_names.get(key, ())))
            for key in selected_status if key in _core._lazy_server_configs
        ]
        remaining_server_names = {
            _key_name(key) for key in all_state_keys if key not in selected_status
        }
        selected_tool_names = set()
        for tool_name, server_name in _core._mcp_tool_server_names.items():
            owner_name = _key_name(server_name)
            if owner_name not in remaining_server_names and (
                    wildcard or names is None or owner_name in names):
                selected_tool_names.add(tool_name)
        # Adopters of the connections being torn down lose their overlays with the tasks' own
        # ``_deregister_tools``; remember them so the next discovery pass re-registers them
        # (``_reregister_orphaned_adopters``).
        if not wildcard:
            for key in selected:
                for adopter in _core._server_tool_scopes.get(key, ()):
                    if adopter != scope:
                        _core._orphaned_adopters.setdefault(adopter, set()).add(_key_name(key))
        # A connection still being established has no shutdown coroutine yet. Revoke its
        # publication claim immediately so completion cannot resurrect a removed config.
        for key in claimed:
            _core._server_connect_claims.pop(key, None)

    def clear_selected_status():
        _core._server_connecting.difference_update(selected_status)
        _core._parallel_safe_servers.difference_update(selected_status)
        for key in selected_status:
            _core._server_connect_errors.pop(key, None)
            _core._server_scope_keys.pop(key, None)
            _core._server_tool_scopes.pop(key, None)
            _core._lazy_server_configs.pop(key, None)
            _core._lazy_server_fingerprints.pop(key, None)
            _core._lazy_server_tool_names.pop(key, None)
            _core._server_error_counts.pop(key, None)
            _core._server_breaker_opened_at.pop(key, None)
            _core._server_errors_all_application.pop(key, None)
            _core._server_trust_levels.pop(key, None)
            _core._tool_read_only_hints.pop(key, None)
            _core._server_connect_claims.pop(key, None)
        for tool_name in selected_tool_names:
            _core._mcp_tool_server_names.pop(tool_name, None)

    # Cache-only lazy servers have no task shutdown hook to deregister them.
    if selected_lazy_tools:
        from tools import mcp_tool_registration as _registration
        for key, tool_names in selected_lazy_tools:
            for tool_name in tool_names:
                _registration._deregister_mcp_tool_all_scopes(key, tool_name)

    # Fast path: nothing to shut down. The connect-cooldown maps can still be populated here — a server that
    # failed to connect is never recorded in ``_servers`` (that is the very premise of the #50394 cooldown),
    # so "no live servers" is the MOST likely state in which stale backoff entries exist. Clear them so a
    # post-shutdown restart re-attempts every configured server immediately.
    if servers_snapshot:
        async def _shutdown():
            results = await asyncio.gather(*(server.shutdown() for server in servers_snapshot), return_exceptions=True)
            for server, result in zip(servers_snapshot, results):
                if isinstance(result, Exception):
                    logger.debug("Error closing MCP server '%s': %s", server.name, result)
            with _core._lock:
                for key in selected:
                    _core._servers.pop(key, None)
                    _core._server_scope_keys.pop(key, None)
                clear_selected_status()
                _clear_connect_cooldowns(None if wildcard else selected_status)

        with _core._lock:
            loop = _core._mcp_loop
        if loop is not None and loop.is_running():
            from agent.async_utils import safe_schedule_threadsafe
            future = safe_schedule_threadsafe(_shutdown(), loop, logger=logger, log_message="MCP shutdown: failed to schedule")
            if future is not None:
                try:
                    future.result(timeout=timeout)
                except BaseException as exc:
                    logger.debug("Error during MCP shutdown: %s", exc)

    # Unconditional final sweep: whether ``_shutdown`` ran, timed out, or was never scheduled
    # (a server that failed to connect is never in ``_servers`` — the most likely state for
    # stale backoff entries), no connect-cooldown state may survive shutdown.
    with _core._lock:
        if not servers_snapshot:
            clear_selected_status()
        _clear_connect_cooldowns(None if wildcard else selected_status)
    _loop._stop_mcp_loop(only_if_idle=not wildcard)
    # A removed subset still shares its profile's log with the remaining servers.
    # Full/profile shutdown must also release handles left by completed CLI/UI probes.
    if names is None:
        from tools.mcp_tool_config import _close_mcp_stderr_logs
        _close_mcp_stderr_logs(scope=scope)


def _take_reapable_pids(include_active: bool, server_name: Any = None) -> tuple[Dict[int, Any], Dict[int, int]]:
    """Pop the PIDs to reap (and their spawn-time pgids) out of the ledgers under the lock, so
    a future spawn can't collide with stale state. Returns ``(pid -> owner, pid -> pgid)``."""
    def _owned(entries: Dict[int, Any]) -> Dict[int, Any]:
        return {pid: owner for pid, owner in entries.items() if server_name is None or owner == server_name}

    with _core._lock:
        pids = _owned({opid: _orphan_stdio_pid_servers.get(opid, "orphan") for opid in _orphan_stdio_pids})
        _orphan_stdio_pids.difference_update(pids)
        for opid in pids:
            _orphan_stdio_pid_servers.pop(opid, None)
        if include_active:
            active = _owned(_stdio_pids)
            pids.update(active)
            for pid in active:
                _stdio_pids.pop(pid, None)
        pgids = {pid: _stdio_pgids.pop(pid) for pid in pids if pid in _stdio_pgids}
    return pids, pgids


def _signal_mcp_process(pid: int, sig: int, server_name: str, pgid: Optional[int], my_pgid: Optional[int]) -> None:
    """SIGTERM/SIGKILL via the spawn-time pgroup on POSIX (reaches reparented grandchildren),
    falling back to a per-pid signal."""
    killpg = getattr(os, "killpg", None)
    if pgid is not None and killpg is not None:
        if my_pgid is not None and pgid == my_pgid:
            # Child shares the gateway's pgroup: killpg would kill the gateway too, so use
            # per-pid kill. Warn because per-pid kill can't reach grandchildren in this group.
            logger.warning("MCP server '%s' pgid %d matches gateway pgid; skipping "
                           # Fall through to the per-pid kill() path instead. Warn because per-pid kill
                           # cannot reach grandchildren in this shared group — if the direct child has
                           # already exited, they may leak (inherent: group-killing them would also kill the
                           # gateway). See #47134.
                           "killpg to avoid self-kill and using per-pid kill — any "
                           "grandchildren in this group may not be reaped", server_name, pgid)
        else:
            try:
                killpg(pgid, sig)
                return
            except (ProcessLookupError, PermissionError, OSError) as exc:
                # Pgroup gone or refused — still try the direct child.
                logger.debug("killpg(%d, %d) failed for MCP server '%s': %s; falling back to kill(pid)",
                             pgid, sig, server_name, exc)
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _mcp_process_group_exists(pgid: Optional[int], my_pgid: Optional[int]) -> bool:
    """Whether a separately-owned MCP process group still has any member."""
    if pgid is None or pgid <= 1 or pgid == my_pgid or not hasattr(os, "killpg"):
        return False
    try:
        os.killpg(pgid, 0)  # windows-footgun: ok — guarded by hasattr(os, "killpg")
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _kill_orphaned_mcp_children(include_active: bool = False, server_name: Optional[str] = None) -> None:
    """Best-effort reap of stdio MCP subprocesses: SIGTERM, wait 2s, SIGKILL survivors. By
    default only ``_orphan_stdio_pids`` are reaped so concurrent cron jobs / live sessions are
    untouched; ``include_active=True`` also kills every ``_stdio_pids`` entry and is only for
    final shutdown after the MCP loop has stopped. ``server_name`` limits the sweep to one
    server (stdio reconnects cleaning up their old transport)."""
    import signal as _signal
    pids, pgids = _take_reapable_pids(include_active, server_name)
    if not pids:  # skip the 2s sleep every MCP-free shutdown would otherwise pay
        return

    try:  # our own pgid, so we never killpg() the gateway itself
        my_pgid = os.getpgrp()
    except (AttributeError, OSError):
        my_pgid = None  # Windows or restricted environment

    for pid, owner in pids.items():
        _signal_mcp_process(pid, _signal.SIGTERM, owner, pgids.get(pid), my_pgid)
        logger.debug("Sent SIGTERM to orphaned MCP process %d (%s)", pid, owner)
    time.sleep(2)
    sigkill = getattr(_signal, "SIGKILL", _signal.SIGTERM)
    from gateway.status import _pid_exists  # ``os.kill(pid, 0)`` is NOT a no-op on Windows
    for pid, owner in pids.items():
        pgid = pgids.get(pid)
        if _pid_exists(pid) or _mcp_process_group_exists(pgid, my_pgid):
            _signal_mcp_process(pid, sigkill, owner, pgids.get(pid), my_pgid)
            logger.warning("Force-killed MCP process %d (%s) after SIGTERM timeout", pid, owner)
    # These groups are reaped. Release them last, so a crash partway through the SIGTERM/SIGKILL
    # dance still leaves the supervisor holding them.
    _core._update_death_supervisor("unregister", pgids.values())


def _stop_mcp_loop_if_idle() -> bool:
    """Stop the MCP loop only when no registered server still owns it. Probe paths create
    temporary MCPServerTasks not placed in ``_servers``; they may clean up an idle loop but
    must not tear down the process-global loop under live agent tools."""
    return _loop._stop_mcp_loop(only_if_idle=True)


async def _drain_mcp_loop_tasks(*, timeout: Optional[float] = None) -> None:
    """Cancel every task still pending on the MCP loop and reap it. ``Task.cancel()`` only
    schedules the throw, so tasks need a cancellation cycle before the loop goes away; wait
    for them here, on their owning loop, bounded so a task that suppresses cancellation
    cannot hang process exit."""
    if timeout is None:
        timeout = _core._MCP_LOOP_DRAIN_TIMEOUT
    current = asyncio.current_task()
    pending = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
    if not pending:
        return
    logger.debug("Draining %d pending task(s) from the MCP loop", len(pending))
    for task in pending:
        task.cancel()
    done, still_pending = await asyncio.wait(pending, timeout=timeout)
    for task in done:
        if not task.cancelled():
            task.exception()  # mark retrieved so asyncio doesn't warn "exception was never retrieved"
    if still_pending:
        logger.warning("%d MCP loop task(s) still pending after %.1fs drain", len(still_pending), timeout)


async def _drain_and_stop_mcp_loop() -> None:
    """Drain pending tasks, then stop the loop from its owning thread. Both must run as one
    loop-owned sequence: a ``loop.stop`` queued separately by a timed-out caller can overtake
    the scheduled drain, leaving the drain coroutine itself pending when the loop is closed."""
    loop = asyncio.get_running_loop()
    try:
        await _drain_mcp_loop_tasks(timeout=_core._MCP_LOOP_DRAIN_TIMEOUT)
    finally:
        loop.call_soon(loop.stop)
