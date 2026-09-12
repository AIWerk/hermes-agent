from __future__ import annotations

import contextlib
import uuid
from pathlib import Path

from hermes_state import SessionDB
from hermes_state_common import FTS_STALE_KEY, _FTS_TRIGGERS


def _make_stale_detached_db(db_path: Path) -> SessionDB:
    db = SessionDB(db_path=db_path)
    sid = db.create_session(session_id=str(uuid.uuid4()), source="cli")
    db.append_message(sid, role="user", content="deferred fts recovery needle")
    cursor = db._conn.cursor()
    cursor.execute(
        "INSERT OR REPLACE INTO state_meta(key, value) VALUES (?, '1')",
        (FTS_STALE_KEY,),
    )
    db._drop_all_fts_triggers(cursor)
    db._conn.commit()
    db._fts_stale = True
    db._fts_enabled = False
    db._trigram_available = False
    db._fts_cjk_available = False
    db._fts_stale_retry_after = 0.0
    db._fts_stale_retry_interval = 0.0
    return db


def _trigger_names(db: SessionDB) -> set[str]:
    rows = db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger'"
    ).fetchall()
    return {str(row[0]) for row in rows}


def _has_stale_marker(db: SessionDB) -> bool:
    row = db._conn.execute(
        "SELECT 1 FROM state_meta WHERE key = ? LIMIT 1",
        (FTS_STALE_KEY,),
    ).fetchone()
    return row is not None


def test_deferred_fts_retry_clears_marker_only_after_verified_recovery(tmp_path):
    db = _make_stale_detached_db(tmp_path / "state.db")
    try:
        assert db.retry_deferred_fts_recovery() is True

        assert db._fts_stale is False
        assert db._fts_enabled is True
        assert _has_stale_marker(db) is False
        assert set(_FTS_TRIGGERS).issubset(_trigger_names(db))
        verified, detail = db._verify_fts_repair(db._conn)
        assert verified, detail
    finally:
        db.close()


def test_deferred_fts_retry_does_not_rebuild_without_admission(tmp_path, monkeypatch):
    import hermes_state_schema

    db = _make_stale_detached_db(tmp_path / "state.db")

    @contextlib.contextmanager
    def _not_admitted(*_args, **_kwargs):
        yield False

    monkeypatch.setattr(hermes_state_schema, "fts_rebuild_admission", _not_admitted)
    monkeypatch.setattr(
        db,
        "_recover_stale_fts_locked",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("rebuilt without admission")),
    )
    try:
        assert db.retry_deferred_fts_recovery() is False
        assert db._fts_stale is True
        assert db._fts_enabled is False
        assert _has_stale_marker(db) is True
        assert set(_FTS_TRIGGERS).isdisjoint(_trigger_names(db))
    finally:
        db.close()


def test_deferred_fts_retry_keeps_like_fallback_when_verification_fails(tmp_path, monkeypatch):
    db = _make_stale_detached_db(tmp_path / "state.db")
    monkeypatch.setattr(db, "_verify_fts_repair", lambda _conn: (False, "rank=1 failed"))
    try:
        assert db.retry_deferred_fts_recovery() is False
        assert db._fts_stale is True
        assert db._fts_enabled is False
        assert db._trigram_available is False
        assert _has_stale_marker(db) is True
        assert set(_FTS_TRIGGERS).isdisjoint(_trigger_names(db))
    finally:
        db.close()
