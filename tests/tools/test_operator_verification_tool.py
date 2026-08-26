from __future__ import annotations

import json
import sys
from pathlib import Path

from hermes_cli.operator_verification import OperatorVerificationConfig, clear_operator_verification_cache
from tools.operator_verification_tool import check_operator_verification_requirements, verify_operator_identity


def _write_script(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o700)


def test_verify_operator_identity_returns_sanitized_success(tmp_path, monkeypatch):
    clear_operator_verification_cache()
    monkeypatch.setattr(
        "tools.operator_verification_tool.current_operator_verification_subject",
        lambda requested_role: {
            "session_id": "s1", "interface": "cli", "provenance": "callback",
            "actor_id": "operator", "requested_role": requested_role,
        },
    )
    script = tmp_path / "verify.py"
    _write_script(
        script,
        "import json\nprint(json.dumps({'ok': True, 'actor_id': 'operator', 'role': 'operator', 'ttl_seconds': 60}))\n",
    )
    monkeypatch.setattr(
        "tools.operator_verification_tool.load_operator_verification_config",
        lambda: OperatorVerificationConfig(
            enabled=True,
            argv=[sys.executable, str(script)],
            timeout_seconds=5,
            ttl_seconds=60,
            interface="cli",
        ),
    )

    payload = json.loads(verify_operator_identity({
        "reason": "prod restart", "requested_role": "operator",
    }))

    assert payload["success"] is True
    assert payload["verified"] is True
    assert payload["actor_id"] == "operator"
    assert payload["role"] == "operator"
    assert "secret" not in json.dumps(payload).lower()


def test_operator_verification_tool_visible_when_gate_enabled_without_command(monkeypatch):
    monkeypatch.setattr(
        "tools.operator_verification_tool.load_operator_verification_config",
        lambda: OperatorVerificationConfig(enabled=True, argv=[]),
    )

    assert check_operator_verification_requirements() is True


def test_verify_operator_identity_fails_closed_without_config(monkeypatch):
    clear_operator_verification_cache()
    monkeypatch.setattr(
        "tools.operator_verification_tool.current_operator_verification_subject",
        lambda requested_role: {
            "session_id": "s1", "interface": "cli", "provenance": "callback",
            "actor_id": "operator", "requested_role": requested_role,
        },
    )
    monkeypatch.setattr(
        "tools.operator_verification_tool.load_operator_verification_config",
        lambda: OperatorVerificationConfig(enabled=True, argv=[]),
    )

    payload = json.loads(verify_operator_identity({"reason": "prod restart"}))

    assert payload["success"] is False
    assert payload["verified"] is False
    assert payload["reason"] == "not_configured"


def test_verify_operator_identity_enforces_requested_role(monkeypatch):
    from hermes_cli.operator_verification import OperatorVerificationResult
    monkeypatch.setattr(
        "tools.operator_verification_tool.current_operator_verification_subject",
        lambda requested_role: {
            "session_id": "s1", "interface": "cli", "provenance": "callback",
            "actor_id": "operator", "requested_role": requested_role,
        },
    )

    monkeypatch.setattr(
        "tools.operator_verification_tool.load_operator_verification_config",
        lambda: OperatorVerificationConfig(enabled=True, argv=["verify"]),
    )
    monkeypatch.setattr(
        "tools.operator_verification_tool.run_operator_verifier",
        lambda *a, **kw: OperatorVerificationResult(
            ok=True, actor_id="operator", role="operator", verified_at=100,
            expires_at=200, session_id="s1", interface="cli",
            provenance="callback", requested_role="operator",
        ),
    )

    payload = json.loads(verify_operator_identity({
        "requested_role": "admin", "reason": "admin action",
    }))

    assert payload["success"] is False
    assert payload["verified"] is False
    assert payload["reason"] == "requested_role_not_granted"


def test_terminal_policy_block_is_not_reported_as_verifier_or_user_denial(monkeypatch):
    import json as _json
    import tools.terminal_tool as terminal

    class FakeEnv:
        cwd = "/tmp"

    monkeypatch.setattr(terminal, "_get_env_config", lambda: {
        "env_type": "local", "cwd": "/tmp", "timeout": 30,
        "host_cwd": "", "local_persistent": False,
    })
    monkeypatch.setattr(terminal, "_create_environment", lambda **kwargs: FakeEnv())
    monkeypatch.setattr(terminal, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(terminal, "_check_all_guards", lambda *a, **kw: {
        "approved": False, "status": "policy_blocked", "reason": "command_policy",
        "message": "blocked by command policy",
    })
    terminal._active_environments.clear()

    payload = _json.loads(terminal.terminal_tool("date", task_id="w1-policy-test"))

    assert payload["status"] == "policy_blocked"
    assert payload["reason"] == "command_policy"
    assert payload.get("user_denied") is not True
    assert payload.get("verifier_denied") is not True


def test_verify_operator_identity_requires_bound_session_and_never_publishes_process_cache(monkeypatch):
    import hermes_cli.operator_verification as verification

    clear_operator_verification_cache()
    payload = json.loads(verify_operator_identity({"requested_role": "operator"}))
    assert payload["success"] is False
    assert payload["reason"] == "trusted_subject_unavailable"
    assert "__process__" not in verification._cache


def test_verify_operator_identity_binds_exact_trusted_subject(monkeypatch):
    from hermes_cli.operator_verification import OperatorVerificationResult

    subject = {
        "session_id": "session-a", "interface": "cli", "provenance": "callback",
        "actor_id": "actor-a", "requested_role": "operator",
    }
    monkeypatch.setattr(
        "tools.operator_verification_tool.current_operator_verification_subject",
        lambda requested_role: dict(subject),
        raising=False,
    )
    monkeypatch.setattr(
        "tools.operator_verification_tool.load_operator_verification_config",
        lambda: OperatorVerificationConfig(enabled=True, verifier_type="callback", interface="cli"),
    )
    monkeypatch.setattr(
        "tools.operator_verification_tool.run_operator_verifier",
        lambda *a, **kw: OperatorVerificationResult(
            ok=True, role="operator", verified_at=100, expires_at=2_000_000_000, **subject,
        ),
    )
    payload = json.loads(verify_operator_identity({"requested_role": "operator"}))
    assert payload["success"] is True
    assert payload["verified"] is True
    assert "__process__" not in __import__("hermes_cli.operator_verification", fromlist=["_cache"])._cache
