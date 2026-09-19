from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from hermes_constants import (
    profile_tombstone_path,
    reset_hermes_home_override,
    set_hermes_home_override,
)
from hermes_state import SessionDB
from hermes_state_common import SCHEMA_VERSION
from plugins.memory.honcho.client import profile_host_key

from hermes_cli import profile_provisioning as provisioning
from hermes_cli.config import load_config
from hermes_cli.profile_provisioning import (
    AuthPolicy,
    ProfileBundleSpec,
    build_profile_bundle,
    publish_named_profile_bundle,
    validate_profile_bundle,
)


_REQUIRED_DIRS = {
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
_REQUIRED_FILES = {
    "profile.yaml",
    "config.yaml",
    "SOUL.md",
    ".env",
    "auth.json",
    "honcho.json",
    "memories/USER.md",
    "memories/MEMORY.md",
    "cron/jobs.json",
    "state.db",
    "bundle-receipt.json",
}


@pytest.fixture
def isolated_fs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    home = tmp_path / "home"
    temp = tmp_path / "tmp"
    hermes_home = home / ".hermes"
    staging = tmp_path / "staging"
    profiles = tmp_path / "profiles"
    for path in (home, temp, hermes_home, staging, profiles):
        path.mkdir(mode=0o700)
        path.chmod(0o700)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TMPDIR", str(temp))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    return {
        "root": tmp_path,
        "home": home,
        "temp": temp,
        "hermes_home": hermes_home,
        "staging": staging,
        "profiles": profiles,
    }


@pytest.fixture
def skills_source(isolated_fs: dict[str, Path]) -> Path:
    source = isolated_fs["root"] / "baseline-skills"
    nested = source / "operations" / "references"
    nested.mkdir(parents=True, mode=0o700)
    (source / "operations" / "SKILL.md").write_bytes(b"---\nname: operations\n---\nprivate baseline\n")
    (nested / "payload.bin").write_bytes(b"\x00\xffbaseline-bytes\n")
    (source / "README.md").write_bytes(b"baseline root file\n")
    return source


def _honcho(profile_id: str, *, peer: str = "theo-user", ai_peer: str = "theo-ai") -> dict[str, Any]:
    return {
        "apiKey": "honcho-secret-value",
        "defaultHost": profile_host_key(profile_id),
        "hosts": {
            profile_host_key(profile_id): {
                "workspace": "theo-private-workspace",
                "peerName": peer,
                "aiPeer": ai_peer,
                "pinUserPeer": True,
            }
        },
    }


def _spec(
    profile_id: str = "theo",
    *,
    request_id: str = "request-20260919-001",
    auth_policy: AuthPolicy = AuthPolicy.PROFILE_LOCAL,
    cron_templates: tuple[dict[str, Any], ...] | None = None,
) -> ProfileBundleSpec:
    return ProfileBundleSpec(
        profile_id=profile_id,
        request_id=request_id,
        baseline_version="baseline-2026.09",
        display_name="Theo",
        description="Private isolated assistant profile.",
        config={
            "_config_version": 21,
            "model": {"provider": "nous", "default": "Hermes-Test"},
            "memory": {"provider": "honcho"},
            "display": {"language": "de"},
        },
        soul="You are Theo. Keep tenant knowledge private.\n",
        honcho=_honcho(profile_id),
        auth_policy=auth_policy,
        cron_templates=cron_templates
        if cron_templates is not None
        else (
            {
                "id": "daily-private-review",
                "name": "Daily private review",
                "schedule": "0 4 * * *",
                "prompt": "Review private memory.",
                "enabled": False,
            },
        ),
        user_memory=b"# User\nTheo user bytes.\n",
        memory=b"# Memory\nTheo memory bytes.\n",
    )


def _build(
    isolated_fs: dict[str, Path],
    skills_source: Path,
    spec: ProfileBundleSpec | None = None,
) -> tuple[Path, ProfileBundleSpec, bytes, bytes]:
    actual_spec = spec or _spec()
    env_bytes = b"PRIVATE_TOKEN=env-secret-value\n"
    auth_bytes = json.dumps(
        {
            "active_provider": "nous",
            "providers": {"nous": {"access_token": "auth-secret-value"}},
        }
    ).encode()
    path = build_profile_bundle(
        isolated_fs["staging"],
        actual_spec,
        env_bytes=env_bytes,
        auth_bytes=auth_bytes,
        skills_source=skills_source,
    )
    return path, actual_spec, env_bytes, auth_bytes


