import json

from tools import session_search_tool


class _DegradedDB:
    def search_messages(self, **kwargs):
        return []

    def fts_rebuild_status(self):
        return None

    def fts_health_state(self):
        return {
            "repair_required": True,
            "severity": "error",
            "repair_command": "hermes sessions repair-search",
        }


def test_discovery_reports_degraded_search_provenance(monkeypatch):
    monkeypatch.setattr(session_search_tool, "_resolve_lineage", lambda *args: None)
    monkeypatch.setattr(session_search_tool, "_title_match_result", lambda *args: None)
    payload = json.loads(
        session_search_tool._discover(
            _DegradedDB(), "needle", None, 3, None, "adaptive"
        )
    )
    assert payload["search_provenance"] == {
        "degraded": True,
        "engine": "canonical_like",
        "repair_required": True,
    }
    assert payload["operator_alert"]["severity"] == "error"
    assert payload["operator_alert"]["repair_command"] == "hermes sessions repair-search"
