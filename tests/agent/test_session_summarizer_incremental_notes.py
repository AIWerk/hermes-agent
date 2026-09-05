"""Tests that final session summaries include incremental notes when present."""

from unittest.mock import patch

from agent.session_summarizer import generate_session_summary, update_session_summary
from hermes_state import SessionDB


class _Message:
    content = '{"short_summary":"Used notes","outline":["Reviewed scratchpad"],"topics":["session-index"]}'


class _Choice:
    message = _Message()


class _Response:
    model = "summary-model"
    choices = [_Choice()]


def test_generate_session_summary_includes_events_and_scratchpad_in_prompt():
    messages = [{"role": "user", "content": "Build the feature"}]
    events = [{"event_type": "decision", "content": {"summary": "Use SQLite events"}}]
    scratchpad = {"current_goal": "Build incremental notes", "decisions": ["Use SQLite events"]}

    with patch("agent.session_summarizer.call_llm", return_value=_Response()) as call:
        summary = generate_session_summary(messages, events=events, scratchpad=scratchpad)

    prompt = call.call_args.kwargs["messages"][1]["content"]
    assert "Incremental notes:" in prompt
    assert "Use SQLite events" in prompt
    assert "Build incremental notes" in prompt
    assert summary["short_summary"] == "Used notes"


def test_update_session_summary_reads_events_and_scratchpad(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("sess-1", source="cli")
    db.append_message("sess-1", "user", "Build the feature")
    db.add_session_event("sess-1", "decision", {"summary": "Use SQLite events"})
    db.set_session_scratchpad("sess-1", {"current_goal": "Build incremental notes"})

    with patch("agent.session_summarizer.generate_session_summary") as generate:
        generate.return_value = {
            "short_summary": "Implemented incremental notes",
            "outline": ["Add storage"],
            "topics": ["session-index"],
            "model": "summary-model",
        }
        assert update_session_summary(db, "sess-1") is True

    assert generate.call_args.kwargs["events"][0]["content"] == {"summary": "Use SQLite events"}
    assert generate.call_args.kwargs["scratchpad"]["current_goal"] == "Build incremental notes"
