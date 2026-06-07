"""Tests for the `hermes proxy` subcommand and its upstream adapters."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli.proxy.adapters import ADAPTERS, get_adapter
from hermes_cli.proxy.adapters.base import UpstreamAdapter, UpstreamCredential
from hermes_cli.proxy.adapters.nous_portal import NousPortalAdapter
from hermes_cli.proxy.adapters.openai_codex import (
    OpenAICodexAdapter,
    chat_payload_to_responses_payload,
    responses_payload_to_chat_completion,
    responses_stream_to_payload,
)
from hermes_cli.proxy.adapters.xai import XAIGrokAdapter


# ---------------------------------------------------------------------------
# Adapter registry
# ---------------------------------------------------------------------------


def test_registry_lists_nous():
    assert "nous" in ADAPTERS
    assert "openai-codex" in ADAPTERS


def test_registry_lists_xai():
    assert "xai" in ADAPTERS


def test_get_adapter_returns_instance():
    adapter = get_adapter("nous")
    assert isinstance(adapter, NousPortalAdapter)
    assert isinstance(adapter, UpstreamAdapter)
    codex_adapter = get_adapter("openai-codex")
    assert isinstance(codex_adapter, OpenAICodexAdapter)
    assert isinstance(codex_adapter, UpstreamAdapter)


def test_get_adapter_returns_xai_instance():
    adapter = get_adapter("xai")
    assert isinstance(adapter, XAIGrokAdapter)
    assert isinstance(adapter, UpstreamAdapter)


def test_get_adapter_case_insensitive():
    assert isinstance(get_adapter("NOUS"), NousPortalAdapter)
    assert isinstance(get_adapter("  Nous  "), NousPortalAdapter)
    assert isinstance(get_adapter("XAI"), XAIGrokAdapter)


def test_get_adapter_unknown_provider_raises():
    with pytest.raises(ValueError, match="anthropic"):
        get_adapter("anthropic")  # not implemented


# ---------------------------------------------------------------------------
# OpenAICodexAdapter + chat shim translation
# ---------------------------------------------------------------------------


def _write_codex_auth_store(hermes_home: Path) -> Path:
    auth_path = hermes_home / "auth.json"
    auth_path.write_text(json.dumps({
        "version": 1,
        "providers": {
            "openai-codex": {
                "tokens": {
                    "access_token": "access-token",
                    "refresh_token": "refresh-token",
                },
                "auth_mode": "chatgpt",
            }
        },
    }))
    return auth_path


def test_codex_adapter_metadata():
    adapter = OpenAICodexAdapter()
    assert adapter.name == "openai-codex"
    assert adapter.display_name == "OpenAI Codex OAuth"
    assert "/chat/completions" in adapter.allowed_paths
    assert "/responses" in adapter.allowed_paths
    assert "/models" in adapter.allowed_paths


def test_codex_adapter_authentication_from_hermes_store(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert not OpenAICodexAdapter().is_authenticated()
    _write_codex_auth_store(tmp_path)
    assert OpenAICodexAdapter().is_authenticated()


def test_codex_adapter_get_credential_uses_runtime_resolver(monkeypatch):
    with patch(
        "hermes_cli.proxy.adapters.openai_codex.resolve_codex_runtime_credentials",
        return_value={
            "api_key": "codex-access-token",
            "base_url": "https://chatgpt.com/backend-api/codex",
        },
    ):
        cred = OpenAICodexAdapter().get_credential()
    assert cred.bearer == "codex-access-token"
    assert cred.base_url == "https://chatgpt.com/backend-api/codex"
    assert cred.headers is not None
    assert cred.headers["originator"] == "codex_cli_rs"
    assert "Authorization" not in cred.headers


def test_codex_adapter_falls_back_to_pooled_credential(monkeypatch):
    class Entry:
        provider = "openai-codex"
        runtime_api_key = "pooled-codex-token"
        runtime_base_url = "https://chatgpt.com/backend-api/codex"
        source = "oauth:codex"
        last_refresh = "2026-05-21T00:00:00Z"

    class Pool:
        _entries = [Entry()]

        def peek(self):
            return self._entries[0]

    with patch(
        "hermes_cli.proxy.adapters.openai_codex.resolve_codex_runtime_credentials",
        side_effect=RuntimeError("stale token store"),
    ), patch("agent.credential_pool.load_pool", return_value=Pool()):
        adapter = OpenAICodexAdapter()
        assert adapter.is_authenticated()
        cred = adapter.get_credential()

    assert cred.bearer == "pooled-codex-token"
    assert cred.base_url == "https://chatgpt.com/backend-api/codex"
    assert cred.headers is not None
    assert cred.headers["originator"] == "codex_cli_rs"


def test_chat_payload_to_responses_payload_is_no_tools_boundary():
    payload = chat_payload_to_responses_payload({
        "model": "gpt-5.4-mini",
        "messages": [
            {"role": "system", "content": "Be concise."},
            {"role": "user", "content": "hi"},
        ],
        "tools": [{"type": "function", "function": {"name": "unsafe"}}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "Result", "schema": {"type": "object"}, "strict": True},
        },
    })
    assert payload["model"] == "gpt-5.4-mini"
    assert payload["instructions"] == "Be concise."
    assert payload["input"] == [{"role": "user", "content": "hi"}]
    assert "tools" not in payload
    assert payload["store"] is False
    assert payload["stream"] is True
    assert payload["text"]["format"]["type"] == "json_schema"


def test_responses_payload_to_chat_completion_extracts_text_and_usage():
    chat = responses_payload_to_chat_completion({
        "id": "resp_123",
        "output": [{
            "type": "message",
            "content": [{"type": "output_text", "text": "hello"}],
        }],
        "usage": {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
    }, model="gpt-5.4-mini")
    assert chat["object"] == "chat.completion"
    assert chat["choices"][0]["message"]["content"] == "hello"
    assert chat["usage"]["total_tokens"] == 5


def test_responses_stream_to_payload_collapses_sse_text():
    payload = responses_stream_to_payload(
        b'data: {"type":"response.output_text.delta","delta":"proxy"}\n\n'
        b'data: {"type":"response.output_text.delta","delta":" ok"}\n\n'
        b'data: {"type":"response.completed","response":{"id":"resp_stream","usage":{"input_tokens":1,"output_tokens":2,"total_tokens":3},"output":[]}}\n\n'
        b'data: [DONE]\n\n'
    )
    chat = responses_payload_to_chat_completion(payload, model="gpt-5.4-mini")
    assert payload["id"] == "resp_stream"
    assert chat["choices"][0]["message"]["content"] == "proxy ok"
    assert chat["usage"]["total_tokens"] == 3


def test_responses_payload_to_chat_completion_coerces_malformed_usage():
    """A non-numeric upstream usage field must not raise (pre-fix: ValueError
    escaped the server's try/except and the proxy returned a bare 500).

    The conversion still yields a valid chat.completion with the bad count
    coerced to 0 instead of crashing.
    """
    chat = responses_payload_to_chat_completion({
        "id": "resp_bad_usage",
        "output": [{
            "type": "message",
            "content": [{"type": "output_text", "text": "still works"}],
        }],
        "usage": {
            "input_tokens": "NOTANUMBER",
            "output_tokens": 4,
            "total_tokens": None,
        },
    }, model="gpt-5.4-mini")

    assert chat["choices"][0]["message"]["content"] == "still works"
    # Malformed input_tokens -> 0; total falls back to prompt+completion sum.
    assert chat["usage"]["prompt_tokens"] == 0
    assert chat["usage"]["completion_tokens"] == 4
    assert chat["usage"]["total_tokens"] == 4


def test_codex_adapter_retry_credential_rotates_on_401(monkeypatch):
    """A 401 from Codex must rotate to a different pooled credential.

    Pre-fix the adapter inherited the base no-op ``get_retry_credential``,
    so the server's retry block could never rotate for Codex — a single
    expired pool key took down every request.
    """
    class Entry:
        def __init__(self, key, base_url="https://chatgpt.com/backend-api/codex"):
            self.runtime_api_key = key
            self.runtime_base_url = base_url

    rotated_to = Entry("second-codex-token")

    class Pool:
        def __init__(self):
            self.calls = []

        def mark_exhausted_and_rotate(self, *, status_code, api_key_hint=None):
            self.calls.append((status_code, api_key_hint))
            return rotated_to

    pool = Pool()
    with patch("agent.credential_pool.load_pool", return_value=pool):
        adapter = OpenAICodexAdapter()
        failed = UpstreamCredential(
            bearer="first-codex-token",
            base_url="https://chatgpt.com/backend-api/codex",
        )
        retry = adapter.get_retry_credential(failed_credential=failed, status_code=401)

    assert retry is not None, "401 must rotate to next pooled Codex credential"
    assert retry.bearer == "second-codex-token"
    assert retry.base_url == "https://chatgpt.com/backend-api/codex"
    # The failed bearer is pinned as the hint so the right entry is exhausted.
    assert pool.calls == [(401, "first-codex-token")]


def test_codex_adapter_retry_credential_skips_unrelated_status(monkeypatch):
    """Non-{401, 429} statuses must not touch the pool at all."""
    def _load_pool_must_not_run(*args, **kwargs):
        raise AssertionError("pool must not be loaded for unrelated statuses")

    with patch("agent.credential_pool.load_pool", _load_pool_must_not_run):
        adapter = OpenAICodexAdapter()
        failed = UpstreamCredential(bearer="x", base_url="https://example/codex")
        assert adapter.get_retry_credential(failed_credential=failed, status_code=500) is None


def test_codex_adapter_retry_credential_none_when_no_rotation(monkeypatch):
    """If rotation yields nothing (or the same bearer), return None so the
    upstream 401/429 flows back to the client unchanged."""
    class Pool:
        def mark_exhausted_and_rotate(self, *, status_code, api_key_hint=None):
            return None  # single-entry pool: nowhere to rotate

    with patch("agent.credential_pool.load_pool", return_value=Pool()):
        adapter = OpenAICodexAdapter()
        failed = UpstreamCredential(bearer="only-token", base_url="https://example/codex")
        assert adapter.get_retry_credential(failed_credential=failed, status_code=429) is None


# ---------------------------------------------------------------------------
# NousPortalAdapter
# ---------------------------------------------------------------------------


def _write_auth_store(hermes_home: Path, nous_state: Dict[str, Any]) -> Path:
    """Write an auth.json with the given nous state into a hermetic HERMES_HOME."""
    auth_path = hermes_home / "auth.json"
    auth_path.write_text(json.dumps({
        "version": 1,
        "providers": {"nous": nous_state},
    }))
    return auth_path


def test_nous_adapter_metadata():
    adapter = NousPortalAdapter()
    assert adapter.name == "nous"
    assert adapter.display_name == "Nous Portal"
    assert "/chat/completions" in adapter.allowed_paths
    assert "/embeddings" in adapter.allowed_paths
    assert "/completions" in adapter.allowed_paths
    assert "/models" in adapter.allowed_paths


def test_nous_adapter_not_authenticated_when_no_auth_file(tmp_path, monkeypatch):
    # HERMES_HOME is already set by conftest, but make doubly sure
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = NousPortalAdapter()
    assert not adapter.is_authenticated()


def test_nous_adapter_not_authenticated_when_provider_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "auth.json").write_text(json.dumps({
        "version": 1,
        "providers": {},
    }))
    assert not NousPortalAdapter().is_authenticated()


def test_nous_adapter_authenticated_with_agent_key(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_auth_store(tmp_path, {
        "agent_key": "ov-test-key",
        "agent_key_expires_at": "2099-01-01T00:00:00Z",
        "inference_base_url": "https://inference-api.nousresearch.com/v1",
    })
    assert NousPortalAdapter().is_authenticated()


def test_nous_adapter_authenticated_with_refresh_token_only(tmp_path, monkeypatch):
    """If access_token+refresh_token exist but no agent_key yet, we can still refresh."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_auth_store(tmp_path, {
        "access_token": "access-tok",
        "refresh_token": "refresh-tok",
    })
    assert NousPortalAdapter().is_authenticated()


