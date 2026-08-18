"""Rank 19: persistent FTS repair-required state and verified offline repair."""

import sqlite3

import pytest

from hermes_state import (
    FTS_CJK_STALE_KEY,
    FTS_STALE_KEY,
    SessionDB,
    _FTS_CJK_TRIGGERS,
    _FTS_TRIGGERS,
)


def _corrupt_fts(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "UPDATE messages_fts_data "
            "SET block = X'DEADBEEFDEADBEEFDEADBEEFDEADBEEF'"
        )
        conn.commit()
    finally:
        conn.close()


def _meta(db_path, key):
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT value FROM state_meta WHERE key = ?", (key,)
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else row[0]


def _triggers(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            f"AND name IN ({','.join('?' for _ in _FTS_TRIGGERS)})",
            _FTS_TRIGGERS,
        ).fetchall()
    finally:
        conn.close()
    return {row[0] for row in rows}


def _enter_repair_required(db, db_path):
    db.create_session("s1", source="test")
    db.append_message("s1", "user", "seed needle")
    _corrupt_fts(db_path)
    db.append_message("s1", "user", "first heal needle")
    _corrupt_fts(db_path)
    db.append_message("s1", "user", "second corruption needle")
    assert db._fts_stale is True


@pytest.fixture
def stale_db(tmp_path):
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    if not db._fts_enabled:
        db.close()
        pytest.skip("FTS5 unavailable in this build")
    _enter_repair_required(db, db_path)
    yield db, db_path
    try:
        db.close()
    except Exception:
        pass


def test_second_corruption_persists_repair_required_and_keeps_canonical_write(stale_db):
    db, db_path = stale_db
    with sqlite3.connect(str(db_path)) as conn:
        contents = [r[0] for r in conn.execute("SELECT content FROM messages ORDER BY id")]
    assert contents == ["seed needle", "first heal needle", "second corruption needle"]
    assert _meta(db_path, FTS_STALE_KEY) == "1"
    assert _triggers(db_path) == set()
    assert db.fts_health_state()["repair_required"] is True


def test_repair_required_survives_reopen_without_automatic_clear(stale_db):
    db, db_path = stale_db
    db.close()
    reopened = SessionDB(db_path=db_path)
    try:
        assert reopened.fts_health_state()["repair_required"] is True
        assert _meta(db_path, FTS_STALE_KEY) == "1"
        assert _triggers(db_path) == set()
    finally:
        reopened.close()


def test_other_process_observes_repair_required(stale_db):
    _, db_path = stale_db
    peer = SessionDB(db_path=db_path)
    try:
        state = peer.fts_health_state()
        assert state["repair_required"] is True
        assert state["severity"] == "error"
    finally:
        peer.close()


def test_health_state_records_first_last_error_and_count(stale_db):
    db, _ = stale_db
    state = db.fts_health_state()
    assert state["failure_count"] >= 1
    assert state["first_error_at"] <= state["last_error_at"]
    assert "corrupt" in state["last_error"].lower() or "malformed" in state["last_error"].lower()
    assert state["repair_command"] == "hermes sessions repair-search"


def test_like_fallback_results_report_degraded_provenance(stale_db):
    db, _ = stale_db
    rows = db.search_messages("second corruption")
    assert rows
    assert all(row["search_provenance"]["degraded"] is True for row in rows)
    assert all(row["search_provenance"]["engine"] == "canonical_like" for row in rows)
    assert all(row["search_provenance"]["repair_required"] is True for row in rows)


