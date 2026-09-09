"""Contract tests for the shared parent-death supervisor for stdio MCP servers.

The end-to-end tests here spawn real processes and really SIGKILL a real parent,
because the whole point of this module is behaviour that only exists when a
process dies without running any Python cleanup. A mocked parent death proves
nothing about the guarantee.
"""

import asyncio
import contextlib
import io
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools import mcp_death_supervisor, mcp_stdio_bootstrap, mcp_tool, mcp_tool_config
from tools import mcp_tool_lifecycle as _mcp_lifecycle

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="the supervisor is POSIX-only (process groups)"
)

SUPERVISOR = os.path.join(os.path.dirname(mcp_tool.__file__), "mcp_death_supervisor.py")

# Long enough that nothing here can pass because the victim exited on its own.
_VICTIM = [sys.executable, "-c", "import time; time.sleep(300)"]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OSError):
        return False
    return True


def _wait_gone(pid: int, timeout: float = 15.0) -> bool:
    """Wait for a process this test does NOT own to disappear."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


def _wait_exited(proc: subprocess.Popen, timeout: float = 15.0) -> bool:
    """Wait for a direct child of this test to exit.

    ``os.kill(pid, 0)`` cannot be used for our own children: a killed child
    stays a zombie until someone reaps it, and signalling a zombie succeeds.
    """
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return False
    return True


def _kill(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, OSError):
        return False
    return True


def _wait_group_gone(pgid: int, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _group_alive(pgid):
            return True
        time.sleep(0.05)
    return False


def _cleanup_client_supervisor_state() -> None:
    proc = mcp_tool._death_supervisor
    if proc is not None:
        try:
            proc.stdin.close()
        except (AttributeError, BrokenPipeError, ValueError, OSError):
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            _kill(proc.pid)
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
    mcp_tool._cleanup_death_supervisor_process(proc)
    mcp_tool._death_supervisor = None
    mcp_tool._supervised_pgids.clear()
    mcp_tool._supervised_pgid_identities.clear()
    mcp_tool._pending_death_supervisor_reservations.clear()
    _mcp_lifecycle._stdio_pids.clear()
    _mcp_lifecycle._stdio_pgids.clear()
    _mcp_lifecycle._orphan_stdio_pids.clear()
    _mcp_lifecycle._orphan_stdio_pid_servers.clear()


# ---------------------------------------------------------------------------
# Target safety: this process signals whole process GROUPS, so a bad target is
# unusually expensive. killpg(0, ...) would signal the supervisor's own group.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pgid", [0, 1, -1, -5])
def test_refuses_process_groups_that_are_never_a_valid_target(pgid):
    assert mcp_death_supervisor._is_safe_target(
        pgid, own_pgid=4242, parent_pgid=777
    ) is False


def test_refuses_its_own_group_and_the_parents_group():
    assert mcp_death_supervisor._is_safe_target(
        4242, own_pgid=4242, parent_pgid=777
    ) is False
    assert mcp_death_supervisor._is_safe_target(
        777, own_pgid=4242, parent_pgid=777
    ) is False


def test_accepts_an_unrelated_group():
    assert mcp_death_supervisor._is_safe_target(
        999, own_pgid=4242, parent_pgid=777
    ) is True


# ---------------------------------------------------------------------------
# Control protocol
# ---------------------------------------------------------------------------


def test_registrations_survive_to_eof_and_unregistrations_are_dropped():
    stream = io.StringIO("register 111\nregister 222\nunregister 111\n")

    still_registered = mcp_death_supervisor._serve(
        stream, own_pgid=4242, parent_pgid=777
    )

    assert still_registered == {222}


def test_garbage_lines_do_not_cost_us_the_other_registrations():
    # A corrupted byte on the control pipe must not take down reaping for every
    # other server -- that would turn a cosmetic bug into leaked processes.
    stream = io.StringIO(
        "register 111\n"
        "\n"
        "register\n"
        "register notanumber\n"
        "register 222 333\n"
        "explode 444\n"
        "register 555\n"
    )

    still_registered = mcp_death_supervisor._serve(
        stream, own_pgid=4242, parent_pgid=777
    )

    assert still_registered == {111, 555}


def test_a_writer_that_never_sends_a_newline_cannot_grow_us_without_bound():
    """Found for real: iterating the stream let /dev/zero reach 15 GB.

    The supervisor is the last line of defense against leaked MCP servers, so
    it must not be the process that dies under memory pressure -- and a reader
    that buffers until a newline arrives is exactly that risk.
    """
    huge = "register " + ("0" * 10_000_000) + "\nregister 222\n"

    still_registered = mcp_death_supervisor._serve(
        io.StringIO(huge), own_pgid=4242, parent_pgid=777
    )

    # The overlong line is skipped, and the stream resyncs on the next one.
    assert still_registered == {222}


def test_a_line_truncated_by_the_cap_is_never_acted_on():
    # Truncation must not turn one pgid into a different, valid-looking one:
    # "register 999999" clipped to "register 9" would reap the wrong group.
    stream = io.StringIO("register " + "9" * (mcp_death_supervisor._MAX_LINE_CHARS))

    assert mcp_death_supervisor._serve(
        stream, own_pgid=4242, parent_pgid=777
    ) == set()


def test_unsafe_targets_are_rejected_at_registration_time():
    stream = io.StringIO("register 0\nregister 777\nregister 999\n")

    still_registered = mcp_death_supervisor._serve(
        stream, own_pgid=4242, parent_pgid=777
    )

    assert still_registered == {999}


def test_socket_registration_consumes_a_reserved_token_once(monkeypatch):
    monkeypatch.setattr(mcp_death_supervisor.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(mcp_death_supervisor, "_process_start_time", lambda pid: "12345")
    reservations = {"token-1"}
    registered = set()

    assert mcp_death_supervisor._apply_socket_registration(
        "register token-1 9001 9001 12345\n",
        registered,
        reservations,
        peer_pid=9001,
        own_pgid=4242,
        parent_pgid=777,
    )
    assert registered == {9001}
    assert reservations == set()

    assert not mcp_death_supervisor._apply_socket_registration(
        "register token-1 9001 9001 12345\n",
        registered,
        reservations,
        peer_pid=9001,
        own_pgid=4242,
        parent_pgid=777,
    )


def test_parent_cancel_removes_pending_reservation():
    reservations = {"token-1"}
    registered = set()

    assert mcp_death_supervisor._apply_parent_command(
        "cancel token-1\n",
        registered,
        reservations,
        own_pgid=4242,
        parent_pgid=777,
    )
    assert reservations == set()


def test_legacy_duplicate_cannot_downgrade_identity_bearing_registration():
    registered = {}
    reservations = set()

    assert mcp_death_supervisor._apply_parent_command(
        "register 9001 9001 start-1\n",
        registered,
        reservations,
        own_pgid=4242,
        parent_pgid=777,
    )
    identity = registered[9001]
    assert identity is not None

    assert mcp_death_supervisor._apply_parent_command(
        "register 9001\n",
        registered,
        reservations,
        own_pgid=4242,
        parent_pgid=777,
    )
    assert registered[9001] == identity


def test_reap_revalidates_process_identity_before_signalling(monkeypatch):
    calls = []
    identity = mcp_death_supervisor._ProcessIdentity(pid=9001, pgid=9001, start_time="old")

    monkeypatch.setattr(mcp_death_supervisor.os, "getpgid", lambda pid: 9001)
    monkeypatch.setattr(mcp_death_supervisor, "_process_start_time", lambda pid: "new")
    monkeypatch.setattr(mcp_death_supervisor.os, "killpg", lambda *args: calls.append(args))

    mcp_death_supervisor._reap({9001: identity})

    assert calls == [], "recycled process identity was signalled"


def test_reap_identity_bound_group_after_leader_exit_reaps_surviving_descendant(monkeypatch):
    calls = []
    terminated = False
    identity = mcp_death_supervisor._ProcessIdentity(pid=9001, pgid=9001, start_time="old")

    def _leader_is_gone(_pid):
        raise ProcessLookupError("leader exited")

    def _killpg(pgid, sig):
        nonlocal terminated
        calls.append((pgid, sig))
        if sig == 0 and terminated:
            raise ProcessLookupError("group gone after TERM")
        if sig == signal.SIGTERM:
            terminated = True

    monkeypatch.setattr(mcp_death_supervisor.os, "getpgid", _leader_is_gone)
    monkeypatch.setattr(mcp_death_supervisor.os, "killpg", _killpg)
    monkeypatch.setattr(mcp_death_supervisor.time, "sleep", lambda _seconds: None)

    mcp_death_supervisor._reap({9001: identity})

    assert (9001, signal.SIGTERM) in calls


def test_socket_parent_reader_drains_multiline_replay_before_socket_registration(monkeypatch):
    read_fd, write_fd = os.pipe()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    socket_dir = tempfile.mkdtemp(prefix="hmcp-")
    socket_path = os.path.join(socket_dir, "control.sock")
    listener.bind(socket_path)
    listener.listen(1)
    listener.setblocking(False)

    monkeypatch.setattr(mcp_death_supervisor, "_linux_peer_pid", lambda conn: 9001)
    monkeypatch.setattr(mcp_death_supervisor.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(mcp_death_supervisor, "_process_start_time", lambda pid: "12345")

    result = {}

    def _serve():
        with os.fdopen(read_fd, "r", encoding="ascii") as stream:
            result["registered"] = mcp_death_supervisor._serve_with_socket(
                stream, listener, own_pgid=4242, parent_pgid=777
            )

    thread = threading.Thread(target=_serve)
    thread.start()
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        os.write(write_fd, b"register 111\nreserve token-1\n")
        client.connect(socket_path)
        client.sendall(b"register token-1 9001 9001 12345\n")
        assert client.recv(16) == b"ok\n"
        os.close(write_fd)
        write_fd = -1
        thread.join(timeout=5)
        assert not thread.is_alive()
        registered = result["registered"]
        assert set(registered) == {111, 9001}
    finally:
        client.close()
        listener.close()
        with contextlib.suppress(OSError):
            os.unlink(socket_path)
        with contextlib.suppress(OSError):
            os.rmdir(socket_dir)
        if write_fd != -1:
            os.close(write_fd)
        thread.join(timeout=5)


def test_socket_rejects_control_commands_and_forged_peer(monkeypatch):
    monkeypatch.setattr(mcp_death_supervisor.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(mcp_death_supervisor, "_process_start_time", lambda pid: "12345")
    reservations = {"token-1", "token-2"}
    registered = {111}

    assert not mcp_death_supervisor._apply_socket_registration(
        "unregister 111\n",
        registered,
        reservations,
        peer_pid=9001,
        own_pgid=4242,
        parent_pgid=777,
    )
    assert not mcp_death_supervisor._apply_socket_registration(
        "register token-1 9001 9001 12345\n",
        registered,
        reservations,
        peer_pid=9002,
        own_pgid=4242,
        parent_pgid=777,
    )
    assert registered == {111}
    assert reservations == {"token-1", "token-2"}


def test_refuses_to_run_inside_the_parents_own_process_group():
    # Started without start_new_session, a killpg of the parent's group would
    # take the supervisor out before it could reap. It must not pretend to work.
    proc = subprocess.run(
        [sys.executable, SUPERVISOR, "--parent-pgid", str(os.getpgid(0))],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert proc.returncode == 2
    assert "process group" in proc.stderr


# ---------------------------------------------------------------------------
# End to end: real processes, real death
# ---------------------------------------------------------------------------


def test_reaps_a_registered_group_when_the_control_pipe_reaches_eof():
    victim = subprocess.Popen(_VICTIM, start_new_session=True)
    supervisor = subprocess.Popen(
        [sys.executable, SUPERVISOR, "--parent-pgid", str(os.getpgid(0))],
        stdin=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        supervisor.stdin.write(f"register {os.getpgid(victim.pid)}\n")
        supervisor.stdin.flush()
        assert victim.poll() is None, "victim should outlive registration"

        # EOF is the death signal, whatever closed the pipe.
        supervisor.stdin.close()

        assert _wait_exited(victim), "registered group survived parent death"
    finally:
        _kill(victim.pid)
        _kill(supervisor.pid)
        victim.wait(timeout=10)
        supervisor.wait(timeout=10)


def test_leaves_an_unregistered_group_alone_at_eof():
    # The other failure direction, and the more damaging one: a clean Hermes
    # shutdown unregisters as it tears each server down, so EOF must not become
    # a kill-everything event for servers that were handed back.
    survivor = subprocess.Popen(_VICTIM, start_new_session=True)
    supervisor = subprocess.Popen(
        [sys.executable, SUPERVISOR, "--parent-pgid", str(os.getpgid(0))],
        stdin=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        pgid = os.getpgid(survivor.pid)
        supervisor.stdin.write(f"register {pgid}\nunregister {pgid}\n")
        supervisor.stdin.flush()
        supervisor.stdin.close()

        supervisor.wait(timeout=15)
        assert survivor.poll() is None, "a cleanly unregistered server was killed"
    finally:
        _kill(survivor.pid)
        _kill(supervisor.pid)
        survivor.wait(timeout=10)
        supervisor.wait(timeout=10)


def test_bootstrap_exits_before_exec_when_registration_is_not_acknowledged(tmp_path):
    marker = tmp_path / "exec-marker"
    target = tmp_path / "target.py"
    target.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('execed')\n")
    env = os.environ.copy()
    env["HERMES_MCP_DEATH_SUPERVISOR_SOCKET"] = str(tmp_path / "missing.sock")

    proc = subprocess.run(
        [sys.executable, mcp_stdio_bootstrap.__file__, sys.executable, str(target)],
        env=env,
        start_new_session=True,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert proc.returncode == 125
    assert not marker.exists(), "bootstrap execed the server without supervisor ACK"


_LIVE_MCP_SERVER = """
import anyio
import mcp_types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

