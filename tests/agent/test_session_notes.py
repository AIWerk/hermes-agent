"""Tests for deterministic session note recording helpers."""

from agent.session_notes import (
    get_session_scratchpad,
    record_session_event,
    redact_sensitive_text,
)
from hermes_state import SessionDB


def test_redact_sensitive_text_masks_common_secret_shapes():
    text = "Authorization: Bearer *** and api_key=AIzaSySECRET and password=hunter2 and token ***"

    redacted = redact_sensitive_text(text)

    assert "Bearer ***" not in redacted
    assert "AIzaSySECRET" not in redacted
    assert "hunter2" not in redacted
    assert "token ***" not in redacted
    assert "[REDACTED]" in redacted


def test_record_session_event_redacts_and_updates_scratchpad(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("sess-1", source="cli")

    record_session_event(
        db,
        "sess-1",
        "artifact",
        {
            "artifact_type": "file",
            "path": "/tmp/secret.txt",
            "summary": "Created file with token sk-live-abc123",
        },
        turn_index=4,
    )

    events = db.get_session_events("sess-1")
    assert events[0]["content"]["summary"] == "Created file with token [REDACTED]"

    scratchpad = get_session_scratchpad(db, "sess-1")
    assert scratchpad["artifacts"] == [
        {
            "artifact_type": "file",
            "path": "/tmp/secret.txt",
            "summary": "Created file with token [REDACTED]",
        }
    ]


def test_record_session_event_redacts_structured_json_secret(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("sess-1", source="cli")

    token = "ya29." + "A0ABCDEFG12345678901234567890"
    record_session_event(
        db,
        "sess-1",
        "decision",
        {"summary": 'tool returned {"access_token":"' + token + '"}'},
    )

    events = db.get_session_events("sess-1")
    persisted = events[0]["content"]["summary"]
    assert token not in persisted
    assert "[REDACTED]" in persisted


def test_record_session_event_keeps_scratchpad_compact(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("sess-1", source="cli")

    for index in range(20):
        record_session_event(
            db,
            "sess-1",
            "decision",
            {"summary": f"Decision {index}"},
        )

    scratchpad = get_session_scratchpad(db, "sess-1")
    assert len(scratchpad["decisions"]) == 12
    assert scratchpad["decisions"][0] == "Decision 8"
