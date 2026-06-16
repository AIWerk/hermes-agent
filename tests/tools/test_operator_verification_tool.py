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
    script = tmp_path / "verify.py"
    _write_script(
        script,
        "import json\nprint(json.dumps({'ok': True, 'actor_id': 'attila', 'role': 'operator', 'ttl_seconds': 60}))\n",
    )
    monkeypatch.setattr(
        "tools.operator_verification_tool.load_operator_verification_config",
        lambda: OperatorVerificationConfig(
            enabled=True,
            argv=[sys.executable, str(script)],
            timeout_seconds=5,
            ttl_seconds=60,
        ),
    )

    payload = json.loads(verify_operator_identity({"reason": "prod restart"}))

    assert payload["success"] is True
    assert payload["verified"] is True
    assert payload["actor_id"] == "attila"
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
        "tools.operator_verification_tool.load_operator_verification_config",
        lambda: OperatorVerificationConfig(enabled=True, argv=[]),
    )

    payload = json.loads(verify_operator_identity({"reason": "prod restart"}))

    assert payload["success"] is False
    assert payload["verified"] is False
    assert payload["reason"] == "not_configured"
