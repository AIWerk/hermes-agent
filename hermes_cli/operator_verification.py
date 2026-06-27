from __future__ import annotations

from dataclasses import dataclass
import base64
import hashlib
import hmac
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any


MAX_VERIFIER_STDOUT_CHARS = 4096
_DEFAULT_TTL_SECONDS = 900
_DEFAULT_TIMEOUT_SECONDS = 60
_STORE = Path.home() / ".hermes" / "operator-verifier.json"
_ITERATIONS = 260_000


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
    interface: str = ""
    verifier_type: str = "command"
    missing_interface: bool = False
    trusted_actor_ids: list[str] | None = None
    allowed_secret_read_patterns: list[str] | None = None


_ADMIN_MUTATING_SUBCOMMANDS = {
    "systemctl": {"start", "stop", "restart", "reload", "enable", "disable", "mask", "unmask", "poweroff", "reboot", "halt", "kexec"},
    "service": {"start", "stop", "restart", "reload", "enable", "disable", "mask", "unmask"},
    "kubectl": {"apply", "delete", "exec", "patch", "replace", "scale", "cordon", "uncordon", "drain", "taint", "create", "edit", "rollout"},
    "helm": {"install", "upgrade", "rollback", "uninstall", "delete"},
    "terraform": {"apply", "destroy", "import", "taint", "untaint", "state", "force-unlock"},
}
_ADMIN_READONLY_SUBCOMMANDS = {
    "systemctl": {"status", "is-active", "is-enabled", "list-units", "list-unit-files", "cat", "show"},
    "service": {"status"},
    "kubectl": {"get", "describe", "logs", "top", "explain", "diff", "version", "config"},
    "helm": {"list", "status", "history", "template", "show", "get", "repo", "search", "version", "lint"},
    "terraform": {"fmt", "validate", "plan", "output", "show", "version", "providers", "workspace"},
}


def _split_command(command: str) -> list[str]:
    try:
        return shlex.split(command, comments=False, posix=True)
    except ValueError:
        return []


def _base_command(tokens: list[str]) -> tuple[str, list[str]]:
    while tokens and tokens[0] in {"sudo", "env", "command", "builtin", "exec", "time"}:
        head = tokens.pop(0)
        if head == "env":
            while tokens and "=" in tokens[0] and not tokens[0].startswith("-"):
                tokens.pop(0)
    return (tokens[0].lower(), tokens[1:]) if tokens else ("", [])


def _first_non_option(args: list[str]) -> str:
    for arg in args:
        if not arg.startswith("-"):
            return arg.lower()
    return ""


def _eval_payloads(tokens: list[str]) -> list[str]:
    payloads: list[str] = []
    for index, token in enumerate(tokens[:-1]):
        if token == "eval":
            payload = tokens[index + 1].strip()
            if payload:
                payloads.append(payload)
    return payloads


def _wrapped_shell_requires_operator_verification(
    tokens: list[str], config: OperatorVerificationConfig
) -> bool | None:
    payloads = _eval_payloads(tokens)
    if not payloads:
        return None
    return any(_requires_operator_verification(payload, config) for payload in payloads)


def _is_remote_path(arg: str) -> bool:
    return bool(re.match(r"^[^/@\s:]+@?[^\s:]+:.+", arg))


def _copy_args(args: list[str]) -> list[str]:
    return [arg for arg in args if not arg.startswith("-")]


def _matches_allowed_secret_read(command: str, patterns: list[str] | None) -> bool:
    normalized = " ".join(_split_command(command)) or command.strip()
    for pattern in patterns or []:
        try:
            if re.fullmatch(pattern, normalized):
                return True
        except re.error:
            continue
    return False


