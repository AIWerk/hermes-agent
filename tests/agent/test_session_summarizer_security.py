"""Security-focused tests for session summary input preparation."""

from unittest.mock import patch

from agent.session_summarizer import generate_session_summary


class _Message:
    content = '{"short_summary":"Safe","outline":["redacted"],"topics":["security"]}'


class _Choice:
    message = _Message()


class _Response:
    model = "summary-model"
    choices = [_Choice()]


def test_generate_session_summary_redacts_transcript_before_auxiliary_llm():
    messages = [
        {"role": "user", "content": "Use password=hunter2 and Authorization: Bearer ***"},
        {"role": "assistant", "content": "Stored token sk-live-abc123"},
    ]

    with patch("agent.session_summarizer.call_llm", return_value=_Response()) as call:
        generate_session_summary(
            messages,
            title="Authorization: Bearer ***",
            events=[{"event_type": "decision", "content": {"summary": "token ***"}}],
            scratchpad={"current_goal": "password=hunter2"},
        )

    prompt = call.call_args.kwargs["messages"][1]["content"]
    assert "hunter2" not in prompt
    assert "Bearer ***" not in prompt
    assert "token ***" not in prompt
    assert "sk-live-abc123" not in prompt
    assert "[REDACTED]" in prompt
