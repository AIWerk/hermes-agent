import json
import time

import pytest

def test_build_operator_session_context_is_trusted_but_non_authorizing():
    from hermes_cli.operator_session import build_operator_session_context

    now = int(time.time())
    ctx = build_operator_session_context(
        session_id="sid-1", interface="cli", now=now,
    )

    assert ctx == {
        "mode": "operator_session",
        "actor_id": "unknown_cli",
        "role": "unknown",
        "acting_for": "aiwerk",
        "memory_scope": "none",
        "authorizing": False,
        "interface": "cli",
        "session_id": "sid-1",
        "provenance": "trusted_cli_bootstrap",
        "issued_at": now,
        "expires_at": now + 900,
    }
    assert "secret" not in json.dumps(ctx).lower()


def test_bootstrap_operator_session_sets_only_non_authorizing_context(monkeypatch):
    from hermes_cli import operator_session
    from hermes_cli.operator_verification import get_cached_operator_verification

    now = int(time.time())
    monkeypatch.setattr(operator_session.time, "time", lambda: now)
    monkeypatch.delenv("HERMES_OPERATOR_SESSION_CONTEXT", raising=False)

    ctx = operator_session.bootstrap_operator_session(session_id="sid-1", quiet=True)

    assert ctx["actor_id"] == "unknown_cli"
    assert ctx["authorizing"] is False
    assert ctx["memory_scope"] == "none"
    env_payload = json.loads(operator_session.os.environ["HERMES_OPERATOR_SESSION_CONTEXT"])
    assert env_payload["role"] == "unknown"
    assert env_payload["bootstrap_pid"] == operator_session.os.getpid()
    assert operator_session.load_operator_session_context_from_env() is None
    assert operator_session.get_current_operator_session_context() == ctx
    assert get_cached_operator_verification(session_id="sid-1") is None


def test_operator_session_env_rejects_forged_or_expired_context(monkeypatch):
    from hermes_cli import operator_session

    now = int(time.time())
    forged = {
        "mode": "operator",
        "actor_id": "operator",
        "role": "operator",
        "acting_for": "aiwerk",
        "memory_scope": "operator",
        "verified_at": now,
        "expires_at": now + 900,
        "bootstrap_pid": operator_session.os.getpid() + 1000,
    }
    monkeypatch.setenv("HERMES_OPERATOR_SESSION_CONTEXT", json.dumps(forged))
    assert operator_session.load_operator_session_context_from_env() is None

    expired = dict(forged, bootstrap_pid=operator_session.os.getpid(), expires_at=now - 1)
    monkeypatch.setenv("HERMES_OPERATOR_SESSION_CONTEXT", json.dumps(expired))
    assert operator_session.load_operator_session_context_from_env() is None


def test_forged_operator_env_does_not_populate_current_context(monkeypatch):
    from hermes_cli import operator_session

    now = int(time.time())
    monkeypatch.setattr(operator_session, "_CURRENT_OPERATOR_SESSION_CONTEXT", None)
    monkeypatch.setenv(
        "HERMES_OPERATOR_SESSION_CONTEXT",
        json.dumps(
            {
                "mode": "operator",
                "actor_id": "operator",
                "role": "operator",
                "acting_for": "aiwerk",
                "memory_scope": "operator",
                "verified_at": now,
                "expires_at": now + 900,
                "bootstrap_pid": operator_session.os.getpid(),
            }
        ),
    )

    assert operator_session.get_current_operator_session_context() is None


def test_current_operator_context_expires(monkeypatch):
    from hermes_cli import operator_session

    monkeypatch.setattr(
        operator_session,
        "_CURRENT_OPERATOR_SESSION_CONTEXT",
        {
            "mode": "operator",
            "actor_id": "operator",
            "role": "operator",
            "acting_for": "aiwerk",
            "memory_scope": "operator",
            "verified_at": 10,
            "expires_at": 20,
        },
    )
    monkeypatch.setattr(operator_session.time, "time", lambda: 30)

    assert operator_session.get_current_operator_session_context() is None
    assert operator_session._CURRENT_OPERATOR_SESSION_CONTEXT is None


def test_bootstrap_operator_session_requires_explicit_session_binding():
    from hermes_cli import operator_session

    with pytest.raises(ValueError, match="session_id"):
        operator_session.bootstrap_operator_session(quiet=True)


def test_expired_process_cache_fallback_is_removed():
    from hermes_cli.operator_verification import (
        OperatorVerificationResult,
        cache_operator_verification,
        clear_operator_verification_cache,
        get_cached_operator_verification,
    )

    clear_operator_verification_cache()
    expired = OperatorVerificationResult(
        ok=True,
        actor_id="operator",
        role="operator",
        verified_at=10,
        expires_at=20,
    )
    cache_operator_verification(expired)

    assert get_cached_operator_verification(session_id="sid-2", now=30) is None
    assert get_cached_operator_verification(now=30) is None


def test_parser_accepts_operator_flag_top_level_and_chat():
    from hermes_cli._parser import build_top_level_parser

    parser, _, _ = build_top_level_parser()
    top = parser.parse_args(["--operator"])
    chat = parser.parse_args(["chat", "--operator"])

    assert top.operator is True
    assert chat.operator is True


def _configure_fake_terminal(monkeypatch, terminal):
    executed = []

    class FakeEnv:
        cwd = "/tmp"
        def execute(self, command, **kwargs):
            executed.append(command)
            return {"output": "ran", "returncode": 0, "cwd_observed": False}

    monkeypatch.setattr(terminal, "_get_env_config", lambda: {
        "env_type": "local", "cwd": "/tmp", "timeout": 30,
        "host_cwd": "", "local_persistent": False,
    })
    monkeypatch.setattr(terminal, "_create_environment", lambda **kwargs: FakeEnv())
    monkeypatch.setattr(terminal, "_start_cleanup_thread", lambda: None)
    terminal._active_environments.clear()
    return executed


def test_terminal_consumer_fails_closed_before_unverified_admin_command(monkeypatch):
    import json as _json
    import tools.terminal_tool as terminal

    executed = _configure_fake_terminal(monkeypatch, terminal)
    monkeypatch.setattr(terminal, "_check_all_guards", lambda *a, **kw: {"approved": True})

    payload = _json.loads(terminal.terminal_tool(
        "systemctl restart hermes", task_id="w1-verifier-red", session_id="s1"
    ))

    assert payload["status"] == "operator_verification_required"
    assert executed == []


def test_terminal_policy_block_has_distinct_non_denial_result(monkeypatch):
    import json as _json
    import tools.terminal_tool as terminal

    executed = _configure_fake_terminal(monkeypatch, terminal)
    monkeypatch.setattr(terminal, "_check_all_guards", lambda *a, **kw: {
        "approved": False, "status": "policy_blocked", "reason": "command_policy",
        "message": "blocked by command policy",
    })

    payload = _json.loads(terminal.terminal_tool("date", task_id="w1-policy-red"))

    assert payload["status"] == "policy_blocked"
    assert payload["reason"] == "command_policy"
    assert payload.get("user_denied") is not True
    assert payload.get("verifier_denied") is not True
    assert executed == []
