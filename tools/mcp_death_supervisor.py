#!/usr/bin/env python3
"""One parent-death supervisor per Hermes process, shared by all stdio MCP servers.

Why this exists
---------------
When Hermes dies without running its cleanup path (SIGKILL, OOM killer, a hard
crash), stdio MCP servers it spawned are reparented to init and keep running
forever.  macOS has no ``PR_SET_PDEATHSIG``, so something has to outlive Hermes
and reap them.

This module is deliberately standard-library-only and must not import anything
from ``tools/``: it runs after Hermes may already be dead, and pulling in
``mcp_tool`` would drag the whole agent with it. The TERM -> grace -> KILL
``killpg`` sweep in ``_reap`` therefore duplicates similar sweeps elsewhere in
the tree on purpose.

The predecessor (``mcp_stdio_watchdog.py``) solved this with one CPython
*per MCP server*, wrapping each server command and polling ``getppid()`` every
two seconds.  That costs ~10 MB of resident memory per server and detects death
up to one poll interval late.  This module replaces the whole fleet of pollers
with a single supervisor per Hermes process:

* **Death detection is a blocking read on a pipe.**  Hermes holds the only write
  end.  When Hermes dies -- by any means, including SIGKILL -- the write end
  closes and the read returns EOF.  Exact, instant, and free.
* **Servers are spawned unwrapped.**  The MCP SDK already spawns stdio children
  with ``start_new_session=True``, so each one is its own process-group leader
  and ``killpg`` still reaches its descendants.  Removing the wrapper also
  removes the signal-forwarding layer the wrapper needed to avoid inverting the
  bug it fixed.

Protocol (line-based)
---------------------
    register <pgid>\n     start reaping this process group on parent death
    unregister <pgid>\n   stop reaping it (its server shut down cleanly)

Hermes still sends lifecycle updates on stdin.  A stdio server bootstrap sends
``register`` over the supervisor's Unix socket and waits for ``ok\n`` before it
execs the real server.  That closes the SIGKILL window between spawn and
post-enter parent bookkeeping: a spawned process group either reaches the
supervisor and gets an acknowledgement before server code runs, or it exits.

On EOF the supervisor SIGTERMs every still-registered process group, waits a
short grace period, SIGKILLs the survivors, and exits.  A registered group that
Hermes never unregistered *is* the orphan set, so a clean Hermes shutdown --
which unregisters as it tears each server down -- ends with nothing to kill.

Unparseable lines are ignored rather than fatal: a corrupted byte on the control
pipe must not cost us the reaping guarantee for every other server.

Process-group reuse
-------------------
Modern registrations retain the process identity as ``pid + pgid + /proc``
start-time.  Immediately before TERM, during the grace-period liveness probes,
and again before KILL, the supervisor verifies that the recorded PID is still
the same process in the same group.  If the identity no longer matches, it
forgets the target and does not signal the group.  Older two-field
``register <pgid>`` commands remain accepted for compatibility, but current
Hermes stdio startup and replay paths use the identity-bearing form.
"""

from __future__ import annotations

import argparse
import os
import select
import selectors
import signal
import socket
import struct
import sys
import time
from dataclasses import dataclass

# Matches the grace period the per-server watchdog used before it escalated.
_TERM_GRACE_S = 3.0
# How often we re-check for survivors during that grace period.
_REAP_POLL_S = 0.1
# A command is "unregister <pgid>" -- around 20 characters. The cap only has to
# be generous enough for a legitimate line; see _serve for why it exists.
_MAX_LINE_CHARS = 256
_SOCKET_BACKLOG = 16
_SOCKET_POLL_S = 1.0
_SOCKET_CLIENT_DEADLINE_S = 5.0


@dataclass(frozen=True)
class _ProcessIdentity:
    pid: int
    pgid: int
    start_time: str | None = None


def _is_safe_target(pgid: int, *, own_pgid: int, parent_pgid: int) -> bool:
    """Return True if ``pgid`` is a process group we may signal.

    Defensive only -- Hermes already filters non-MCP children before it
    registers anything (see ``_filter_mcp_children`` in ``tools/mcp_tool.py``).
    But this process signals whole process *groups*, so a bad value here is
    unusually expensive: ``killpg(0, ...)`` signals our own group, and pgid 1
    is init.  A caller bug should cost us one unreaped server, never the
    Hermes process tree or the session.
    """
    if pgid <= 1:
        return False
    if pgid == own_pgid or pgid == parent_pgid:
        return False
    return True


def _identity_by_pgid(targets) -> dict[int, _ProcessIdentity | None]:
    if isinstance(targets, dict):
        return dict(targets)
    return {int(pgid): None for pgid in targets}


