"""session_stack foreign keys must cascade on session delete.

Covers both the legacy-DB rebuild migration and the end-to-end cascade.
"""

from hermes_state import SessionDB


def _install_legacy_session_stack(conn):
    """Recreate session_stack in its original (no ON DELETE CASCADE) shape."""
    conn.executescript(
        """
        DROP TABLE IF EXISTS session_stack;
        CREATE TABLE session_stack (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            parent_session_id TEXT NOT NULL REFERENCES sessions(id),
            side_session_id TEXT NOT NULL REFERENCES sessions(id),
            title TEXT,
            pushed_at REAL NOT NULL,
            popped_at REAL,
            status TEXT NOT NULL DEFAULT 'active'
        );
        """
    )


def test_legacy_session_stack_fk_is_rebuilt_with_cascade(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("parent", source="cli")
    db.create_session("side", source="cli")

    conn = db._conn
    _install_legacy_session_stack(conn)
    conn.execute(
        "INSERT INTO session_stack (source, parent_session_id, side_session_id, pushed_at, status) "
        "VALUES ('cli', 'parent', 'side', 1.0, 'active')"
    )
    conn.commit()

    legacy = conn.execute("PRAGMA foreign_key_list('session_stack')").fetchall()
    assert legacy and all((str(row[6]) or "").upper() != "CASCADE" for row in legacy)

    db._init_schema()  # triggers _reconcile_session_stack_fk

    migrated = conn.execute("PRAGMA foreign_key_list('session_stack')").fetchall()
    assert len(migrated) == 2
    assert all((str(row[6]) or "").upper() == "CASCADE" for row in migrated)
    # The existing stack row is preserved by the rebuild.
    assert conn.execute("SELECT COUNT(*) FROM session_stack").fetchone()[0] == 1


def test_deleting_referenced_session_cascades_stack_row(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("parent", source="cli")
    db.create_session("side", source="cli")
    db.push_side_session("cli", "parent", "side", title="topic")

    conn = db._conn
    assert conn.execute("SELECT COUNT(*) FROM session_stack").fetchone()[0] == 1

    # Previously this aborted with a FOREIGN KEY constraint error.
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("DELETE FROM sessions WHERE id = 'parent'")
    conn.commit()

    assert conn.execute("SELECT COUNT(*) FROM session_stack").fetchone()[0] == 0


def test_reconcile_recovers_from_leftover_session_stack_new(tmp_path):
    """A leftover session_stack_new (crash mid-rebuild) must not wedge the migration.

    If a previous reconcile was killed after CREATE TABLE session_stack_new but
    before DROP TABLE session_stack, the temp table persists on disk.  Without a
    leading DROP TABLE IF EXISTS, the next CREATE raises 'table already exists',
    aborting the whole executescript and leaving session_stack without CASCADE
    forever.  The reconcile must instead drop the stale temp table and succeed.
    """
    db = SessionDB(tmp_path / "state.db")
    db.create_session("parent", source="cli")
    db.create_session("side", source="cli")

    conn = db._conn
    _install_legacy_session_stack(conn)
    conn.execute(
        "INSERT INTO session_stack (source, parent_session_id, side_session_id, pushed_at, status) "
        "VALUES ('cli', 'parent', 'side', 1.0, 'active')"
    )
    # Simulate the crash residue: a leftover temp table from a half-done rebuild.
    conn.execute("CREATE TABLE session_stack_new (bogus INTEGER)")
    conn.commit()

    legacy = conn.execute("PRAGMA foreign_key_list('session_stack')").fetchall()
    assert legacy and all((str(row[6]) or "").upper() != "CASCADE" for row in legacy)

    db._init_schema()  # triggers _reconcile_session_stack_fk

    migrated = conn.execute("PRAGMA foreign_key_list('session_stack')").fetchall()
    assert len(migrated) == 2
    assert all((str(row[6]) or "").upper() == "CASCADE" for row in migrated)
    # Original row survived the rebuild, and the stale temp table is gone.
    assert conn.execute("SELECT COUNT(*) FROM session_stack").fetchone()[0] == 1
    leftover = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='session_stack_new'"
    ).fetchall()
    assert leftover == []


def test_fk_rebuild_is_atomic_no_populated_temp_table_left_behind(tmp_path):
    """After a successful rebuild no populated session_stack_new may survive.

    The rebuild wraps DROP session_stack + RENAME session_stack_new in one
    transaction, so the table named session_stack is always present and the
    temp table is always gone once the reconcile returns. (The dangerous crash
    window was: session_stack dropped while a populated session_stack_new
    persisted, orphaning the rows permanently.)
    """
    db = SessionDB(tmp_path / "state.db")
    db.create_session("parent", source="cli")
    db.create_session("side", source="cli")

    conn = db._conn
    _install_legacy_session_stack(conn)
    conn.execute(
        "INSERT INTO session_stack (source, parent_session_id, side_session_id, pushed_at, status) "
        "VALUES ('cli', 'parent', 'side', 1.0, 'active')"
    )
    conn.commit()

    db._init_schema()  # triggers _reconcile_session_stack_fk

    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('session_stack', 'session_stack_new')"
        ).fetchall()
    }
    assert "session_stack" in tables
    assert "session_stack_new" not in tables
    assert conn.execute("SELECT COUNT(*) FROM session_stack").fetchone()[0] == 1


def test_fk_rebuild_drops_orphan_rows_referencing_missing_sessions(tmp_path):
    """Stack rows whose parent/side session is gone are filtered by the rebuild."""
    db = SessionDB(tmp_path / "state.db")
    db.create_session("parent", source="cli")
    db.create_session("side", source="cli")

    conn = db._conn
    _install_legacy_session_stack(conn)
    # One valid row and one orphan referencing a non-existent session. The
    # orphan can only be inserted with FK enforcement off (the legacy no-CASCADE
    # schema still rejects it otherwise) — this mirrors rows a pre-FK-pragma
    # build could accumulate.
    conn.execute(
        "INSERT INTO session_stack (source, parent_session_id, side_session_id, pushed_at, status) "
        "VALUES ('cli', 'parent', 'side', 1.0, 'active')"
    )
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute(
        "INSERT INTO session_stack (source, parent_session_id, side_session_id, pushed_at, status) "
        "VALUES ('cli', 'parent', 'ghost', 2.0, 'active')"
    )
    conn.execute("PRAGMA foreign_keys=ON")
    conn.commit()

    db._init_schema()  # triggers _reconcile_session_stack_fk

    rows = conn.execute(
        "SELECT side_session_id FROM session_stack"
    ).fetchall()
    assert [r[0] for r in rows] == ["side"]