async def list_tools(ctx, params):
    return types.ListToolsResult(tools=[
        types.Tool(
            name="echo",
            description="Echo a nonce",
            inputSchema={"type": "object", "properties": {"nonce": {"type": "string"}}},
        )
    ])

async def call_tool(ctx, params):
    nonce = (params.arguments or {}).get("nonce", "")
    return types.CallToolResult(content=[types.TextContent(type="text", text="pong:" + nonce)])

server = Server("live-stdio-test", on_list_tools=list_tools, on_call_tool=call_tool)

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())

anyio.run(main)
"""


@pytest.mark.skipif(not mcp_tool._MCP_AVAILABLE, reason="MCP SDK not installed")
def test_real_stdio_transport_call_succeeds_with_one_shared_supervisor(tmp_path):
    script = tmp_path / "live_mcp_server.py"
    script.write_text(_LIVE_MCP_SERVER)

    async def _run():
        first = mcp_tool.MCPServerTask("live-a")
        second = mcp_tool.MCPServerTask("live-b")
        await first.start({"command": sys.executable, "args": [str(script)], "connect_timeout": 5.0})
        first_supervisor = mcp_tool._death_supervisor
        await second.start({"command": sys.executable, "args": [str(script)], "connect_timeout": 5.0})
        assert first_supervisor is not None
        assert mcp_tool._death_supervisor is first_supervisor
        assert len(mcp_tool._supervised_pgids) == 2

        result = await first.session.call_tool("echo", arguments={"nonce": "ack-stdio"})
        assert result.content[0].text == "pong:ack-stdio"

        await first.shutdown()
        assert mcp_tool._death_supervisor is first_supervisor
        assert len(mcp_tool._supervised_pgids) == 1
        await second.shutdown()
        assert mcp_tool._death_supervisor is None

    asyncio.run(_run())


# A stand-in for Hermes: registers a real child, then blocks forever holding the
# only write end of the control pipe. SIGKILLing it is the scenario the whole
# module exists for -- no cleanup code of ours gets to run.
_FAKE_PARENT = """
import os, subprocess, sys, time

