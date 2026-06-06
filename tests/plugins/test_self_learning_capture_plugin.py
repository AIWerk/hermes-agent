from __future__ import annotations

from pathlib import Path

import pytest

from plugins.self_learning_capture import (
    _sanitize,
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


def test_pre_llm_call_honors_disabled_config(tmp_path, monkeypatch):
    inbox = _setup_paths(tmp_path, monkeypatch)

    pre_llm_call(user_message="Nem ezt kértem, jegyezd meg így.", session_id="s1", config={"enabled": False})

    assert not inbox.exists()


def test_configured_feedback_inbox_path_overrides_wiki_path(tmp_path, monkeypatch):
    default_inbox = _setup_paths(tmp_path, monkeypatch)
    custom_inbox = tmp_path / "custom" / "feedback.md"

    pre_llm_call(
        user_message="Nem ezt kértem, jegyezd meg így.",
        session_id="s4",
        config={"feedback_inbox": str(custom_inbox)},
    )

    assert custom_inbox.exists()
    assert "correction-detector" in custom_inbox.read_text(encoding="utf-8")
    assert not default_inbox.exists()


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


# Synthetic secrets are assembled from fragments at runtime so the contiguous
# token literal never appears in this file — that avoids GitHub secret-scanning
# push-protection false positives on test fixtures while still exercising the
# regexes on the full assembled value.
@pytest.mark.parametrize(
    "prefix,body",
    [
        ("ghp_", "0123456789abcdefghij"),       # bare GitHub token (was kept verbatim)
        ("sk-", "anttest0123456789ABCDEF"),      # bare OpenAI/Anthropic-style token
        ("AKIA", "ABCDEFGHIJKLMNOP"),            # AWS access key id
        ("sk_live_", "0123456789abcdefABCD"),    # Stripe live key
        ("xoxb-", "123456789012-abcdefghijkl"),  # Slack token
    ],
)
def test_sanitize_redacts_bare_high_entropy_tokens(prefix, body):
    secret = prefix + body
    out = _sanitize(f"the value is {secret} ok")
    assert secret not in out
    assert "[REDACTED]" in out


def test_sanitize_redacts_quoted_json_values():
    # The exact shape _excerpt produces via json.dumps for dict args/results.
    api_key = "AKIA" + "_my_real_key_value"
    payload = '{"api_key": "' + api_key + '", "password": "hunter2"}'
    out = _sanitize(payload)
    assert api_key not in out
    assert "hunter2" not in out
    assert "[REDACTED]" in out


def test_sanitize_redacts_url_embedded_password():
    password = "p4ss" + "w0rd"
    out = _sanitize(f"connection string postgres://user:{password}@db.host:5432/app")
    assert password not in out
    assert "[REDACTED]" in out


def test_sanitize_redacts_jwt():
    jwt = ".".join(
        ["eyJ" + "hbGciOiJIUzI1NiJ9", "eyJ" + "zdWIiOiIxMjM0NTY3ODkwIn0", "SflKxwRJSMeKKF2QT4fwpMeJf36"]
    )
    out = _sanitize(f"token {jwt}")
    assert jwt not in out
    assert "[REDACTED]" in out


def test_register_hooks():
    calls = []

    class Ctx:
        def register_hook(self, name, fn):
            calls.append((name, fn.__name__))

    register(Ctx())

    assert calls == [("pre_llm_call", "pre_llm_call"), ("post_tool_call", "post_tool_call")]
