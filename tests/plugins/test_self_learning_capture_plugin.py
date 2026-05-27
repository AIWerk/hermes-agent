from __future__ import annotations

from pathlib import Path

from plugins.self_learning_capture import (
    post_tool_call,
    pre_llm_call,
    register,
)


def _setup_paths(tmp_path: Path, monkeypatch):
    hermes_home = tmp_path / "hermes-home"
    wiki = tmp_path / "wiki"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("WIKI_PATH", str(wiki))
    return wiki / "feedback" / "_inbox.md"


def test_pre_llm_call_captures_correction_candidate(tmp_path, monkeypatch):
    inbox = _setup_paths(tmp_path, monkeypatch)

    pre_llm_call(user_message="Ezt rosszul csináltad, legközelebb ne így.", session_id="s1")

    text = inbox.read_text(encoding="utf-8")
    assert "correction-detector" in text
    assert "status: candidate" in text
    assert "daily-memory-curator" in text
    assert "rosszul" in text


def test_pre_llm_call_ignores_and_deduplicates_non_corrections(tmp_path, monkeypatch):
    inbox = _setup_paths(tmp_path, monkeypatch)

    pre_llm_call(user_message="Kérlek nézd meg a logot.", session_id="s1")
    assert not inbox.exists()

    msg = "Nem ezt kértem, jegyezd meg így."
    pre_llm_call(user_message=msg, session_id="s1")
    pre_llm_call(user_message=msg, session_id="s1")

    text = inbox.read_text(encoding="utf-8")
    assert text.count("correction-detector") == 1


def test_post_tool_call_captures_failed_tool_and_sanitizes_secrets(tmp_path, monkeypatch):
    inbox = _setup_paths(tmp_path, monkeypatch)

    post_tool_call(
        tool_name="terminal",
        args={"command": "curl -H 'Authorization: Bearer sk-testsecret1234567890' example.invalid"},
        result={"success": False, "error": "token=abcd1234 failed", "exit_code": 1},
        session_id="s2",
        duration_ms=12,
    )

    text = inbox.read_text(encoding="utf-8")
    assert "failure-capture" in text
    assert "terminal" in text
    assert "[REDACTED]" in text
    assert "sk-testsecret" not in text
    assert "abcd1234" not in text


def test_post_tool_call_ignores_successful_result(tmp_path, monkeypatch):
    inbox = _setup_paths(tmp_path, monkeypatch)

    post_tool_call(tool_name="terminal", result={"success": True, "exit_code": 0}, session_id="s3")

    assert not inbox.exists()


def test_register_hooks():
    calls = []

    class Ctx:
        def register_hook(self, name, fn):
            calls.append((name, fn.__name__))

    register(Ctx())

    assert calls == [("pre_llm_call", "pre_llm_call"), ("post_tool_call", "post_tool_call")]
