from __future__ import annotations

from dataclasses import asdict, dataclass
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import shlex
import socket
import struct
import subprocess
import sys
import threading
import time
import uuid
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
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|\n")
        lexer.whitespace_split = True
        lexer.whitespace = " \t\r"
        lexer.commenters = ""
        return list(lexer)
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


_ADMIN_GLOBAL_VALUE_OPTIONS = {
    "systemctl": {
        "--host", "-H", "--machine", "-M", "--root", "--image",
        "--image-policy", "--type", "-t", "--state", "--property", "-p",
        "--output", "-o", "--lines", "-n", "--job-mode", "--signal",
        "--kill-whom", "--kill-value", "--what", "--check-inhibitors",
    },
    "kubectl": {
        "--as", "--as-group", "--as-uid", "--cache-dir",
        "--certificate-authority", "--client-certificate", "--client-key",
        "--cluster", "--context", "--kubeconfig", "--kuberc", "--namespace", "-n",
        "--password", "--profile", "--profile-output", "--request-timeout",
        "--server", "-s", "--tls-server-name", "--token", "--user",
        "--username", "-v",
    },
    "helm": {
        "--burst-limit", "--kube-apiserver", "--kube-as-group",
        "--kube-as-user", "--kube-ca-file", "--kube-context", "--kube-token",
        "--kube-tls-server-name", "--kubeconfig", "--namespace", "-n", "--qps",
        "--registry-config", "--repository-cache", "--repository-config",
    },
    "terraform": {"-chdir"},
}


def _admin_subcommand_and_rest(cmd: str, args: list[str]) -> tuple[str, list[str]]:
    value_options = _ADMIN_GLOBAL_VALUE_OPTIONS.get(cmd, set())
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            index += 1
            break
        if not arg.startswith("-"):
            return arg.lower(), args[index + 1 :]
        bare = arg.split("=", 1)[0]
        if bare in value_options and "=" not in arg:
            index += 2
        else:
            index += 1
    if index < len(args):
        return args[index].lower(), args[index + 1 :]
    return "", []


# git global options that consume a following value token; the verb (push, etc.)
# only appears *after* both the option and its value, so they must be skipped
# before reading the subcommand. Without this, ``git -C /repo push --force``
# would read ``/repo`` as the verb and bypass the push force-check entirely.
_GIT_GLOBAL_VALUE_OPTIONS = {"-c", "-C", "--git-dir", "--work-tree", "--namespace", "--exec-path"}


def _git_subcommand_and_rest(args: list[str]) -> tuple[str, list[str]]:
    """Return git's (subcommand, remaining-args), skipping global value options.

    Handles both ``-c key=val``/``-C dir`` (value as separate token) and the
    ``--git-dir=...`` glued form so an attacker cannot push the verb out of
    reach of the force-detection logic.
    """
    index = 0
    while index < len(args):
        arg = args[index]
        if not arg.startswith("-"):
            return arg.lower(), args[index + 1 :]
        bare = arg.split("=", 1)[0]
        if bare in _GIT_GLOBAL_VALUE_OPTIONS and "=" not in arg:
            # Option and its value are separate tokens; skip both.
            index += 2
            continue
        # Glued ``--git-dir=...``/``-cfoo`` or a valueless flag: skip this token.
        index += 1
    return "", []


def _eval_payloads(tokens: list[str]) -> list[str]:
    """Return the full command(s) that follow an ``eval`` token.

    The previous implementation captured only the single token after ``eval``,
    which let an unquoted ``eval git push --force`` collapse to the payload
    ``git`` (no force, no match) and — worse — suppressed the regex fallback.
    We now reconstruct the entire remaining command after each ``eval`` so the
    real payload is scanned. The quoted form (single token) is unchanged.
    """
    payloads: list[str] = []
    for index, token in enumerate(tokens[:-1]):
        if token == "eval":
            rest = tokens[index + 1 :]
            if len(rest) == 1 or any(char.isspace() for char in rest[0]):
                payload = rest[0].strip()
            else:
                payload = shlex.join(rest).strip()
            if payload:
                payloads.append(payload)
    return payloads


def _wrapped_shell_requires_operator_verification(
    tokens: list[str], config: OperatorVerificationConfig
) -> bool:
    """True iff any ``eval`` payload requires verification (never suppresses)."""
    payloads = _eval_payloads(tokens)
    return any(_requires_operator_verification(payload, config) for payload in payloads)


