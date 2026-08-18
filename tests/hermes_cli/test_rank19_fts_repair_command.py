from pathlib import Path
from types import SimpleNamespace

import hermes_state
from hermes_cli.sessions_cmd import cmd_sessions


class _FakeDB:
    def __init__(self, *args, **kwargs):
        self.db_path = hermes_state.DEFAULT_DB_PATH

    def repair_fts_offline(self):
        return {"repaired": True, "verified": True, "indexes_rebuilt": 2}

    def close(self):
        pass


class _NoopDB(_FakeDB):
    def repair_fts_offline(self):
        return {"repaired": False, "verified": True, "reason": "not-required"}


class _RestoreWarningDB(_FakeDB):
    def repair_fts_offline(self):
        return {
            "repaired": True,
            "verified": True,
            "connection_restored": False,
            "connection_restore_error": "injected reconnect failure",
        }


def _args():
    return SimpleNamespace(sessions_action="repair-search")


def test_repair_search_refuses_when_other_process_holds_database(monkeypatch, tmp_path, capsys):
    db_path = tmp_path / "state.db"
    db_path.touch()
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(hermes_state, "count_db_holders", lambda path: 1)
    monkeypatch.setattr(hermes_state, "SessionDB", _FakeDB)
    rc = cmd_sessions(_args())
    assert rc == 2
    assert "offline" in capsys.readouterr().out.lower()


def test_repair_search_reports_verified_success(monkeypatch, tmp_path, capsys):
    db_path = tmp_path / "state.db"
    db_path.touch()
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(hermes_state, "count_db_holders", lambda path: 0)
    monkeypatch.setattr(hermes_state, "SessionDB", _FakeDB)
    rc = cmd_sessions(_args())
    assert rc in (None, 0)
    output = capsys.readouterr().out.lower()
    assert "verified" in output
    assert "repair-required" in output


def test_repair_search_treats_not_required_as_successful_noop(
    monkeypatch, tmp_path, capsys
):
    db_path = tmp_path / "state.db"
    db_path.touch()
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(hermes_state, "count_db_holders", lambda path: 0)
    monkeypatch.setattr(hermes_state, "SessionDB", _NoopDB)
    rc = cmd_sessions(_args())
    assert rc in (None, 0)
    output = capsys.readouterr().out.lower()
    assert "not required" in output
    assert "already clear" in output
    assert "remains set" not in output


def test_repair_search_reports_repair_success_with_restore_warning(
    monkeypatch, tmp_path, capsys
):
    db_path = tmp_path / "state.db"
    db_path.touch()
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(hermes_state, "count_db_holders", lambda path: 0)
    monkeypatch.setattr(hermes_state, "SessionDB", _RestoreWarningDB)
    rc = cmd_sessions(_args())
    assert rc in (None, 0)
    output = capsys.readouterr().out.lower()
    assert "repair-required was cleared" in output
    assert "connection restoration failed" in output
    assert "remains set" not in output