def test_healthy_fts_results_report_fts_provenance(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        if not db._fts_enabled:
            pytest.skip("FTS5 unavailable in this build")
        db.create_session("s1", source="test")
        db.append_message("s1", "user", "healthy provenance needle")
        rows = db.search_messages("needle")
        assert rows
        assert all(row["search_provenance"]["degraded"] is False for row in rows)
        assert all(row["search_provenance"]["engine"].startswith("fts") for row in rows)
        projected = db.search_messages(
            "needle", fields=("session_id", "role", "snippet")
        )
        assert projected
        assert all(
            set(row) == {"session_id", "role", "snippet"} for row in projected
        )
    finally:
        db.close()


def test_verified_offline_repair_clears_state_only_after_integrity_success(stale_db):
    db, db_path = stale_db
    cjk_was_stale = _meta(db_path, FTS_CJK_STALE_KEY) is not None
    report = db.repair_fts_offline()
    assert report["repaired"] is True, report
    assert report["verified"] is True, report
    assert _meta(db_path, FTS_STALE_KEY) is None
    assert _triggers(db_path) == set(_FTS_TRIGGERS)
    assert report["cjk_rebuilt"] is cjk_was_stale
    if cjk_was_stale:
        assert _meta(db_path, FTS_CJK_STALE_KEY) is None
        with sqlite3.connect(str(db_path)) as conn:
            cjk_triggers = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger' "
                    f"AND name IN ({','.join('?' for _ in _FTS_CJK_TRIGGERS)})",
                    _FTS_CJK_TRIGGERS,
                )
            }
        assert cjk_triggers == set(_FTS_CJK_TRIGGERS)
    assert db.fts_health_state()["repair_required"] is False