def _normalize_short_flag_chars(args: list[str]) -> set[str]:
    """Collect bundled short-flag characters across all -prefixed option args.

    ``rm -rf`` and ``rm -r -f`` and ``rm -d -r -f`` all yield ``{'r','f',...}``
    so recursive+force detection no longer depends on flag spelling. Long
    options (``--force``) are ignored here and matched separately by callers.
    """
    chars: set[str] = set()
    for arg in args:
        if arg.startswith("-") and not arg.startswith("--"):
            chars.update(arg[1:])
    return chars


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


def _git_push_requires_verification(args: list[str]) -> bool:
    """True for any history-overwriting / destructive push form.

    Covers ``--force``/``-f``, ``--force-with-lease`` (bare or ``=value``),
    ``--mirror``, ``--delete``/``-d``, leading-``+`` force refspecs, and
    ``:branch`` delete refspecs.
    """
    subcommand, rest = _git_subcommand_and_rest(args)
    if subcommand != "push":
        return False
    lowered = {arg.lower() for arg in rest}
    if {"--force", "-f", "--mirror", "--delete", "-d"} & lowered:
        return True
    if any(arg.lower().split("=", 1)[0] == "--force-with-lease" for arg in rest):
        return True
    # ``+refspec`` force push (no colon) and ``src:dst`` / ``:branch`` deletes.
    return any(arg.startswith("+") or arg.startswith(":") or ":" in arg for arg in rest)


def _chmod_requires_verification(args: list[str]) -> bool:
    arg_text = " ".join(args).lower()
    if re.search(r"(^|\s)-(?:\S*r\S*|\S*recursive\S*)\b", arg_text):
        return True
    if re.search(r"(^|\s)(777|666)($|\s)", arg_text):
        return True
    # Setuid/setgid grants (privilege escalation): 4-digit octal whose leading
    # digit sets the suid(4)/sgid(2)/sticky(6+) bit, or symbolic +s.
    for arg in args:
        if arg.startswith("-"):
            continue
        if re.fullmatch(r"[1-7][0-7]{3}", arg):
            return True
        if re.search(r"[ugoa]*\+[rwxXst]*s", arg):
            return True
    return False


def _rm_requires_verification(args: list[str]) -> bool:
    short_chars = _normalize_short_flag_chars(args)
    long_flags = {arg.lower() for arg in args if arg.startswith("--")}
    recursive = "r" in short_chars or "R" in short_chars or {"--recursive", "--recursive=true"} & long_flags
    force = "f" in short_chars or "--force" in long_flags
    return bool(recursive and force)


def _chown_requires_verification(args: list[str]) -> bool:
    arg_text = " ".join(args).lower()
    if re.search(r"(^|\s)-(?:\S*r\S*|\S*recursive\S*)\b", arg_text):
        return True
    index = 0
    while index < len(args):
        arg = args[index]
        bare = arg.split("=", 1)[0]
        if bare == "--reference":
            return True
        if bare == "--from" and "=" not in arg:
            index += 2
            continue
        if arg.startswith("-"):
            index += 1
            continue
        owner = arg.lower()
        return bool(re.fullmatch(r"(?:root|\+?0)(?::[^\s]*)?|:(?:root|\+?0)", owner))
    return False


# Shell statement / pipeline separators. A compound command is split on these
# so EACH segment is classified independently — a benign leading segment can no
# longer hide a dangerous trailing one (and a benign segment is not over-blocked
# by the coarse regex just because a later segment is sensitive).
_SHELL_SEPARATORS = {";", "&&", "||", "|", "&", "|&", "\n"}


def _split_segments(tokens: list[str]) -> list[list[str]]:
    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token in _SHELL_SEPARATORS:
            if current:
                segments.append(current)
            current = []
        else:
            current.append(token)
    if current:
        segments.append(current)
    return segments or [tokens]


