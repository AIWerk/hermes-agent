from __future__ import annotations

import json
import os
import socket
import sys
import time
from pathlib import Path
from typing import Any


_IDLE_TIMEOUT_SECONDS = 900
_MAX_PAYLOAD_BYTES = 8192


def _coerce_result(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    if "result" in payload:
        payload = payload.get("result")
    if not isinstance(payload, dict):
        return None
    if payload.get("ok") is not True:
        return None
    actor_id = payload.get("actor_id")
    role = payload.get("role")
    if not isinstance(actor_id, str) or not actor_id.strip():
        return None
    if not isinstance(role, str) or not role.strip():
        return None
    raw_verified_at = payload.get("verified_at")
    raw_expires_at = payload.get("expires_at")
    if raw_verified_at is None or raw_expires_at is None:
        return None
    try:
        verified_at = int(raw_verified_at)
        expires_at = int(raw_expires_at)
    except (TypeError, ValueError):
        return None
    return {
        "ok": True,
        "actor_id": actor_id.strip(),
        "role": role.strip(),
        "verified_at": verified_at,
        "expires_at": expires_at,
        "reason": str(payload.get("reason") or ""),
    }


def _serve(socket_path: Path, result: dict[str, Any], cache_key: str) -> int:
    socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        socket_path.unlink()
    except FileNotFoundError:
        pass

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(socket_path))
        os.chmod(socket_path, 0o600)
        server.listen(8)
        server.settimeout(1.0)
        deadline = time.monotonic() + _IDLE_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            with conn:
                try:
                    conn.settimeout(2.0)
                    raw_request = conn.recv(_MAX_PAYLOAD_BYTES)
                    request_key = "__process__"
                    try:
                        request = json.loads(raw_request.decode("utf-8")) if raw_request else {}
                        if isinstance(request, dict):
                            raw_session = request.get("session_id")
                            request_key = str(raw_session) if raw_session else "__process__"
                    except Exception:
                        request_key = "__process__"
                    if cache_key not in {"__process__", request_key}:
                        conn.sendall(b"{}\n")
                        continue
                    conn.sendall(json.dumps(result, separators=(",", ":")).encode("utf-8") + b"\n")
                except Exception:
                    continue
        return 0
    finally:
        server.close()
        try:
            socket_path.unlink()
        except FileNotFoundError:
            pass


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        return 2
    try:
        raw = sys.stdin.buffer.read(_MAX_PAYLOAD_BYTES + 1)
        if len(raw) > _MAX_PAYLOAD_BYTES:
            return 2
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        return 2
    cache_key = str(payload.get("key") or "__process__") if isinstance(payload, dict) else "__process__"
    result = _coerce_result(payload)
    if result is None:
        return 2
    return _serve(Path(argv[0]), result, cache_key)


if __name__ == "__main__":
    raise SystemExit(main())
