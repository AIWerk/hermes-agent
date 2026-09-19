"""Build, validate, and atomically publish isolated Hermes profile bundles."""

from __future__ import annotations

import ctypes
import errno
import json
import os
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from hermes_cli.profiles import validate_profile_name
from hermes_constants import (
    named_profile_is_deleted,
    reset_hermes_home_override,
    set_hermes_home_override,
)
from hermes_state import SessionDB
from hermes_state_common import SCHEMA_VERSION
from plugins.memory.honcho.client import profile_host_key
from utils import atomic_json_write, atomic_write_text, atomic_yaml_write


class AuthPolicy(str, Enum):
    """How a completed profile resolves model authentication."""

    PROFILE_LOCAL = "profile_local"
    ROOT_FALLBACK = "root_fallback"


@dataclass(frozen=True)
class ProfileBundleSpec:
    """Complete, non-runtime inputs for one isolated profile bundle."""

    profile_id: str
    request_id: str
    baseline_version: str
    display_name: str
    description: str
    config: Mapping[str, Any]
    soul: str
    honcho: Mapping[str, Any]
    auth_policy: AuthPolicy
    cron_templates: Sequence[Mapping[str, Any]]
    user_memory: bytes
    memory: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.auth_policy, AuthPolicy):
            raise TypeError("auth_policy must be an AuthPolicy value")


_PROFILE_DIRS = frozenset(
    {
        "memories",
        "sessions",
        "skills",
        "skins",
        "logs",
        "plans",
        "workspace",
        "cron",
        "home",
        "private-knowledge",
        "cases",
        "scripts",
        "plugins",
        "local",
    }
)
_ROOT_FILES = frozenset(
    {
        "profile.yaml",
        "config.yaml",
        "SOUL.md",
        ".env",
        "auth.json",
        "honcho.json",
        "state.db",
        "bundle-receipt.json",
    }
)
_EXACT_DIRECTORY_FILES: dict[str, frozenset[str]] = {
    "memories": frozenset({"USER.md", "MEMORY.md"}),
    "cron": frozenset({"jobs.json"}),
}
_EMPTY_DIRS = _PROFILE_DIRS - frozenset({"memories", "cron", "skills"})
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CRON_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_RUNTIME_NAMES = frozenset(
    {
        "gateway.pid",
        "gateway_state.json",
        "processes.json",
        "state.db-wal",
        "state.db-shm",
        ".jobs.lock",
        ".tick.lock",
        "ticker_heartbeat",
        "ticker_last_success",
    }
)
_SESSION_DB_BUILD_ARTIFACT_SUFFIXES = (
    "-wal",
    "-shm",
    ".fts_rebuild.lock",
    ".hermes-offline.lock",
    ".quarantine.lock",
)


def _plain(value: Any) -> Any:
    """Convert accepted mapping/sequence inputs into serializer-safe plain containers."""
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a mapping")
    plain = _plain(value)
    if not isinstance(plain, dict):
        raise TypeError(f"{label} must be a mapping")
    return plain


