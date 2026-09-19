"""Behavior-policy central store contract (initial RED)."""
from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path

import pytest

from tests.profile_authorization_support import ADMIN, capability, install_policy


def _store(tmp_path, monkeypatch, *, max_versions=256):
    bp = capability("hermes_cli.dashboard_auth.behavior_policy")
    root = tmp_path / "central-store"
    root.mkdir(mode=0o700)
    monkeypatch.setattr(bp, "BEHAVIOR_POLICY_ROOT", str(root))
    monkeypatch.setattr(bp, "TRUSTED_SERVICE_UID", os.getuid())
    monkeypatch.setattr(bp, "MAX_RETAINED_VERSIONS", max_versions)
    return bp, bp.Store(), root


def _commit(store, text, expected_revision=None, rollback_of=None):
    return store.commit(
        tenant_id="tenant-a", target_profile="employee-home", instructions=text,
        actor_id="admin", expected_revision=expected_revision, rollback_of=rollback_of,
    )


def _paths(root):
    profile = root / hashlib.sha256(b"tenant-a").hexdigest() / "employee-home"
    versions = profile / "versions"
    history = next(versions.iterdir())
    return {"root": root, "tenant": profile.parent, "profile": profile,
            "lock": profile / ".lock", "current": profile / "current.json",
            "versions": versions, "history": history}


def test_store_rejects_oversize_unknown_fields_bad_utf8_and_forbidden_controls(tmp_path, monkeypatch):
    bp, store, _ = _store(tmp_path, monkeypatch)
    invalid = ["x" * 8193, "nul\x00", "cr\r", "del\x7f", "c1\u0085", "bidi\u202e", "bom\ufeff"]
    for text in invalid:
        with pytest.raises(bp.BehaviorPolicyValidationError):
            _commit(store, text)
    with pytest.raises(bp.BehaviorPolicyValidationError):
        store.validate_document({
            "schema_version": 1, "revision": 1, "content_digest": "0" * 64,
            "policy": {"kind": "assistant_behavior", "instructions": "ok"},
            "created_at": "2026-09-19T00:00:00Z", "created_by": "admin",
            "rollback_of": None, "unexpected": True,
        })
    with pytest.raises((UnicodeEncodeError, bp.BehaviorPolicyValidationError)):
        _commit(store, "surrogate \ud800")


@pytest.mark.parametrize("node", ["root", "tenant", "profile", "lock", "current", "versions", "history"])
def test_store_refuses_symlink_root_ancestor_lock_current_versions_and_history_leaf(tmp_path, monkeypatch, node):
    bp, store, root = _store(tmp_path, monkeypatch)
    _commit(store, "first")
    selected = _paths(root)[node]
    real = selected.with_name(selected.name + ".real")
    selected.rename(real)
    selected.symlink_to(real, target_is_directory=real.is_dir())
    with pytest.raises(bp.BehaviorPolicyStoreError):
        store.read_current("tenant-a", "employee-home")


@pytest.mark.parametrize("unsafe", ["owner", "mode", "nonregular"])
def test_store_refuses_wrong_owner_nonregular_and_group_or_world_writable_nodes(tmp_path, monkeypatch, unsafe):
    bp, store, root = _store(tmp_path, monkeypatch)
    _commit(store, "first")
    current = _paths(root)["current"]
    if unsafe == "owner":
        monkeypatch.setattr(bp, "TRUSTED_SERVICE_UID", os.getuid() + 1)
    elif unsafe == "mode":
        current.chmod(0o666)
    else:
        current.unlink()
        os.mkfifo(current)
    with pytest.raises(bp.BehaviorPolicyStoreError):
        store.read_current("tenant-a", "employee-home")


@pytest.mark.parametrize("boundary", ["history_write", "history_fsync", "current_replace", "directory_fsync"])
def test_atomic_fault_before_current_publish_keeps_old_current_and_retry_is_safe(tmp_path, monkeypatch, boundary):
    bp, store, _ = _store(tmp_path, monkeypatch)
    first = _commit(store, "first")
    real_write, real_fsync, real_replace = os.write, os.fsync, os.replace
    state = {"writes": 0, "fsyncs": 0, "replaced": False}

    def write(fd, data):
        state["writes"] += 1
        if boundary == "history_write" and state["writes"] == 1:
            raise OSError("injected history write fault")
        return real_write(fd, data)

    def replace(src, dst, *args, **kwargs):
        if boundary == "current_replace":
            raise OSError("injected current replace fault")
        result = real_replace(src, dst, *args, **kwargs)
        state["replaced"] = True
        return result

    def fsync(fd):
        state["fsyncs"] += 1
        if boundary == "history_fsync" and state["fsyncs"] == 1:
            raise OSError("injected history fsync fault")
        if boundary == "directory_fsync" and state["replaced"]:
            raise OSError("injected directory fsync fault")
        return real_fsync(fd)

    with monkeypatch.context() as faults:
        faults.setattr(os, "write", write)
        faults.setattr(os, "fsync", fsync)
        faults.setattr(os, "replace", replace)
        with pytest.raises(bp.BehaviorPolicyStoreError):
            _commit(store, "second", first.revision)
    assert store.read_current("tenant-a", "employee-home").instructions == "first"
    second = _commit(store, "second", first.revision)
    assert second.revision == first.revision + 1
    assert store.read_current("tenant-a", "employee-home").content_digest == second.content_digest


