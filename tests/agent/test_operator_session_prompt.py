from agent.system_prompt import build_system_prompt_parts


class DummyAgent:
    load_soul_identity = False
    skip_context_files = True
    valid_tool_names = set()
    _task_completion_guidance = False
    _tool_use_enforcement = False
    model = "test-model"
    provider = ""
    platform = "cli"
    _memory_store = None
    _memory_enabled = False
    _user_profile_enabled = False
    _memory_manager = None
    pass_session_id = False
    session_id = "sid"
    operator_session_context = {
        "mode": "operator_session",
        "actor_id": "unknown_cli",
        "role": "unknown",
        "acting_for": "aiwerk",
        "memory_scope": "none",
        "authorizing": False,
        "interface": "cli",
        "session_id": "sid",
        "provenance": "trusted_cli_bootstrap",
        "issued_at": 1,
        "expires_at": 9999999999,
    }


def test_operator_session_context_is_in_stable_prompt():
    parts = build_system_prompt_parts(DummyAgent())

    stable = parts["stable"]
    assert "Operator session context" in stable
    assert "unknown_cli" in stable
    assert "does not authenticate or authorize" in stable
    assert "must not be persisted" in stable