def _validate_spec(spec: ProfileBundleSpec) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    if not isinstance(spec, ProfileBundleSpec):
        raise TypeError("spec must be a ProfileBundleSpec")
    validate_profile_name(spec.profile_id)
    if not _REQUEST_ID_RE.fullmatch(spec.request_id):
        raise ValueError("request_id must be a non-secret stable identifier safe for a file name")
    for label, value in (
        ("baseline_version", spec.baseline_version),
        ("display_name", spec.display_name),
        ("description", spec.description),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} must be a nonempty string")
    if not isinstance(spec.soul, str):
        raise TypeError("soul must be text")
    if not isinstance(spec.user_memory, bytes) or not isinstance(spec.memory, bytes):
        raise TypeError("user_memory and memory must be bytes")

    config = _require_mapping(spec.config, "config")
    memory_config = config.get("memory")
    if not isinstance(memory_config, Mapping) or memory_config.get("provider") != "honcho":
        raise ValueError("config must explicitly set memory.provider to honcho")

    honcho = _require_mapping(spec.honcho, "honcho")
    expected_host = profile_host_key(spec.profile_id)
    hosts = honcho.get("hosts")
    if not isinstance(hosts, Mapping) or set(hosts) != {expected_host}:
        raise ValueError(f"honcho hosts must contain only the active host key {expected_host!r}")
    if honcho.get("defaultHost") != expected_host:
        raise ValueError("honcho defaultHost must be the exact active profile host key")
    host = hosts.get(expected_host)
    if not isinstance(host, Mapping):
        raise ValueError("honcho active host must be a mapping")
    for key in ("workspace", "peerName", "aiPeer"):
        if not isinstance(host.get(key), str) or not str(host[key]).strip():
            raise ValueError(f"honcho active host requires nonempty {key}")
    if host.get("pinUserPeer") is not True:
        raise ValueError("honcho active host must set pinUserPeer true")
    if host["peerName"] == host["aiPeer"]:
        raise ValueError("honcho peerName and aiPeer must be distinct")

    if not isinstance(spec.cron_templates, Sequence) or isinstance(spec.cron_templates, (str, bytes)):
        raise TypeError("cron_templates must be a sequence of mappings")
    jobs: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, template in enumerate(spec.cron_templates):
        job = _require_mapping(template, f"cron_templates[{index}]")
        job_id = job.get("id")
        if not isinstance(job_id, str) or not _CRON_ID_RE.fullmatch(job_id):
            raise ValueError(f"cron_templates[{index}] must have a stable id")
        if job_id in seen_ids:
            raise ValueError(f"duplicate cron template id: {job_id}")
        if job.get("enabled") is not False:
            raise ValueError(f"cron template {job_id!r} must be disabled")
        forbidden_state = {"last_run", "next_run", "output", "ledger", "run_count"} & set(job)
        if forbidden_state:
            raise ValueError(f"cron template {job_id!r} contains output/ledger runtime state")
        seen_ids.add(job_id)
        jobs.append(job)
    return config, honcho, jobs


def _parse_auth_bytes(auth_bytes: bytes, policy: AuthPolicy) -> dict[str, Any]:
    if not isinstance(auth_bytes, bytes):
        raise TypeError("auth_bytes must be bytes")
    try:
        loaded = json.loads(auth_bytes.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("auth_bytes must contain a JSON mapping") from exc
    if not isinstance(loaded, dict):
        raise ValueError("auth_bytes must contain a JSON mapping")
    if policy is AuthPolicy.PROFILE_LOCAL and not loaded:
        raise ValueError("profile_local auth.json must contain explicit local credential state")
    if policy is AuthPolicy.ROOT_FALLBACK and loaded:
        raise ValueError("root_fallback auth.json must be an explicit empty local mapping")
    return loaded


def _require_real_private_directory(path: Path, *, label: str) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ValueError(f"{label} must already exist as a real private directory") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"{label} must be a real directory, not a symlink or special file")
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError(f"{label} must be private with mode 0700")


def _require_no_symlinked_path_components(path: Path, *, label: str) -> None:
    absolute = path.absolute()
    for component in reversed((absolute, *absolute.parents[:-1])):
        try:
            if stat.S_ISLNK(component.lstat().st_mode):
                raise ValueError(f"{label} has a symlinked parent: {component}")
        except FileNotFoundError:
            raise ValueError(f"{label} parent does not exist: {component}") from None


def _scan_regular_tree(root: Path, *, label: str) -> tuple[list[Path], list[Path]]:
    try:
        root_info = root.lstat()
    except OSError as exc:
        raise ValueError(f"{label} does not exist") from exc
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise ValueError(f"{label} must be a real directory")
    directories: list[Path] = [root]
    files: list[Path] = []
    for current, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        for name in list(dirnames):
            path = current_path / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise ValueError(f"{label} contains a symlink: {path.relative_to(root)}")
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError(f"{label} contains a special file: {path.relative_to(root)}")
            directories.append(path)
        for name in filenames:
            path = current_path / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise ValueError(f"{label} contains a symlink: {path.relative_to(root)}")
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(f"{label} contains a special file: {path.relative_to(root)}")
            files.append(path)
    return directories, files