def _all_entries(root: Path) -> set[str]:
    return {str(path.relative_to(root)) for path in root.rglob("*")}


def _assert_private_tree(root: Path) -> None:
    for path in (root, *root.rglob("*")):
        info = path.lstat()
        assert not stat.S_ISLNK(info.st_mode)
        if stat.S_ISDIR(info.st_mode):
            assert stat.S_IMODE(info.st_mode) == 0o700, path
        else:
            assert stat.S_ISREG(info.st_mode), path
            assert stat.S_IMODE(info.st_mode) == 0o600, path


def _walk_scalars(value: Any):
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _walk_scalars(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_scalars(item)
    else:
        yield str(value)


def test_builds_complete_current_schema_private_bundle(
    isolated_fs: dict[str, Path], skills_source: Path
) -> None:
    bundle, spec, env_bytes, auth_bytes = _build(isolated_fs, skills_source)

    assert bundle.parent == isolated_fs["staging"]
    assert bundle.name.endswith(".bundle")
    assert bundle.is_dir()
    entries = _all_entries(bundle)
    assert _REQUIRED_DIRS <= entries
    assert _REQUIRED_FILES <= entries
    assert (bundle / ".env").read_bytes() == env_bytes
    assert (bundle / "auth.json").read_bytes() == auth_bytes
    assert (bundle / "SOUL.md").read_text(encoding="utf-8") == spec.soul
    assert (bundle / "memories" / "USER.md").read_bytes() == spec.user_memory
    assert (bundle / "memories" / "MEMORY.md").read_bytes() == spec.memory
    assert (bundle / "skills" / "operations" / "references" / "payload.bin").read_bytes() == b"\x00\xffbaseline-bytes\n"
    assert not list(isolated_fs["staging"].glob("*.incomplete"))
    _assert_private_tree(bundle)

    raw_config = yaml.safe_load((bundle / "config.yaml").read_text(encoding="utf-8"))
    assert raw_config == spec.config
    validation_home = isolated_fs["temp"] / "canonical-config-validation"
    validation_home.mkdir(mode=0o700)
    validation_config = validation_home / "config.yaml"
    validation_config.write_bytes((bundle / "config.yaml").read_bytes())
    validation_config.chmod(0o600)
    token = set_hermes_home_override(validation_home)
    try:
        canonical = load_config()
    finally:
        reset_hermes_home_override(token)
    assert canonical["memory"]["provider"] == "honcho"

    with sqlite3.connect(bundle / "state.db") as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchone() == (SCHEMA_VERSION,)
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert connection.execute("SELECT COUNT(*) FROM sessions").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone() == (0,)
    reopen_probe = isolated_fs["temp"] / "state-reopen-probe.db"
    shutil.copy2(bundle / "state.db", reopen_probe)
    with SessionDB(reopen_probe, read_only=True):
        pass

    receipt = validate_profile_bundle(bundle, spec)
    assert receipt == json.loads((bundle / "bundle-receipt.json").read_text(encoding="utf-8"))
    assert receipt["profile_id"] == spec.profile_id
    assert receipt["state_db"]["schema_version"] == SCHEMA_VERSION
    assert receipt["counts"]["cron_jobs"] == 1
    receipt_text = json.dumps(receipt, sort_keys=True)
    for forbidden in (
        "env-secret-value",
        "auth-secret-value",
        "honcho-secret-value",
        "theo-user",
        "theo-ai",
        "Hermes-Test",
        "private baseline",
        "Theo memory bytes",
        "sha256",
        "hash",
    ):
        assert forbidden not in receipt_text


def test_honcho_uses_exact_profile_host_and_distinct_pinned_peers(
    isolated_fs: dict[str, Path], skills_source: Path
) -> None:
    bundle, spec, _, _ = _build(isolated_fs, skills_source)
    raw = json.loads((bundle / "honcho.json").read_text(encoding="utf-8"))
    expected_key = profile_host_key(spec.profile_id)

    assert set(raw["hosts"]) == {expected_key}
    host = raw["hosts"][expected_key]
    assert host["workspace"]
    assert host["peerName"]
    assert host["aiPeer"]
    assert host["peerName"] != host["aiPeer"]
    assert host["pinUserPeer"] is True


def test_auth_policy_is_frozen_explicit_and_root_fallback_writes_empty_local_store(
    isolated_fs: dict[str, Path], skills_source: Path
) -> None:
    profile_local = _spec()
    with pytest.raises(FrozenInstanceError):
        profile_local.profile_id = "changed"  # type: ignore[misc]
    with pytest.raises((TypeError, ValueError)):
        replace(profile_local, auth_policy="profile_local")  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="profile_local"):
        build_profile_bundle(
            isolated_fs["staging"],
            replace(profile_local, request_id="request-empty-local-auth"),
            env_bytes=b"",
            auth_bytes=b"{}",
            skills_source=skills_source,
        )

    fallback = _spec(
        profile_id="fallback-agent",
        request_id="request-fallback-001",
        auth_policy=AuthPolicy.ROOT_FALLBACK,
    )
    bundle = build_profile_bundle(
        isolated_fs["staging"],
        fallback,
        env_bytes=b"",
        auth_bytes=b"{}",
        skills_source=skills_source,
    )
    assert json.loads((bundle / "auth.json").read_text(encoding="utf-8")) == {}
    receipt = validate_profile_bundle(bundle, fallback)
    assert receipt["auth_policy"] == "root_fallback"

    local_bundle, local_spec, _, _ = _build(
        isolated_fs,
        skills_source,
        replace(profile_local, request_id="request-local-auth-tamper"),
    )
    (local_bundle / "auth.json").write_text("{}", encoding="utf-8")
    (local_bundle / "auth.json").chmod(0o600)
    with pytest.raises(ValueError, match="profile_local"):
        validate_profile_bundle(local_bundle, local_spec)


