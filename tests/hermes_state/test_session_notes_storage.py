"""Tests for incremental per-session event and scratchpad storage."""

import pytest

from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    return SessionDB(tmp_path / "state.db")


def test_session_events_round_trip_and_limit(db):
    db.create_session("sess-1", source="cli")

    first_id = db.add_session_event(
        "sess-1",
        "decision",
        {"summary": "Use incremental notes", "tags": ["session-index"]},
        turn_index=2,
        source="runtime",
    )
    second_id = db.add_session_event(
        "sess-1",
        "artifact",
        {"path": "agent/session_notes.py", "action": "created"},
        turn_index=3,
    )

    assert first_id < second_id
    assert db.get_session_events("sess-1", limit=1)[0]["event_type"] == "artifact"

    events = db.get_session_events("sess-1")
    assert [event["event_type"] for event in events] == ["decision", "artifact"]
    assert events[0]["content"] == {"summary": "Use incremental notes", "tags": ["session-index"]}
    assert events[0]["turn_index"] == 2
    assert events[0]["source"] == "runtime"


def test_add_session_event_rejects_unknown_type(db):
    db.create_session("sess-1", source="cli")

    with pytest.raises(ValueError):
        db.add_session_event("sess-1", "raw_dump", {"summary": "too broad"})


def test_session_scratchpad_upsert_round_trip(db):
    db.create_session("sess-1", source="cli")

    db.set_session_scratchpad(
        "sess-1",
        {
            "current_goal": "Build incremental notes",
            "decisions": ["Store events in SQLite"],
            "artifacts": [{"path": "hermes_state.py"}],
            "open_items": ["Wire runtime hook"],
            "candidates": [{"type": "wiki", "summary": "document feature"}],
        },
    )
    db.set_session_scratchpad(
        "sess-1",
        {
            "current_goal": "Verify incremental notes",
            "decisions": ["Keep scratchpad after finalization for debugging"],
        },
    )

    scratchpad = db.get_session_scratchpad("sess-1")
    assert scratchpad["session_id"] == "sess-1"
    assert scratchpad["current_goal"] == "Verify incremental notes"
    assert scratchpad["decisions"] == ["Keep scratchpad after finalization for debugging"]
    assert scratchpad["artifacts"] == []
    assert scratchpad["open_items"] == []
    assert scratchpad["candidates"] == []
    assert scratchpad["updated_at"] > 0


def test_session_notes_are_deleted_with_session(db):
    db.create_session("sess-1", source="cli")
    db.add_session_event("sess-1", "decision", {"summary": "temporary"})
    db.set_session_scratchpad("sess-1", {"current_goal": "temporary"})

    db.delete_session("sess-1")

    assert db.get_session_events("sess-1") == []
    assert db.get_session_scratchpad("sess-1") is None
