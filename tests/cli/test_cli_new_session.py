"""Regression tests for CLI fresh-session commands."""

from __future__ import annotations

import importlib
import os
import sys
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from hermes_state import SessionDB
from tools.todo_tool import TodoStore


class _FakeCompressor:
    """Minimal stand-in for ContextCompressor."""

    def __init__(self):
        self.last_prompt_tokens = 500
        self.last_completion_tokens = 200
        self.last_total_tokens = 700
        self.compression_count = 3
        self._context_probed = True


class _FakeAgent:
    def __init__(self, session_id: str, session_start):
        self.session_id = session_id
        self.session_start = session_start
        self.model = "anthropic/claude-opus-4.6"
        self._last_flushed_db_idx = 7
        self._todo_store = TodoStore()
        self._todo_store.write(
            [{"id": "t1", "content": "unfinished task", "status": "in_progress"}]
        )
        self.commit_memory_session = MagicMock()
        self._invalidate_system_prompt = MagicMock()

        # Token counters (non-zero to verify reset)
        self.session_total_tokens = 1000
        self.session_input_tokens = 600
        self.session_output_tokens = 400
        self.session_prompt_tokens = 550
        self.session_completion_tokens = 350
        self.session_cache_read_tokens = 100
        self.session_cache_write_tokens = 50
        self.session_reasoning_tokens = 80
        self.session_api_calls = 5
        self.session_estimated_cost_usd = 0.42
        self.session_cost_status = "estimated"
        self.session_cost_source = "openrouter"
        self.context_compressor = _FakeCompressor()
        self.ephemeral_system_prompt = None

    def reset_session_state(self):
        """Mirror the real AIAgent.reset_session_state()."""
        self.session_total_tokens = 0
        self.session_input_tokens = 0
        self.session_output_tokens = 0
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0
        self.session_cache_read_tokens = 0
        self.session_cache_write_tokens = 0
        self.session_reasoning_tokens = 0
        self.session_api_calls = 0
        self.session_estimated_cost_usd = 0.0
        self.session_cost_status = "unknown"
        self.session_cost_source = "none"
        if hasattr(self, "context_compressor") and self.context_compressor:
            self.context_compressor.last_prompt_tokens = 0
            self.context_compressor.last_completion_tokens = 0
            self.context_compressor.last_total_tokens = 0
            self.context_compressor.compression_count = 0
            self.context_compressor._context_probed = False


def _make_cli(env_overrides=None, config_overrides=None, **kwargs):
    """Create a HermesCLI instance with minimal mocking."""
    _clean_config = {
        "model": {
            "default": "anthropic/claude-opus-4.6",
            "base_url": "https://openrouter.ai/api/v1",
            "provider": "auto",
        },
        "display": {"compact": False, "tool_progress": "all"},
        "agent": {},
        "terminal": {"env_type": "local"},
    }
    if config_overrides:
        _clean_config.update(config_overrides)
    clean_env = {"LLM_MODEL": "", "HERMES_MAX_ITERATIONS": ""}
    if env_overrides:
        clean_env.update(env_overrides)
    prompt_toolkit_stubs = {
        "prompt_toolkit": MagicMock(),
        "prompt_toolkit.history": MagicMock(),
        "prompt_toolkit.styles": MagicMock(),
        "prompt_toolkit.patch_stdout": MagicMock(),
        "prompt_toolkit.application": MagicMock(),
        "prompt_toolkit.layout": MagicMock(),
        "prompt_toolkit.layout.processors": MagicMock(),
        "prompt_toolkit.filters": MagicMock(),
        "prompt_toolkit.layout.dimension": MagicMock(),
        "prompt_toolkit.layout.menus": MagicMock(),
        "prompt_toolkit.widgets": MagicMock(),
        "prompt_toolkit.key_binding": MagicMock(),
        "prompt_toolkit.completion": MagicMock(),
        "prompt_toolkit.formatted_text": MagicMock(),
        "prompt_toolkit.auto_suggest": MagicMock(),
    }
    with patch.dict(sys.modules, prompt_toolkit_stubs), patch.dict(
        "os.environ", clean_env, clear=False
    ):
        import cli as _cli_mod

        _cli_mod = importlib.reload(_cli_mod)
        with patch.object(_cli_mod, "get_tool_definitions", return_value=[]), patch.dict(
            _cli_mod.__dict__, {"CLI_CONFIG": _clean_config}
        ):
            return _cli_mod.HermesCLI(**kwargs)