def test_changed_retry_after_orphan_history_never_publishes_or_duplicates_revision(tmp_path, monkeypatch):
    bp, store, root = _store(tmp_path, monkeypatch)
    first = _commit(store, "first")
    real_replace = os.replace
    failed_once = False

    def replace(src, dst, *args, **kwargs):
        nonlocal failed_once
        if dst == "current.json" and not failed_once:
            failed_once = True
            raise OSError("injected current publication fault")
        return real_replace(src, dst, *args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(os, "replace", replace)
        with pytest.raises(bp.BehaviorPolicyStoreError):
            _commit(store, "orphan second", first.revision)

    with pytest.raises((bp.BehaviorPolicyConflict, bp.BehaviorPolicyStoreError)):
        _commit(store, "different retry", first.revision)

    assert store.read_current("tenant-a", "employee-home").instructions == "first"
    versions = _paths(root)["versions"]
    revision_two = [path for path in versions.iterdir() if path.name.startswith("00000000000000000002-")]
    assert len(revision_two) == 1


def test_concurrent_writers_are_serialized_and_one_stale_cas_loses(tmp_path, monkeypatch):
    bp, store, _ = _store(tmp_path, monkeypatch)
    first = _commit(store, "first")
    barrier = threading.Barrier(2)
    results = []
    def writer(text):
        barrier.wait()
        try:
            results.append(("ok", _commit(store, text, first.revision)))
        except Exception as exc:  # result is asserted by exact type below
            results.append(("error", exc))
    threads = [threading.Thread(target=writer, args=(text,)) for text in ("left", "right")]
    for thread in threads: thread.start()
    for thread in threads: thread.join(5)
    assert not any(thread.is_alive() for thread in threads)
    assert [kind for kind, _ in results].count("ok") == 1
    loser = next(value for kind, value in results if kind == "error")
    assert isinstance(loser, bp.BehaviorPolicyConflict)


def test_history_is_bounded_metadata_and_get_revision_requires_read_grant(tmp_path, monkeypatch):
    bp, store, _ = _store(tmp_path, monkeypatch, max_versions=3)
    current = None
    for text in ("one", "two", "three", "four"):
        current = _commit(store, text, None if current is None else current.revision)
    rows = store.history("tenant-a", "employee-home", limit=100)
    assert len(rows) == 3
    assert all("instructions" not in row for row in rows)
    assert rows == sorted(rows, key=lambda row: row["revision"], reverse=True)
    authority_root = tmp_path / "authority"
    authority_root.mkdir()
    env = install_policy(authority_root, monkeypatch)
    env.revoke(actor="admin", action="behavior.read")
    with pytest.raises(bp.BehaviorPolicyDenied, match="^profile access denied$"):
        bp.read_current(ADMIN, "employee-home", revision=current.revision)


def test_rollback_requires_actual_confirmation_and_creates_forward_revision(tmp_path, monkeypatch):
    bp, store, _ = _store(tmp_path, monkeypatch)
    first = _commit(store, "first")
    second = _commit(store, "second", first.revision)
    denied = store.rollback("tenant-a", "employee-home", first.revision, second.revision,
                            actor_id="admin", approval_fn=lambda *_a, **_k: "decline")
    assert denied is None
    assert store.read_current("tenant-a", "employee-home").revision == second.revision
    rolled = store.rollback("tenant-a", "employee-home", first.revision, second.revision,
                            actor_id="admin", approval_fn=lambda *_a, **_k: "accept")
    assert rolled.revision == second.revision + 1
    assert rolled.rollback_of == first.revision
    assert rolled.instructions == "first"


def test_rollback_revocation_or_cas_conflict_is_no_write(tmp_path, monkeypatch):
    bp, store, _ = _store(tmp_path, monkeypatch)
    first = _commit(store, "first")
    second = _commit(store, "second", first.revision)
    for expected in (first.revision, second.revision):
        calls = []
        def approval(*_a, **_k):
            calls.append(True)
            if expected == second.revision:
                _commit(store, "winner", second.revision)
            return "accept"
        with pytest.raises((bp.BehaviorPolicyConflict, bp.BehaviorPolicyDenied)):
            bp.rollback(ADMIN, "employee-home", first.revision, expected, approval_fn=approval)
    assert calls


def test_behavior_audit_allowlist_excludes_policy_preview_transcript_query_paths_and_secrets(tmp_path, monkeypatch):
    bp, store, _ = _store(tmp_path, monkeypatch)
    event = bp.audit_behavior_event(
        actor=ADMIN, target_profile="employee-home", operation="preview", result="denied",
        authorization_revision="a" * 64, base_revision=0, result_revision=None,
        content_digest="b" * 64, byte_count=4,
        instructions="SECRET POLICY", preview="DIFF", token="TOKEN", transcript="CHAT",
        query="QUERY", path="/private/path", secret="COOKIE",
    )
    allowed = {"event_id", "correlation_id", "actor_id", "tenant_id", "target_profile",
               "operation", "authorization_revision", "base_revision", "result_revision",
               "content_digest", "result", "byte_count"}
    assert set(event) <= allowed
    encoded = json.dumps(event)
    for forbidden in ("SECRET POLICY", "DIFF", "TOKEN", "CHAT", "QUERY", "/private/path", "COOKIE"):
        assert forbidden not in encoded