def _identity_still_matches(identity: _ProcessIdentity | None) -> bool:
    if identity is None:
        return True
    try:
        if os.getpgid(identity.pid) != identity.pgid:
            return False
    except ProcessLookupError:
        # A process-group leader may exit while descendants legitimately retain its PGID.
        # Such a PGID cannot be reused while those descendants survive. Probe the group;
        # if a new leader later reuses the numeric PID, the start-time check below rejects it.
        try:
            os.killpg(identity.pgid, 0)  # windows-footgun: ok — supervisor is POSIX-only
        except (ProcessLookupError, PermissionError, OSError):
            return False
        return True
    except (PermissionError, OSError):
        return False
    if identity.start_time and identity.start_time != "unknown":
        current_start_time = _process_start_time(identity.pid)
        if current_start_time is None or current_start_time != identity.start_time:
            return False
    return True


def _reap(targets) -> None:
    """SIGTERM every group, then SIGKILL whatever is still alive.

    Every process-group call below is POSIX-only by construction: this whole
    module only ever runs as a child of ``_update_death_supervisor``, which
    returns early unless ``os.name == "posix"``, so the supervisor is never
    spawned on Windows in the first place.
    """
    identities = _identity_by_pgid(targets)
    if not identities:
        return

    alive = set()
    for pgid, identity in identities.items():
        if not _identity_still_matches(identity):
            continue
        try:
            os.killpg(pgid, signal.SIGTERM)  # windows-footgun: ok — POSIX-only process
            alive.add(pgid)
        except (ProcessLookupError, PermissionError, OSError):
            # Already gone, or not ours to signal. Either way, nothing to reap.
            pass

    deadline = time.monotonic() + _TERM_GRACE_S
    while alive and time.monotonic() < deadline:
        time.sleep(_REAP_POLL_S)
        for pgid in list(alive):
            if not _identity_still_matches(identities.get(pgid)):
                alive.discard(pgid)
                continue
            try:
                # Signal 0 probes liveness: succeeds iff some member survives.
                os.killpg(pgid, 0)  # windows-footgun: ok — POSIX-only process
            except (ProcessLookupError, PermissionError, OSError):
                alive.discard(pgid)

    for pgid in alive:
        if not _identity_still_matches(identities.get(pgid)):
            continue
        try:
            os.killpg(pgid, signal.SIGKILL)  # windows-footgun: ok — POSIX-only
        except (ProcessLookupError, PermissionError, OSError):
            pass


def _linux_peer_pid(conn: socket.socket) -> int | None:
    if not hasattr(socket, "SO_PEERCRED"):
        return None
    try:
        raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        pid, _uid, _gid = struct.unpack("3i", raw)
        return int(pid)
    except OSError:
        return None


def _process_start_time(pid: int) -> str | None:
    try:
        stat = open(f"/proc/{pid}/stat", encoding="ascii").read()
    except OSError:
        return None
    end = stat.rfind(")")
    if end == -1:
        return None
    fields = stat[end + 2 :].split()
    if len(fields) <= 19:
        return None
    return fields[19]


def _apply_parent_command(
    line: str,
    registered,
    reservations: set[str],
    *,
    own_pgid: int,
    parent_pgid: int,
) -> bool:
    parts = line.split()
    if len(parts) == 2 and parts[0] == "reserve":
        reservations.add(parts[1])
        return True
    if len(parts) == 2 and parts[0] == "cancel":
        reservations.discard(parts[1])
        return True
    if len(parts) == 4 and parts[0] == "register":
        _verb, raw_pid, raw_pgid, start_time = parts
        try:
            pid = int(raw_pid)
            pgid = int(raw_pgid)
        except ValueError:
            return False
        if not _is_safe_target(pgid, own_pgid=own_pgid, parent_pgid=parent_pgid):
            return False
        if isinstance(registered, dict):
            registered[pgid] = _ProcessIdentity(pid=pid, pgid=pgid, start_time=start_time)
        else:
            registered.add(pgid)
        return True
    if len(parts) != 2:
        return False
    verb, raw = parts
    try:
        pgid = int(raw)
    except ValueError:
        return False
    if verb == "register":
        if _is_safe_target(pgid, own_pgid=own_pgid, parent_pgid=parent_pgid):
            if isinstance(registered, dict):
                # A bootstrap socket registration carries pid/start-time identity.
                # A raced legacy duplicate from parent PID discovery may fill an absent
                # entry but must never downgrade an acknowledged modern registration.
                registered.setdefault(pgid, None)
            else:
                registered.add(pgid)
            return True
        return False
    if verb == "unregister":
        if isinstance(registered, dict):
            registered.pop(pgid, None)
        else:
            registered.discard(pgid)
        return True
    return False