def _prepare_cli_with_active_session(tmp_path, config_overrides=None):
    cli = _make_cli(config_overrides=config_overrides)
    cli._session_db = SessionDB(db_path=tmp_path / "state.db")
    cli._session_db.create_session(session_id=cli.session_id, source="cli", model=cli.model)

    cli.agent = _FakeAgent(cli.session_id, cli.session_start)
    cli.conversation_history = [{"role": "user", "content": "hello"}]

    old_session_start = cli.session_start - timedelta(seconds=1)
    cli.session_start = old_session_start
    cli.agent.session_start = old_session_start

    # Bypass the destructive-slash confirmation gate — these tests focus on
    # the new-session mechanics, not the confirm prompt itself (covered in
    # tests/cli/test_destructive_slash_confirm.py).
    cli._confirm_destructive_slash = lambda *_a, **_kw: "once"
    return cli


def test_new_command_creates_real_fresh_session_and_resets_agent_state(tmp_path):
    cli = _prepare_cli_with_active_session(tmp_path)
    old_session_id = cli.session_id
    old_session_start = cli.session_start

    cli.process_command("/new")

    assert cli.session_id != old_session_id

    old_session = cli._session_db.get_session(old_session_id)
    assert old_session is not None
    assert old_session["end_reason"] == "new_session"

    new_session = cli._session_db.get_session(cli.session_id)
    assert new_session is not None

    cli._session_db.append_message(cli.session_id, role="user", content="next turn")

    assert cli.agent.session_id == cli.session_id
    assert cli.agent._last_flushed_db_idx == 0
    assert cli.agent._todo_store.read() == []
    assert cli.session_start > old_session_start
    assert cli.agent.session_start == cli.session_start
    cli.agent._invalidate_system_prompt.assert_called_once()


def test_reset_command_is_alias_for_new_session(tmp_path):
    cli = _prepare_cli_with_active_session(tmp_path)
    old_session_id = cli.session_id

    cli.process_command("/reset")

    assert cli.session_id != old_session_id
    assert cli._session_db.get_session(old_session_id)["end_reason"] == "new_session"
    assert cli._session_db.get_session(cli.session_id) is not None


def test_clear_command_starts_new_session_before_redrawing(tmp_path):
    cli = _prepare_cli_with_active_session(tmp_path)
    cli.console = MagicMock()
    cli.show_banner = MagicMock()

    old_session_id = cli.session_id
    cli.process_command("/clear")

    assert cli.session_id != old_session_id
    assert cli._session_db.get_session(old_session_id)["end_reason"] == "new_session"
    assert cli._session_db.get_session(cli.session_id) is not None
    cli.console.clear.assert_called_once()
    cli.show_banner.assert_called_once()
    assert cli.conversation_history == []


def test_new_session_resets_token_counters(tmp_path):
    """Regression test for #2099: /new must zero all token counters."""
    cli = _prepare_cli_with_active_session(tmp_path)

    # Verify counters are non-zero before reset
    agent = cli.agent
    assert agent.session_total_tokens > 0
    assert agent.session_api_calls > 0
    assert agent.context_compressor.compression_count > 0

    cli.process_command("/new")

    # All agent token counters must be zero
    assert agent.session_total_tokens == 0
    assert agent.session_input_tokens == 0
    assert agent.session_output_tokens == 0
    assert agent.session_prompt_tokens == 0
    assert agent.session_completion_tokens == 0
    assert agent.session_cache_read_tokens == 0
    assert agent.session_cache_write_tokens == 0
    assert agent.session_reasoning_tokens == 0
    assert agent.session_api_calls == 0
    assert agent.session_estimated_cost_usd == 0.0
    assert agent.session_cost_status == "unknown"
    assert agent.session_cost_source == "none"

    # Context compressor counters must also be zero
    comp = agent.context_compressor
    assert comp.last_prompt_tokens == 0
    assert comp.last_completion_tokens == 0
    assert comp.last_total_tokens == 0
    assert comp.compression_count == 0
    assert comp._context_probed is False


def test_new_session_with_title(capsys):
    """new_session(title=...) creates a session and sets the title."""
    cli = _make_cli()
    cli._session_db = MagicMock()
    cli.agent = _FakeAgent("old_session_id", datetime.now())
    cli.conversation_history = []

    cli.new_session(title="My Test Session")

    # Assert set_session_title was called with the new session ID and sanitized title
    cli._session_db.set_session_title.assert_called_once()
    call_args = cli._session_db.set_session_title.call_args
    assert call_args[0][0] == cli.session_id
    assert call_args[0][1] == "My Test Session"

    captured = capsys.readouterr()
    assert "My Test Session" in captured.out