supervisor = sys.argv[1]
victim = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(300)"], start_new_session=True
)
sup = subprocess.Popen(
    [sys.executable, supervisor, "--parent-pgid", str(os.getpgid(0))],
    stdin=subprocess.PIPE, text=True, start_new_session=True,
)
sup.stdin.write("register %d\\n" % os.getpgid(victim.pid))
sup.stdin.flush()
print("%d %d" % (victim.pid, sup.pid), flush=True)
time.sleep(300)
"""


_FAKE_PARENT_SPAWN_GAP = """
import asyncio, os, sys, time

from tools import mcp_tool
from tools import mcp_tool_lifecycle as lifecycle

real_snapshot = lifecycle._snapshot_child_pids
calls = 0
baseline = set()

def blocked_snapshot():
    global baseline, calls
    calls += 1
    if calls == 1:
        baseline = real_snapshot()
        return baseline
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        current = real_snapshot()
        new_pids = current - baseline
        if new_pids:
            child_pid = min(new_pids)
            print("%d %d" % (child_pid, mcp_tool._death_supervisor.pid), flush=True)
            time.sleep(300)
        time.sleep(0.01)
    return real_snapshot()

lifecycle._snapshot_child_pids = blocked_snapshot

async def main():
    server = mcp_tool.MCPServerTask("spawn-gap")
    await server._run_stdio({
        "command": sys.executable,
        "args": ["-c", "import time; time.sleep(300)"],
        "connect_timeout": 30.0,
    })

asyncio.run(main())
"""


_FAKE_PARENT_SILENT_SOCKET = """
import os, socket, subprocess, sys, time