def _apply_socket_registration(
    line: str,
    registered,
    reservations: set[str],
    *,
    peer_pid: int | None,
    own_pgid: int,
    parent_pgid: int,
) -> bool:
    parts = line.split()
    if len(parts) != 5 or parts[0] != "register":
        return False
    _verb, token, raw_pid, raw_pgid, start_time = parts
    if token not in reservations:
        return False
    try:
        pid = int(raw_pid)
        pgid = int(raw_pgid)
    except ValueError:
        return False
    if peer_pid is not None and peer_pid != pid:
        return False
    if pid != pgid:
        return False
    try:
        if os.getpgid(pid) != pgid:
            return False
    except OSError:
        return False
    current_start_time = _process_start_time(pid)
    if current_start_time is not None and current_start_time != start_time:
        return False
    if not _is_safe_target(pgid, own_pgid=own_pgid, parent_pgid=parent_pgid):
        return False
    reservations.discard(token)
    if isinstance(registered, dict):
        registered[pgid] = _ProcessIdentity(pid=pid, pgid=pgid, start_time=start_time)
    else:
        registered.add(pgid)
    return True


def _serve(stream, *, own_pgid: int, parent_pgid: int) -> set[int]:
    """Read control lines until EOF; return the groups still registered.

    Reads are length-capped rather than newline-terminated. Iterating the
    stream instead lets a writer that never sends a newline grow this process
    without bound -- feeding it ``/dev/zero`` reached 15 GB before it was
    stopped. Nothing in Hermes can produce that today, but this process is the
    last line of defense against leaked servers, so it must not be the thing
    that dies under memory pressure. A line truncated by the cap fails to parse
    and is skipped; the remainder resyncs at the next newline.
    """
    registered: set[int] = set()
    while True:
        line = stream.readline(_MAX_LINE_CHARS)
        if not line:
            break  # EOF: the parent is gone.
        if not line.endswith("\n"):
            # Truncated by the cap, or an unterminated tail at EOF. Either way
            # it is not a command we are willing to act on.
            continue
        _apply_parent_command(
            line, registered, set(), own_pgid=own_pgid, parent_pgid=parent_pgid
        )
    return registered


class _SocketClient:
    def __init__(self, conn: socket.socket):
        self.conn = conn
        self.buf = bytearray()
        self.deadline = time.monotonic() + _SOCKET_CLIENT_DEADLINE_S
        self.peer_pid = _linux_peer_pid(conn)


class _ParentFdReader:
    def __init__(self, stream):
        self.fd = stream.fileno()
        os.set_blocking(self.fd, False)
        self.buf = bytearray()
        self.discarding = False

    def drain(self, registered, reservations: set[str], *, own_pgid: int, parent_pgid: int) -> bool:
        while True:
            try:
                data = os.read(self.fd, 4096)
            except BlockingIOError:
                break
            if not data:
                return False
            self.buf.extend(data)
            while True:
                newline = self.buf.find(b"\n")
                if newline == -1:
                    if len(self.buf) >= _MAX_LINE_CHARS:
                        self.buf.clear()
                        self.discarding = True
                    break
                raw_line = bytes(self.buf[: newline + 1])
                del self.buf[: newline + 1]
                if self.discarding:
                    self.discarding = False
                    continue
                if len(raw_line) > _MAX_LINE_CHARS:
                    continue
                try:
                    line = raw_line.decode("ascii", "strict")
                except UnicodeError:
                    continue
                _apply_parent_command(line, registered, reservations, own_pgid=own_pgid, parent_pgid=parent_pgid)
        return True


def _read_parent_line(stream, registered, reservations: set[str], *, own_pgid: int, parent_pgid: int) -> bool:
    line = stream.readline(_MAX_LINE_CHARS)
    if not line:
        return False
    if line.endswith("\n"):
        _apply_parent_command(line, registered, reservations, own_pgid=own_pgid, parent_pgid=parent_pgid)
    return True


def _close_client(sel: selectors.BaseSelector, client: _SocketClient) -> None:
    try:
        sel.unregister(client.conn)
    except (KeyError, ValueError, OSError):
        pass
    try:
        client.conn.close()
    except OSError:
        pass


def _parent_eof_pending(parent_reader: _ParentFdReader, registered, reservations: set[str], *, own_pgid: int, parent_pgid: int) -> bool:
    ready, _write, _err = select.select([parent_reader.fd], [], [], 0)
    return bool(ready) and not parent_reader.drain(
        registered, reservations, own_pgid=own_pgid, parent_pgid=parent_pgid
    )