def _validate_skills_source(skills_source: Path) -> tuple[list[Path], list[Path]]:
    directories, files = _scan_regular_tree(skills_source, label="skills_source")
    if not files:
        raise ValueError("skills_source must contain a mandatory skills baseline")
    return directories, files


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    if not isinstance(content, bytes):
        raise TypeError("binary profile content must be bytes")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary_path = Path(temporary)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        path.chmod(0o600)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        temporary_path.unlink(missing_ok=True)
        raise


def _copy_skills(skills_source: Path, destination: Path) -> None:
    # Keep the pre-scan as a cheap shape/cardinality gate, but never consume
    # through those pathnames: every byte is read from a retained no-follow fd.
    _validate_skills_source(skills_source)
    _require_no_symlinked_path_components(skills_source, label="skills_source")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        source_fd = os.open(skills_source, flags)
    except OSError as exc:
        raise ValueError("skills_source changed or contains a symlink") from exc
    try:
        copied = _copy_skills_from_fd(source_fd, destination)
    finally:
        os.close(source_fd)
    if copied == 0:
        raise ValueError("skills_source must contain a mandatory skills baseline")


def _copy_skills_from_fd(source_fd: int, destination: Path) -> int:
    copied = 0
    entry_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    entry_flags |= getattr(os, "O_NOFOLLOW", 0)
    for name in sorted(os.listdir(source_fd)):
        try:
            descriptor = os.open(name, entry_flags, dir_fd=source_fd)
        except OSError as exc:
            raise ValueError(f"skills_source entry changed or is a symlink: {name}") from exc
        try:
            before = os.fstat(descriptor)
            target = destination / name
            if stat.S_ISDIR(before.st_mode):
                target.mkdir(mode=0o700)
                copied += _copy_skills_from_fd(descriptor, target)
                continue
            if not stat.S_ISREG(before.st_mode):
                raise ValueError(f"skills_source contains a special file: {name}")
            chunks: list[bytes] = []
            remaining = before.st_size
            while remaining:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    raise ValueError(f"skills_source file changed while reading: {name}")
                chunks.append(chunk)
                remaining -= len(chunk)
            if os.read(descriptor, 1):
                raise ValueError(f"skills_source file grew while reading: {name}")
            after = os.fstat(descriptor)
            before_identity = (
                before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns
            )
            after_identity = (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns
            )
            if before_identity != after_identity:
                raise ValueError(f"skills_source file changed while reading: {name}")
            _atomic_write_bytes(target, b"".join(chunks))
            copied += 1
        finally:
            os.close(descriptor)
    return copied


def _set_private_modes(root: Path) -> None:
    directories, files = _scan_regular_tree(root, label="bundle")
    for directory in directories:
        directory.chmod(0o700)
    for file_path in files:
        file_path.chmod(0o600)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_tree_directories_bottom_up(root: Path) -> None:
    directories, _ = _scan_regular_tree(root, label="bundle")
    for directory in sorted(
        directories,
        key=lambda item: len(item.relative_to(root).parts),
        reverse=True,
    ):
        _fsync_directory(directory)


def _directory_identity(path: Path) -> tuple[int, int]:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"profile bundle identity changed: {path}") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("profile bundle is no longer a directory")
        return info.st_dev, info.st_ino
    finally:
        os.close(descriptor)


def _rename_noreplace(source: Path, target: Path) -> None:
    """One atomic no-replace directory rename; fail closed where unavailable."""
    if sys.platform == "linux":
        renameat2 = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
        if renameat2 is None:
            raise OSError(errno.ENOSYS, "renameat2 unavailable")
        renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        renameat2.restype = ctypes.c_int
        if renameat2(-100, os.fsencode(source), -100, os.fsencode(target), 1) == 0:
            return
        error_number = ctypes.get_errno()
        if error_number == errno.EEXIST:
            raise FileExistsError(error_number, os.strerror(error_number), target)
        raise OSError(error_number, os.strerror(error_number), target)
    if os.name == "nt":
        os.rename(source, target)  # Windows rename refuses an existing destination.
        return
    raise OSError(errno.ENOTSUP, "atomic no-replace profile publication is unsupported")