supervisor = sys.argv[1]
socket_path = sys.argv[2]
bootstrap = sys.argv[3]
sup = subprocess.Popen(
    [sys.executable, supervisor, "--parent-pgid", str(os.getpgid(0)), "--socket-path", socket_path],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, start_new_session=True,
)
assert sup.stdout.readline().strip() == "ready"
token = "silent-socket-token"
sup.stdin.write("reserve %s\\n" % token)
sup.stdin.flush()
env = os.environ.copy()
env["HERMES_MCP_DEATH_SUPERVISOR_SOCKET"] = socket_path
env["HERMES_MCP_DEATH_SUPERVISOR_TOKEN"] = token
victim = subprocess.Popen(
    [sys.executable, bootstrap, sys.executable, "-c", "import time; print('execed', flush=True); time.sleep(300)"],
    env=env,
    stdout=subprocess.PIPE,
    text=True,
    start_new_session=True,
)
assert victim.stdout.readline().strip() == "execed"
client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
client.connect(socket_path)
print("%d %d" % (victim.pid, sup.pid), flush=True)
time.sleep(300)
"""


# Reparented-to-init processes are by definition outside this test's subtree,
# so cleaning them up trips conftest's live-system kill guard. Real signal
# delivery to a real orphan is the entire point of these two tests.
@pytest.mark.live_system_guard_bypass
def test_reaps_the_server_when_the_registering_parent_is_sigkilled(tmp_path):
    script = tmp_path / "fake_parent.py"
    script.write_text(_FAKE_PARENT)

    parent = subprocess.Popen(
        [sys.executable, str(script), SUPERVISOR],
        stdout=subprocess.PIPE,
        text=True,
    )
    victim_pid = supervisor_pid = None
    try:
        victim_pid, supervisor_pid = (
            int(x) for x in parent.stdout.readline().split()
        )
        assert _alive(victim_pid)

        # No graceful anything: the parent never runs another line of Python.
        parent.kill()
        parent.wait(timeout=10)

        assert _wait_gone(victim_pid), (
            "stdio MCP server survived kill -9 of its Hermes parent"
        )
    finally:
        for pid in (victim_pid, supervisor_pid):
            if pid is not None:
                _kill(pid)
        _kill(parent.pid)


@pytest.mark.live_system_guard_bypass
@pytest.mark.skipif(not mcp_tool._MCP_AVAILABLE, reason="MCP SDK not installed")
def test_stdio_spawn_gap_is_supervised_before_parent_tracking(tmp_path):
    script = tmp_path / "fake_parent_spawn_gap.py"
    script.write_text(_FAKE_PARENT_SPAWN_GAP)

    parent = subprocess.Popen(
        [sys.executable, str(script)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    child_pid = supervisor_pid = child_pgid = None
    try:
        line = parent.stdout.readline().strip()
        assert line, parent.stderr.read()
        child_pid, supervisor_pid = (int(x) for x in line.split())
        child_pgid = os.getpgid(child_pid)
        assert _group_alive(child_pgid)

        # The fake Hermes process is paused before the old post-enter tracking
        # could run. SIGKILL gives it no chance to register or clean up.
        parent.kill()
        parent.wait(timeout=10)

        assert _wait_group_gone(child_pgid), (
            "stdio MCP process group survived SIGKILL in the spawn-before-registration window"
        )
        assert _wait_gone(supervisor_pid), "supervisor did not exit after reaping the registered group"
    finally:
        if child_pgid is not None:
            try:
                os.killpg(child_pgid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
        for pid in (child_pid, supervisor_pid):
            if pid is not None:
                _kill(pid)
        _kill(parent.pid)


@pytest.mark.live_system_guard_bypass
def test_silent_socket_client_cannot_block_parent_eof_reaping(tmp_path):
    socket_dir = tempfile.mkdtemp(prefix="hmcp-")
    socket_path = os.path.join(socket_dir, "c.sock")
    script = tmp_path / "fake_parent_silent_socket.py"
    script.write_text(_FAKE_PARENT_SILENT_SOCKET)

    parent = subprocess.Popen(
        [sys.executable, str(script), SUPERVISOR, socket_path, mcp_stdio_bootstrap.__file__],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    victim_pid = supervisor_pid = None
    try:
        line = parent.stdout.readline().strip()
        assert line, parent.stderr.read()
        victim_pid, supervisor_pid = (int(x) for x in line.split())
        assert _alive(victim_pid)

        parent.kill()
        parent.wait(timeout=10)

        assert _wait_gone(victim_pid), "silent socket client blocked EOF reaping"
        assert _wait_gone(supervisor_pid), "supervisor did not exit after parent EOF"
    finally:
        for pid in (victim_pid, supervisor_pid):
            if pid is not None:
                _kill(pid)
        _kill(parent.pid)
        with contextlib.suppress(OSError):
            os.unlink(socket_path)
        with contextlib.suppress(OSError):
            os.rmdir(socket_dir)


@pytest.mark.live_system_guard_bypass
def test_reaps_a_grandchild_left_in_the_registered_group(tmp_path):
    # Real shape of the bug: mcp-remote exits but leaves the `node` it spawned
    # behind. The grandchild reparents to init but keeps the pgid, so killpg
    # still reaches it -- which is why we track groups and not pids.
    script = tmp_path / "leaky_server.py"
    script.write_text(
        "import subprocess, sys\n"
        "child = subprocess.Popen([sys.executable, '-c',"
        " 'import time; time.sleep(300)'])\n"
        "print(child.pid, flush=True)\n"
        "sys.stdin.read(1)\n"
    )

    # start_new_session mirrors how the MCP SDK spawns stdio servers.
    server = subprocess.Popen(
        [sys.executable, str(script)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    grandchild_pid = int(server.stdout.readline())
    server_start_time = mcp_death_supervisor._process_start_time(server.pid)
    assert server_start_time is not None
    server.stdin.close()
    server.wait(timeout=10)  # the direct child exits; the grandchild does not

    supervisor = subprocess.Popen(
        [sys.executable, SUPERVISOR, "--parent-pgid", str(os.getpgid(0))],
        stdin=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        assert _alive(grandchild_pid), "grandchild should outlive its parent"
        # server.pid is its own pgid leader, captured at spawn time exactly as
        # mcp_tool records it -- still usable after the leader itself exited.
        supervisor.stdin.write(
            f"register {server.pid} {server.pid} {server_start_time}\n"
        )
        supervisor.stdin.flush()
        supervisor.stdin.close()

        assert _wait_gone(grandchild_pid), "orphaned grandchild was not reaped"
    finally:
        _kill(grandchild_pid)
        _kill(supervisor.pid)
        supervisor.wait(timeout=10)


# ---------------------------------------------------------------------------
# Client side: what mcp_tool tells the supervisor
# ---------------------------------------------------------------------------


class _FakeSupervisor:
    """Stands in for the supervisor process, recording the control stream."""

    def __init__(self, exited=False):
        self.stdin = io.StringIO()
        self.pid = 4242
        self._hermes_socket_path = "/tmp/hermes-fake-mcp-supervisor.sock"
        self._exited = exited
        self._sent = ""
        self.closed = False
        _real_close = self.stdin.close

        def _close():
            # Mirror a real pipe: capture what was written before the write
            # end goes away, so tests can still assert on the control stream.
            self._sent = self.stdin.getvalue()
            self.closed = True
            _real_close()

        self.stdin.close = _close

    def poll(self):
        return 1 if self._exited else None

    def wait(self, timeout=None):
        self.waited = True
        return 0

    def lines(self):
        if self.closed:
            return self._sent.splitlines()
        return self.stdin.getvalue().splitlines()


@pytest.fixture(autouse=True)
def _reset_client_state():
    _cleanup_client_supervisor_state()
    yield
    _cleanup_client_supervisor_state()


@pytest.fixture
def all_groups_alive(monkeypatch):
    """Answer every liveness probe with "this group exists".

    The protocol tests below register synthetic pgids that were never real
    process groups. Without this, the liveness prune correctly discards them
    before the control stream can be asserted on -- so state the precondition
    rather than letting these tests depend on pid-space luck.
    """
    monkeypatch.setattr(mcp_tool.os, "killpg", lambda pgid, sig: None)


def test_cancel_write_failure_immediately_restarts_and_replays_surviving_groups(monkeypatch):
    class _BrokenSupervisor(_FakeSupervisor):
        def __init__(self):
            super().__init__()

            class _Stdin:
                def write(self, _payload):
                    raise BrokenPipeError("dead cancel pipe")

                def flush(self):
                    pass

            self.stdin = _Stdin()

    broken = _BrokenSupervisor()
    replacement = _FakeSupervisor()
    mcp_tool._death_supervisor = broken
    mcp_tool._supervised_pgids.add(111)
    mcp_tool._pending_death_supervisor_reservations.add("token-1")
    monkeypatch.setattr(mcp_tool, "_spawn_death_supervisor", lambda: replacement)

    mcp_tool._cancel_death_supervisor_reservation("token-1")

    assert mcp_tool._pending_death_supervisor_reservations == set()
    assert mcp_tool._death_supervisor is replacement
    assert replacement.lines() == ["register 111"]


def test_orphan_reaper_escalates_for_leaderless_live_process_group(monkeypatch):
    signals = []
    unregisters = []

    monkeypatch.setattr(
        _mcp_lifecycle,
        "_take_reapable_pids",
        lambda _include_active, _server_name: ({9001: "server"}, {9001: 9001}),
    )
    monkeypatch.setattr(_mcp_lifecycle.os, "getpgrp", lambda: 4242)
    monkeypatch.setattr(_mcp_lifecycle.os, "killpg", lambda _pgid, _sig: None)
    monkeypatch.setattr(_mcp_lifecycle.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        _mcp_lifecycle,
        "_signal_mcp_process",
        lambda pid, sig, *_args: signals.append((pid, sig)),
    )
    monkeypatch.setattr("gateway.status._pid_exists", lambda _pid: False)
    monkeypatch.setattr(
        mcp_tool,
        "_update_death_supervisor",
        lambda verb, pgids: unregisters.append((verb, list(pgids))),
    )

    _mcp_lifecycle._kill_orphaned_mcp_children()

    assert signals == [(9001, signal.SIGTERM), (9001, signal.SIGKILL)]
    assert unregisters == [("unregister", [9001])]


def test_register_starts_the_supervisor_once_and_reuses_it(monkeypatch, all_groups_alive):
    spawned = []

    def _spawn():
        fake = _FakeSupervisor()
        spawned.append(fake)
        return fake

    monkeypatch.setattr(mcp_tool, "_spawn_death_supervisor", _spawn)

    mcp_tool._update_death_supervisor("register", [111])
    mcp_tool._update_death_supervisor("register", [222])

    assert len(spawned) == 1, "each register spawned its own supervisor"
    assert spawned[0].lines() == ["register 111", "register 222"]


def test_unregister_is_forwarded(monkeypatch, all_groups_alive):
    fake = _FakeSupervisor()
    monkeypatch.setattr(mcp_tool, "_spawn_death_supervisor", lambda: fake)

    mcp_tool._update_death_supervisor("register", [111])
    mcp_tool._update_death_supervisor("unregister", [111])

    assert fake.lines() == ["register 111", "unregister 111"]
    assert mcp_tool._supervised_pgids == set()


def test_supervisor_is_released_once_nothing_is_left_to_reap(monkeypatch, all_groups_alive):
    """An empty registration set must not keep a supervisor resident.

    A gateway that once connected a stdio server would otherwise carry a
    ~15 MB process and a live pipe for the rest of its life. Closing our
    write end is the same EOF the supervisor treats as parent death; with
    nothing registered it exits without reaping. The next register starts a
    fresh one, exactly like the dead-supervisor replay path.
    """
    spawned = []

    def _spawn():
        fake = _FakeSupervisor()
        spawned.append(fake)
        return fake

    monkeypatch.setattr(mcp_tool, "_spawn_death_supervisor", _spawn)

    mcp_tool._update_death_supervisor("register", [111, 222])
    mcp_tool._update_death_supervisor("unregister", [111])
    assert not spawned[0].closed, "released the supervisor while a group was still registered"

    mcp_tool._update_death_supervisor("unregister", [222])
    assert spawned[0].closed, "supervisor kept resident with nothing left to reap"
    assert getattr(spawned[0], "waited", False), "released supervisor was never wait()ed -> zombie until the next Popen"
    assert spawned[0].lines()[-1] == "unregister 222", "release happened before the last unregister was sent"
    assert mcp_tool._death_supervisor is None

    mcp_tool._update_death_supervisor("register", [333])
    assert len(spawned) == 2 and spawned[1].lines() == ["register 333"]


def test_supervisor_survives_the_real_eof_release():
    """End to end: closing the control pipe with nothing registered exits cleanly."""
    if os.name != "posix":
        pytest.skip("POSIX-only supervisor")
    child = subprocess.Popen(_VICTIM, start_new_session=True)
    try:
        mcp_tool._update_death_supervisor("register", [os.getpgid(child.pid)])
        proc = mcp_tool._death_supervisor
        assert proc is not None and proc.poll() is None
        mcp_tool._update_death_supervisor("unregister", [os.getpgid(child.pid)])
        assert mcp_tool._death_supervisor is None
        assert proc.wait(timeout=10) == 0, "supervisor did not exit on the release EOF"
        assert child.poll() is None, "release reaped a group that had been unregistered"
    finally:
        _kill(child.pid)
        child.wait(timeout=10)


def test_unregister_alone_does_not_start_a_supervisor(monkeypatch):
    spawned = []
    monkeypatch.setattr(
        mcp_tool,
        "_spawn_death_supervisor",
        lambda: spawned.append(1) or _FakeSupervisor(),
    )

    mcp_tool._update_death_supervisor("unregister", [111])

    assert spawned == []


def test_a_dead_supervisor_is_replaced_and_live_coverage_replayed(monkeypatch, all_groups_alive):
    dead = _FakeSupervisor(exited=True)
    replacement = _FakeSupervisor()
    queue = [dead, replacement]
    monkeypatch.setattr(mcp_tool, "_spawn_death_supervisor", lambda: queue.pop(0))

    mcp_tool._update_death_supervisor("register", [111])
    mcp_tool._update_death_supervisor("register", [222])

    # Losing the supervisor must not silently drop the server registered with
    # it -- the replacement has to be told about 111 as well as 222.
    assert set(replacement.lines()) == {"register 111", "register 222"}


def test_ensure_socket_propagates_stale_prunes_before_exposure(monkeypatch):
    fake = _FakeSupervisor()
    mcp_tool._death_supervisor = fake
    mcp_tool._supervised_pgids.update({111, 222})

    def _probe(pgid, _sig):
        if pgid == 111:
            raise ProcessLookupError("gone")

    monkeypatch.setattr(mcp_tool.os, "killpg", _probe)

    assert mcp_tool._ensure_death_supervisor_socket() == fake._hermes_socket_path
    assert mcp_tool._supervised_pgids == {222}
    assert "unregister 111" in fake.lines()


def test_ensure_socket_restarts_and_replays_when_stale_unregister_write_fails(monkeypatch):
    class _BrokenSupervisor(_FakeSupervisor):
        def __init__(self):
            super().__init__()

            class _Stdin:
                def write(self, _payload):
                    raise BrokenPipeError("dead pipe")

                def flush(self):
                    pass

            self.stdin = _Stdin()

    broken = _BrokenSupervisor()
    replacement = _FakeSupervisor()
    mcp_tool._death_supervisor = broken
    mcp_tool._supervised_pgids.update({111, 222})

    def _probe(pgid, _sig):
        if pgid == 111:
            raise ProcessLookupError("gone")

    monkeypatch.setattr(mcp_tool.os, "killpg", _probe)
    monkeypatch.setattr(mcp_tool, "_spawn_death_supervisor", lambda: replacement)

    assert mcp_tool._ensure_death_supervisor_socket() == replacement._hermes_socket_path
    assert mcp_tool._death_supervisor is replacement
    assert replacement.lines() == ["register 222"]


def test_pending_reservation_prevents_idle_release_during_concurrent_startup():
    fake = _FakeSupervisor()
    mcp_tool._death_supervisor = fake
    mcp_tool._pending_death_supervisor_reservations.add("pending-token")

    mcp_tool._release_idle_death_supervisor()

    assert mcp_tool._death_supervisor is fake
    assert not fake.closed, "pending bootstrap reservation was treated as idle"


def test_concurrent_success_and_failed_startup_cleans_only_failed_reservation(monkeypatch, all_groups_alive):
    spawned = []

    def _spawn():
        fake = _FakeSupervisor()
        spawned.append(fake)
        return fake

    monkeypatch.setattr(mcp_tool, "_spawn_death_supervisor", _spawn)
    monkeypatch.setattr(
        mcp_tool.secrets,
        "token_urlsafe",
        lambda _n: f"{threading.current_thread().name}-token",
    )
    monkeypatch.setattr(mcp_tool.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(mcp_tool, "_process_start_time", lambda pid: "start-222")
    barrier = threading.Barrier(2)
    results = {}

    def _successful_startup():
        socket_path, token = mcp_tool._reserve_death_supervisor_registration()
        results["success"] = (socket_path, token)
        barrier.wait(timeout=5)
        mcp_tool._update_death_supervisor("register", [222], identities={222: (222, "start-222")})
        mcp_tool._adopt_death_supervisor_reservation(token)

    def _failed_startup():
        socket_path, token = mcp_tool._reserve_death_supervisor_registration()
        results["failed"] = (socket_path, token)
        barrier.wait(timeout=5)
        mcp_tool._cancel_death_supervisor_reservation(token)

    threads = [
        threading.Thread(target=_successful_startup, name="success"),
        threading.Thread(target=_failed_startup, name="failed"),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert mcp_tool._pending_death_supervisor_reservations == set()
    assert mcp_tool._supervised_pgids == {222}
    lines = spawned[0].lines()
    assert "reserve success-token" in lines
    assert "reserve failed-token" in lines
    assert "cancel failed-token" in lines
    assert "register 222 222 start-222" in lines
    assert not spawned[0].closed, "successful startup coverage was released with failed startup cleanup"


def test_repeated_failed_startups_do_not_grow_reservations_or_keep_supervisor(monkeypatch):
    spawned = []

    def _spawn():
        fake = _FakeSupervisor()
        spawned.append(fake)
        return fake

    monkeypatch.setattr(mcp_tool, "_spawn_death_supervisor", _spawn)
    monkeypatch.setattr(
        mcp_tool.secrets,
        "token_urlsafe",
        MagicMock(side_effect=[f"failed-token-{index}" for index in range(5)]),
    )

    for index in range(5):
        _socket_path, token = mcp_tool._reserve_death_supervisor_registration()
        assert token == f"failed-token-{index}"
        mcp_tool._cancel_death_supervisor_reservation(token)
        assert mcp_tool._pending_death_supervisor_reservations == set()
        assert mcp_tool._death_supervisor is None

    assert len(spawned) == 5
    assert all(fake.closed for fake in spawned)
    assert all(f"cancel failed-token-{index}" in spawned[index].lines() for index in range(5))


def test_stdio_startup_exception_before_pid_discovery_cancels_reservation(monkeypatch):
    class _FailingStdioClient:
        async def __aenter__(self):
            raise RuntimeError("spawn failed before pid discovery")

        async def __aexit__(self, exc_type, exc, tb):
            return False

    fake = _FakeSupervisor()
    monkeypatch.setattr(mcp_tool, "_spawn_death_supervisor", lambda: fake)
    monkeypatch.setattr(mcp_tool.secrets, "token_urlsafe", lambda _n: "pre-adoption-token")
    monkeypatch.setattr(mcp_tool, "_ensure_mcp_sdk", lambda: True)
    monkeypatch.setattr(mcp_tool, "StdioServerParameters", lambda **kwargs: SimpleNamespace(**kwargs))
    monkeypatch.setattr(mcp_tool, "stdio_client", lambda *_args, **_kwargs: _FailingStdioClient())
    monkeypatch.setattr(mcp_tool, "_preflight_stdio_command", AsyncMock(return_value=(sys.executable, [])))
    monkeypatch.setattr(_mcp_lifecycle, "_kill_orphaned_mcp_children", lambda: None)
    monkeypatch.setattr(_mcp_lifecycle, "_snapshot_child_pids", lambda: set())
    monkeypatch.setattr(mcp_tool_config, "_write_stderr_log_header", lambda _name: None)
    monkeypatch.setattr(mcp_tool_config, "_get_mcp_stderr_log", lambda: None)

    server = mcp_tool.MCPServerTask("pre-adoption-failure")
    with pytest.raises(RuntimeError, match="spawn failed before pid discovery"):
        asyncio.run(server._run_stdio({"command": sys.executable, "args": []}))

    assert mcp_tool._pending_death_supervisor_reservations == set()
    assert "reserve pre-adoption-token" in fake.lines()
    assert "cancel pre-adoption-token" in fake.lines()
    assert fake.closed
    assert mcp_tool._death_supervisor is None


def test_stdio_setup_exception_before_existing_try_cancels_reservation(monkeypatch):
    fake = _FakeSupervisor()
    monkeypatch.setattr(mcp_tool, "_spawn_death_supervisor", lambda: fake)
    monkeypatch.setattr(mcp_tool.secrets, "token_urlsafe", lambda _n: "early-setup-token")
    monkeypatch.setattr(mcp_tool, "_ensure_mcp_sdk", lambda: True)
    monkeypatch.setattr(mcp_tool, "StdioServerParameters", lambda **kwargs: SimpleNamespace(**kwargs))
    monkeypatch.setattr(mcp_tool, "_preflight_stdio_command", AsyncMock(return_value=(sys.executable, [])))
    monkeypatch.setattr(_mcp_lifecycle, "_kill_orphaned_mcp_children", lambda: None)
    monkeypatch.setattr(_mcp_lifecycle, "_snapshot_child_pids", lambda: set())
    monkeypatch.setattr(
        mcp_tool_config,
        "_write_stderr_log_header",
        lambda _name: (_ for _ in ()).throw(RuntimeError("stderr setup failed")),
    )

    server = mcp_tool.MCPServerTask("early-setup-failure")
    with pytest.raises(RuntimeError, match="stderr setup failed"):
        asyncio.run(server._run_stdio({"command": sys.executable, "args": []}))

    assert mcp_tool._pending_death_supervisor_reservations == set()
    assert "reserve early-setup-token" in fake.lines()
    assert "cancel early-setup-token" in fake.lines()
    assert fake.closed
    assert mcp_tool._death_supervisor is None


def test_replay_does_not_resurrect_an_unregistered_group(monkeypatch, all_groups_alive):
    dead = _FakeSupervisor(exited=True)
    replacement = _FakeSupervisor()
    queue = [dead, replacement]
    monkeypatch.setattr(mcp_tool, "_spawn_death_supervisor", lambda: queue.pop(0))

    mcp_tool._update_death_supervisor("register", [111])
    mcp_tool._update_death_supervisor("register", [222])
    mcp_tool._update_death_supervisor("unregister", [111])

    assert mcp_tool._supervised_pgids == {222}
    # 111 was legitimately replayed to the replacement (it was live when the
    # dead supervisor was swapped out), then unregistered. What must never
    # happen is a replay AFTER the unregister bringing it back.
    lines = replacement.lines()
    assert lines.index("unregister 111") > lines.index("register 111")
    assert "register 111" not in lines[lines.index("unregister 111") :]
    mcp_tool._update_death_supervisor("register", [333])  # any later replay/append
    assert "register 111" not in replacement.lines()[len(lines) :]


def test_a_broken_pipe_never_propagates_into_a_live_mcp_session(monkeypatch, all_groups_alive):
    class _BrokenPipe(_FakeSupervisor):
        def __init__(self):
            super().__init__()

            class _Stdin:
                def write(self, _payload):
                    raise BrokenPipeError("supervisor exited after poll()")

                def flush(self):
                    pass

            self.stdin = _Stdin()

    broken = _BrokenPipe()
    replacement = _FakeSupervisor()
    queue = [broken, replacement]
    monkeypatch.setattr(mcp_tool, "_spawn_death_supervisor", lambda: queue.pop(0))

    mcp_tool._update_death_supervisor("register", [111])  # must not raise

    assert mcp_tool._death_supervisor is replacement
    assert replacement.lines() == ["register 111"]


def test_unregister_after_a_broken_pipe_rebuilds_coverage_for_survivors(monkeypatch, all_groups_alive):
    """A lost supervisor is replaced immediately and later unregister preserves survivors.

    Sequence from the #93517 review: two groups live, the control pipe dies
    (write fails and coverage is replayed), then a clean teardown unregisters
    one of them without dropping the remaining groups.
    """
    spawned = []

    def _spawn():
        fake = _FakeSupervisor()
        spawned.append(fake)
        return fake

    monkeypatch.setattr(mcp_tool, "_spawn_death_supervisor", _spawn)
    mcp_tool._update_death_supervisor("register", [111, 222])

    class _DeadStdin:
        def write(self, _payload):
            raise BrokenPipeError("supervisor died")

        def flush(self):
            pass

    spawned[0].stdin = _DeadStdin()
    mcp_tool._update_death_supervisor("register", [333])
    assert mcp_tool._death_supervisor is spawned[1]
    assert sorted(spawned[1].lines()) == ["register 111", "register 222", "register 333"]
    assert mcp_tool._supervised_pgids == {111, 222, 333}

    mcp_tool._update_death_supervisor("unregister", [222])

    assert len(spawned) == 2
    assert spawned[1].lines()[-1] == "unregister 222"
    assert mcp_tool._death_supervisor is spawned[1]


def test_a_supervisor_that_cannot_start_is_not_fatal(monkeypatch, all_groups_alive):
    monkeypatch.setattr(mcp_tool, "_spawn_death_supervisor", lambda: None)

    mcp_tool._update_death_supervisor("register", [111])  # must not raise

    assert mcp_tool._death_supervisor is None


@contextlib.contextmanager
def _stdio_connection(child_pid, fake_supervisor):
    """Drive the real MCPServerTask._run_stdio with a known spawned child.

    Only the MCP transport itself is mocked. Everything the supervisor wiring
    depends on -- child discovery, _filter_mcp_children, the real os.getpgid
    lookup -- runs for real against ``child_pid``, so the pgid asserted on is
    the pgid of an actual process rather than a fixture value.
    """
    session = MagicMock()
    session.initialize = AsyncMock()
    session.list_tools = AsyncMock(return_value=SimpleNamespace(tools=[]))

    stdio_cm = MagicMock()
    stdio_cm.__aenter__ = AsyncMock(return_value=(object(), object()))
    stdio_cm.__aexit__ = AsyncMock(return_value=False)
    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("tools.mcp_tool.stdio_client", return_value=stdio_cm),
        patch("tools.mcp_tool.ClientSession", return_value=session_cm),
        # First call is the pids_before baseline; the second reports our child
        # as the newly spawned server.
        patch(
            "tools.mcp_tool_lifecycle._snapshot_child_pids",
            side_effect=[set(), {child_pid}],
        ),
        patch("tools.mcp_tool_config._write_stderr_log_header"),
        patch("tools.mcp_tool._get_mcp_stderr_log", return_value=None),
        patch(
            "tools.mcp_tool._spawn_death_supervisor",
            return_value=fake_supervisor,
        ),
    ):
        yield mcp_tool.MCPServerTask("supervisor-wiring")


@pytest.mark.skipif(not mcp_tool._MCP_AVAILABLE, reason="MCP SDK not installed")
def test_connecting_a_stdio_server_registers_its_real_process_group():
    fake = _FakeSupervisor()
    child = subprocess.Popen(_VICTIM, start_new_session=True)
    try:
        with _stdio_connection(child.pid, fake) as server:
            async def _connect_then_shutdown():
                await server.start({"command": "echo", "args": ["hi"]})
                pgid = os.getpgid(child.pid)
                assert any(line.startswith(f"register {pgid} ") for line in fake.lines()), (
                    "connecting a stdio server did not hand its process group to the "
                    f"supervisor; control stream was {fake.lines()}"
                )
                await server.shutdown()

            asyncio.run(_connect_then_shutdown())
    finally:
        _kill(child.pid)
        child.wait(timeout=10)


@pytest.mark.skipif(not mcp_tool._MCP_AVAILABLE, reason="MCP SDK not installed")
def test_a_server_that_exited_is_released_on_teardown():
    fake = _FakeSupervisor()
    child = subprocess.Popen(_VICTIM, start_new_session=True)
    pgid = None
    try:
        with _stdio_connection(child.pid, fake) as server:

            async def _connect_then_lose_the_child():
                await server.start({"command": "echo", "args": ["hi"]})
                nonlocal pgid
                pgid = os.getpgid(child.pid)
                # The server exits while connected. Reap it here so the
                # teardown path sees a genuinely dead pid, not a zombie.
                child.kill()
                child.wait(timeout=10)
                await server.shutdown()

            asyncio.run(_connect_then_lose_the_child())

        assert any(line.startswith(f"register {pgid} ") for line in fake.lines())
        assert f"unregister {pgid}" in fake.lines(), (
            "a stdio server with nothing left alive stayed registered, so the "
            f"supervisor would keep a stale group; stream was {fake.lines()}"
        )
    finally:
        _kill(child.pid)


@pytest.mark.skipif(not mcp_tool._MCP_AVAILABLE, reason="MCP SDK not installed")
def test_a_server_that_survived_teardown_stays_registered():
    # The case the whole module exists for: teardown did not manage to kill it.
    # Releasing it here would hand the orphan back to nobody.
    fake = _FakeSupervisor()
    child = subprocess.Popen(_VICTIM, start_new_session=True)
    try:
        with _stdio_connection(child.pid, fake) as server:

            async def _connect_then_shutdown():
                await server.start({"command": "echo", "args": ["hi"]})
                await server.shutdown()

            asyncio.run(_connect_then_shutdown())

        pgid = os.getpgid(child.pid)
        assert any(line.startswith(f"register {pgid} ") for line in fake.lines())
        assert f"unregister {pgid}" not in fake.lines(), (
            "a server that outlived teardown was released from the supervisor, "
            "so an ungraceful exit would leave it running forever"
        )
    finally:
        _kill(child.pid)
        child.wait(timeout=10)


@pytest.mark.live_system_guard_bypass
def test_scoped_teardown_of_one_owner_keeps_the_other_owner_supervised(monkeypatch):
    """Two owners (profiles / agents) each hold a stdio group; tearing one down
    must release only that owner's group and leave the other covered, and the
    per-process supervisor must then still know about the survivor.

    Exercises the real registry + ``_kill_orphaned_mcp_children`` scoping
    rather than the control protocol alone (review request on #93517).
    """
    fake = _FakeSupervisor()
    monkeypatch.setattr(mcp_tool, "_spawn_death_supervisor", lambda: fake)
    monkeypatch.setattr(_mcp_lifecycle.time, "sleep", lambda _s: None)  # skip the SIGTERM grace wait
    a = subprocess.Popen(_VICTIM, start_new_session=True)
    b = subprocess.Popen(_VICTIM, start_new_session=True)
    try:
        pg_a, pg_b = os.getpgid(a.pid), os.getpgid(b.pid)
        with mcp_tool._lock:
            _mcp_lifecycle._stdio_pids[a.pid] = "profile-a"
            _mcp_lifecycle._stdio_pids[b.pid] = "profile-b"
            _mcp_lifecycle._stdio_pgids[a.pid] = pg_a
            _mcp_lifecycle._stdio_pgids[b.pid] = pg_b
        mcp_tool._update_death_supervisor("register", [pg_a, pg_b])

        _mcp_lifecycle._kill_orphaned_mcp_children(include_active=True, server_name="profile-a")
        a.wait(timeout=10)

        assert b.poll() is None, "scoped teardown of profile-a killed profile-b's server"
        assert f"unregister {pg_a}" in fake.lines()
        assert f"unregister {pg_b}" not in fake.lines(), (
            "scoped teardown released the OTHER owner's group from the supervisor"
        )
        assert mcp_tool._supervised_pgids == {pg_b}
        assert b.pid in _mcp_lifecycle._stdio_pids and b.pid in _mcp_lifecycle._stdio_pgids
    finally:
        for p in (a, b):
            _kill(p.pid)
            try:
                p.wait(timeout=10)
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass
        with mcp_tool._lock:
            for p in (a, b):
                _mcp_lifecycle._stdio_pids.pop(p.pid, None)
                _mcp_lifecycle._stdio_pgids.pop(p.pid, None)


@pytest.mark.live_system_guard_bypass
def test_a_group_with_nothing_left_alive_is_forgotten_and_unregistered(monkeypatch):
    """A dead group must not stay registered: its pgid can be recycled.

    Uses a real process so the liveness probe is answered by the kernel rather
    than a fixture -- the whole point is that we notice actual death.
    """
    fake = _FakeSupervisor()
    monkeypatch.setattr(mcp_tool, "_spawn_death_supervisor", lambda: fake)

    doomed = subprocess.Popen(_VICTIM, start_new_session=True)
    doomed_pgid = os.getpgid(doomed.pid)
    survivor = subprocess.Popen(_VICTIM, start_new_session=True)
    survivor_pgid = os.getpgid(survivor.pid)
    try:
        mcp_tool._update_death_supervisor("register", [doomed_pgid, survivor_pgid])
        assert mcp_tool._supervised_pgids == {doomed_pgid, survivor_pgid}

        # Reap it fully so the group is genuinely empty, not a zombie.
        doomed.kill()
        doomed.wait(timeout=10)

        # Any later registration change is when we notice.
        mcp_tool._update_death_supervisor("register", [survivor_pgid])

        assert doomed_pgid not in mcp_tool._supervised_pgids, (
            "a group with no members left stayed registered, so a recycled "
            "pgid could later be reaped as if it were an MCP server"
        )
        assert survivor_pgid in mcp_tool._supervised_pgids, (
            "pruning dropped a group that is still alive"
        )
        assert f"unregister {doomed_pgid}" in fake.lines(), (
            "the supervisor was never told to forget the dead group"
        )
    finally:
        _kill(survivor.pid)
        survivor.wait(timeout=10)
        _kill(doomed.pid)


def test_pruning_keeps_groups_it_cannot_prove_are_gone(monkeypatch):
    # An ambiguous probe (EPERM: exists but not ours) must not drop coverage --
    # losing a real registration is worse than keeping a doubtful one.
    monkeypatch.setattr(mcp_tool, "_supervised_pgids", {111, 222}, raising=False)

    def _probe(pgid, sig):
        if pgid == 111:
            raise PermissionError("exists, not ours")
        raise ProcessLookupError("gone")

    monkeypatch.setattr(mcp_tool.os, "killpg", _probe)

    stale = mcp_tool._prune_dead_supervised_pgids()

    assert stale == {222}
    assert mcp_tool._supervised_pgids == {111}


def test_pruning_recycled_identity_forgets_without_killpg(monkeypatch):
    calls = []
    mcp_tool._supervised_pgids.add(9001)
    mcp_tool._supervised_pgid_identities[9001] = mcp_tool._DeathSupervisorIdentity(
        pid=9001, pgid=9001, start_time="old"
    )
    monkeypatch.setattr(mcp_tool.os, "getpgid", lambda pid: 9001)
    monkeypatch.setattr(mcp_tool, "_process_start_time", lambda pid: "new")
    monkeypatch.setattr(mcp_tool.os, "killpg", lambda *args: calls.append(args))

    stale = mcp_tool._prune_dead_supervised_pgids()

    assert stale == {9001}
    assert mcp_tool._supervised_pgids == set()
    assert 9001 not in mcp_tool._supervised_pgid_identities
    assert calls == [], "recycled process identity was probed by pgid"


def test_pruning_keeps_identity_bound_group_when_leader_exited_but_descendant_survives(monkeypatch):
    mcp_tool._supervised_pgids.add(9001)
    mcp_tool._supervised_pgid_identities[9001] = mcp_tool._DeathSupervisorIdentity(
        pid=9001, pgid=9001, start_time="old"
    )

    def _leader_is_gone(_pid):
        raise ProcessLookupError("leader exited")

    monkeypatch.setattr(mcp_tool.os, "getpgid", _leader_is_gone)
    monkeypatch.setattr(mcp_tool.os, "killpg", lambda _pgid, _sig: None)

    stale = mcp_tool._prune_dead_supervised_pgids()

    assert stale == set()
    assert mcp_tool._supervised_pgids == {9001}
    assert 9001 in mcp_tool._supervised_pgid_identities


def test_no_pgids_is_a_no_op(monkeypatch):
    spawned = []
    monkeypatch.setattr(
        mcp_tool,
        "_spawn_death_supervisor",
        lambda: spawned.append(1) or _FakeSupervisor(),
    )

    mcp_tool._update_death_supervisor("register", [])

    assert spawned == []