def _serve_with_socket(stream, listener, *, own_pgid: int, parent_pgid: int) -> dict[int, _ProcessIdentity | None]:
    registered: dict[int, _ProcessIdentity | None] = {}
    reservations: set[str] = set()
    parent_reader = _ParentFdReader(stream)
    sel = selectors.DefaultSelector()
    sel.register(parent_reader.fd, selectors.EVENT_READ, "parent")
    sel.register(listener, selectors.EVENT_READ, "socket")
    try:
        while True:
            now = time.monotonic()
            for key in list(sel.get_map().values()):
                if isinstance(key.data, _SocketClient) and key.data.deadline <= now:
                    _close_client(sel, key.data)
            for key, _events in sorted(sel.select(_SOCKET_POLL_S), key=lambda item: item[0].data != "parent"):
                if key.data == "parent":
                    if not parent_reader.drain(registered, reservations, own_pgid=own_pgid, parent_pgid=parent_pgid):
                        return registered
                    continue
                if key.data == "socket":
                    try:
                        conn, _addr = listener.accept()
                        conn.setblocking(False)
                        sel.register(conn, selectors.EVENT_READ, _SocketClient(conn))
                    except OSError:
                        continue
                    continue
                client = key.data
                try:
                    data = client.conn.recv(_MAX_LINE_CHARS - len(client.buf))
                except OSError:
                    _close_client(sel, client)
                    continue
                if not data:
                    _close_client(sel, client)
                    continue
                client.buf.extend(data)
                if len(client.buf) >= _MAX_LINE_CHARS and b"\n" not in client.buf:
                    _close_client(sel, client)
                    continue
                if b"\n" not in client.buf:
                    continue
                raw_line, _sep, _tail = bytes(client.buf).partition(b"\n")
                try:
                    line = raw_line.decode("ascii", "strict") + "\n"
                except UnicodeError:
                    _close_client(sel, client)
                    continue
                if _parent_eof_pending(
                    parent_reader, registered, reservations, own_pgid=own_pgid, parent_pgid=parent_pgid
                ):
                    _close_client(sel, client)
                    return registered
                if _apply_socket_registration(
                    line,
                    registered,
                    reservations,
                    peer_pid=client.peer_pid,
                    own_pgid=own_pgid,
                    parent_pgid=parent_pgid,
                ):
                    try:
                        client.conn.sendall(b"ok\n")
                    except OSError:
                        pass
                _close_client(sel, client)
    finally:
        sel.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Reap registered process groups when the parent dies."
    )
    parser.add_argument(
        "--parent-pgid",
        type=int,
        required=True,
        help="Process group of the spawning Hermes process; never signalled.",
    )
    parser.add_argument(
        "--socket-path",
        help="Unix socket used by stdio bootstraps for acknowledged registration.",
    )
    args = parser.parse_args(argv)

    # The parent may be torn down with killpg on its own group. We are spawned
    # with start_new_session=True precisely so that sweep cannot take us with
    # it before we have reaped -- assert that here rather than trust the caller.
    own_pgid = os.getpgid(0)
    if own_pgid == args.parent_pgid:
        print(
            "mcp_death_supervisor: refusing to run inside the parent's process "
            "group (a killpg of the parent would kill us before we can reap)",
            file=sys.stderr,
        )
        return 2

    # A dying parent's SIGINT/SIGHUP must not preempt the reap; the pipe's EOF
    # is our only shutdown signal. SIGHUP is POSIX-only, which is fine here --
    # this process is never spawned on Windows (see _reap's docstring).
    for sig in (signal.SIGINT, signal.SIGHUP):  # windows-footgun: ok — POSIX-only process
        try:
            signal.signal(sig, signal.SIG_IGN)
        except (ValueError, OSError):
            pass

    listener = None
    if args.socket_path:
        try:
            if os.path.exists(args.socket_path):
                os.unlink(args.socket_path)
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(args.socket_path)
            listener.listen(_SOCKET_BACKLOG)
            listener.setblocking(False)
            print("ready", flush=True)
        except OSError as exc:
            print(f"mcp_death_supervisor: could not listen on {args.socket_path}: {exc}", file=sys.stderr)
            return 3

    if listener is None:
        registered = _serve(sys.stdin, own_pgid=own_pgid, parent_pgid=args.parent_pgid)
    else:
        try:
            registered = _serve_with_socket(sys.stdin, listener, own_pgid=own_pgid, parent_pgid=args.parent_pgid)
        finally:
            listener.close()
            try:
                os.unlink(args.socket_path)
            except OSError:
                pass
    _reap(registered)
    return 0


if __name__ == "__main__":
    sys.exit(main())