def _requires_operator_verification(command: str, config: OperatorVerificationConfig) -> bool:
    tokens = _split_command(command)
    if not tokens:
        return bool(_SENSITIVE_COMMAND_RE.search(command or ""))
    wrapped = _wrapped_shell_requires_operator_verification(tokens, config)
    if wrapped is not None:
        return wrapped
    cmd, args = _base_command(tokens)
    verb = _first_non_option(args)

    if cmd == "pass" and verb == "show":
        return not _matches_allowed_secret_read(command, config.allowed_secret_read_patterns)
    if cmd == "bw" and verb == "get":
        return not _matches_allowed_secret_read(command, config.allowed_secret_read_patterns)
    if cmd in {"vault", "get-secret"}:
        return True
    if cmd == "gh" and verb == "secret":
        return True

    if cmd in _ADMIN_MUTATING_SUBCOMMANDS:
        if cmd == "service" and len(args) >= 2:
            verb = args[1].lower()
        if verb in _ADMIN_READONLY_SUBCOMMANDS.get(cmd, set()):
            return False
        if cmd == "kubectl" and verb == "rollout":
            return any(arg.lower() == "restart" for arg in args)
        return verb in _ADMIN_MUTATING_SUBCOMMANDS[cmd]

    if cmd == "docker":
        if args[:1] == ["compose"]:
            compose_verb = _first_non_option(args[1:])
            return compose_verb in {"restart", "stop", "kill", "down", "rm", "rmi"}
        return verb in {"restart", "stop", "kill", "rm", "rmi"}

    if cmd in {"scp", "rsync"}:
        paths = _copy_args(args)
        if len(paths) >= 2:
            src, dest = paths[-2], paths[-1]
            return not (_is_remote_path(src) and not _is_remote_path(dest))
        return True

    if cmd == "ansible-playbook":
        return True
    if cmd == "chmod":
        arg_text = " ".join(args).lower()
        return bool(re.search(r"(^|\s)-(?:\S*r\S*|\S*recursive\S*)\b", arg_text) or re.search(r"(^|\s)(777|666)($|\s)", arg_text))
    if cmd == "chown":
        arg_text = " ".join(args).lower()
        return "root" in args or bool(re.search(r"(^|\s)-(?:\S*r\S*|\S*recursive\S*)\b", arg_text))
    if cmd == "rm":
        return any("r" in arg and "f" in arg for arg in args if arg.startswith("-"))
    if cmd in {"dd", "mkfs"}:
        return True
    if cmd == "git" and verb == "push":
        lowered = {arg.lower() for arg in args}
        return "--force" in lowered or "-f" in lowered or any(arg.startswith(":") or ":" in arg for arg in args[1:])
    if cmd == "hermes" and len(args) >= 2:
        area, action = args[0].lower(), args[1].lower()
        return (area == "gateway" and action in {"restart", "stop", "start"}) or (area == "profile" and action in {"delete", "use", "rename"}) or (area == "cron" and action in {"remove", "create", "edit"})

    return bool(_SENSITIVE_COMMAND_RE.search(command or ""))


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
_callback_tls = threading.local()


def _get_operator_verification_callback():
    return getattr(_callback_tls, "operator_verification", None)


def set_operator_verification_callback(cb) -> None:
    """Register a masked in-process operator verifier prompt callback."""
    _callback_tls.operator_verification = cb


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
    cache_key = key
    result = _cache.get(key)
    if result is None and session_id is not None:
        cache_key = _cache_key(None)
        result = _cache.get(cache_key)
    if result is None:
        return None
    if not result.is_valid(now=now):
        _cache.pop(cache_key, None)
        return None
    return result


def _coerce_positive_int(value: Any, default: int, *, minimum: int = 1, maximum: int = 86400) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _normalize_interface(value: Any) -> str:
    raw = str(value or "").strip().lower().replace("_", "-")
    aliases = {
        "terminal": "cli",
        "shell": "cli",
        "cui": "web",
        "web-cui": "web",
        "desktop": "local",
        "gui": "local",
    }
    return aliases.get(raw, raw)


