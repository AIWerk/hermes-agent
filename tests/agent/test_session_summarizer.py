"""Tests for compact per-session summary generation."""

from unittest.mock import MagicMock, patch

from agent.session_summarizer import generate_session_summary, maybe_update_session_summary, update_session_summary


def _response(text: str):
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = text
    resp.model = None
    return resp


def test_generate_session_summary_parses_json_response():
    messages = [
        {"role": "user", "content": "Use hooks to save session notes."},
        {"role": "assistant", "content": "We can use on_session_end."},
    ]
    raw = """
    {
      "short_summary": "Discussed hook-based session notes for later search.",
      "outline": ["Reuse auto-title routing", "Save a compact session outline"],
      "topics": ["hermes", "hooks", "session-search"]
    }
    """

    with patch("agent.session_summarizer.call_llm", return_value=_response(raw)) as call:
        summary = generate_session_summary(messages, title="Session note hooks")

    assert summary == {
        "short_summary": "Discussed hook-based session notes for later search.",
        "outline": ["Reuse auto-title routing", "Save a compact session outline"],
        "topics": ["hermes", "hooks", "session-search"],
        "model": None,
    }
    assert call.call_args.kwargs["task"] == "title_generation"


def test_generate_session_summary_accepts_fenced_json():
    raw = """```json
    {"short_summary":"A short note.","outline":["One"],"topics":["notes"]}
    ```"""

    with patch("agent.session_summarizer.call_llm", return_value=_response(raw)):
        summary = generate_session_summary([{"role": "user", "content": "hello"}])

    assert summary["short_summary"] == "A short note."
    assert summary["outline"] == ["One"]
    assert summary["topics"] == ["notes"]


def test_generate_session_summary_returns_none_on_empty_or_bad_input():
    assert generate_session_summary([]) is None
    assert generate_session_summary([{"role": "system", "content": "hidden"}]) is None

    with patch("agent.session_summarizer.call_llm", return_value=_response("not json")):
        assert generate_session_summary([{"role": "user", "content": "hello"}]) is None


def test_generate_session_summary_limits_outline_and_topics():
    raw = {
        "short_summary": "Summary",
        "outline": [f"step {i}" for i in range(20)],
        "topics": [f"topic-{i}" for i in range(20)],
    }

    with patch("agent.session_summarizer.call_llm", return_value=_response(str(raw).replace("'", '"'))):
        summary = generate_session_summary([{"role": "user", "content": "hello"}])

    assert len(summary["outline"]) == 8
    assert len(summary["topics"]) == 8


def test_update_session_summary_reads_db_and_persists():
    db = MagicMock()
    db.get_messages.return_value = [
        {"role": "user", "content": "Need a searchable session note."},
        {"role": "assistant", "content": "I will save a compact index."},
    ]
    db.get_session_title.return_value = "Searchable notes"
    db.set_session_summary.return_value = True

    with patch(
        "agent.session_summarizer.generate_session_summary",
        return_value={
            "short_summary": "Built a searchable session note.",
            "outline": ["Generate", "Persist"],
            "topics": ["session-search"],
            "model": "m",
        },
    ):
        assert update_session_summary(db, "sess-1") is True

    db.set_session_summary.assert_called_once_with(
        "sess-1",
        short_summary="Built a searchable session note.",
        outline=["Generate", "Persist"],
        topics=["session-search"],
        model="m",
    )


def test_maybe_update_session_summary_synchronous_waits():
    db = MagicMock()
    with patch("agent.session_summarizer.update_session_summary", return_value=True) as update:
        assert maybe_update_session_summary(db, "sess-1", synchronous=True) is True
    update.assert_called_once()
