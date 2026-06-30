from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CONVERSATION_LOOP = ROOT / "agent" / "conversation_loop.py"


def test_session_api_calls_counted_independently_of_usage_metadata():
    source = CONVERSATION_LOOP.read_text(encoding="utf-8")
    attempt_increment = "api_call_count += 1"
    session_increment = "agent.session_api_calls += 1"
    usage_gate = "if hasattr(response, 'usage') and response.usage:"

    assert attempt_increment in source
    assert session_increment in source
    assert usage_gate in source
    assert source.index(attempt_increment) < source.index(session_increment) < source.index(usage_gate)

    usage_block_start = source.index(usage_gate)
    usage_block_end = source.index("# Log API call details for debugging/observability", usage_block_start)
    assert session_increment not in source[usage_block_start:usage_block_end]
