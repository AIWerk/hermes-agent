import json
import time

import pytest

from hermes_cli.operator_verification import OperatorVerificationResult


def test_build_operator_session_context_is_sanitized_and_memory_scoped():
    from hermes_cli.operator_session import build_operator_session_context

    now = int(time.time())
    ctx = build_operator_session_context(
        OperatorVerificationResult(
            ok=True,
            actor_id="attila",
            role="operator",
            verified_at=now,
            expires_at=now + 900,
        )
    )

    assert ctx == {
        "mode": "operator",
        "actor_id": "attila",
        "role": "operator",
        "acting_for": "aiwerk",
        "memory_scope": "operator",
        "verified_at": now,
        "expires_at": now + 900,
    }
    assert "secret" not in json.dumps(ctx).lower()


def test_bootstrap_operator_session_sets_env_and_cache(monkeypatch):
    from hermes_cli import operator_session
    from hermes_cli.operator_verification import get_cached_operator_verification

    now = int(time.time())
    result = OperatorVerificationResult(
        ok=True,
        actor_id="attila",
        role="operator",
        verified_at=now,
        expires_at=now + 900,
    )
    monkeypatch.setattr(operator_session, "run_operator_verifier", lambda: result)
    monkeypatch.delenv("HERMES_OPERATOR_SESSION_CONTEXT", raising=False)

    ctx = operator_session.bootstrap_operator_session(session_id="sid-1", quiet=True)

    assert ctx["actor_id"] == "attila"
    assert ctx["memory_scope"] == "operator"
    env_payload = json.loads(operator_session.os.environ["HERMES_OPERATOR_SESSION_CONTEXT"])
    assert env_payload["role"] == "operator"
    assert env_payload["bootstrap_pid"] == operator_session.os.getpid()
    assert operator_session.load_operator_session_context_from_env() == ctx
    assert operator_session.get_current_operator_session_context() == ctx
    assert get_cached_operator_verification(session_id="sid-1").actor_id == "attila"


def test_operator_session_env_rejects_forged_or_expired_context(monkeypatch):
    from hermes_cli import operator_session

    now = int(time.time())
    forged = {
        "mode": "operator",
        "actor_id": "attila",
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
                "actor_id": "attila",
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
            "actor_id": "attila",
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


def test_bootstrap_operator_session_fails_closed(monkeypatch):
    from hermes_cli import operator_session

    result = OperatorVerificationResult(
        ok=False,
        verified_at=10,
        expires_at=10,
        reason="not_configured",
    )
    monkeypatch.setattr(operator_session, "run_operator_verifier", lambda: result)

    with pytest.raises(SystemExit) as exc:
        operator_session.bootstrap_operator_session(quiet=True)

    assert exc.value.code == 1


def test_expired_process_cache_fallback_is_removed():
    from hermes_cli.operator_verification import (
        cache_operator_verification,
        clear_operator_verification_cache,
        get_cached_operator_verification,
    )

    clear_operator_verification_cache()
    expired = OperatorVerificationResult(
        ok=True,
        actor_id="attila",
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