def test_rejects_every_symlink_in_skills_without_publishing_bundle(
    isolated_fs: dict[str, Path], skills_source: Path
) -> None:
    (skills_source / "operations" / "escape").symlink_to(isolated_fs["home"], target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        _build(isolated_fs, skills_source)

    assert not list(isolated_fs["staging"].glob("*.bundle"))


def test_cron_templates_must_have_stable_ids_and_be_disabled(
    isolated_fs: dict[str, Path], skills_source: Path
) -> None:
    enabled = _spec(cron_templates=({"id": "unsafe-job", "enabled": True, "prompt": "run"},))
    with pytest.raises(ValueError, match="disabled"):
        _build(isolated_fs, skills_source, enabled)

    missing_id = _spec(
        request_id="request-no-id-001",
        cron_templates=({"enabled": False, "prompt": "do not run"},),
    )
    with pytest.raises(ValueError, match="id"):
        _build(isolated_fs, skills_source, missing_id)

    assert not list(isolated_fs["staging"].glob("*.bundle"))


def test_bundle_has_no_runtime_transients_sidecars_output_or_ledger_state(
    isolated_fs: dict[str, Path], skills_source: Path
) -> None:
    bundle, spec, _, _ = _build(isolated_fs, skills_source)
    entries = _all_entries(bundle)

    assert "state.db-wal" not in entries
    assert "state.db-shm" not in entries
    assert "cron/output" not in entries
    assert "cron/.jobs.lock" not in entries
    assert "gateway.pid" not in entries
    assert "gateway_state.json" not in entries
    assert "processes.json" not in entries
    assert not any("cache" in Path(item).name.casefold() for item in entries)
    jobs = json.loads((bundle / "cron" / "jobs.json").read_text(encoding="utf-8"))
    assert jobs == {"jobs": [dict(spec.cron_templates[0])]}
    assert not ({"last_run", "next_run", "output", "ledger", "run_count"} & set(jobs["jobs"][0]))
    validate_profile_bundle(bundle, spec)


def test_validate_rejects_symlink_anywhere_in_bundle(
    isolated_fs: dict[str, Path], skills_source: Path
) -> None:
    bundle, spec, _, _ = _build(isolated_fs, skills_source)
    soul = bundle / "SOUL.md"
    soul.unlink()
    soul.symlink_to(isolated_fs["home"] / "outside-soul")

    with pytest.raises(ValueError, match="symlink"):
        validate_profile_bundle(bundle, spec)


def test_validate_rejects_wrong_honcho_identity(
    isolated_fs: dict[str, Path], skills_source: Path
) -> None:
    bundle, spec, _, _ = _build(isolated_fs, skills_source)
    honcho_path = bundle / "honcho.json"
    honcho = json.loads(honcho_path.read_text(encoding="utf-8"))
    host = honcho["hosts"][profile_host_key(spec.profile_id)]
    host["aiPeer"] = host["peerName"]
    honcho_path.write_text(json.dumps(honcho), encoding="utf-8")
    honcho_path.chmod(0o600)

    with pytest.raises(ValueError, match="distinct"):
        validate_profile_bundle(bundle, spec)


def test_named_publish_is_one_atomic_rename_and_fsyncs_parent(
    isolated_fs: dict[str, Path], skills_source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, spec, _, _ = _build(isolated_fs, skills_source)
    real_rename = provisioning._rename_noreplace
    rename_calls: list[tuple[Path, Path]] = []
    fsync_calls: list[int] = []

    def recording_rename(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        rename_calls.append((Path(source), Path(target)))
        real_rename(Path(source), Path(target))

    def forbidden_replace(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("publication must never use replace")

    real_fsync = os.fsync

    def recording_fsync(fd: int) -> None:
        fsync_calls.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(provisioning, "_rename_noreplace", recording_rename)
    monkeypatch.setattr(provisioning.os, "replace", forbidden_replace)
    monkeypatch.setattr(provisioning.os, "fsync", recording_fsync)

    target = publish_named_profile_bundle(bundle, isolated_fs["profiles"], spec)

    assert target == isolated_fs["profiles"] / spec.profile_id
    assert target.is_dir()
    assert not bundle.exists()
    assert rename_calls == [(bundle, target)]
    assert fsync_calls
    validate_profile_bundle(target, spec)


def test_publish_refuses_default_profile(
    isolated_fs: dict[str, Path], skills_source: Path
) -> None:
    spec = _spec(profile_id="default", request_id="request-default-001")
    bundle, _, _, _ = _build(isolated_fs, skills_source, spec)

    with pytest.raises(ValueError, match="default"):
        publish_named_profile_bundle(bundle, isolated_fs["profiles"], spec)

    assert bundle.is_dir()
    assert not (isolated_fs["profiles"] / "default").exists()


@pytest.mark.parametrize("blocked_by", ["target", "tombstone"])
def test_publish_refuses_existing_or_tombstoned_target(
    isolated_fs: dict[str, Path], skills_source: Path, blocked_by: str
) -> None:
    spec = _spec(request_id=f"request-blocked-{blocked_by}")
    bundle, _, _, _ = _build(isolated_fs, skills_source, spec)
    target = isolated_fs["profiles"] / spec.profile_id
    if blocked_by == "target":
        target.mkdir(mode=0o700)
        (target / "sentinel").write_text("unchanged", encoding="utf-8")
    else:
        tombstone = profile_tombstone_path(target)
        tombstone.parent.mkdir(mode=0o700)
        tombstone.write_text("deleted\n", encoding="utf-8")

    with pytest.raises(FileExistsError):
        publish_named_profile_bundle(bundle, isolated_fs["profiles"], spec)

    assert bundle.is_dir()
    if blocked_by == "target":
        assert (target / "sentinel").read_text(encoding="utf-8") == "unchanged"
    else:
        assert not target.exists()


def test_late_build_failure_leaves_no_bundle_and_no_live_change(
    isolated_fs: dict[str, Path], skills_source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(request_id="request-late-failure-001")
    live_target = isolated_fs["profiles"] / spec.profile_id
    live_target.mkdir(mode=0o700)
    sentinel = live_target / "sentinel"
    sentinel.write_bytes(b"live profile unchanged")

    def fail_final_validation(path: Path, expected_spec: ProfileBundleSpec) -> dict[str, Any]:
        assert path.name.endswith(".incomplete")
        assert expected_spec == spec
        raise RuntimeError("injected late validation failure")

    monkeypatch.setattr(provisioning, "validate_profile_bundle", fail_final_validation)

    with pytest.raises(RuntimeError, match="injected late validation failure"):
        build_profile_bundle(
            isolated_fs["staging"],
            spec,
            env_bytes=b"SECRET=late\n",
            auth_bytes=b'{"provider":{"token":"synthetic-fixture"}}',
            skills_source=skills_source,
        )

    assert not list(isolated_fs["staging"].glob("*.bundle"))
    assert sentinel.read_bytes() == b"live profile unchanged"


def test_build_refuses_duplicate_request_and_non_private_or_symlinked_staging_parent(
    isolated_fs: dict[str, Path], skills_source: Path
) -> None:
    bundle, spec, _, _ = _build(isolated_fs, skills_source)
    original_receipt = (bundle / "bundle-receipt.json").read_bytes()
    with pytest.raises(FileExistsError):
        _build(isolated_fs, skills_source, spec)
    assert (bundle / "bundle-receipt.json").read_bytes() == original_receipt

    public_parent = isolated_fs["root"] / "public-staging"
    public_parent.mkdir(mode=0o755)
    public_parent.chmod(0o755)
    with pytest.raises(ValueError, match="private"):
        build_profile_bundle(
            public_parent,
            replace(spec, request_id="request-public-parent"),
            env_bytes=b"",
            auth_bytes=b"{}",
            skills_source=skills_source,
        )

    real_parent = isolated_fs["root"] / "real-staging"
    real_parent.mkdir(mode=0o700)
    linked_parent = isolated_fs["root"] / "linked-staging"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(ValueError, match="real|symlinked parent"):
        build_profile_bundle(
            linked_parent,
            replace(spec, request_id="request-linked-parent"),
            env_bytes=b"",
            auth_bytes=b"{}",
            skills_source=skills_source,
        )


def test_publish_target_created_in_final_window_is_not_replaced(
    isolated_fs: dict[str, Path], skills_source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, spec, _, _ = _build(isolated_fs, skills_source)
    target = isolated_fs["profiles"] / spec.profile_id
    real_rename = provisioning._rename_noreplace

    def racing_rename(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
        if Path(source) == bundle and Path(destination) == target:
            target.mkdir(mode=0o700)
        real_rename(Path(source), Path(destination))

    monkeypatch.setattr(provisioning, "_rename_noreplace", racing_rename)
    with pytest.raises(FileExistsError):
        publish_named_profile_bundle(bundle, isolated_fs["profiles"], spec)

    assert bundle.is_dir()
    assert target.is_dir()
    assert not any(target.iterdir())


def test_publish_rejects_source_exchanged_after_validation(
    isolated_fs: dict[str, Path], skills_source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, spec, _, _ = _build(isolated_fs, skills_source)
    target = isolated_fs["profiles"] / spec.profile_id
    saved = bundle.with_name(bundle.name + ".validated")
    real_validate = provisioning.validate_profile_bundle
    exchanged = False

    def exchange_after_validation(path: Path, expected: ProfileBundleSpec) -> dict[str, Any]:
        nonlocal exchanged
        result = real_validate(path, expected)
        if Path(path) == bundle and not exchanged:
            exchanged = True
            os.rename(bundle, saved)
            bundle.mkdir(mode=0o700)
            (bundle / "unvalidated").write_text("must not publish", encoding="utf-8")
        return result

    monkeypatch.setattr(provisioning, "validate_profile_bundle", exchange_after_validation)
    with pytest.raises(ValueError, match="identity|changed"):
        publish_named_profile_bundle(bundle, isolated_fs["profiles"], spec)

    assert not target.exists()
    assert saved.is_dir()
    assert (bundle / "unvalidated").is_file()


def test_publish_tombstone_created_in_final_window_rolls_back_target(
    isolated_fs: dict[str, Path], skills_source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, spec, _, _ = _build(isolated_fs, skills_source)
    target = isolated_fs["profiles"] / spec.profile_id
    tombstone = profile_tombstone_path(target)
    real_rename = provisioning._rename_noreplace

    def racing_rename(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
        if Path(source) == bundle and Path(destination) == target:
            tombstone.parent.mkdir(mode=0o700, exist_ok=True)
            tombstone.write_text("deleted\n", encoding="utf-8")
        real_rename(Path(source), Path(destination))

    monkeypatch.setattr(provisioning, "_rename_noreplace", racing_rename)
    with pytest.raises(FileExistsError, match="tombstoned"):
        publish_named_profile_bundle(bundle, isolated_fs["profiles"], spec)

    assert not target.exists()
    assert bundle.is_dir()
    assert tombstone.is_file()


def test_build_rejects_symlinked_staging_ancestor_before_mutation(
    isolated_fs: dict[str, Path], skills_source: Path
) -> None:
    spec = _spec(request_id="request-symlinked-ancestor")
    ancestor_target = isolated_fs["root"] / "ancestor-target"
    private_below_target = ancestor_target / "private-staging"
    private_below_target.mkdir(parents=True, mode=0o700)
    private_below_target.chmod(0o700)
    ancestor_link = isolated_fs["root"] / "ancestor-link"
    ancestor_link.symlink_to(ancestor_target, target_is_directory=True)
    redirected_staging = ancestor_link / "private-staging"
    before = set(private_below_target.iterdir())

    with pytest.raises(ValueError, match="symlinked parent"):
        build_profile_bundle(
            redirected_staging,
            spec,
            env_bytes=b"",
            auth_bytes=b'{"provider":{"token":"synthetic-fixture"}}',
            skills_source=skills_source,
        )

    assert set(private_below_target.iterdir()) == before


def test_skill_file_swap_to_symlink_is_rejected_without_copying_external_bytes(
    isolated_fs: dict[str, Path], skills_source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    victim = skills_source / "operations" / "SKILL.md"
    outside = isolated_fs["root"] / "outside-skill"
    sentinel = b"EXTERNAL-SENTINEL-MUST-NOT-COPY"
    outside.write_bytes(sentinel)
    original_scan = provisioning._scan_regular_tree
    scans = 0

    def swap_after_copy_scan(root: Path, *, label: str):
        nonlocal scans
        result = original_scan(root, label=label)
        if Path(root) == skills_source:
            scans += 1
            if scans == 2:
                victim.unlink()
                victim.symlink_to(outside)
        return result

    monkeypatch.setattr(provisioning, "_scan_regular_tree", swap_after_copy_scan)
    with pytest.raises(ValueError, match="symlink|changed"):
        _build(isolated_fs, skills_source, _spec(request_id="request-skill-file-race"))

    for path in isolated_fs["staging"].rglob("*"):
        if path.is_file() and not path.is_symlink():
            assert sentinel not in path.read_bytes()


def test_skill_directory_swap_to_symlink_is_rejected(
    isolated_fs: dict[str, Path], skills_source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    victim = skills_source / "operations" / "references"
    saved = skills_source / "operations" / "references.saved"
    outside = isolated_fs["root"] / "outside-skill-dir"
    outside.mkdir(mode=0o700)
    (outside / "payload.bin").write_bytes(b"EXTERNAL-DIRECTORY-SENTINEL")
    original_scan = provisioning._scan_regular_tree
    scans = 0

    def swap_after_copy_scan(root: Path, *, label: str):
        nonlocal scans
        result = original_scan(root, label=label)
        if Path(root) == skills_source:
            scans += 1
            if scans == 2:
                victim.rename(saved)
                victim.symlink_to(outside, target_is_directory=True)
        return result

    monkeypatch.setattr(provisioning, "_scan_regular_tree", swap_after_copy_scan)
    with pytest.raises(ValueError, match="symlink|changed"):
        _build(isolated_fs, skills_source, _spec(request_id="request-skill-dir-race"))


def test_build_fsyncs_every_bundle_directory_bottom_up_before_publish(
    isolated_fs: dict[str, Path], skills_source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(request_id="request-directory-fsync")
    incomplete = isolated_fs["staging"] / f"{spec.profile_id}.{spec.request_id}.incomplete"
    calls: list[Path] = []
    real_fsync_directory = provisioning._fsync_directory

    def recording_fsync(path: Path) -> None:
        calls.append(Path(path))
        real_fsync_directory(path)

    monkeypatch.setattr(provisioning, "_fsync_directory", recording_fsync)
    bundle, _, _, _ = _build(isolated_fs, skills_source, spec)
    expected_relative = {Path(".")}
    expected_relative.update(path.relative_to(bundle) for path in bundle.rglob("*") if path.is_dir())
    prepublish_calls = [
        path.relative_to(incomplete)
        for path in calls
        if path == incomplete or incomplete in path.parents
    ]

    assert set(prepublish_calls) == expected_relative
    positions = {path: index for index, path in enumerate(prepublish_calls)}
    for path in expected_relative - {Path(".")}:
        parent = path.parent if path.parent != Path("") else Path(".")
        assert positions[path] < positions[parent]
    assert calls[-1] == isolated_fs["staging"]