@contextmanager
def _profile_publish_lock(profiles_root: Path, profile_id: str):
    lock_dir = profiles_root / ".provision-locks"
    try:
        lock_dir.mkdir(mode=0o700)
        _fsync_directory(profiles_root)
    except FileExistsError:
        pass
    _require_no_symlinked_path_components(lock_dir, label="profile publish lock directory")
    _require_real_private_directory(lock_dir, label="profile publish lock directory")
    lock_path = lock_dir / f"{profile_id}.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags, 0o600)
    handle = os.fdopen(descriptor, "a+b", buffering=0)
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            if handle.read(1) == b"":
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError("profile publication is already in progress") from exc
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError) as exc:
                raise RuntimeError("profile publication is already in progress") from exc
        acquired = True
        yield
    finally:
        if acquired:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _rollback_published_target(target: Path, bundle: Path) -> None:
    try:
        _rename_noreplace(target, bundle)
    except (FileExistsError, OSError):
        quarantine = bundle.parent / f".{bundle.name}.rejected-{uuid.uuid4().hex}"
        _rename_noreplace(target, quarantine)
    _fsync_directory(target.parent)
    if bundle.parent != target.parent:
        _fsync_directory(bundle.parent)


def _bundle_names(spec: ProfileBundleSpec) -> tuple[str, str, str]:
    stem = f"{spec.profile_id}.{spec.request_id}"
    return f"{stem}.incomplete", f"{stem}.bundle", f"{stem}.failed"


