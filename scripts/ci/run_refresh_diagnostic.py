#!/usr/bin/env python3
"""Run a refresh diagnostic inside Linux bubblewrap containment.

The merge candidate is untrusted executable code. Any diagnostic that imports it
must use this launcher so SessionDB and startup paths cannot open or migrate the
operator live HERMES_HOME, inherit credentials, modify the checkout, or use the
network. This is Linux-only containment and fails closed elsewhere.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile


_TRUSTED_BWRAP = Path("/usr/bin/bwrap")
_SANDBOX_PATH = "/usr/bin:/bin"
_ENV_ALLOWLIST = {
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
}
_SYSTEM_LIBRARY_ROOTS = (
    Path("/usr/lib"),
    Path("/usr/lib64"),
)
_RUNTIME_ETC_FILES = (
    Path("/etc/passwd"),
    Path("/etc/group"),
    Path("/etc/nsswitch.conf"),
    Path("/etc/ssl/certs/ca-certificates.crt"),
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _sandbox_env() -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key in _ENV_ALLOWLIST and value
    }
    env.update(
        {
            "HOME": "/tmp/home",
            "HERMES_HOME": "/tmp/home/.hermes",
            "TMPDIR": "/tmp/tmp",
            "XDG_CACHE_HOME": "/tmp/cache",
            "UV_CACHE_DIR": "/tmp/cache/uv",
            "PIP_CACHE_DIR": "/tmp/cache/pip",
            "PYTHONDONTWRITEBYTECODE": "1",
            "HERMES_DISABLE_LAZY_INSTALLS": "1",
            "HERMES_STATE_DB_GUARD_BYPASS": "1",
            "PATH": _SANDBOX_PATH,
            "PYTEST_ADDOPTS": "-p no:cacheprovider",
            "TZ": "UTC",
        }
    )
    env.pop("HERMES_PROFILE", None)
    env.pop("HERMES_AGENT_PROFILE", None)
    return env


def _dir_chain(path: Path) -> list[str]:
    parts: list[str] = []
    current = Path("/")
    for part in path.parts[1:-1]:
        current /= part
        parts.extend(["--dir", os.fspath(current)])
    return parts


def _authenticate_bubblewrap(path: Path | None = None) -> str:
    if path is None:
        path = _TRUSTED_BWRAP
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise RuntimeError(f"trusted bubblewrap binary is unavailable at {path}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(f"trusted bubblewrap path is not a regular file: {path}")
    if metadata.st_uid != 0:
        raise RuntimeError(f"trusted bubblewrap binary is not root-owned: {path}")
    if metadata.st_mode & 0o022:
        raise RuntimeError(f"trusted bubblewrap binary is group/world-writable: {path}")
    if not metadata.st_mode & 0o111:
        raise RuntimeError(f"trusted bubblewrap binary is not executable: {path}")
    return os.fspath(path)


def _ro_bind_required(args: list[str], path: Path) -> None:
    resolved = path.resolve(strict=True)
    destination = path if path.is_absolute() else resolved
    args.extend(_dir_chain(destination))
    args.extend(["--ro-bind", os.fspath(resolved), os.fspath(destination)])


def _interpreter_prefixes(repo_root: Path) -> set[Path]:
    prefixes: set[Path] = {Path(sys.base_prefix).resolve(strict=True)}
    executable_paths = {Path(sys.executable), repo_root / ".venv" / "bin" / "python"}
    for executable in executable_paths:
        if not executable.exists():
            continue
        prefixes.add(executable.resolve(strict=True).parent.parent)
        try:
            target = executable.readlink()
        except OSError:
            continue
        if target.is_absolute():
            prefixes.add(target.parent.parent)
            prefixes.add(target.parent.parent.resolve(strict=True))
    return prefixes


def _bubblewrap_command(command: list[str], *, repo_root: Path) -> list[str]:
    bwrap = _authenticate_bubblewrap()

    args = [
        bwrap,
        "--die-with-parent",
        "--clearenv",
        "--unshare-all",
        "--new-session",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--dir",
        "/tmp/home",
        "--dir",
        "/tmp/home/.hermes",
        "--dir",
        "/tmp/tmp",
        "--dir",
        "/tmp/cache",
        "--dir",
        "/tmp/cache/uv",
        "--dir",
        "/tmp/cache/pip",
        "--dir",
        "/tmp/work",
    ]

    for path in _SYSTEM_LIBRARY_ROOTS:
        _ro_bind_required(args, path)
    args.extend(["--symlink", "usr/lib", "/lib"])
    args.extend(["--symlink", "usr/lib64", "/lib64"])
    for path in _RUNTIME_ETC_FILES:
        _ro_bind_required(args, path)

    args.extend(_dir_chain(repo_root))
    for live_path in {
        Path(os.environ.get("HERMES_HOME", "")),
        Path(os.environ.get("HOME", "")) / ".hermes",
    }:
        if live_path.is_absolute():
            args.extend(_dir_chain(live_path))
            args.extend(["--tmpfs", os.fspath(live_path)])
    args.extend(["--ro-bind", os.fspath(repo_root), os.fspath(repo_root)])

    for prefix in sorted(_interpreter_prefixes(repo_root)):
        _ro_bind_required(args, prefix)

    for key, value in _sandbox_env().items():
        args.extend(["--setenv", key, value])
    args.extend(["--chdir", os.fspath(repo_root), "--", *command])
    return args


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = list(args.command)
    if command and command[0] == "--":
        command.pop(0)
    if not command:
        parser.error("a command is required after --")
    if not sys.platform.startswith("linux"):
        print("refresh diagnostic containment requires Linux bubblewrap", file=sys.stderr)
        return 125

    try:
        wrapped = _bubblewrap_command(command, repo_root=_repo_root())
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 125

    with tempfile.TemporaryDirectory(prefix="hdiag-", dir="/tmp"):
        result = subprocess.run(
            wrapped,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    return int(result.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
