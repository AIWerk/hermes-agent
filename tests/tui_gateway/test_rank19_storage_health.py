from contextlib import contextmanager


class _RepairRequiredDB:
    def fts_health_state(self):
        return {
            "repair_required": True,
            "severity": "error",
            "failure_count": 2,
            "repair_command": "hermes sessions repair-search",
        }


@contextmanager
def _db_context():
    yield _RepairRequiredDB()


def test_session_list_surfaces_persistent_repair_required_warning(monkeypatch):
    import tui_gateway.server as server

    monkeypatch.setattr(server, "_profile_db", lambda params: _db_context())
    monkeypatch.setattr(server, "current_cui_actor_context", lambda: None)
    monkeypatch.setattr(server, "_iter_visible_persisted_session_rows", lambda *a, **k: iter(()))
    response = server._methods["session.list"](1, {})
    health = response["result"]["storage_health"]
    assert health["repair_required"] is True
    assert health["severity"] == "error"
    assert health["repair_command"] == "hermes sessions repair-search"