def current_operator_interface() -> str:
    """Return the active communication surface for operator verification.

    This is not a fallback preference. Gateway/CUI platform context wins. A
    local CLI is only ``cli`` when the process has a real controlling terminal;
    Hermes tool-runner calls from a local desktop session otherwise route to
    ``local`` so they can use the desktop verifier instead of an invisible
    /dev/tty prompt.
    """
    try:
        from gateway.session_context import get_session_env

        platform = get_session_env("HERMES_SESSION_PLATFORM", "")
    except Exception:
        platform = os.getenv("HERMES_SESSION_PLATFORM", "")
    platform = _normalize_interface(platform)
    if platform:
        return platform
    if os.getenv("AIWERK_CUI_ACTOR_CONTEXT") or os.getenv("AIWERK_CUI_ACTOR_ROLE"):
        return "web"
    explicit = _normalize_interface(os.getenv("HERMES_OPERATOR_INTERFACE", ""))
    if explicit:
        return explicit
    if os.getenv("HERMES_INTERACTIVE", "").strip().lower() in {"1", "true", "yes", "on"}:
        # The interactive Hermes CLI owns the user-facing prompt even when tool
        # workers themselves have no controlling TTY. Route to the in-process
        # masked callback, not the desktop GUI verifier.
        return "cli"
    return "local"


def _command_settings(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    nested = raw.get("command")
    if isinstance(nested, dict):
        merged = dict(raw)
        merged.update(nested)
        return merged
    return raw


def _argv_from_command(command: dict[str, Any], fallback: Any = None) -> list[str]:
    argv = command.get("argv", fallback if fallback is not None else [])
    if isinstance(argv, str):
        return [argv] if argv else []
    if isinstance(argv, (list, tuple)):
        return [str(part) for part in argv if str(part)]
    return []


def load_operator_verification_config(interface: str | None = None) -> OperatorVerificationConfig:
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

    selected_interface = _normalize_interface(interface) or current_operator_interface()
    command = _command_settings(section.get("command"))
    verifier_type = str(section.get("verifier") or "command").strip().lower() or "command"
    trusted_actor_ids = [str(item) for item in (section.get("trusted_actor_ids") or []) if str(item)]
    allowed_secret_read_patterns = [
        str(item)
        for item in (section.get("allowed_secret_read_patterns") or [])
        if str(item)
    ]
    interfaces = section.get("interfaces", section.get("verifiers", {}))
    missing_interface = False
    if isinstance(interfaces, dict):
        selected = interfaces.get(selected_interface)
        if isinstance(selected, dict):
            selected_command = _command_settings(selected)
            verifier_type = str(selected.get("verifier") or selected_command.get("verifier") or verifier_type).strip().lower() or "command"
            if isinstance(selected.get("trusted_actor_ids"), list):
                trusted_actor_ids = [str(item) for item in selected.get("trusted_actor_ids", []) if str(item)]
            command = selected_command
        elif interfaces:
            # If interface-specific verifiers are configured, never fall back to
            # the generic command for a different channel. Invisible verifier
            # prompts are worse than failing closed.
            command = {}
            missing_interface = True
    argv = _argv_from_command(command, section.get("argv", []))

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
        interface=selected_interface,
        verifier_type=verifier_type,
        missing_interface=missing_interface,
        trusted_actor_ids=trusted_actor_ids,
        allowed_secret_read_patterns=allowed_secret_read_patterns,
    )


def _failure(reason: str, *, now: int | None = None) -> OperatorVerificationResult:
    current = int(time.time()) if now is None else int(now)
    return OperatorVerificationResult(
        ok=False,
        verified_at=current,
        expires_at=current,
        reason=reason,
    )


def _load_operator_store() -> dict | None:
    try:
        data = json.loads(_STORE.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict) or data.get("version") != 1:
        return None
    if not data.get("salt") or not data.get("hash"):
        return None
    return data


def _derive_operator_secret(secret: str, salt_b64: str) -> str:
    salt = base64.b64decode(salt_b64.encode("ascii"))
    digest = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, _ITERATIONS)
    return base64.b64encode(digest).decode("ascii")


def _verify_operator_secret(secret: str, data: dict | None = None) -> bool:
    if not secret:
        return False
    data = data or _load_operator_store()
    if not data:
        return False
    try:
        actual = _derive_operator_secret(secret, str(data.get("salt") or ""))
    except Exception:
        return False
    return hmac.compare_digest(actual, str(data.get("hash") or ""))