def _structured_segment_verdict(
    command: str, tokens: list[str], config: OperatorVerificationConfig
) -> bool | None:
    """Classify ONE command segment.

    Returns True (verification required), False (recognized as safe — the
    backstop regex must NOT override this for the segment), or None (leading
    command not recognized — fall through to the regex backstop).
    """
    cmd, args = _base_command(tokens)
    verb = _first_non_option(args)

    # docker-compose / podman-compose are aliases of "<engine> compose".
    if cmd in {"docker-compose", "podman-compose"}:
        cmd, args = ("docker", ["compose", *args])
        verb = _first_non_option(args)
    elif cmd == "podman":
        cmd = "docker"

    if cmd == "pass" and verb == "show":
        return not _matches_allowed_secret_read(command, config.allowed_secret_read_patterns)
    if cmd == "bw" and verb == "get":
        return not _matches_allowed_secret_read(command, config.allowed_secret_read_patterns)
    if cmd in {"vault", "get-secret"}:
        return True
    if cmd == "gh" and verb == "secret":
        return True

    if cmd in _ADMIN_MUTATING_SUBCOMMANDS:
        if cmd == "service":
            verb = args[1].lower() if len(args) >= 2 else ""
            rest = args[2:] if len(args) >= 2 else []
        else:
            verb, rest = _admin_subcommand_and_rest(cmd, args)

        if cmd == "kubectl" and verb == "config":
            nested, _ = _admin_subcommand_and_rest(cmd, rest)
            return bool(nested and nested not in {"view", "current-context", "get-contexts"})
        if cmd == "helm" and verb == "repo":
            nested, _ = _admin_subcommand_and_rest(cmd, rest)
            return bool(nested and nested != "list")
        if cmd == "terraform" and verb == "workspace":
            nested, _ = _admin_subcommand_and_rest(cmd, rest)
            return bool(nested and nested not in {"list", "show"})
        if verb in _ADMIN_READONLY_SUBCOMMANDS.get(cmd, set()):
            return False
        if cmd == "kubectl" and verb == "rollout":
            return any(arg.lower() == "restart" for arg in rest)
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
        return _chmod_requires_verification(args)
    if cmd == "chown":
        return _chown_requires_verification(args)
    if cmd == "rm":
        return _rm_requires_verification(args)
    if cmd in {"dd", "mkfs"}:
        return True
    if cmd == "git":
        return _git_push_requires_verification(args)
    if cmd == "hermes" and len(args) >= 2:
        area, action = args[0].lower(), args[1].lower()
        return (area == "gateway" and action in {"restart", "stop", "start"}) or (area == "profile" and action in {"delete", "use", "rename"}) or (area == "cron" and action in {"remove", "create", "edit"})

    return None


def _structured_requires_operator_verification(
    command: str, tokens: list[str], config: OperatorVerificationConfig
) -> bool:
    """True iff ANY segment's leading command is recognized as requiring it."""
    for segment in _split_segments(tokens):
        if _structured_segment_verdict(command, segment, config) is True:
            return True
    return False


def _segment_text(tokens: list[str]) -> str:
    try:
        return shlex.join(tokens)
    except Exception:
        return " ".join(tokens)