def test_mixed_legacy_base_uses_external_integrity_for_cjk():
    assert SessionDB._fts_schema_uses_external_content(
        "CREATE VIRTUAL TABLE inline_fts USING fts5(content)"
    ) is False
    assert SessionDB._fts_schema_uses_external_content(
        "CREATE VIRTUAL TABLE contentless_fts USING fts5(content, content='')"
    ) is False
    assert SessionDB._fts_schema_uses_external_content(
        "CREATE VIRTUAL TABLE token_fts USING fts5("
        "body, tokenize='unicode61 content=fake')"
    ) is False
    assert SessionDB._fts_schema_uses_external_content(
        "/* USING fts5(content='') */ "
        "CREATE VIRTUAL TABLE decoy_fts USING fts5("
        "body, content='canonical_view')"
    ) is True
    assert SessionDB._fts_schema_uses_external_content(
        "CREATE VIRTUAL TABLE whitespace_name_fts USING fts5("
        "body, content=' ')"
    ) is True
    assert SessionDB._fts_schema_uses_external_content(
        "CREATE VIRTUAL TABLE quoted_fts USING fts5("
        "body, tokenize='unicode61 content=fake', /* option */ "
        '"content" = \'canonical_view\')'
    ) is True
    assert SessionDB._fts_schema_uses_external_content(
        "CREATE VIRTUAL TABLE trailing_comment_fts USING fts5("
        "body, content='canonical_view'); /* valid trailing comment */"
    ) is True

    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY,
            role TEXT NOT NULL,
            content TEXT,
            tool_name TEXT,
            tool_calls TEXT
        );
        INSERT INTO messages(id, role, content)
        VALUES (1, 'user', 'canonically present but not indexed');
        CREATE VIRTUAL TABLE messages_fts USING fts5(content);
        INSERT INTO messages_fts(content) VALUES ('healthy inline row');
        CREATE VIEW messages_fts_cjk_src AS
            SELECT id, role, content, tool_name, tool_calls
            FROM messages WHERE role <> 'tool';
        CREATE VIRTUAL TABLE messages_fts_cjk USING fts5(
            role UNINDEXED,
            content,
            tool_name,
            tool_calls,
            content='messages_fts_cjk_src',
            content_rowid='id',
            tokenize='unicode61'
        );
        """
    )
    db = object.__new__(SessionDB)
    db._conn = conn
    try:
        assert db._db_has_legacy_inline_fts(conn.cursor()) is True
        verified, detail = db._verify_fts_repair()
        assert verified is False
        assert detail != "ok"
    finally:
        conn.close()


@pytest.mark.parametrize(
    "schema",
    [
        "CREATE VIRTUAL TABLE bad USING fts5(body, content=)",
        "CREATE VIRTUAL TABLE bad USING fts5(body,,content='canonical')",
        "CREATE VIRTUAL TABLE bad USING fts5(body, content='canonical',)",
        "CREATE VIRTUAL TABLE bad USING fts5(body, content='canonical') garbage",
        "CREATE VIRTUAL TABLE bad USING fts5(body, content='canonical') "
        "'unterminated",
        "CREATE VIRTUAL TABLE bad USING fts5(body, content='canonical') "
        "/* unterminated",
    ],
)
def test_fts_schema_parser_rejects_malformed_forms(schema):
    with pytest.raises(sqlite3.DatabaseError):
        SessionDB._fts_schema_uses_external_content(schema)


@pytest.mark.parametrize(
    "failure_stage", ["connect", "init_schema", "keyboard_interrupt"]
)
def test_verified_repair_survives_writer_restore_failure(
    stale_db, monkeypatch, failure_stage
):
    db, db_path = stale_db

    def fail_restore(*args, **kwargs):
        if failure_stage == "keyboard_interrupt":
            raise KeyboardInterrupt("injected restore cancellation")
        raise sqlite3.OperationalError(f"injected {failure_stage} restore failure")

    if failure_stage in {"connect", "keyboard_interrupt"}:
        monkeypatch.setattr("hermes_state._connect_tracked_db", fail_restore)
    else:
        monkeypatch.setattr(db, "_init_schema", fail_restore)

    if failure_stage == "keyboard_interrupt":
        with pytest.raises(KeyboardInterrupt, match="restore cancellation"):
            db.repair_fts_offline()
        assert _meta(db_path, FTS_STALE_KEY) is None
        assert db._conn is None
        db.close()
        return

    report = db.repair_fts_offline()
    assert report["repaired"] is True
    assert report["verified"] is True
    assert report["connection_restored"] is False
    assert failure_stage in report["connection_restore_error"]
    assert _meta(db_path, FTS_STALE_KEY) is None
    assert db._conn is None
    assert db._fts_enabled is False
    assert db._fts_cjk_loaded is False
    db.close()


def test_failed_offline_repair_keeps_state_and_triggers_detached(stale_db, monkeypatch):
    db, db_path = stale_db
    cjk_was_stale = _meta(db_path, FTS_CJK_STALE_KEY) is not None

    def injected_verification_failure():
        intruder = sqlite3.connect(str(db_path), timeout=0)
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                intruder.execute("SELECT COUNT(*) FROM messages").fetchone()
        finally:
            intruder.close()
        return False, "injected failure"

    monkeypatch.setattr(db, "_verify_fts_repair", injected_verification_failure)
    report = db.repair_fts_offline()
    assert report["repaired"] is False
    assert report["verified"] is False
    assert _meta(db_path, FTS_STALE_KEY) == "1"
    assert _triggers(db_path) == set()
    if cjk_was_stale:
        assert _meta(db_path, FTS_CJK_STALE_KEY) == "1"
    assert db.fts_health_state()["repair_required"] is True


def test_repeated_failure_injection_never_reports_healthy(stale_db, monkeypatch):
    db, db_path = stale_db
    monkeypatch.setattr(db, "_verify_fts_repair", lambda: (False, "injected failure one"))
    assert db.repair_fts_offline()["verified"] is False
    db.close()
    reopened = SessionDB(db_path=db_path)
    try:
        monkeypatch.setattr(reopened, "_verify_fts_repair", lambda: (False, "injected failure two"))
        assert reopened.repair_fts_offline()["verified"] is False
        assert reopened.fts_health_state()["repair_required"] is True
        rows = reopened.search_messages("needle")
        assert rows and rows[0]["search_provenance"]["degraded"] is True
    finally:
        reopened.close()