def test_new_session_with_duplicate_title_surfaces_error(capsys):
    """new_session(title=...) handles ValueError from a duplicate-title conflict.

    The session is still created; the title assignment fails; the success banner
    must not claim the rejected title as the session name.
    """
    cli = _make_cli()
    cli._session_db = MagicMock()
    cli._session_db.set_session_title.side_effect = ValueError(
        "Title 'Dup' is already in use by session abc-123"
    )
    cli.agent = _FakeAgent("old_session_id", datetime.now())
    cli.conversation_history = []

    # Capture warnings printed via cli._cprint. After importlib.reload(),
    # the method's __globals__ dict is the one from the live module — patch
    # the exact dict the method will read.
    warnings: list[str] = []
    method_globals = cli.new_session.__globals__
    original = method_globals["_cprint"]
    method_globals["_cprint"] = lambda msg: warnings.append(msg)
    try:
        cli.new_session(title="Dup")
    finally:
        method_globals["_cprint"] = original

    cli._session_db.set_session_title.assert_called_once()
    joined = "\n".join(warnings)
    assert "already in use" in joined
    assert "session started untitled" in joined

    # The success banner must NOT claim the rejected title as the session name.
    captured = capsys.readouterr()
    assert "New session started: Dup" not in captured.out
    assert "New session started!" in captured.out


def test_new_command_prints_honcho_preview_after_session_switch(tmp_path):
    cli = _prepare_cli_with_active_session(tmp_path)
    old_session_id = cli.session_id
    calls = []
    cli._print_honcho_reset_injection_preview = lambda event="new_session": calls.append((event, cli.session_id))

    cli.process_command("/new")

    assert calls == [("new_session", cli.session_id)]
    assert calls[0][1] != old_session_id


def test_new_command_skips_honcho_preview_when_honcho_disabled(tmp_path):
    cli = _prepare_cli_with_active_session(
        tmp_path,
        config_overrides={"honcho": {"enabled": False}},
    )

    enabled, fail_quietly = cli._honcho_injection_preview_config("new_session")
    assert enabled is False
    assert fail_quietly is True

    cli.process_command("/new")


def test_reset_command_prints_honcho_preview_after_session_switch(tmp_path):
    cli = _prepare_cli_with_active_session(tmp_path)
    old_session_id = cli.session_id
    calls = []
    cli._print_honcho_reset_injection_preview = lambda event="new_session": calls.append((event, cli.session_id))

    cli.process_command("/reset")

    assert calls == [("new_session", cli.session_id)]
    assert calls[0][1] != old_session_id


def test_clear_command_prints_honcho_preview_after_redraw(tmp_path):
    cli = _prepare_cli_with_active_session(tmp_path)
    calls = []
    cli.console = MagicMock()
    cli.console.clear.side_effect = lambda: calls.append("clear")
    cli.show_banner = MagicMock(side_effect=lambda: calls.append("banner"))
    cli._print_honcho_reset_injection_preview = lambda event="new_session": calls.append(f"preview:{event}")

    cli.process_command("/clear")

    assert calls == ["clear", "banner", "preview:clear"]


def test_fresh_command_starts_new_session_with_read_only_carryover_context(tmp_path):
    cli = _prepare_cli_with_active_session(tmp_path)
    old_session_id = cli.session_id
    cli.conversation_history = [
        {"role": "user", "content": "old one"},
        {"role": "assistant", "content": "old answer"},
        {"role": "tool", "content": "hidden tool output"},
        {"role": "user", "content": "keep this"},
        {"role": "assistant", "content": "and this"},
    ]
    cli.agent.ephemeral_system_prompt = "Global prompt"

    cli.process_command("/fresh 2")

    assert cli.session_id != old_session_id
    assert cli.conversation_history == []
    assert cli._session_db.get_session(old_session_id)["end_reason"] == "new_session"
    new_session = cli._session_db.get_session(cli.session_id)
    assert new_session is not None
    assert new_session["system_prompt"] is None

    prompt = cli.agent.ephemeral_system_prompt
    assert prompt.startswith("Global prompt")
    assert "[Hermes /fresh carryover context]" in prompt
    assert f"Source session: {old_session_id}" in prompt
    assert "Carried messages: 2" in prompt
    assert "keep this" in prompt
    assert "and this" in prompt
    assert "old one" not in prompt
    assert "hidden tool output" not in prompt


def test_fresh_command_redacts_secrets_in_carryover_context(tmp_path):
    cli = _prepare_cli_with_active_session(tmp_path)
    cli.conversation_history = [
        {"role": "user", "content": "token sk-abcdefghijklmnopqrstuvwxyz123456"},
    ]

    cli.process_command("/fresh 1")

    prompt = cli.agent.ephemeral_system_prompt
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in prompt
    assert "sk-" in prompt


def test_new_command_clears_prior_fresh_carryover_but_keeps_global_ephemeral_prompt(tmp_path):
    cli = _prepare_cli_with_active_session(tmp_path)
    cli.agent.ephemeral_system_prompt = "Global prompt"
    cli.conversation_history = [{"role": "user", "content": "carried"}]
    cli.process_command("/fresh 1")
    assert "carried" in cli.agent.ephemeral_system_prompt

    cli.conversation_history = [{"role": "user", "content": "next"}]
    cli.process_command("/new")

    assert cli.agent.ephemeral_system_prompt == "Global prompt"