def _has_executable_command_substitution(command: str) -> bool:
    single_quoted = False
    double_quoted = False
    escaped = False
    index = 0
    while index < len(command):
        char = command[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\" and not single_quoted:
            escaped = True
            index += 1
            continue
        if char == "'" and not double_quoted:
            single_quoted = not single_quoted
            index += 1
            continue
        if single_quoted:
            index += 1
            continue
        if char == '"':
            double_quoted = not double_quoted
            index += 1
            continue
        if char == "`" or (
            char == "$"
            and index + 1 < len(command)
            and command[index + 1] in {"(", "'"}
        ):
            return True
        index += 1
    return False


def _requires_operator_verification(command: str, config: OperatorVerificationConfig) -> bool:
    if _has_executable_command_substitution(command):
        return True
    raw_tokens = _split_command(command)
    if raw_tokens and _wrapped_shell_requires_operator_verification(raw_tokens, config):
        return True
    tokens = raw_tokens
    if not tokens:
        return bool(_SENSITIVE_COMMAND_RE.search(command or ""))
    # OR semantics: structured parse, eval payloads, AND the raw-string regex are
    # ALL consulted. No branch (especially an eval token) may reduce coverage by
    # short-circuiting before the fallback. Each compound segment is judged on
    # its own so a benign one neither hides nor is masked by another.
    for segment in _split_segments(tokens):
        verdict = _structured_segment_verdict(command, segment, config)
        if verdict is True:
            return True
        if verdict is None and _SENSITIVE_COMMAND_RE.search(_segment_text(segment)):
            # Unrecognized leading command: the regex backstop catches sensitive
            # invocations the structured parser could not resolve.
            return True
    if _wrapped_shell_requires_operator_verification(tokens, config):
        return True
    return False


# Coarse backstop for sensitive commands that appear OUTSIDE the leading token
# (chained with ;/&&/|/eval etc.) where the structured parser only sees token 0.
# Conditionally-sensitive commands (git push, docker compose up, benign chmod
# octals) are matched here ONLY in their dangerous variants so the OR with the
# structured parse cannot over-block legitimate non-force pushes / benign modes.
_SENSITIVE_COMMAND_RE = re.compile(
    r"\b("
    r"systemctl|service|supervisorctl|"
    r"docker\s+compose\s+(?:down|restart|stop|kill|rm|rmi)|"
    r"docker-compose\s+(?:down|restart|stop|kill|rm|rmi)|"
    r"podman-compose\s+(?:down|restart|stop|kill|rm|rmi)|"
    r"docker\s+(?:restart|rm|rmi)|podman\s+(?:restart|rm|rmi)|"
    r"kubectl|helm|terraform|ansible-playbook|"
    r"rsync|scp|pass\s+show|bw\s+get|vault\s+kv|get-secret|"
    r"chmod\s+(?:[1-7][0-7]{3}|777|666)|chmod\s+[ugoa]*\+[rwxXt]*s|chown|"
    r"rm\s+-[^\sr]*r[^\s]*\s+-[^\sf]*f|rm\s+-[^\sf]*f[^\s]*\s+-[^\sr]*r|rm\s+-rf|"
    r"dd\s+if=|mkfs|"
    r"git\s+push\s+(?:[^\n]*\s)?(?:--force|-f|--mirror|--delete|-d|--force-with-lease)\b|"
    r"git\s+push\s+[^\n]*(?:\s\+|\s:|:[^\s]+\s|[^\s]:[^\s])|"
    r"gh\s+secret|hermes\s+gateway\s+(?:restart|stop|start)|"
    r"hermes\s+profile\s+(?:delete|use|rename)|hermes\s+cron\s+(?:remove|create|edit)"
    r")\b",
    re.IGNORECASE,
)


_cache: dict[str, OperatorVerificationResult] = {}
_callback_tls = threading.local()
_broker_proc: subprocess.Popen[str] | None = None
_broker_lock = threading.Lock()
_BROKER_SOCKET_ENV = "HERMES_OPERATOR_VERIFIER_BROKER_SOCKET"
_BROKER_PID_ENV = "HERMES_OPERATOR_VERIFIER_BROKER_PID"
_BROKER_PARENT_PID_ENV = "HERMES_OPERATOR_VERIFIER_BROKER_PARENT_PID"
_BROKER_CAPABILITY_ENV = "HERMES_OPERATOR_VERIFIER_CAPABILITY"


def _get_operator_verification_callback():
    return getattr(_callback_tls, "operator_verification", None)


def set_operator_verification_callback(cb) -> None:
    """Register a masked in-process operator verifier prompt callback."""
    _callback_tls.operator_verification = cb


def _cache_key(session_id: str | None = None) -> str:
    return session_id or "__process__"


def _clear_broker_env() -> None:
    os.environ.pop(_BROKER_SOCKET_ENV, None)
    os.environ.pop(_BROKER_PID_ENV, None)
    os.environ.pop(_BROKER_PARENT_PID_ENV, None)
    os.environ.pop(_BROKER_CAPABILITY_ENV, None)


def _broker_runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or str(Path.home() / ".hermes" / "run")
    return Path(base) / "hov"


def _runtime_dir_is_private(path: Path) -> bool:
    try:
        st = path.stat()
    except FileNotFoundError:
        return True
    except Exception:
        return False
    if not hasattr(os, "getuid"):
        return False
    return st.st_uid == os.getuid() and (st.st_mode & 0o077) == 0  # windows-footgun: ok


def _parent_pid(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as handle:
            stat = handle.read()
        return int(stat.rsplit(")", 1)[1].split()[1])
    except Exception:
        return None


def _operator_result_from_payload(payload: Any) -> OperatorVerificationResult | None:
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
    return OperatorVerificationResult(
        ok=True,
        actor_id=actor_id.strip(),
        role=role.strip(),
        verified_at=verified_at,
        expires_at=expires_at,
        reason=str(payload.get("reason") or ""),
    )


def _start_operator_verification_broker(result: OperatorVerificationResult, *, session_id: str | None = None) -> None:
    global _broker_proc
    if os.name != "posix" or not hasattr(socket, "AF_UNIX") or not hasattr(socket, "SO_PEERCRED"):
        return
    with _broker_lock:
        if _broker_proc is not None and _broker_proc.poll() is None:
            try:
                _broker_proc.terminate()
                _broker_proc.wait(timeout=1)
            except Exception:
                try:
                    _broker_proc.kill()
                except Exception:
                    pass
        _broker_proc = None
        _clear_broker_env()
        runtime_dir = _broker_runtime_dir()
        if not _runtime_dir_is_private(runtime_dir):
            return
        try:
            runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(runtime_dir, 0o700)
        except Exception:
            return
        socket_path = runtime_dir / f"b-{os.getpid()}-{uuid.uuid4().hex[:8]}.sock"
        capability = secrets.token_urlsafe(32)
        payload = json.dumps(
            {"key": _cache_key(session_id), "capability": capability, "parent_pid": os.getpid(), "result": asdict(result)},
            separators=(",", ":"),
        )
        try:
            proc = subprocess.Popen(
                [sys.executable, "-m", "hermes_cli.operator_verification_broker", str(socket_path)],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                start_new_session=True,
            )
            if proc.stdin is not None:
                proc.stdin.write(payload)
                proc.stdin.close()
        except Exception:
            _clear_broker_env()
            return
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                _clear_broker_env()
                return
            if socket_path.exists():
                _broker_proc = proc
                os.environ[_BROKER_SOCKET_ENV] = str(socket_path)
                os.environ[_BROKER_PID_ENV] = str(proc.pid)
                os.environ[_BROKER_PARENT_PID_ENV] = str(os.getpid())
                os.environ[_BROKER_CAPABILITY_ENV] = capability
                return
            time.sleep(0.02)
        try:
            proc.terminate()
        except Exception:
            pass
        _clear_broker_env()


def _trusted_broker_env() -> tuple[str, int, str] | None:
    socket_path = os.environ.get(_BROKER_SOCKET_ENV, "").strip()
    pid_raw = os.environ.get(_BROKER_PID_ENV, "").strip()
    parent_raw = os.environ.get(_BROKER_PARENT_PID_ENV, "").strip()
    capability = os.environ.get(_BROKER_CAPABILITY_ENV, "").strip()
    if not socket_path or not pid_raw or not parent_raw or not capability:
        return None
    try:
        broker_pid = int(pid_raw)
        parent_pid = int(parent_raw)
    except ValueError:
        return None
    if parent_pid != os.getpid():
        return None
    if _broker_proc is None or _broker_proc.pid != broker_pid or _broker_proc.poll() is not None:
        return None
    if _parent_pid(broker_pid) != parent_pid:
        return None
    return socket_path, broker_pid, capability


def _query_operator_verification_broker(
    *, session_id: str | None = None, now: int | None = None
) -> OperatorVerificationResult | None:
    if os.name != "posix" or not hasattr(socket, "AF_UNIX") or not hasattr(socket, "SO_PEERCRED"):
        return None
    trusted = _trusted_broker_env()
    if trusted is None:
        return None
    socket_path, expected_pid, capability = trusted
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(1.0)
            client.connect(socket_path)
            creds = client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            peer_pid, _peer_uid, _peer_gid = struct.unpack("3i", creds)
            if peer_pid != expected_pid:
                return None
            request = {"session_id": session_id, "capability": capability}
            client.sendall(json.dumps(request, separators=(",", ":")).encode("utf-8"))
            raw = client.recv(8192)
    except Exception:
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    result = _operator_result_from_payload(payload)
    if result is None or not result.is_valid(now=now):
        return None
    return result


def clear_operator_verification_cache() -> None:
    global _broker_proc
    _cache.clear()
    with _broker_lock:
        if _broker_proc is not None and _broker_proc.poll() is None:
            try:
                _broker_proc.terminate()
                _broker_proc.wait(timeout=1)
            except Exception:
                try:
                    _broker_proc.kill()
                except Exception:
                    pass
        _broker_proc = None
        _clear_broker_env()


def cache_operator_verification(
    result: OperatorVerificationResult, *, session_id: str | None = None
) -> None:
    if result.ok and result.actor_id and result.role:
        _cache[_cache_key(session_id)] = result
        _start_operator_verification_broker(result, session_id=session_id)


def get_cached_operator_verification(
    *, session_id: str | None = None, now: int | None = None
) -> OperatorVerificationResult | None:
    key = _cache_key(session_id)
    cache_key = key
    result = _cache.get(key)
    if result is None and session_id is not None:
        cache_key = _cache_key(None)
        result = _cache.get(cache_key)
    if result is not None:
        if result.is_valid(now=now):
            return result
        _cache.pop(cache_key, None)
    result = _query_operator_verification_broker(session_id=session_id, now=now)
    if result is not None:
        _cache[_cache_key(session_id)] = result
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

    This is not a fallback preference. Gateway/CUI platform context wins. The
    ``cli`` surface is selected purely from the deployment-injected
    ``HERMES_INTERACTIVE`` flag (no controlling-TTY/isatty check): the
    interactive Hermes CLI owns the user-facing prompt even when its tool
    workers have no controlling terminal, so they route to the in-process
    masked callback. Local desktop tool-runner calls without that flag route to
    ``local`` and use the desktop verifier instead of an invisible /dev/tty
    prompt.
    """
    try:
        from gateway.session_context import get_session_env

        platform = get_session_env("HERMES_SESSION_PLATFORM", "")
    except Exception:
        platform = os.getenv("HERMES_SESSION_PLATFORM", "")
    platform = _normalize_interface(platform)
    if platform:
        return platform
    # An authenticated CUI actor (ContextVar-bound or env-bridged) routes to the
    # web verifier surface.
    if _cui_actor_context_data():
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


def _cui_actor_context_data() -> dict[str, Any]:
    """Sanitized CUI actor context from the canonical helper.

    Delegates to ``agent.cui_actor_context.current_cui_actor_context`` so the
    verifier path respects the per-turn ContextVar once bound, falling back to
    the os.environ bridge for CLI/cron/subprocess contexts. Imported lazily to
    keep this module importable without pulling in the agent package eagerly.
    """
    try:
        from agent.cui_actor_context import current_cui_actor_context

        return dict(current_cui_actor_context())
    except Exception:
        return {}


def _cui_actor_verification(config: OperatorVerificationConfig, *, now: int) -> OperatorVerificationResult:
    """Trust an authenticated AIWerk CUI admin/operator actor as verifier.

    The CUI auth layer is the channel verifier: if the child process has actor
    metadata for an admin/operator role, no second approval prompt is needed.
    Missing or customer-only actor context fails closed.

    Sources the actor identity from the canonical ``agent.cui_actor_context``
    helper, which prefers the per-turn ContextVar (race-free across concurrent
    in-process gateway turns) and falls back to the os.environ bridge for the
    CLI/cron/subprocess paths.
    """
    data = _cui_actor_context_data()
    actor_id = str(
        data.get("actor_id")
        or data.get("user_id")
        or ""
    ).strip()
    role = str(data.get("role") or "").strip().lower()
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


def _verifier_is_provisioned(config: OperatorVerificationConfig) -> bool:
    """True only when SOME verifier could actually succeed for this config.

    Without this guard the gate ships fail-closed-deadlocked: on a fresh
    install (default ``command``/``callback`` verifier, no
    ~/.hermes/operator-verifier.json store, no registered callback, no CUI/
    platform actor) every admin-sensitive command is blocked but the
    remediation (verify_operator_identity) can never succeed, so the command is
    permanently un-runnable. When nothing can verify, the gate is inert until an
    operator explicitly provisions a verifier.
    """
    verifier = (config.verifier_type or "").strip().lower()
    if verifier in {"cui_actor", "cui-actor", "cui_admin", "cui-admin", "cui_actor_context", "cui-actor-context"}:
        return bool(_cui_actor_context_data())
    if verifier in {"trusted_platform_actor", "trusted-platform-actor", "platform_actor", "platform-actor"}:
        return bool(config.trusted_actor_ids)
    if verifier in {"callback", "operator_callback", "operator-callback", "prompt", "modal"}:
        return _load_operator_store() is not None
    # Generic "command" verifier: usable when an argv is configured, or when the
    # callback store + callback are wired (the CLI resolves "command" to the
    # masked callback verifier interactively).
    if config.argv:
        return True
    if config.missing_interface:
        return False
    return _load_operator_store() is not None and _get_operator_verification_callback() is not None


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
    if not _verifier_is_provisioned(cfg):
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
