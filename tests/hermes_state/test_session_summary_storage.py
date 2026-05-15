"""Tests for persistent per-session summary/index storage."""

import pytest

from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    return SessionDB(tmp_path / "state.db")


def test_set_and_get_session_summary(db):
    db.create_session("sess-1", source="cli")

    assert db.set_session_summary(
        "sess-1",
        short_summary="Discussed hook-based session notes.",
        outline=["Inspect title generation", "Store compact summary"],
        topics=["hermes", "session-index"],
        model="openai-codex/gpt-5.3-codex-spark",
    ) is True

    summary = db.get_session_summary("sess-1")

    assert summary == {
        "session_id": "sess-1",
        "short_summary": "Discussed hook-based session notes.",
        "outline": ["Inspect title generation", "Store compact summary"],
        "topics": ["hermes", "session-index"],
        "model": "openai-codex/gpt-5.3-codex-spark",
        "created_at": pytest.approx(summary["created_at"]),
        "updated_at": pytest.approx(summary["updated_at"]),
    }


def test_set_session_summary_updates_existing_row(db):
    db.create_session("sess-1", source="cli")
    db.set_session_summary(
        "sess-1",
        short_summary="Old summary",
        outline=["old"],
        topics=["old"],
        model="model-a",
    )

    assert db.set_session_summary(
        "sess-1",
        short_summary="New summary",
        outline=["new step"],
        topics=["new"],
        model="model-b",
    ) is True

    summary = db.get_session_summary("sess-1")
    assert summary["short_summary"] == "New summary"
    assert summary["outline"] == ["new step"]
    assert summary["topics"] == ["new"]
    assert summary["model"] == "model-b"
    assert summary["updated_at"] >= summary["created_at"]


def test_set_session_summary_returns_false_for_missing_session(db):
    assert db.set_session_summary(
        "missing",
        short_summary="No row should be created",
        outline=["nothing"],
        topics=["missing"],
        model="model",
    ) is False
    assert db.get_session_summary("missing") is None


def test_list_sessions_rich_includes_summary_and_topics(db):
    session_id = db.create_session(session_id="sess-1", source="cli")
    db.append_message(session_id, "user", "First real message")
    assert db.set_session_summary(
        session_id,
        short_summary="Implemented hook-based session notes.",
        outline=["Add schema", "Expose list output"],
        topics=["hermes", "session-index"],
        model="summary-model",
    )

    sessions = db.list_sessions_rich(limit=5)

    assert sessions[0]["summary"] == "Implemented hook-based session notes."
    assert sessions[0]["topics"] == ["hermes", "session-index"]


def test_search_session_summaries_finds_topics_without_message_hit(db):
    session_id = db.create_session(session_id="sess-1", source="cli")
    db.append_message(session_id, "user", "Plain conversation without the tag")
    db.set_session_summary(
        session_id,
        short_summary="Discussed durable outline notes.",
        outline=["Persist compact summaries"],
        topics=["session-index"],
    )

    results = db.search_session_summaries("session-index")

    assert len(results) == 1
    assert results[0]["session_id"] == session_id
    assert results[0]["role"] == "summary"
    assert results[0]["topics"] == ["session-index"]


def test_search_session_summaries_clamps_limit(db):
    for index in range(3):
        session_id = db.create_session(session_id=f"sess-{index}", source="cli")
        db.set_session_summary(
            session_id,
            short_summary="Discussed durable outline notes.",
            outline=["Persist compact summaries"],
            topics=["session-index"],
        )

    results = db.search_session_summaries("session-index", limit=-1)

    assert len(results) == 1
