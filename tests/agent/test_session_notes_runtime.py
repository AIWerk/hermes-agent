"""Tests for runtime incremental session note capture."""

from unittest.mock import patch

from run_agent import AIAgent
from hermes_state import SessionDB


def test_record_incremental_session_notes_captures_tool_results(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("sess-1", source="cli")
    agent = AIAgent.__new__(AIAgent)
    agent._session_db = db
    agent.session_id = "sess-1"
    agent._user_turn_count = 3

    messages = [
        {"role": "user", "content": "Create a file"},
        {"role": "assistant", "content": None, "tool_calls": [{"function": {"name": "write_file"}}]},
        {"role": "tool", "tool_name": "write_file", "content": "Wrote /tmp/a.txt with token sk-live-abc123"},
        {"role": "assistant", "content": "Done"},
    ]

    agent._record_incremental_session_notes(messages, conversation_history=[], final_response="Done")

    events = db.get_session_events("sess-1")
    assert [event["event_type"] for event in events] == ["tool_result", "turn_note"]
    assert events[0]["content"]["summary"] == "Wrote /tmp/a.txt with token [REDACTED]"
    scratchpad = db.get_session_scratchpad("sess-1")
    assert scratchpad is not None
    assert scratchpad["current_goal"] == "Aktuelle Nutzeraufgabe fortsetzen"


def test_record_incremental_session_notes_schedules_mid_session_retitle(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("sess-1", source="cli")
    agent = AIAgent.__new__(AIAgent)
    agent._session_db = db
    agent.session_id = "sess-1"
    agent._user_turn_count = 6
    agent.provider = "openai"
    agent.model = "gpt-test"
    agent._emit_auxiliary_failure = None
    messages = [{"role": "user", "content": "Continue the recovery"}]
    history = [{"role": "user", "content": "Earlier work"}]

    with patch("agent.title_generator.maybe_retitle_session") as retitle:
        agent._record_incremental_session_notes(
            messages,
            conversation_history=history,
            final_response="Done",
        )

    retitle.assert_called_once_with(
        db,
        "sess-1",
        messages,
        history,
        turn_index=6,
        failure_callback=None,
        main_runtime={"provider": "openai", "model": "gpt-test"},
    )
    db.close()
