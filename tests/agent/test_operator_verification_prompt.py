from types import SimpleNamespace
from unittest.mock import patch

from agent.system_prompt import build_system_prompt_parts


def _make_agent(**overrides):
    base = dict(
        load_soul_identity=False,
        skip_context_files=False,
        valid_tool_names=[],
        _task_completion_guidance=False,
        _tool_use_enforcement=False,
        _environment_probe=False,
        _kanban_worker_guidance="",
        _memory_store=None,
        _memory_manager=None,
        model="",
        provider="",
        platform="cli",
        pass_session_id=False,
        session_id="",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _stable_prompt(agent):
    with (
        patch("run_agent.load_soul_md", return_value=""),
        patch("run_agent.build_nous_subscription_prompt", return_value=""),
        patch("run_agent.build_environment_hints", return_value=""),
        patch("run_agent.build_context_files_prompt", return_value=""),
    ):
        return build_system_prompt_parts(agent)["stable"]


def test_operator_verification_guidance_injected_only_when_tool_available():
    without_tool = _stable_prompt(_make_agent(valid_tool_names=["terminal"]))
    with_tool = _stable_prompt(_make_agent(valid_tool_names=["terminal", "verify_operator_identity"]))

    assert "Operator verification" not in without_tool
    assert "Operator verification" in with_tool
    assert "verify_operator_identity" in with_tool
    assert "Never ask the user to paste the operator secret into chat" in with_tool