def test_nous_adapter_get_credential_uses_runtime_resolver(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_auth_store(tmp_path, {
        "access_token": "access-tok",
        "refresh_token": "refresh-tok",
        "client_id": "hermes-cli",
        "portal_base_url": "https://portal.nousresearch.com",
        "inference_base_url": "https://inference-api.nousresearch.com/v1",
    })

    refreshed_state = {
        "api_key": "jwt-bearer",
        "base_url": "https://inference-api.nousresearch.com/v1",
        "expires_at": "2099-01-01T00:00:00Z",
    }

    with patch(
        "hermes_cli.proxy.adapters.nous_portal.resolve_nous_runtime_credentials",
        return_value=refreshed_state,
    ) as mock_resolve:
        adapter = NousPortalAdapter()
        cred = adapter.get_credential()

    mock_resolve.assert_called_once()
    assert cred.bearer == "jwt-bearer"
    assert cred.base_url == "https://inference-api.nousresearch.com/v1"
    assert cred.expires_at == "2099-01-01T00:00:00Z"
    assert cred.token_type == "Bearer"


def test_nous_adapter_retry_credential_force_refreshes_on_jwt_401(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_auth_store(tmp_path, {
        "access_token": "jwt-access",
        "refresh_token": "refresh-tok",
        "client_id": "hermes-cli",
        "portal_base_url": "https://portal.nousresearch.com",
        "inference_base_url": "https://inference-api.nousresearch.com/v1",
        "agent_key": "jwt-access",
    })
    refreshed_state = {
        "api_key": "fresh-jwt-bearer",
        "base_url": "https://inference-api.nousresearch.com/v1",
        "expires_at": "2099-01-01T00:00:00Z",
    }

    with patch(
        "hermes_cli.proxy.adapters.nous_portal.resolve_nous_runtime_credentials",
        return_value=refreshed_state,
    ) as mock_resolve:
        adapter = NousPortalAdapter()
        cred = adapter.get_retry_credential(
            failed_credential=UpstreamCredential(
                bearer="header.jwt.signature",
                base_url="https://inference-api.nousresearch.com/v1",
            ),
            status_code=401,
        )

    assert cred is not None
    assert cred.bearer == "fresh-jwt-bearer"
    assert mock_resolve.call_args.kwargs["force_refresh"] is True


def test_nous_adapter_retry_credential_skips_non_401(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_auth_store(tmp_path, {
        "access_token": "jwt-access",
        "refresh_token": "refresh-tok",
        "agent_key": "opaque-bearer",
    })

    with patch(
        "hermes_cli.proxy.adapters.nous_portal.resolve_nous_runtime_credentials",
    ) as mock_resolve:
        adapter = NousPortalAdapter()
        cred = adapter.get_retry_credential(
            failed_credential=UpstreamCredential(
                bearer="opaque-bearer",
                base_url="https://inference-api.nousresearch.com/v1",
            ),
            status_code=403,
        )

    assert cred is None
    mock_resolve.assert_not_called()


def test_nous_adapter_get_credential_raises_when_not_logged_in(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = NousPortalAdapter()
    with pytest.raises(RuntimeError, match="hermes auth add nous"):
        adapter.get_credential()


def test_nous_adapter_get_credential_raises_on_refresh_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_auth_store(tmp_path, {
        "access_token": "access-tok",
        "refresh_token": "refresh-tok",
    })

    with patch(
        "hermes_cli.proxy.adapters.nous_portal.resolve_nous_runtime_credentials",
        side_effect=RuntimeError("Refresh session has been revoked"),
    ):
        adapter = NousPortalAdapter()
        with pytest.raises(RuntimeError, match="Refresh session has been revoked"):
            adapter.get_credential()


def test_nous_adapter_quarantines_terminal_refresh_failure(tmp_path, monkeypatch):
    from hermes_cli.auth import AuthError
    from agent.credential_pool import load_pool

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_auth_store(tmp_path, {
        "access_token": "access-tok",
        "refresh_token": "refresh-tok",
        "agent_key": "stale-agent-key",
    })
    assert load_pool("nous").select() is not None

    with patch(
        "hermes_cli.proxy.adapters.nous_portal.resolve_nous_runtime_credentials",
        side_effect=AuthError(
            "Refresh session has been revoked",
            provider="nous",
            code="invalid_grant",
            relogin_required=True,
        ),
    ):
        adapter = NousPortalAdapter()
        with pytest.raises(RuntimeError, match="Refresh session has been revoked"):
            adapter.get_credential()

    stored = json.loads((tmp_path / "auth.json").read_text())
    nous_state = stored["providers"]["nous"]
    assert not nous_state.get("refresh_token")
    assert not nous_state.get("access_token")
    assert not nous_state.get("agent_key")
    assert nous_state["last_auth_error"]["code"] == "invalid_grant"
    assert stored.get("credential_pool", {}).get("nous") == []


def test_nous_adapter_get_credential_raises_when_no_jwt_returned(tmp_path, monkeypatch):
    """If the refresh helper succeeds but produces no JWT, we surface a clear error."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_auth_store(tmp_path, {
        "access_token": "access-tok",
        "refresh_token": "refresh-tok",
    })

    with patch(
        "hermes_cli.proxy.adapters.nous_portal.resolve_nous_runtime_credentials",
        return_value={"access_token": "a", "refresh_token": "r"},
    ):
        adapter = NousPortalAdapter()
        with pytest.raises(RuntimeError, match="did not return a usable inference JWT"):
            adapter.get_credential()


def test_nous_adapter_concurrent_refresh_serialized(tmp_path, monkeypatch):
    """Two parallel get_credential() calls must serialize through the lock."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_auth_store(tmp_path, {
        "access_token": "a", "refresh_token": "r",
    })

    call_log: list = []
    in_flight = threading.Event()
    overlap_detected = threading.Event()
    counter = [0]
    counter_lock = threading.Lock()

    def serializing_refresh(**kwargs):
        # If another thread is already inside refresh, the lock is broken.
        if in_flight.is_set():
            overlap_detected.set()
        in_flight.set()
        try:
            call_log.append(threading.current_thread().ident)
            # Simulate refresh latency so any race window is exposed.
            import time
            time.sleep(0.05)
            with counter_lock:
                counter[0] += 1
                idx = counter[0]
            return {
                "api_key": f"key-{idx}",
                "expires_at": "2099-01-01T00:00:00Z",
                "base_url": "https://inference-api.nousresearch.com/v1",
            }
        finally:
            in_flight.clear()

    adapter = NousPortalAdapter()
    results: list = []
    errors: list = []

    def worker():
        try:
            results.append(adapter.get_credential().bearer)
        except Exception as exc:  # pragma: no cover - shouldn't happen
            errors.append(exc)

    with patch(
        "hermes_cli.proxy.adapters.nous_portal.resolve_nous_runtime_credentials",
        side_effect=serializing_refresh,
    ):
        threads = [threading.Thread(target=worker) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert not errors, f"workers errored: {errors}"
    assert len(results) == 3
    assert len(call_log) == 3
    assert not overlap_detected.is_set(), "refresh calls overlapped — lock is broken"
    assert all(r.startswith("key-") for r in results)


# ---------------------------------------------------------------------------
# XAIGrokAdapter
# ---------------------------------------------------------------------------


def _write_xai_pool_entry(
    hermes_home: Path,
    *,
    access_token: str = "xai-access-token",
    refresh_token: str = "xai-refresh-token",
    base_url: str = "https://api.x.ai/v1",
    source: str = "manual:xai_pkce",
) -> Path:
    """Write an xai-oauth pool entry into a hermetic HERMES_HOME."""
    auth_path = hermes_home / "auth.json"
    auth_path.write_text(json.dumps({
        "version": 1,
        "providers": {},
        "credential_pool": {
            "xai-oauth": [
                {
                    "id": "xai123",
                    "label": "xai-test",
                    "auth_type": "oauth",
                    "priority": 0,
                    "source": source,
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                    "base_url": base_url,
                }
            ]
        },
    }))
    return auth_path


def test_xai_adapter_metadata():
    adapter = XAIGrokAdapter()
    assert adapter.name == "xai"
    assert adapter.display_name == "xAI Grok OAuth"
    assert "/responses" in adapter.allowed_paths
    assert "/chat/completions" in adapter.allowed_paths
    assert "/models" in adapter.allowed_paths


def test_xai_adapter_not_authenticated_when_no_pool_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "auth.json").write_text(json.dumps({
        "version": 1,
        "providers": {},
        "credential_pool": {},
    }))
    assert not XAIGrokAdapter().is_authenticated()


def test_xai_adapter_authenticated_with_pool_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_xai_pool_entry(tmp_path)
    assert XAIGrokAdapter().is_authenticated()


def test_xai_adapter_get_credential_uses_oauth_pool(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_xai_pool_entry(
        tmp_path,
        access_token="pool-access-token",
        base_url="https://api.x.ai/v1/",
    )

    cred = XAIGrokAdapter().get_credential()

    assert cred.bearer == "pool-access-token"
    assert cred.base_url == "https://api.x.ai/v1"
    assert cred.token_type == "Bearer"


def test_xai_adapter_get_credential_defaults_base_url(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_xai_pool_entry(tmp_path, base_url="")

    cred = XAIGrokAdapter().get_credential()

    assert cred.base_url == "https://api.x.ai/v1"


def test_xai_adapter_retry_refreshes_current_pool_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_xai_pool_entry(tmp_path, access_token="old-access-token")

    def fake_refresh(access_token, refresh_token, **kwargs):
        assert access_token == "old-access-token"
        assert refresh_token == "xai-refresh-token"
        return {
            "access_token": "new-access-token",
            "refresh_token": "new-refresh-token",
            "last_refresh": "2026-05-19T00:00:00Z",
        }

    monkeypatch.setattr("hermes_cli.auth.refresh_xai_oauth_pure", fake_refresh)

    adapter = XAIGrokAdapter()
    failed = adapter.get_credential()
    retry = adapter.get_retry_credential(
        failed_credential=failed,
        status_code=401,
    )

    assert retry is not None
    assert retry.bearer == "new-access-token"


def test_xai_adapter_retry_rotates_pool_entry_on_429(tmp_path, monkeypatch):
    """429 from xAI must rotate to the next pool entry, not attempt refresh.

    Pre-fix (#28932) ``get_retry_credential`` only fired on 401, so a 429
    rate-limit response flowed back to the client unchanged AND the
    rate-limited bearer stayed active for the next request — defeating
    the whole point of pool rotation.

    Post-fix: 429 lands on ``mark_exhausted_and_rotate`` (no refresh —
    that's irrelevant for rate limits), stamps the 1-hour cooldown
    via ``EXHAUSTED_TTL_429_SECONDS`` on the offending key, and
    returns the next available credential.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    # Two pool entries so rotation has somewhere to go.
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(json.dumps({
        "version": 1,
        "providers": {},
        "credential_pool": {
            "xai-oauth": [
                {
                    "id": "xai-first",
                    "label": "xai-first",
                    "auth_type": "oauth",
                    "priority": 0,
                    "source": "manual:xai_pkce",
                    "access_token": "first-access-token",
                    "refresh_token": "first-refresh-token",
                    "base_url": "https://api.x.ai/v1",
                },
                {
                    "id": "xai-second",
                    "label": "xai-second",
                    "auth_type": "oauth",
                    "priority": 1,
                    "source": "manual:xai_pkce",
                    "access_token": "second-access-token",
                    "refresh_token": "second-refresh-token",
                    "base_url": "https://api.x.ai/v1",
                },
            ]
        },
    }))

    # Refresh must NOT be called on the 429 path — guard against
    # the fix accidentally trying to refresh-on-rate-limit.
    def _refresh_must_not_run(*args, **kwargs):
        raise AssertionError("refresh_xai_oauth_pure must not run on 429")

    monkeypatch.setattr("hermes_cli.auth.refresh_xai_oauth_pure", _refresh_must_not_run)

    adapter = XAIGrokAdapter()
    failed = adapter.get_credential()
    assert failed.bearer == "first-access-token", "starting bearer should be the first entry"

    retry = adapter.get_retry_credential(
        failed_credential=failed,
        status_code=429,
    )

    assert retry is not None, "429 must rotate to next pool entry"
    assert retry.bearer == "second-access-token", (
        f"expected rotation to second entry, got {retry.bearer!r}"
    )


def test_xai_adapter_retry_returns_none_on_429_when_pool_exhausted(tmp_path, monkeypatch):
    """Single-entry pool: 429 has nowhere to rotate to → return None
    so the 429 flows back to the client unchanged (existing behavior
    preserved)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_xai_pool_entry(tmp_path)  # single entry

    def _refresh_must_not_run(*args, **kwargs):
        raise AssertionError("refresh_xai_oauth_pure must not run on 429")

    monkeypatch.setattr("hermes_cli.auth.refresh_xai_oauth_pure", _refresh_must_not_run)

    adapter = XAIGrokAdapter()
    failed = adapter.get_credential()
    retry = adapter.get_retry_credential(
        failed_credential=failed,
        status_code=429,
    )

    assert retry is None, (
        "single-entry pool: 429 must return None so the response "
        "flows back to the client unchanged"
    )


def test_xai_adapter_retry_returns_none_for_unrelated_status(tmp_path, monkeypatch):
    """Non-{401, 429} statuses must NOT trigger any retry — pool
    untouched, no refresh attempted, return None immediately."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_xai_pool_entry(tmp_path)

    def _refresh_must_not_run(*args, **kwargs):
        raise AssertionError("refresh_xai_oauth_pure must not run on non-retry status")

    monkeypatch.setattr("hermes_cli.auth.refresh_xai_oauth_pure", _refresh_must_not_run)

    adapter = XAIGrokAdapter()
    failed = adapter.get_credential()
    for status in (200, 400, 403, 500, 502, 503):
        retry = adapter.get_retry_credential(
            failed_credential=failed,
            status_code=status,
        )
        assert retry is None, (
            f"status {status} must not trigger retry, got {retry!r}"
        )


# ---------------------------------------------------------------------------
# Server: path filtering + forwarding
#
# We run the proxy AND a fake upstream as real aiohttp servers on ephemeral
# ports. Avoids pytest-aiohttp's fixtures (extra dependency for one test file).
# ---------------------------------------------------------------------------

aiohttp = pytest.importorskip("aiohttp")
from aiohttp import web  # noqa: E402

from hermes_cli.proxy.server import create_app  # noqa: E402


class FakeAdapter(UpstreamAdapter):
    """A test adapter that returns a fixed credential without touching disk."""

    def __init__(self, base_url: str, bearer: str = "test-bearer",
                 allowed=None, raise_on_credential=False, name="fake",
                 retry_bearer: str | None = None):
        self._base_url = base_url
        self._bearer = bearer
        self._allowed = frozenset(allowed or ["/chat/completions"])
        self._raise = raise_on_credential
        self._name = name
        self._retry_bearer = retry_bearer
        self.calls = 0
        self.retry_calls = 0

    @property
    def name(self): return self._name

    @property
    def display_name(self): return "Fake Provider"

    @property
    def allowed_paths(self): return self._allowed

    def is_authenticated(self): return True

    def get_credential(self):
        self.calls += 1
        if self._raise:
            raise RuntimeError("simulated auth failure")
        return UpstreamCredential(
            bearer=self._bearer, base_url=self._base_url,
            expires_at="2099-01-01T00:00:00Z",
        )

    def get_retry_credential(self, *, failed_credential, status_code):
        _ = failed_credential
        self.retry_calls += 1
        if status_code != 401 or not self._retry_bearer:
            return None
        return UpstreamCredential(
            bearer=self._retry_bearer,
            base_url=self._base_url,
            expires_at="2099-01-01T00:00:00Z",
        )


async def _start_runner(app: "web.Application"):
    """Spin up an aiohttp app on an ephemeral localhost port. Returns (runner, base_url)."""
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host="127.0.0.1", port=0)
    await site.start()
    sockets = list(site._server.sockets)  # type: ignore[union-attr]
    port = sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


def _build_fake_upstream(captured: Dict[str, Any]) -> "web.Application":
    async def echo(request):
        body = await request.read()
        captured["requests"].append({
            "method": request.method,
            "path": request.path,
            "auth": request.headers.get("Authorization"),
            "body": body.decode("utf-8") if body else "",
        })
        return web.json_response({"echoed": True, "path": request.path})

    async def sse(request):
        resp = web.StreamResponse(
            status=200, headers={"Content-Type": "text/event-stream"},
        )
        await resp.prepare(request)
        for chunk in [b"data: hello\n\n", b"data: world\n\n", b"data: [DONE]\n\n"]:
            await resp.write(chunk)
        await resp.write_eof()
        return resp

    async def responses(request):
        body = await request.read()
        captured["requests"].append({
            "method": request.method,
            "path": request.path,
            "auth": request.headers.get("Authorization"),
            "originator": request.headers.get("originator"),
            "body": body.decode("utf-8") if body else "",
        })
        return web.json_response({
            "id": "resp_fake",
            "output": [{
                "type": "message",
                "content": [{"type": "output_text", "text": "codex ok"}],
            }],
            "usage": {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6},
        }, headers={"x-oai-request-id": "req_fake_123"})

    app = web.Application()
    app.router.add_route("*", "/v1/chat/completions", echo)
    app.router.add_route("*", "/v1/embeddings", echo)
    app.router.add_route("*", "/v1/responses", responses)
    app.router.add_route("*", "/v1/sse", sse)
    return app


def _build_retrying_fake_upstream(captured: Dict[str, Any]) -> "web.Application":
    async def maybe_unauthorized(request):
        body = await request.read()
        auth = request.headers.get("Authorization")
        captured["requests"].append({
            "method": request.method,
            "path": request.path,
            "auth": auth,
            "body": body.decode("utf-8") if body else "",
        })
        if auth == "Bearer jwt-bearer":
            return web.json_response({"error": "bad token"}, status=401)
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_route("*", "/v1/chat/completions", maybe_unauthorized)
    return app


def test_server_forwards_chat_completions():
    async def run():
        captured: Dict[str, Any] = {"requests": []}
        upstream_runner, upstream_base = await _start_runner(_build_fake_upstream(captured))
        adapter = FakeAdapter(f"{upstream_base}/v1", bearer="real-portal-key")
        proxy_runner, proxy_base = await _start_runner(create_app(adapter))

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{proxy_base}/v1/chat/completions",
                    json={"model": "Hermes-4-70B",
                          "messages": [{"role": "user", "content": "hi"}]},
                    headers={"Authorization": "Bearer client-dummy-key"},
                ) as resp:
                    assert resp.status == 200
                    data = await resp.json()
                    assert data["echoed"] is True

            assert len(captured["requests"]) == 1
            req = captured["requests"][0]
            assert req["auth"] == "Bearer real-portal-key"
            assert "Hermes-4-70B" in req["body"]
        finally:
            await proxy_runner.cleanup()
            await upstream_runner.cleanup()

    asyncio.run(run())


def test_server_codex_chat_completions_translates_to_responses():
    async def run():
        captured: Dict[str, Any] = {"requests": []}
        upstream_runner, upstream_base = await _start_runner(_build_fake_upstream(captured))
        adapter = FakeAdapter(
            f"{upstream_base}/v1",
            bearer="codex-token",
            allowed=["/chat/completions", "/responses", "/models"],
            name="openai-codex",
        )
        proxy_runner, proxy_base = await _start_runner(create_app(adapter))

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{proxy_base}/v1/chat/completions",
                    json={
                        "model": "gpt-5.4-mini",
                        "messages": [
                            {"role": "system", "content": "Be concise."},
                            {"role": "user", "content": "hi"},
                        ],
                        "tools": [{"type": "function", "function": {"name": "blocked"}}],
                    },
                    headers={"Authorization": "Bearer client-dummy-key"},
                ) as resp:
                    assert resp.status == 200
                    data = await resp.json()
                    assert data["object"] == "chat.completion"
                    assert data["choices"][0]["message"]["content"] == "codex ok"
                    assert data["usage"]["total_tokens"] == 6

            assert len(captured["requests"]) == 1
            req = captured["requests"][0]
            assert req["path"] == "/v1/responses"
            assert req["auth"] == "Bearer codex-token"
            forwarded = json.loads(req["body"])
            assert forwarded["instructions"] == "Be concise."
            assert forwarded["input"] == [{"role": "user", "content": "hi"}]
            assert "tools" not in forwarded
            assert forwarded["store"] is False
            assert forwarded["stream"] is True
        finally:
            await proxy_runner.cleanup()
            await upstream_runner.cleanup()

    asyncio.run(run())


def test_server_retries_once_with_adapter_retry_credential_on_401():
    async def run():
        captured: Dict[str, Any] = {"requests": []}
        upstream_runner, upstream_base = await _start_runner(
            _build_retrying_fake_upstream(captured)
        )
        adapter = FakeAdapter(
            f"{upstream_base}/v1",
            bearer="jwt-bearer",
            retry_bearer="legacy-bearer",
        )
        proxy_runner, proxy_base = await _start_runner(create_app(adapter))

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{proxy_base}/v1/chat/completions",
                    json={"model": "Hermes-4-70B"},
                ) as resp:
                    assert resp.status == 200
                    data = await resp.json()
                    assert data["ok"] is True

            assert adapter.retry_calls == 1
            assert [req["auth"] for req in captured["requests"]] == [
                "Bearer jwt-bearer",
                "Bearer legacy-bearer",
            ]
        finally:
            await proxy_runner.cleanup()
            await upstream_runner.cleanup()

    asyncio.run(run())


def test_server_codex_chat_completions_writes_safe_proxy_log(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    async def run():
        captured: Dict[str, Any] = {"requests": []}
        upstream_runner, upstream_base = await _start_runner(_build_fake_upstream(captured))
        adapter = FakeAdapter(
            f"{upstream_base}/v1",
            bearer="codex-token-secret",
            allowed=["/chat/completions", "/responses", "/models"],
            name="openai-codex",
        )
        proxy_runner, proxy_base = await _start_runner(create_app(adapter))

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{proxy_base}/v1/chat/completions",
                    json={
                        "model": "gpt-5.4-mini",
                        "messages": [{"role": "user", "content": "private prompt must not be logged"}],
                    },
                    headers={"Authorization": "Bearer client-secret"},
                ) as resp:
                    assert resp.status == 200
                    await resp.json()
        finally:
            await proxy_runner.cleanup()
            await upstream_runner.cleanup()

    asyncio.run(run())
    log_path = tmp_path / "logs" / "proxy.log"
    lines = log_path.read_text().splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["method"] == "POST"
    assert event["path"] == "/chat/completions"
    assert event["provider"] == "openai-codex"
    assert event["model"] == "gpt-5.4-mini"
    assert event["status"] == 200
    assert event["usage"] == {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}
    assert event["upstream_request_id"] == "req_fake_123"
    assert "latency_ms" in event
    assert "private prompt" not in lines[0]
    assert "client-secret" not in lines[0]
    assert "codex-token-secret" not in lines[0]


def test_server_rejects_disallowed_path():
    async def run():
        adapter = FakeAdapter("http://unused.example/v1", allowed=["/chat/completions"])
        runner, base = await _start_runner(create_app(adapter))
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{base}/v1/random/endpoint") as resp:
                    assert resp.status == 404
                    body = await resp.json()
                    assert body["error"]["type"] == "path_not_allowed"
                    assert "/chat/completions" in body["error"]["message"]
        finally:
            await runner.cleanup()

    asyncio.run(run())


def test_server_returns_401_when_adapter_fails():
    async def run():
        adapter = FakeAdapter("http://unused.example/v1", raise_on_credential=True)
        runner, base = await _start_runner(create_app(adapter))
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(f"{base}/v1/chat/completions", json={}) as resp:
                    assert resp.status == 401
                    body = await resp.json()
                    assert body["error"]["type"] == "upstream_auth_failed"
                    assert "simulated auth failure" in body["error"]["message"]
        finally:
            await runner.cleanup()

    asyncio.run(run())


def test_server_health_endpoint():
    async def run():
        adapter = FakeAdapter("http://unused.example/v1")
        runner, base = await _start_runner(create_app(adapter))
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{base}/health") as resp:
                    assert resp.status == 200
                    body = await resp.json()
                    assert body["status"] == "ok"
                    assert body["upstream"] == "Fake Provider"
                    assert body["authenticated"] is True
        finally:
            await runner.cleanup()

    asyncio.run(run())


def test_server_streams_sse():
    async def run():
        captured: Dict[str, Any] = {"requests": []}
        upstream_runner, upstream_base = await _start_runner(_build_fake_upstream(captured))
        adapter = FakeAdapter(f"{upstream_base}/v1", allowed=["/sse"])
        proxy_runner, proxy_base = await _start_runner(create_app(adapter))
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{proxy_base}/v1/sse") as resp:
                    assert resp.status == 200
                    chunks = []
                    async for chunk in resp.content.iter_any():
                        chunks.append(chunk)
                    full = b"".join(chunks)
                    assert b"data: hello" in full
                    assert b"data: [DONE]" in full
        finally:
            await proxy_runner.cleanup()
            await upstream_runner.cleanup()

    asyncio.run(run())


def test_server_strips_client_auth_header():
    """The client's Authorization header MUST NOT reach the upstream."""
    async def run():
        captured: Dict[str, Any] = {"requests": []}
        upstream_runner, upstream_base = await _start_runner(_build_fake_upstream(captured))
        adapter = FakeAdapter(f"{upstream_base}/v1", bearer="ours")
        proxy_runner, proxy_base = await _start_runner(create_app(adapter))
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{proxy_base}/v1/chat/completions",
                    json={},
                    headers={"Authorization": "Bearer SHOULD_NOT_LEAK"},
                ) as resp:
                    await resp.read()
            assert captured["requests"][0]["auth"] == "Bearer ours"
            assert "SHOULD_NOT_LEAK" not in captured["requests"][0]["auth"]
        finally:
            await proxy_runner.cleanup()
            await upstream_runner.cleanup()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# CLI handlers
# ---------------------------------------------------------------------------


def test_cmd_proxy_status_runs(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.proxy.cli import cmd_proxy_status

    args = MagicMock()
    rc = cmd_proxy_status(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "nous" in out
    assert "Nous Portal" in out
    assert "not logged in" in out


def test_cmd_proxy_providers_runs(capsys):
    from hermes_cli.proxy.cli import cmd_proxy_list_providers

    args = MagicMock()
    rc = cmd_proxy_list_providers(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "nous" in out
    assert "Nous Portal" in out


def test_cmd_proxy_start_refuses_unknown_provider(capsys):
    from hermes_cli.proxy.cli import cmd_proxy_start

    args = MagicMock()
    args.provider = "no-such-provider"
    args.host = None
    args.port = None
    rc = cmd_proxy_start(args)
    assert rc == 2
    err = capsys.readouterr().err
    assert "no-such-provider" in err


def test_cmd_proxy_start_refuses_when_unauthenticated(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.proxy.cli import cmd_proxy_start

    args = MagicMock()
    args.provider = "nous"
    args.host = None
    args.port = None
    rc = cmd_proxy_start(args)
    assert rc == 2
    err = capsys.readouterr().err
    assert "hermes auth add nous" in err


# ---------------------------------------------------------------------------
# Regression guard for the missing `import json` (the codex chat shim crashed
# on every request — silently breaking Honcho's memory deriver, which is wired
# to this proxy via /chat/completions in the AIWerk base-agent overlay).
# The pre-existing tests never booted the HTTP app, so they missed it.
# ---------------------------------------------------------------------------

class _FakeCodexAdapter(UpstreamAdapter):
    """Minimal openai-codex adapter so create_app routes /chat/completions into
    _handle_codex_chat_completion without a real Codex OAuth login."""

    @property
    def name(self) -> str:
        return "openai-codex"

    @property
    def display_name(self) -> str:
        return "Fake Codex"

    @property
    def allowed_paths(self):
        return frozenset({"/chat/completions", "/responses", "/models"})

    def is_authenticated(self) -> bool:
        return True

    def get_credential(self) -> UpstreamCredential:
        return UpstreamCredential(bearer="fake-bearer", base_url="http://upstream.invalid")

    def get_retry_credential(self, *args, **kwargs) -> UpstreamCredential:
        return self.get_credential()

    def describe(self) -> str:
        return "fake"


def test_safe_model_from_body_parses_json():
    # Returned None on the bug (json.loads raised NameError, swallowed).
    from hermes_cli.proxy.server import _safe_model_from_body

    assert _safe_model_from_body(b'{"model": "gpt-5-codex"}') == "gpt-5-codex"
    assert _safe_model_from_body(b"") is None


def test_chat_completions_shim_does_not_crash_on_missing_json_import():
    # Boot the real aiohttp app and POST a malformed body so the codex shim
    # reaches its json.loads. With `import json` missing this raised NameError,
    # surfaced as "name 'json' is not defined" in the 400 body.
    import asyncio
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_cli.proxy.server import create_app

    app = create_app(_FakeCodexAdapter())

    async def _run():
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/v1/chat/completions", data=b"{ not valid json ")
            return resp.status, await resp.text()

    status, text = asyncio.run(_run())
    assert status == 400
    assert "is not defined" not in text
    assert "invalid json request body" in text.lower()