def _callback_operator_verification(config: OperatorVerificationConfig, *, now: int) -> OperatorVerificationResult:
    data = _load_operator_store()
    if not data:
        return _failure("not_configured", now=now)
    callback = _get_operator_verification_callback()
    if callback is None:
        return _failure("callback_not_available", now=now)
    try:
        secret = callback() or ""
    except Exception:
        return _failure("invalid_or_cancelled", now=now)
    if not secret:
        return _failure("invalid_or_cancelled", now=now)
    if not _verify_operator_secret(secret, data):
        return _failure("verification_failed", now=now)
    return OperatorVerificationResult(
        ok=True,
        actor_id=str(data.get("actor_id") or "attila"),
        role=str(data.get("role") or "operator"),
        verified_at=now,
        expires_at=now + config.ttl_seconds,
    )


def _cui_actor_verification(config: OperatorVerificationConfig, *, now: int) -> OperatorVerificationResult:
    """Trust an authenticated AIWerk CUI admin/operator actor as verifier.

    The CUI auth layer is the channel verifier: if the child process has actor
    metadata for an admin/operator role, no second approval prompt is needed.
    Missing or customer-only actor context fails closed.
    """
    raw = os.getenv("AIWERK_CUI_ACTOR_CONTEXT", "") or ""
    data: dict[str, Any] = {}
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                data = parsed
        except Exception:
            data = {}
    actor_id = str(
        data.get("actor_id")
        or data.get("user_id")
        or os.getenv("AIWERK_CUI_ACTOR_ID", "")
        or ""
    ).strip()
    role = str(data.get("role") or os.getenv("AIWERK_CUI_ACTOR_ROLE", "") or "").strip().lower()
    allowed_roles = {"aiwerk_admin", "admin", "operator", "owner", "tenant_admin"}
    if not actor_id or role not in allowed_roles:
        return _failure("cui_actor_not_authorized", now=now)
    return OperatorVerificationResult(
        ok=True,
        actor_id=actor_id,
        role=role,
        verified_at=now,
        expires_at=now + config.ttl_seconds,
    )


def _trusted_platform_actor_verification(config: OperatorVerificationConfig, *, now: int) -> OperatorVerificationResult:
    """Trust the current gateway platform actor only when explicitly allowlisted."""
    try:
        from gateway.session_context import get_session_env
        platform = get_session_env("HERMES_SESSION_PLATFORM", "") or os.getenv("HERMES_SESSION_PLATFORM", "") or ""
        actor_id = (
            get_session_env("HERMES_SESSION_USER_ID", "")
            or get_session_env("HERMES_SESSION_CHAT_ID", "")
            or os.getenv("HERMES_SESSION_USER_ID", "")
            or os.getenv("HERMES_SESSION_CHAT_ID", "")
            or ""
        )
        actor_name = get_session_env("HERMES_SESSION_USER_NAME", "") or os.getenv("HERMES_SESSION_USER_NAME", "") or ""
    except Exception:
        platform = os.getenv("HERMES_SESSION_PLATFORM", "") or ""
        actor_id = os.getenv("HERMES_SESSION_USER_ID", "") or os.getenv("HERMES_SESSION_CHAT_ID", "") or ""
        actor_name = os.getenv("HERMES_SESSION_USER_NAME", "") or ""
    trusted = set(config.trusted_actor_ids or [])
    candidates = {str(actor_id), str(actor_name)} - {""}
    if not platform or not candidates or not (trusted & candidates):
        return _failure("platform_actor_not_authorized", now=now)
    return OperatorVerificationResult(
        ok=True,
        actor_id=str(actor_id or actor_name),
        role="operator",
        verified_at=now,
        expires_at=now + config.ttl_seconds,
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
    if not _requires_operator_verification(command or "", cfg):
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
    if cfg.missing_interface:
        return _failure("not_configured_for_interface", now=current)
    if cfg.verifier_type in {"cui_actor", "cui-actor", "cui_admin", "cui-admin", "cui_actor_context", "cui-actor-context"}:
        return _cui_actor_verification(cfg, now=current)
    if cfg.verifier_type in {"trusted_platform_actor", "trusted-platform-actor", "platform_actor", "platform-actor"}:
        return _trusted_platform_actor_verification(cfg, now=current)
    if cfg.verifier_type in {"callback", "operator_callback", "operator-callback", "prompt", "modal"}:
        return _callback_operator_verification(cfg, now=current)
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
