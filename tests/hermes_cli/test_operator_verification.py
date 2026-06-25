from __future__ import annotations

import json
import sys
from pathlib import Path

from hermes_cli.config import DEFAULT_CONFIG
from hermes_cli.operator_verification import (
    OperatorVerificationConfig,
    OperatorVerificationResult,
    cache_operator_verification,
    clear_operator_verification_cache,
    current_operator_interface,
    get_cached_operator_verification,
    load_operator_verification_config,
    operator_verification_block_reason_for_command,
    run_operator_verifier,
)


def test_default_config_enables_operator_verification_gate():
    section = DEFAULT_CONFIG["security"]["operator_verification"]

    assert section["enabled"] is True
    assert section["require_for_cli_admin"] is True
    assert section["command"]["argv"] == []
    assert section["interfaces"] == {}


def test_current_operator_interface_prefers_gateway_platform(monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setenv("HERMES_OPERATOR_INTERFACE", "cli")

    assert current_operator_interface() == "telegram"


def test_operator_verification_config_selects_interface_specific_command(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "security": {
                "operator_verification": {
                    "enabled": True,
                    "ttl_seconds": 900,
                    "require_for_cli_admin": True,
                    "command": {"argv": ["local-gui"], "timeout_seconds": 60},
                    "interfaces": {
                        "cli": {"argv": ["tty-prompt"], "timeout_seconds": 30},
                        "telegram": {"command": {"argv": ["telegram-approve"], "timeout_seconds": 120}},
                    },
                }
            }
        },
    )

    cli_cfg = load_operator_verification_config(interface="cli")
    telegram_cfg = load_operator_verification_config(interface="telegram")
    web_cfg = load_operator_verification_config(interface="web")

    assert cli_cfg.argv == ["tty-prompt"]
    assert cli_cfg.timeout_seconds == 30
    assert cli_cfg.interface == "cli"
    assert telegram_cfg.argv == ["telegram-approve"]
    assert telegram_cfg.timeout_seconds == 120
    assert web_cfg.argv == ["local-gui"]
    assert web_cfg.timeout_seconds == 60

def test_operator_verification_result_valid_until_expiry():
    result = OperatorVerificationResult(
        ok=True,
        actor_id="attila",
        role="operator",
        verified_at=100,
        expires_at=200,
    )

    assert result.is_valid(now=150) is True
    assert result.is_valid(now=200) is False


def test_operator_verification_result_requires_actor_and_role():
    assert OperatorVerificationResult(ok=True, actor_id="", role="operator", expires_at=200).is_valid(now=100) is False
    assert OperatorVerificationResult(ok=True, actor_id="attila", role="", expires_at=200).is_valid(now=100) is False
    assert OperatorVerificationResult(ok=False, actor_id="attila", role="operator", expires_at=200).is_valid(now=100) is False


def _write_script(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o700)


def test_run_operator_verifier_parses_success_without_exposing_secret(tmp_path):
    script = tmp_path / "verify.py"
    _write_script(
        script,
        "import json\nprint(json.dumps({'ok': True, 'actor_id': 'attila', 'role': 'operator', 'ttl_seconds': 60}))\n",
    )

    result = run_operator_verifier(
        OperatorVerificationConfig(enabled=True, argv=[sys.executable, str(script)], timeout_seconds=5),
        now=100,
    )

    assert result.ok is True
    assert result.actor_id == "attila"
    assert result.role == "operator"
    assert result.verified_at == 100
    assert result.expires_at == 160
    assert result.reason == ""


def test_run_operator_verifier_fails_closed_on_invalid_json_and_sanitizes_output(tmp_path):
    script = tmp_path / "verify.py"
    _write_script(
        script,
        "import sys\nprint('not json secret=super-secret-code')\nprint('stderr secret=super-secret-code', file=sys.stderr)\n",
    )

    result = run_operator_verifier(
        OperatorVerificationConfig(enabled=True, argv=[sys.executable, str(script)], timeout_seconds=5),
        now=100,
    )

    assert result.ok is False
    assert result.reason == "invalid_verifier_output"
    assert "secret" not in json.dumps(result.__dict__).lower()
    assert "super-secret-code" not in json.dumps(result.__dict__)


def test_run_operator_verifier_fails_closed_when_disabled_or_missing_command():
    disabled = run_operator_verifier(OperatorVerificationConfig(enabled=False, argv=["ignored"]), now=100)
    missing = run_operator_verifier(OperatorVerificationConfig(enabled=True, argv=[]), now=100)

    assert disabled.ok is False
    assert disabled.reason == "disabled"
    assert missing.ok is False
    assert missing.reason == "not_configured"


def test_operator_verification_cache_is_in_memory_and_expires():
    clear_operator_verification_cache()
    valid = OperatorVerificationResult(ok=True, actor_id="attila", role="operator", verified_at=100, expires_at=200)

    assert get_cached_operator_verification(session_id="s1", now=150) is None
    cache_operator_verification(valid, session_id="s1")

    assert get_cached_operator_verification(session_id="s1", now=150) == valid
    assert get_cached_operator_verification(session_id="s1", now=250) is None


def test_admin_sensitive_command_is_blocked_until_operator_verified():
    clear_operator_verification_cache()
    config = OperatorVerificationConfig(enabled=True, argv=["verify"], require_for_cli_admin=True)

    safe = operator_verification_block_reason_for_command("date", config=config, now=100)
    blocked = operator_verification_block_reason_for_command("systemctl restart hermes", config=config, now=100)

    assert safe is None
    assert blocked is not None
    assert "verify_operator_identity" in blocked

    verified = OperatorVerificationResult(ok=True, actor_id="attila", role="operator", verified_at=100, expires_at=200)
    cache_operator_verification(verified)
    assert operator_verification_block_reason_for_command("systemctl restart hermes", config=config, now=150) is None
