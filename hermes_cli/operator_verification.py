from __future__ import annotations

from dataclasses import dataclass
import json
import re
import subprocess
import time
from typing import Any


MAX_VERIFIER_STDOUT_CHARS = 4096
_DEFAULT_TTL_SECONDS = 900
_DEFAULT_TIMEOUT_SECONDS = 60


@dataclass(frozen=True)
class OperatorVerificationResult:
    ok: bool
    actor_id: str = ""
    role: str = ""
    verified_at: int = 0
    expires_at: int = 0
    reason: str = ""

    def is_valid(self, *, now: int | None = None) -> bool:
        current = int(time.time()) if now is None else int(now)
        return self.ok and bool(self.actor_id) and bool(self.role) and current < self.expires_at


@dataclass(frozen=True)
class OperatorVerificationConfig:
    enabled: bool = False
    argv: list[str] | None = None
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS
    ttl_seconds: int = _DEFAULT_TTL_SECONDS
    require_for_cli_admin: bool = True


_SENSITIVE_COMMAND_RE = re.compile(
    r"\b("
    r"systemctl|service|supervisorctl|docker\s+compose\s+(?:up|down|restart)|"
    r"docker\s+(?:restart|rm|rmi)|kubectl|helm|terraform|ansible-playbook|"
    r"rsync|scp|pass\s+show|bw\s+get|vault\s+kv|get-secret|"
    r"chmod\s+(?:777|[0-7]{3,4})|chown|rm\s+-rf|dd\s+if=|mkfs|"
    r"git\s+push|gh\s+secret|hermes\s+gateway\s+(?:restart|stop|start)|"
    r"hermes\s+profile\s+(?:delete|use|rename)|hermes\s+cron\s+(?:remove|create|edit)"
    r")\b",
    re.IGNORECASE,
)


_cache: dict[str, OperatorVerificationResult] = {}


def _cache_key(session_id: str | None = None) -> str:
    return session_id or "__process__"


def clear_operator_verification_cache() -> None:
    _cache.clear()


def cache_operator_verification(
    result: OperatorVerificationResult, *, session_id: str | None = None
) -> None:
    if result.ok and result.actor_id and result.role:
        _cache[_cache_key(session_id)] = result


def get_cached_operator_verification(
    *, session_id: str | None = None, now: int | None = None
) -> OperatorVerificationResult | None:
    key = _cache_key(session_id)
    result = _cache.get(key)
    if result is None:
        return None
    if not result.is_valid(now=now):
        _cache.pop(key, None)
        return None
    return result


def _coerce_positive_int(value: Any, default: int, *, minimum: int = 1, maximum: int = 86400) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def load_operator_verification_config() -> OperatorVerificationConfig:
    """Load operator verification settings from config.yaml.

    The verifier command may request secrets from the human, but the command
    path/argv itself is non-secret configuration and therefore belongs in
    config.yaml rather than .env.
    """
    try:
        from hermes_cli.config import load_config
        config = load_config()
    except Exception:
        config = {}

    section = (
        (config.get("security") or {})
        .get("operator_verification", {})
        if isinstance(config, dict)
        else {}
    )
    if not isinstance(section, dict):
        section = {}

    raw_command = section.get("command")
    command = raw_command if isinstance(raw_command, dict) else {}
    argv = command.get("argv", section.get("argv", []))
    if isinstance(argv, str):
        argv = [argv]
    elif isinstance(argv, (list, tuple)):
        argv = [str(part) for part in argv if str(part)]
    else:
        argv = []

    return OperatorVerificationConfig(
        enabled=bool(section.get("enabled", False)),
        argv=argv,
        timeout_seconds=_coerce_positive_int(
            command.get("timeout_seconds", section.get("timeout_seconds")),
            _DEFAULT_TIMEOUT_SECONDS,
            maximum=300,
        ),
        ttl_seconds=_coerce_positive_int(
            section.get("ttl_seconds"),
            _DEFAULT_TTL_SECONDS,
            maximum=86400,
        ),
        require_for_cli_admin=bool(section.get("require_for_cli_admin", True)),
    )


def _failure(reason: str, *, now: int | None = None) -> OperatorVerificationResult:
    current = int(time.time()) if now is None else int(now)
    return OperatorVerificationResult(
        ok=False,
        verified_at=current,
        expires_at=current,
        reason=reason,
    )


def operator_verification_block_reason_for_command(
    command: str,
    *,
    config: OperatorVerificationConfig | None = None,
    session_id: str | None = None,
    now: int | None = None,
) -> str | None:
    cfg = config or load_operator_verification_config()
    if not cfg.enabled or not cfg.require_for_cli_admin:
        return None
    if not _SENSITIVE_COMMAND_RE.search(command or ""):
        return None
    if get_cached_operator_verification(session_id=session_id, now=now) is not None:
        return None
    return (
        "Operator verification required before running this admin-sensitive "
        "command from a CLI/TUI session. Call verify_operator_identity first; "
        "do not ask the user to paste the operator secret into chat."
    )


def run_operator_verifier(
    config: OperatorVerificationConfig | None = None,
    *,
    now: int | None = None,
) -> OperatorVerificationResult:
    cfg = config or load_operator_verification_config()
    current = int(time.time()) if now is None else int(now)

    if not cfg.enabled:
        return _failure("disabled", now=current)
    if not cfg.argv:
        return _failure("not_configured", now=current)

    try:
        completed = subprocess.run(
            cfg.argv,
            capture_output=True,
            text=True,
            timeout=cfg.timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _failure("timeout", now=current)
    except FileNotFoundError:
        return _failure("command_not_found", now=current)
    except Exception:
        return _failure("verifier_error", now=current)

    if completed.returncode != 0:
        return _failure("verification_failed", now=current)

    stdout = (completed.stdout or "")[:MAX_VERIFIER_STDOUT_CHARS]
    try:
        payload = json.loads(stdout)
    except Exception:
        return _failure("invalid_verifier_output", now=current)
    if not isinstance(payload, dict):
        return _failure("invalid_verifier_output", now=current)

    if not payload.get("ok"):
        reason = str(payload.get("reason") or "verification_failed")
        if reason not in {"invalid_or_cancelled", "verification_failed"}:
            reason = "verification_failed"
        return _failure(reason, now=current)

    actor_id = str(payload.get("actor_id") or "").strip()
    role = str(payload.get("role") or "").strip()
    ttl = _coerce_positive_int(payload.get("ttl_seconds"), cfg.ttl_seconds, maximum=cfg.ttl_seconds)
    result = OperatorVerificationResult(
        ok=True,
        actor_id=actor_id,
        role=role,
        verified_at=current,
        expires_at=current + ttl,
    )
    if not result.is_valid(now=current):
        return _failure("invalid_verifier_output", now=current)
    return result
