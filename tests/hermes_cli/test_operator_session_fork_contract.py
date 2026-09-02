import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

from hermes_cli.operator_verification import OperatorVerificationResult


def _valid_result(now: int | None = None) -> OperatorVerificationResult:
    now = int(time.time()) if now is None else now
    return OperatorVerificationResult(
        ok=True,
        actor_id="attila",
        role="operator",
        verified_at=now,
        expires_at=now + 900,
    )


def test_verified_result_builds_authorizing_operator_context():
    from hermes_cli.operator_session import build_operator_session_context

    result = _valid_result()
    context = build_operator_session_context(result)

    assert context == {
        "mode": "operator",
        "actor_id": "attila",
        "role": "operator",
        "acting_for": "aiwerk",
        "memory_scope": "operator",
        "verified_at": result.verified_at,
        "expires_at": result.expires_at,
    }


def test_serialized_context_is_allowlisted_and_validated(monkeypatch):
    from hermes_cli import operator_session

    monkeypatch.setattr(operator_session.time, "time", lambda: 200)
    context = {**operator_session.build_operator_session_context(_valid_result(200)), "secret": "no"}
    encoded = operator_session.serialize_operator_session_context(
        {**context, "bootstrap_pid": os.getpid()}
    )
    payload = json.loads(encoded)

    assert "secret" not in payload
    assert payload["bootstrap_pid"] == os.getpid()
    assert operator_session._validated_operator_context(payload) == {
        key: context[key] for key in context if key != "secret"
    }


def test_bootstrap_verifies_once_and_exports_only_sanitized_context(monkeypatch):
    from hermes_cli import operator_session

    calls = []
    monkeypatch.setattr(operator_session, "run_operator_verifier", lambda: _valid_result())
    monkeypatch.setattr(
        operator_session,
        "cache_operator_verification",
        lambda result, session_id=None: calls.append((result.actor_id, session_id)),
    )
    monkeypatch.delenv(operator_session.ENV_OPERATOR_SESSION_CONTEXT, raising=False)

    context = operator_session.bootstrap_operator_session(session_id="sid-1", quiet=True)
    exported = json.loads(os.environ[operator_session.ENV_OPERATOR_SESSION_CONTEXT])

    assert calls == [("attila", "sid-1")]
    assert context["mode"] == "operator"
    assert context["memory_scope"] == "operator"
    assert exported["bootstrap_pid"] == os.getpid()
    assert "secret" not in exported


def test_main_preserves_documented_aiwerk_operator_design_and_three_bootstraps():
    import hermes_cli.main as main

    source = Path(main.__file__).read_text(encoding="utf-8")
    assert "AIWerk design intent (Attila, 2026-09-02)" in source
    assert source.count("bootstrap_operator_session(quiet=") == 3
    assert "operator_session_context = bootstrap_operator_session(" in source


def test_operator_session_is_content_loss_protected():
    root = Path(__file__).resolve().parents[2]
    baseline = json.loads((root / ".ci/content-loss/baseline.json").read_text())
    protected = baseline["protected_paths"]
    entry = next(item for item in protected if item["path"] == "hermes_cli/operator_session.py")
    assert 'mode\": \"operator\"' in entry["markers"]


def test_verified_actor_owns_new_session_row(monkeypatch):
    from run_agent import AIAgent

    created = []

    class DB:
        def create_session(self, **kwargs):
            created.append(kwargs)

    agent = SimpleNamespace(
        _persist_disabled=False,
        _session_db_created=False,
        _session_db=DB(),
        platform="cli",
        operator_session_context={
            "mode": "operator",
            "actor_id": "attila",
            "role": "operator",
            "memory_scope": "operator",
        },
        _session_init_model_config={},
        session_id="sid-operator",
        model="test-model",
        _cached_system_prompt="prompt",
        _parent_session_id=None,
    )
    monkeypatch.setattr("tools.approval.is_session_yolo_enabled", lambda _sid: False)

    AIAgent._ensure_db_session(agent)

    assert created[0]["user_id"] == "attila"
    assert agent._session_db_created is True
