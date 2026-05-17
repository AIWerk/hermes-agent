from datetime import datetime
from zoneinfo import ZoneInfo

from plugins.temporal_context import (
    build_temporal_context,
    pre_llm_call,
    transform_llm_output,
)


def test_temporal_context_is_neutral_by_default():
    now = datetime(2026, 5, 17, 17, 35, tzinfo=ZoneInfo("UTC"))

    context = build_temporal_context(now=now, config={})

    assert context is not None
    assert "for Tenant Operator" not in context
    assert "America/New_York" not in context
    assert "2026-05-17 17:35 UTC (+0000)" in context
    assert "Relative time/daypart claims" in context


def test_temporal_context_uses_per_agent_timezone_and_label_from_config():
    now = datetime(2026, 5, 17, 17, 35, tzinfo=ZoneInfo("UTC"))
    config = {
        "enabled": True,
        "timezone": "America/New_York",
        "display_name": "Tenant Operator",
    }

    context = build_temporal_context(now=now, config=config)

    assert context is not None
    assert "for Tenant Operator" in context
    assert "America/New_York" in context
    assert "2026-05-17 13:35 EDT (-0400)" in context


def test_temporal_context_can_be_disabled():
    now = datetime(2026, 5, 17, 17, 35, tzinfo=ZoneInfo("UTC"))

    assert build_temporal_context(now=now, config={"enabled": False}) is None


def test_pre_llm_call_returns_ephemeral_context_dict():
    now = datetime(2026, 5, 17, 17, 35, tzinfo=ZoneInfo("UTC"))

    result = pre_llm_call(now=now, config={"timezone": "UTC"})

    assert isinstance(result, dict)
    assert set(result) == {"context"}
    assert "Temporal context" in result["context"]


def test_transform_llm_output_is_noop_unless_configured():
    text = "Earlier today I checked the logs."

    assert transform_llm_output(text, config={}) == text


def test_transform_llm_output_uses_configured_replacements_only():
    text = "I'll stop here."
    config = {
        "output_guard": {
            "enabled": True,
            "replacements": [
                {"pattern": "I'll stop here", "replacement": "I will pause here"}
            ],
        }
    }

    assert transform_llm_output(response_text=text, config=config) == "I will pause here."
