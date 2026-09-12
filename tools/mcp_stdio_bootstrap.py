#!/usr/bin/env python3
"""Register the stdio MCP process group before execing the real server."""

from __future__ import annotations

import os
import socket
import sys

_SOCKET_ENV = "HERMES_MCP_DEATH_SUPERVISOR_SOCKET"
_TOKEN_ENV = "HERMES_MCP_DEATH_SUPERVISOR_TOKEN"
_ACK_TIMEOUT_S = 5.0


def _ensure_process_group() -> int:
    """Return this process group, creating a private session if the caller did not."""
    if hasattr(os, "setsid") and os.getpgrp() != os.getpid():
        os.setsid()  # windows-footgun: ok — guarded by hasattr(os, "setsid")
    return os.getpgrp()


def _process_start_time(pid: int) -> str:
    try:
        stat = open(f"/proc/{pid}/stat", encoding="ascii").read()
    except OSError:
        return "unknown"
    end = stat.rfind(")")
    if end == -1:
        return "unknown"
    fields = stat[end + 2 :].split()
    if len(fields) <= 19:
        return "unknown"
    return fields[19]


def _register_or_die(pgid: int) -> None:
    socket_path = os.environ.get(_SOCKET_ENV)
    if not socket_path:
        raise RuntimeError("missing MCP death-supervisor socket")
    token = os.environ.get(_TOKEN_ENV)
    if not token:
        raise RuntimeError("missing MCP death-supervisor reservation token")
    pid = os.getpid()
    start_time = _process_start_time(pid)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(_ACK_TIMEOUT_S)
        sock.connect(socket_path)
        sock.sendall(f"register {token} {pid} {pgid} {start_time}\n".encode("ascii"))
        ack = sock.recv(16)
    if ack != b"ok\n":
        raise RuntimeError("MCP death-supervisor registration was not acknowledged")


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        print("mcp_stdio_bootstrap: missing server command", file=sys.stderr)
        return 2
    command, args = argv[0], argv[1:]
    try:
        pgid = _ensure_process_group()
        _register_or_die(pgid)
    except Exception as exc:
        print(f"mcp_stdio_bootstrap: {exc}", file=sys.stderr)
        return 125
    env = dict(os.environ)
    env.pop(_SOCKET_ENV, None)
    env.pop(_TOKEN_ENV, None)
    os.execvpe(command, [command, *args], env)
    return 127


if __name__ == "__main__":
    sys.exit(main())
