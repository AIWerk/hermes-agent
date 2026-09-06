"""Offline repair must complete while retaining its non-reentrant writer lock."""
import os
from pathlib import Path
import subprocess
import sys


def test_offline_repair_does_not_reenter_writer_lock(tmp_path):
    code = '''
import faulthandler
import sqlite3
from pathlib import Path
import sys
from hermes_state import SessionDB
faulthandler.dump_traceback_later(3)
db = SessionDB(db_path=Path(sys.argv[1]))
try:
    assert db._enter_fts_fail_open(sqlite3.DatabaseError("fts5: corrupt structure"))
    # Both helpers must use the same live transaction, with the caller
    # retaining the non-reentrant lock throughout rebuilding and verification.
    for name in ("_repair_stale_cjk_fts_offline", "_verify_fts_repair"):
        original = getattr(db, name)
        def check_ownership(*args, _original=original):
            assert db._lock.locked()
            assert db._conn.in_transaction
            if args:
                assert args[0] is db._conn
            return _original(*args)
        setattr(db, name, check_ownership)
    report = db.repair_fts_offline()
    assert report["repaired"] and report["verified"], report
    assert not db.fts_health_state()["repair_required"]
finally:
    db.close()
faulthandler.cancel_dump_traceback_later()
'''
    env = dict(os.environ, HERMES_HOME=str(tmp_path / "home"))
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path / "state.db")],
        cwd=Path(__file__).resolve().parents[2], env=env,
        capture_output=True, text=True, timeout=8,
    )
    assert result.returncode == 0, result.stdout + result.stderr
