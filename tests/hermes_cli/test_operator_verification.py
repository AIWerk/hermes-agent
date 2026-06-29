from __future__ import annotations

import json
import sys
from pathlib import Path

from hermes_cli.config import DEFAULT_CONFIG
from hermes_cli.operator_verification import (
    OperatorVerificationConfig,
    OperatorVerificationResult,
    _derive_operator_secret,
    cache_operator_verification,
    clear_operator_verification_cache,
    current_operator_interface,
    get_cached_operator_verification,
    load_operator_verification_config,
    operator_verification_block_reason_for_command,
    run_operator_verifier,
    set_operator_verification_callback,
)


def test_default_config_enables_operator_verification_gate():
    section = DEFAULT_CONFIG["security"]["operator_verification"]

    assert section["enabled"] is True
    assert section["require_for_cli_admin"] is True
    assert section["command"]["argv"] == []
    assert section["interfaces"] == {}
    assert section["allowed_secret_read_patterns"] == []


def test_current_operator_interface_prefers_gateway_platform(monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setenv("HERMES_OPERATOR_INTERFACE", "cli")

    assert current_operator_interface() == "telegram"


def test_current_operator_interface_routes_interactive_tool_worker_to_cli(monkeypatch):
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    monkeypatch.delenv("HERMES_OPERATOR_INTERFACE", raising=False)
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    assert current_operator_interface() == "cli"


def test_current_operator_interface_detects_cui_actor_context(monkeypatch):
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    monkeypatch.delenv("HERMES_OPERATOR_INTERFACE", raising=False)
    monkeypatch.setenv("AIWERK_CUI_ACTOR_ROLE", "aiwerk_admin")

    assert current_operator_interface() == "web"


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
                        "local": {"argv": ["local-gui"], "timeout_seconds": 60},
                        "cli": {"argv": ["tty-prompt"], "timeout_seconds": 30},
                        "telegram": {"command": {"argv": ["telegram-approve"], "timeout_seconds": 120}},
                        "web": {"verifier": "cui_actor"},
                    },
                }
            }
        },
    )

    cli_cfg = load_operator_verification_config(interface="cli")
    telegram_cfg = load_operator_verification_config(interface="telegram")
    web_cfg = load_operator_verification_config(interface="web")
    missing_cfg = load_operator_verification_config(interface="discord")

    assert cli_cfg.argv == ["tty-prompt"]
    assert cli_cfg.timeout_seconds == 30
    assert cli_cfg.interface == "cli"
    assert telegram_cfg.argv == ["telegram-approve"]
    assert telegram_cfg.timeout_seconds == 120
    assert web_cfg.verifier_type == "cui_actor"
    assert web_cfg.argv == []
    assert missing_cfg.argv == []
    assert missing_cfg.missing_interface is True


def test_cui_admin_actor_context_verifies_without_prompt(monkeypatch):
    monkeypatch.setenv("AIWERK_CUI_ACTOR_CONTEXT", json.dumps({"actor_id": "attila", "role": "aiwerk_admin"}))

    result = run_operator_verifier(
        OperatorVerificationConfig(enabled=True, verifier_type="cui_actor", ttl_seconds=60),
        now=100,
    )

    assert result.ok is True
    assert result.actor_id == "attila"
    assert result.role == "aiwerk_admin"
    assert result.expires_at == 160


def test_cui_customer_actor_context_does_not_self_upgrade(monkeypatch):
    monkeypatch.setenv("AIWERK_CUI_ACTOR_CONTEXT", json.dumps({"actor_id": "customer", "role": "tenant_user"}))

    result = run_operator_verifier(
        OperatorVerificationConfig(enabled=True, verifier_type="cui_actor", ttl_seconds=60),
        now=100,
    )

    assert result.ok is False
    assert result.reason == "cui_actor_not_authorized"


