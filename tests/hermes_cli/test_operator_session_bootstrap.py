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
    assert json.loads(operator_session.os.environ["HERMES_OPERATOR_SESSION_CONTEXT"])["role"] == "operator"
    assert get_cached_operator_verification(session_id="sid-1").actor_id == "attila"


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


def test_parser_accepts_operator_flag_top_level_and_chat():
    from hermes_cli._parser import build_top_level_parser

    parser, _, _ = build_top_level_parser()
    top = parser.parse_args(["--operator"])
    chat = parser.parse_args(["chat", "--operator"])

    assert top.operator is True
    assert chat.operator is True