def _sqlite_status(path: Path) -> dict[str, Any]:
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise ValueError("state.db is not a readable SQLite database") from exc
    try:
        version_row = connection.execute("SELECT version FROM schema_version").fetchone()
        if version_row != (SCHEMA_VERSION,):
            raise ValueError("state.db does not use the current schema version")
        integrity_rows = connection.execute("PRAGMA integrity_check").fetchall()
        if integrity_rows != [("ok",)]:
            raise ValueError("state.db integrity_check did not return exactly ok")
        session_count = int(connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])
        message_count = int(connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
        if session_count != 0 or message_count != 0:
            raise ValueError("state.db must contain no session or message state")
    except sqlite3.Error as exc:
        raise ValueError("state.db schema validation failed") from exc
    finally:
        connection.close()
    return {
        "schema_version": SCHEMA_VERSION,
        "integrity": "ok",
        "sessions": 0,
        "messages": 0,
    }


def _settle_new_state_db(path: Path) -> None:
    """Leave a sole-opener build database self-contained and free of runtime locks."""
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        journal_mode = connection.execute("PRAGMA journal_mode=DELETE").fetchone()
        if journal_mode is None or str(journal_mode[0]).casefold() != "delete":
            raise RuntimeError("could not settle new state.db out of WAL mode")
    finally:
        connection.close()
    for suffix in _SESSION_DB_BUILD_ARTIFACT_SUFFIXES:
        path.with_name(path.name + suffix).unlink(missing_ok=True)


def _receipt(bundle: Path, spec: ProfileBundleSpec, *, cron_jobs: int) -> dict[str, Any]:
    directories, files = _scan_regular_tree(bundle, label="bundle")
    skill_directories, skill_files = _scan_regular_tree(bundle / "skills", label="bundle skills")
    return {
        "format_version": 1,
        "profile_id": spec.profile_id,
        "request_id": spec.request_id,
        "baseline_version": spec.baseline_version,
        "auth_policy": spec.auth_policy.value,
        "counts": {
            "directories": len(directories),
            "files": len(files),
            "skill_directories": len(skill_directories),
            "skill_files": len(skill_files),
            "cron_jobs": cron_jobs,
        },
        "state_db": _sqlite_status(bundle / "state.db"),
    }


def _validate_exact_layout(bundle: Path) -> None:
    root_names = {entry.name for entry in bundle.iterdir()}
    expected = _PROFILE_DIRS | _ROOT_FILES
    if root_names != expected:
        missing = sorted(expected - root_names)
        extra = sorted(root_names - expected)
        raise ValueError(f"bundle root contents differ from contract; missing={missing}, extra={extra}")
    for directory in _PROFILE_DIRS:
        if not (bundle / directory).is_dir():
            raise ValueError(f"required bundle directory is missing: {directory}")
    for file_name in _ROOT_FILES:
        if not (bundle / file_name).is_file():
            raise ValueError(f"required bundle file is missing: {file_name}")
    for directory, expected_files in _EXACT_DIRECTORY_FILES.items():
        actual = {entry.name for entry in (bundle / directory).iterdir()}
        if actual != expected_files:
            raise ValueError(f"{directory} contents differ from the bundle contract")
    for directory in _EMPTY_DIRS:
        if any((bundle / directory).iterdir()):
            raise ValueError(f"runtime directory must be empty in a new bundle: {directory}")


def _validate_private_modes(directories: list[Path], files: list[Path], bundle: Path) -> None:
    for directory in directories:
        if stat.S_IMODE(directory.lstat().st_mode) != 0o700:
            raise ValueError(f"bundle directory is not private: {directory.relative_to(bundle)}")
    for file_path in files:
        if stat.S_IMODE(file_path.lstat().st_mode) != 0o600:
            raise ValueError(f"bundle file is not private: {file_path.relative_to(bundle)}")


def _read_json_mapping(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} must contain valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain a JSON mapping")
    return value


def validate_profile_bundle(path: str | os.PathLike[str], expected_spec: ProfileBundleSpec) -> dict[str, Any]:
    """Validate a finalized or pre-finalized bundle and return its sanitized receipt."""
    bundle = Path(path)
    config, expected_honcho, expected_jobs = _validate_spec(expected_spec)
    directories, files = _scan_regular_tree(bundle, label="bundle")
    _validate_private_modes(directories, files, bundle)
    _validate_exact_layout(bundle)

    for entry in files:
        if entry.name in _RUNTIME_NAMES or entry.name.casefold().endswith((".lock", "-wal", "-shm")):
            raise ValueError(f"bundle contains a transient runtime artifact: {entry.relative_to(bundle)}")
        if "cache" in entry.name.casefold():
            raise ValueError(f"bundle contains a cache artifact: {entry.relative_to(bundle)}")

    try:
        from hermes_cli.config import read_user_config_raw

        raw_config = read_user_config_raw(bundle / "config.yaml")
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError("config.yaml is not valid YAML") from exc
    if not isinstance(raw_config, dict) or raw_config != config:
        raise ValueError("config.yaml does not exactly match the complete specification mapping")
    # Canonical config loading may bootstrap runtime directories under the active
    # HERMES_HOME. Validate against a disposable config-only home so validation is
    # observational and cannot change the sealed bundle or its receipt counts.
    with tempfile.TemporaryDirectory(prefix="hermes-profile-config-check.") as temporary_home:
        validation_home = Path(temporary_home)
        validation_home.chmod(0o700)
        validation_config = validation_home / "config.yaml"
        validation_config.write_bytes((bundle / "config.yaml").read_bytes())
        validation_config.chmod(0o600)
        token = set_hermes_home_override(validation_home)
        try:
            from hermes_cli.config import load_config

            canonical_config = load_config()
        finally:
            reset_hermes_home_override(token)
    if not isinstance(canonical_config, dict):
        raise ValueError("canonical load_config did not return a mapping")
    canonical_memory = canonical_config.get("memory")
    if not isinstance(canonical_memory, dict) or canonical_memory.get("provider") != "honcho":
        raise ValueError("canonical config does not select the honcho memory provider")

    profile_meta = yaml.safe_load((bundle / "profile.yaml").read_text(encoding="utf-8"))
    expected_meta = {
        "profile_id": expected_spec.profile_id,
        "request_id": expected_spec.request_id,
        "baseline_version": expected_spec.baseline_version,
        "display_name": expected_spec.display_name,
        "description": expected_spec.description,
        "description_auto": False,
        "auth_policy": expected_spec.auth_policy.value,
    }
    if profile_meta != expected_meta:
        raise ValueError("profile.yaml does not exactly match the specification")
    if (bundle / "SOUL.md").read_text(encoding="utf-8") != expected_spec.soul:
        raise ValueError("SOUL.md does not match the specification")
    if (bundle / "memories" / "USER.md").read_bytes() != expected_spec.user_memory:
        raise ValueError("memories/USER.md does not match the specification")
    if (bundle / "memories" / "MEMORY.md").read_bytes() != expected_spec.memory:
        raise ValueError("memories/MEMORY.md does not match the specification")

    _parse_auth_bytes(
        (bundle / "auth.json").read_bytes(),
        expected_spec.auth_policy,
    )
    honcho = _read_json_mapping(bundle / "honcho.json", "honcho.json")
    expected_host = profile_host_key(expected_spec.profile_id)
    hosts = honcho.get("hosts")
    host = hosts.get(expected_host) if isinstance(hosts, dict) else None
    if isinstance(host, dict) and host.get("peerName") == host.get("aiPeer"):
        raise ValueError("honcho peerName and aiPeer must be distinct")
    if honcho != expected_honcho:
        raise ValueError("honcho.json does not exactly match the profile-local specification")

    cron = _read_json_mapping(bundle / "cron" / "jobs.json", "cron/jobs.json")
    if cron != {"jobs": expected_jobs}:
        raise ValueError("cron/jobs.json must have the canonical jobs shape and exact disabled templates")
    if any(job.get("enabled") is not False for job in cron["jobs"]):
        raise ValueError("cron/jobs.json contains a job that is not disabled")

    for sidecar in (bundle / "state.db-wal", bundle / "state.db-shm"):
        if os.path.lexists(sidecar):
            raise ValueError(f"closed bundle retains SQLite sidecar: {sidecar.name}")
    calculated = _receipt(bundle, expected_spec, cron_jobs=len(expected_jobs))
    stored = _read_json_mapping(bundle / "bundle-receipt.json", "bundle-receipt.json")
    if stored != calculated:
        raise ValueError("bundle-receipt.json does not match sanitized bundle counts")
    return calculated


def build_profile_bundle(
    staging_parent: str | os.PathLike[str],
    spec: ProfileBundleSpec,
    *,
    env_bytes: bytes,
    auth_bytes: bytes,
    skills_source: str | os.PathLike[str],
) -> Path:
    """Build and validate a private bundle, then atomically rename it to ``*.bundle``."""
    staging = Path(staging_parent)
    _require_no_symlinked_path_components(staging, label="staging_parent")
    _require_real_private_directory(staging, label="staging_parent")
    config, honcho, jobs = _validate_spec(spec)
    if not isinstance(env_bytes, bytes):
        raise TypeError("env_bytes must be bytes")
    _parse_auth_bytes(auth_bytes, spec.auth_policy)
    skills = Path(skills_source)
    _validate_skills_source(skills)

    incomplete_name, bundle_name, failed_name = _bundle_names(spec)
    incomplete = staging / incomplete_name
    bundle = staging / bundle_name
    failed = staging / failed_name
    for candidate in (incomplete, bundle, failed):
        if os.path.lexists(candidate):
            raise FileExistsError(f"profile bundle request already exists: {candidate}")

    incomplete.mkdir(mode=0o700)
    try:
        for directory in sorted(_PROFILE_DIRS):
            (incomplete / directory).mkdir(mode=0o700)
        _copy_skills(skills, incomplete / "skills")

        profile_meta = {
            "profile_id": spec.profile_id,
            "request_id": spec.request_id,
            "baseline_version": spec.baseline_version,
            "display_name": spec.display_name,
            "description": spec.description,
            "description_auto": False,
            "auth_policy": spec.auth_policy.value,
        }
        atomic_yaml_write(incomplete / "profile.yaml", profile_meta, sort_keys=False, create_mode=0o600)
        atomic_yaml_write(incomplete / "config.yaml", config, sort_keys=False, create_mode=0o600)
        atomic_write_text(incomplete / "SOUL.md", spec.soul, create_mode=0o600)
        _atomic_write_bytes(incomplete / ".env", env_bytes)
        _atomic_write_bytes(incomplete / "auth.json", auth_bytes)
        atomic_json_write(incomplete / "honcho.json", honcho, mode=0o600, sort_keys=True)
        _atomic_write_bytes(incomplete / "memories" / "USER.md", spec.user_memory)
        _atomic_write_bytes(incomplete / "memories" / "MEMORY.md", spec.memory)
        atomic_json_write(incomplete / "cron" / "jobs.json", {"jobs": jobs}, mode=0o600, sort_keys=True)

        database = SessionDB(incomplete / "state.db")
        database.close()
        _settle_new_state_db(incomplete / "state.db")
        for sidecar in (incomplete / "state.db-wal", incomplete / "state.db-shm"):
            if os.path.lexists(sidecar):
                raise RuntimeError(f"SessionDB did not close cleanly: {sidecar.name} remains")

        _set_private_modes(incomplete)
        # Include the receipt itself in the final file count without ever recording content hashes.
        atomic_json_write(incomplete / "bundle-receipt.json", {}, mode=0o600, sort_keys=True)
        receipt = _receipt(incomplete, spec, cron_jobs=len(jobs))
        atomic_json_write(incomplete / "bundle-receipt.json", receipt, mode=0o600, sort_keys=True)
        _set_private_modes(incomplete)
        validate_profile_bundle(incomplete, spec)
        _fsync_tree_directories_bottom_up(incomplete)
        os.rename(incomplete, bundle)
        _fsync_directory(staging)
        return bundle
    except BaseException:
        if os.path.lexists(incomplete) and not os.path.lexists(failed):
            try:
                os.rename(incomplete, failed)
                _fsync_directory(staging)
            except OSError:
                pass
        raise


def publish_named_profile_bundle(
    bundle_path: str | os.PathLike[str],
    profiles_root: str | os.PathLike[str],
    expected_spec: ProfileBundleSpec,
) -> Path:
    """Validate and atomically move a bundle into a new named-profile target."""
    bundle = Path(bundle_path)
    root = Path(profiles_root)
    _validate_spec(expected_spec)
    if expected_spec.profile_id == "default":
        raise ValueError("the default profile may be staged but cannot be published as a named profile")
    _require_no_symlinked_path_components(root, label="profiles_root")
    _require_real_private_directory(root, label="profiles_root")
    if bundle.is_symlink() or not bundle.is_dir():
        raise ValueError("bundle_path must be a real bundle directory")
    if bundle.parent.stat().st_dev != root.stat().st_dev or bundle.stat().st_dev != root.stat().st_dev:
        raise ValueError("bundle and profiles_root must be on the same filesystem")

    target = root / expected_spec.profile_id
    expected_identity = _directory_identity(bundle)
    with _profile_publish_lock(root, expected_spec.profile_id):
        if os.path.lexists(target) or named_profile_is_deleted(target):
            raise FileExistsError(f"named profile target already exists or is tombstoned: {target}")
        validate_profile_bundle(bundle, expected_spec)
        if _directory_identity(bundle) != expected_identity:
            raise ValueError("profile bundle identity changed after validation")
        if named_profile_is_deleted(target):
            raise FileExistsError(f"named profile target is tombstoned: {target}")
        _rename_noreplace(bundle, target)
        published_identity = _directory_identity(target)
        if published_identity != expected_identity:
            _rollback_published_target(target, bundle)
            raise ValueError("published profile identity changed during rename")
        if named_profile_is_deleted(target):
            _rollback_published_target(target, bundle)
            raise FileExistsError(
                f"named profile target became tombstoned during publication: {target}"
            )
        _fsync_directory(root)
        return target