def test_cui_actor_verification_sources_context_from_canonical_helper(monkeypatch):
    """The cui_actor verifier must read identity via the canonical
    agent.cui_actor_context helper (env-driven here; ContextVar-backed once PR-2
    lands), not re-parse os.environ itself."""
    # No AIWERK_CUI_* env at all — drive purely through the helper.
    for key in (
        "AIWERK_CUI_ACTOR_CONTEXT",
        "AIWERK_CUI_ACTOR_ID",
        "AIWERK_CUI_ACTOR_ROLE",
        "AIWERK_CUI_TENANT_ID",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(
        "agent.cui_actor_context.current_cui_actor_context",
        lambda: {"actor_id": "attila", "role": "aiwerk_admin"},
    )

    result = run_operator_verifier(
        OperatorVerificationConfig(enabled=True, verifier_type="cui_actor", ttl_seconds=60),
        now=200,
    )
    assert result.ok is True
    assert result.actor_id == "attila"
    assert result.role == "aiwerk_admin"

    # An empty helper (no actor) fails closed even with the verifier configured.
    monkeypatch.setattr(
        "agent.cui_actor_context.current_cui_actor_context",
        lambda: {},
    )
    blocked = run_operator_verifier(
        OperatorVerificationConfig(enabled=True, verifier_type="cui_actor", ttl_seconds=60),
        now=200,
    )
    assert blocked.ok is False
    assert blocked.reason == "cui_actor_not_authorized"


def test_missing_interface_fails_closed_before_generic_command():
    result = run_operator_verifier(
        OperatorVerificationConfig(enabled=True, argv=["wrong-gui"], missing_interface=True),
        now=100,
    )

    assert result.ok is False
    assert result.reason == "not_configured_for_interface"


def test_trusted_platform_actor_verifies_only_allowlisted_actor(monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setenv("HERMES_SESSION_USER_ID", "12345")

    valid = run_operator_verifier(
        OperatorVerificationConfig(
            enabled=True,
            verifier_type="trusted_platform_actor",
            trusted_actor_ids=["12345"],
            ttl_seconds=60,
        ),
        now=100,
    )
    invalid = run_operator_verifier(
        OperatorVerificationConfig(
            enabled=True,
            verifier_type="trusted_platform_actor",
            trusted_actor_ids=["999"],
            ttl_seconds=60,
        ),
        now=100,
    )

    assert valid.ok is True
    assert valid.actor_id == "12345"
    assert valid.role == "operator"
    assert invalid.ok is False
    assert invalid.reason == "platform_actor_not_authorized"


def test_callback_operator_verifier_uses_masked_callback_and_store(monkeypatch, tmp_path):
    store = tmp_path / "operator-verifier.json"
    salt = "MTIzNDU2Nzg5MDEyMzQ1Njc4OTAxMjM0NTY="
    store.write_text(json.dumps({
        "version": 1,
        "actor_id": "attila",
        "role": "operator",
        "salt": salt,
        "hash": _derive_operator_secret("secret", salt),
    }), encoding="utf-8")
    monkeypatch.setattr("hermes_cli.operator_verification._STORE", store)
    set_operator_verification_callback(lambda: "secret")

    result = run_operator_verifier(
        OperatorVerificationConfig(enabled=True, verifier_type="callback", ttl_seconds=60),
        now=100,
    )

    assert result.ok is True
    assert result.actor_id == "attila"
    assert result.role == "operator"
    assert result.expires_at == 160
    set_operator_verification_callback(None)


def test_callback_operator_verifier_fails_closed_without_callback(monkeypatch, tmp_path):
    store = tmp_path / "operator-verifier.json"
    store.write_text(json.dumps({
        "version": 1,
        "salt": "MTIzNDU2Nzg5MDEyMzQ1Njc4OTAxMjM0NTY=",
        "hash": "unused",
    }), encoding="utf-8")
    monkeypatch.setattr("hermes_cli.operator_verification._STORE", store)
    set_operator_verification_callback(None)

    result = run_operator_verifier(
        OperatorVerificationConfig(enabled=True, verifier_type="callback"),
        now=100,
    )

    assert result.ok is False
    assert result.reason == "callback_not_available"


def test_cli_agent_thread_wires_operator_verifier_callback():
    import inspect
    import cli

    src = inspect.getsource(cli.HermesCLI.chat)
    assert "set_operator_verification_callback(self._operator_verification_callback)" in src
    assert "set_operator_verification_callback(None)" in src

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


def test_operator_verification_allows_read_only_admin_tool_subcommands():
    clear_operator_verification_cache()
    config = OperatorVerificationConfig(enabled=True, argv=["verify"], require_for_cli_admin=True)

    allowed = [
        "systemctl status ssh",
        "service ssh status",
        "kubectl get pods",
        "kubectl describe pod web-1",
        "kubectl logs deploy/web",
        "helm list",
        "helm status my-release",
        "terraform plan",
        "terraform validate",
        "terraform output",
    ]

    for command in allowed:
        assert operator_verification_block_reason_for_command(command, config=config, now=100) is None, command


def test_operator_verification_still_blocks_mutating_admin_tool_subcommands():
    clear_operator_verification_cache()
    config = OperatorVerificationConfig(enabled=True, argv=["verify"], require_for_cli_admin=True)

    blocked = [
        "systemctl restart ssh",
        "service ssh restart",
        "kubectl apply -f deploy.yaml",
        "kubectl delete pod web-1",
        "kubectl exec -it web-1 -- sh",
        "helm upgrade my-release ./chart",
        "helm uninstall my-release",
        "terraform apply",
        "terraform destroy",
    ]

    for command in blocked:
        assert operator_verification_block_reason_for_command(command, config=config, now=100) is not None, command


def test_operator_verification_allows_normal_user_file_and_git_operations():
    clear_operator_verification_cache()
    config = OperatorVerificationConfig(enabled=True, argv=["verify"], require_for_cli_admin=True)

    allowed = [
        "chmod 644 README.md",
        "chmod 755 scripts/run.sh",
        "chown attila notes.txt",
        "git push origin main",
        "docker compose up -d",
    ]

    for command in allowed:
        assert operator_verification_block_reason_for_command(command, config=config, now=100) is None, command


def test_operator_verification_allows_safe_git_push_inside_terminal_shell_wrapper():
    clear_operator_verification_cache()
    config = OperatorVerificationConfig(enabled=True, argv=["verify"], require_for_cli_admin=True)
    command = (
        "source /tmp/hermes-snap.sh >/dev/null 2>&1 || true "
        "builtin cd -- /repo || exit 126 "
        "eval 'git push -u aiwerk aiwerk/fix/lsp-idle-reaper' "
        "__hermes_ec=$? export -p > /tmp/hermes-snap.sh exit $__hermes_ec"
    )

    assert operator_verification_block_reason_for_command(command, config=config, now=100) is None


def test_operator_verification_blocks_force_git_push_inside_terminal_shell_wrapper():
    clear_operator_verification_cache()
    config = OperatorVerificationConfig(enabled=True, argv=["verify"], require_for_cli_admin=True)
    command = (
        "source /tmp/hermes-snap.sh >/dev/null 2>&1 || true "
        "builtin cd -- /repo || exit 126 "
        "eval 'git push --force origin main' "
        "__hermes_ec=$? export -p > /tmp/hermes-snap.sh exit $__hermes_ec"
    )

    assert operator_verification_block_reason_for_command(command, config=config, now=100) is not None


def test_operator_verification_still_blocks_broad_or_privileged_file_and_git_operations():
    clear_operator_verification_cache()
    config = OperatorVerificationConfig(enabled=True, argv=["verify"], require_for_cli_admin=True)

    blocked = [
        "chmod -R 777 /srv/app",
        "chmod 777 /tmp/shared",
        "chown -R root /srv/app",
        "rm -rf /tmp/testdir",
        "git push --force origin main",
        "git push origin :main",
    ]

    for command in blocked:
        assert operator_verification_block_reason_for_command(command, config=config, now=100) is not None, command


def test_operator_verification_allows_configured_pass_show_entries_only():
    clear_operator_verification_cache()
    config = OperatorVerificationConfig(
        enabled=True,
        argv=["verify"],
        require_for_cli_admin=True,
        allowed_secret_read_patterns=[r"^pass show homeassistant-hermes-local-token$"],
    )

    assert operator_verification_block_reason_for_command(
        "pass show homeassistant-hermes-local-token", config=config, now=100
    ) is None
    assert operator_verification_block_reason_for_command(
        "pass show email/imap", config=config, now=100
    ) is not None


def test_operator_verification_allows_read_only_remote_copy_downloads_only():
    clear_operator_verification_cache()
    config = OperatorVerificationConfig(enabled=True, argv=["verify"], require_for_cli_admin=True)

    allowed = [
        "scp server:/tmp/report.txt ./report.txt",
        "rsync -av server:/tmp/reports/ ./reports/",
    ]
    blocked = [
        "scp ./secret.txt server:/tmp/secret.txt",
        "rsync -av ./reports/ server:/tmp/reports/",
    ]

    for command in allowed:
        assert operator_verification_block_reason_for_command(command, config=config, now=100) is None, command
    for command in blocked:
        assert operator_verification_block_reason_for_command(command, config=config, now=100) is not None, command


def _gate_config():
    return OperatorVerificationConfig(enabled=True, argv=["verify"], require_for_cli_admin=True)


def test_unquoted_eval_does_not_suppress_sensitive_command_fallback():
    """Regression: `eval <sensitive...>` (unquoted) must NOT disable the gate.

    Previously _eval_payloads captured only the single token after eval, which
    collapsed `eval git push --force` to the benign payload `git` and — worse —
    short-circuited before the regex fallback.
    """
    clear_operator_verification_cache()
    config = _gate_config()

    blocked = [
        "eval git push --force origin main",
        "eval pass show prod/db",
        "eval systemctl restart hermes",
        'eval "git push --force origin main"',
        "eval rm -r -f /etc",
    ]
    for command in blocked:
        assert operator_verification_block_reason_for_command(command, config=config, now=100) is not None, command


def test_benign_eval_cannot_mask_a_trailing_admin_command():
    clear_operator_verification_cache()
    config = _gate_config()

    blocked = [
        "eval 'echo hi' ; systemctl restart hermes",
        "eval echo ok && docker-compose down",
        "true ; eval echo ok ; git push --force origin main",
    ]
    for command in blocked:
        assert operator_verification_block_reason_for_command(command, config=config, now=100) is not None, command


def test_git_global_options_do_not_hide_force_push():
    clear_operator_verification_cache()
    config = _gate_config()

    blocked = [
        "git -c protocol.version=2 push --force origin main",
        "git -C /srv/repo push --force",
        "git --git-dir=/srv/.git push --force",
        "git --work-tree=/srv -C /srv/repo push --force",
        "git --namespace=ns push --force",
        "git --exec-path=/x push --force",
    ]
    allowed = [
        "git -c user.name=x commit -m hi",
        "git -C /repo status",
        "git -c protocol.version=2 push origin main",
    ]
    for command in blocked:
        assert operator_verification_block_reason_for_command(command, config=config, now=100) is not None, command
    for command in allowed:
        assert operator_verification_block_reason_for_command(command, config=config, now=100) is None, command


def test_all_force_and_destructive_push_forms_are_gated():
    clear_operator_verification_cache()
    config = _gate_config()

    blocked = [
        "git push origin +refs/heads/main",
        "git push --mirror origin",
        "git push --delete origin main",
        "git push -d origin main",
        "git push --force-with-lease origin main",
        "git push --force-with-lease=main origin main",
        "git push origin :main",
    ]
    allowed = [
        "git push origin main",
        "git push -u origin feature/x",
        "git push --tags origin",
    ]
    for command in blocked:
        assert operator_verification_block_reason_for_command(command, config=config, now=100) is not None, command
    for command in allowed:
        assert operator_verification_block_reason_for_command(command, config=config, now=100) is None, command


def test_docker_compose_hyphen_and_podman_destructive_subcommands_are_gated():
    clear_operator_verification_cache()
    config = _gate_config()

    blocked = [
        "docker-compose down",
        "docker-compose restart",
        "docker-compose stop",
        "docker-compose kill",
        "docker-compose rm -f",
        "podman restart web",
        "podman-compose down",
    ]
    allowed = [
        "docker-compose up -d",
        "docker-compose ps",
        "podman ps",
    ]
    for command in blocked:
        assert operator_verification_block_reason_for_command(command, config=config, now=100) is not None, command
    for command in allowed:
        assert operator_verification_block_reason_for_command(command, config=config, now=100) is None, command


def test_setuid_and_setgid_chmod_are_gated():
    clear_operator_verification_cache()
    config = _gate_config()

    blocked = [
        "chmod 4755 /bin/sh",
        "chmod 2755 /usr/bin/x",
        "chmod 6755 /usr/bin/y",
        "chmod u+s /bin/bash",
        "chmod g+s /usr/bin/x",
        "chmod 777 /tmp/shared",
    ]
    allowed = [
        "chmod 644 README.md",
        "chmod 755 scripts/run.sh",
        "chmod u+x scripts/run.sh",
    ]
    for command in blocked:
        assert operator_verification_block_reason_for_command(command, config=config, now=100) is not None, command
    for command in allowed:
        assert operator_verification_block_reason_for_command(command, config=config, now=100) is None, command


def test_split_flag_recursive_force_rm_is_gated():
    clear_operator_verification_cache()
    config = _gate_config()

    blocked = [
        "rm -r -f /tmp/x",
        "rm -f -r /tmp/x",
        "rm -d -r -f /etc",
        "rm -r --force /etc",
        "rm --recursive --force /etc",
        "rm -rf /tmp/x",
    ]
    allowed = [
        "rm file.txt",
        "rm -r /tmp/onlydir",
        "rm -f /tmp/onlyfile",
        "ls -rf",
    ]
    for command in blocked:
        assert operator_verification_block_reason_for_command(command, config=config, now=100) is not None, command
    for command in allowed:
        assert operator_verification_block_reason_for_command(command, config=config, now=100) is None, command


def test_chained_segment_neither_hides_nor_masks_sibling_segments():
    clear_operator_verification_cache()
    config = _gate_config()

    # Benign leading segment must not let a dangerous one slip through...
    assert operator_verification_block_reason_for_command(
        "git push origin main ; systemctl restart hermes", config=config, now=100
    ) is not None
    # ...and a benign segment must not be over-blocked by a sibling.
    assert operator_verification_block_reason_for_command(
        "git push origin main && echo done", config=config, now=100
    ) is None


def test_sensitive_command_regex_is_redos_bounded():
    import time

    from hermes_cli.operator_verification import _SENSITIVE_COMMAND_RE

    adversarial = [
        "git push " + "a " * 6000 + "x",
        "rm -" + "r" * 9000,
        "rm -" + "x" * 6000 + " -" + "y" * 6000,
        "chmod " + "7" * 9000,
    ]
    for payload in adversarial:
        start = time.time()
        _SENSITIVE_COMMAND_RE.search(payload)
        assert time.time() - start < 1.0, payload[:40]


def test_gate_is_inert_until_a_verifier_is_provisioned(monkeypatch, tmp_path):
    """Fail-closed-deadlock guard.

    With the default callback verifier but no operator store and no callback,
    nothing can ever satisfy the gate, so it must NOT block (otherwise the
    command is permanently un-runnable). Once a store exists, it blocks.
    """
    clear_operator_verification_cache()
    set_operator_verification_callback(None)
    monkeypatch.setattr(
        "hermes_cli.operator_verification._STORE", tmp_path / "missing.json"
    )

    unprovisioned = OperatorVerificationConfig(
        enabled=True, require_for_cli_admin=True, verifier_type="callback", argv=[]
    )
    assert operator_verification_block_reason_for_command(
        "systemctl restart hermes", config=unprovisioned, now=100
    ) is None

    # An argv-based verifier is provisioned -> the gate is live again.
    live = OperatorVerificationConfig(
        enabled=True, require_for_cli_admin=True, verifier_type="command", argv=["verify"]
    )
    assert operator_verification_block_reason_for_command(
        "systemctl restart hermes", config=live, now=100
    ) is not None


def test_callback_verifier_with_store_is_provisioned(monkeypatch, tmp_path):
    clear_operator_verification_cache()
    store = tmp_path / "operator-verifier.json"
    store.write_text(json.dumps({
        "version": 1,
        "actor_id": "attila",
        "role": "operator",
        "salt": "MTIzNDU2Nzg5MDEyMzQ1Njc4OTAxMjM0NTY=",
        "hash": "unused",
    }), encoding="utf-8")
    monkeypatch.setattr("hermes_cli.operator_verification._STORE", store)

    config = OperatorVerificationConfig(
        enabled=True, require_for_cli_admin=True, verifier_type="callback", argv=[]
    )
    assert operator_verification_block_reason_for_command(
        "systemctl restart hermes", config=config, now=100
    ) is not None


def test_block_check_honors_session_scoped_verification_cache():
    """The block-check must read the same cache key verify_operator_identity writes.

    A real (non-None) session_id is used on both sides; with session_id dropped
    at the enforcement point this would hard re-block forever.
    """
    clear_operator_verification_cache()
    config = _gate_config()

    assert operator_verification_block_reason_for_command(
        "systemctl restart hermes", config=config, session_id="s1", now=100
    ) is not None

    verified = OperatorVerificationResult(
        ok=True, actor_id="attila", role="operator", verified_at=100, expires_at=200
    )
    cache_operator_verification(verified, session_id="s1")

    assert operator_verification_block_reason_for_command(
        "systemctl restart hermes", config=config, session_id="s1", now=150
    ) is None


def test_terminal_tool_passes_session_id_to_operator_block_check():
    import inspect

    import tools.terminal_tool as terminal_tool

    src = inspect.getsource(terminal_tool.terminal_tool)
    assert "operator_verification_block_reason_for_command(" in src
    assert "session_id=session_id" in src
