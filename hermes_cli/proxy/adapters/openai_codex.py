"""OpenAI Codex OAuth upstream adapter.

This adapter lets the local Hermes proxy expose a small OpenAI-compatible
surface backed by the ChatGPT Codex OAuth endpoint. It is intentionally scoped
for auxiliary/backend consumers such as Honcho:

- each Hermes profile owns its own auth store and therefore its own Codex login
- no Hermes agent loop is invoked
- no tools are forwarded through the chat shim
- request bodies are not stored by Hermes
"""

from __future__ import annotations

import base64
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, FrozenSet, Optional

from hermes_cli.auth import DEFAULT_CODEX_BASE_URL, resolve_codex_runtime_credentials
from hermes_cli.proxy.adapters.base import UpstreamAdapter, UpstreamCredential


def _pooled_codex_credential() -> dict[str, Any]:
    """Return the active OpenAI Codex credential from the pooled auth store.

    Some installations keep valid Codex OAuth entries only in the credential
    pool while providers.openai-codex.tokens is empty or stale. The normal
    Hermes chat path already handles that. The proxy must do the same so
    OpenAI-compatible clients such as Honcho can use the working OAuth route.
    """
    try:
        from agent.credential_pool import load_pool
    except Exception as exc:  # pragma: no cover - import/env failure
        raise RuntimeError("OpenAI Codex credential pool is not available") from exc

    pool = load_pool("openai-codex")
    now = time.time()

    # Only consider entries the pool itself reports as available. This honors
    # last_status (STATUS_EXHAUSTED / STATUS_DEAD) and the last_error_reset_at
    # cooldown window, so we never re-hand a rate-limited (429) or revoked (401)
    # credential straight back to the upstream just because its JWT has not yet
    # expired. Falling back to _entries here would resurrect a key the pool's
    # own rotation just retired.
    try:
        entries = list(pool._available_entries())
    except Exception:
        # Defensive: if the pool internals change, degrade to peek() rather than
        # touch the private _entries list (which ignores exhaustion state).
        entries = []

    def _score(entry: Any) -> tuple[int, int]:
        token = str(getattr(entry, "runtime_api_key", "") or "")
        claims = _jwt_claims(token)
        exp = claims.get("exp")
        valid = isinstance(exp, (int, float)) and float(exp) > now + 120
        return (1 if valid else 0, int(float(exp)) if isinstance(exp, (int, float)) else 0)

    candidates = [e for e in entries if str(getattr(e, "runtime_api_key", "") or "").strip()]
    candidates.sort(key=_score, reverse=True)
    # peek() also routes through current()/_available_entries(), so the fallback
    # likewise stays within the pool's availability contract.
    entry = candidates[0] if candidates else pool.peek()
    if entry is None or not entry.runtime_api_key:
        raise RuntimeError("No usable OpenAI Codex pooled credential found")
    return {
        "provider": "openai-codex",
        "base_url": entry.runtime_base_url or DEFAULT_CODEX_BASE_URL,
        "api_key": entry.runtime_api_key,
        "source": entry.source,
        "last_refresh": entry.last_refresh,
        "auth_mode": "chatgpt",
    }

logger = logging.getLogger(__name__)

_ALLOWED_PATHS: FrozenSet[str] = frozenset({
    "/chat/completions",
    "/responses",
    "/models",
})


def _coerce_int(value: Any, default: int = 0) -> int:
    """Best-effort int coercion that never raises on malformed input.

    Upstream usage counts are nominally integers, but a flaky Codex response
    can ship a non-numeric string (e.g. ``"NOTANUMBER"``) or other junk. A bare
    ``int(...)`` there raises ValueError and the proxy returns an opaque 500, so
    coerce defensively and fall back to ``default`` instead.
    """
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _jwt_claims(token: str) -> Dict[str, Any]:
    try:
        parts = str(token or "").split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        decoded = base64.urlsafe_b64decode(payload.encode("utf-8"))
        claims = json.loads(decoded.decode("utf-8"))
        return claims if isinstance(claims, dict) else {}
    except Exception:
        return {}


def _codex_headers(access_token: str) -> Dict[str, str]:
    headers = {
        "User-Agent": "codex_cli_rs/0.0.0 (Hermes Agent Proxy)",
        "originator": "codex_cli_rs",
    }
    claims = _jwt_claims(access_token)
    account_id = claims.get("https://api.openai.com/auth", {}).get("chatgpt_account_id")
    if isinstance(account_id, str) and account_id.strip():
        headers["ChatGPT-Account-ID"] = account_id.strip()
    return headers


def _token_expiry_iso(access_token: str) -> Optional[str]:
    exp = _jwt_claims(access_token).get("exp")
    if isinstance(exp, (int, float)) and exp > 0:
        try:
            return datetime.fromtimestamp(float(exp), tz=timezone.utc).isoformat().replace("+00:00", "Z")
        except Exception:
            return None
    return None


def _convert_content_for_responses(content: Any) -> Any:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content) if content else ""
    converted: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype == "text":
            converted.append({"type": "input_text", "text": part.get("text", "")})
        elif ptype == "image_url":
            image_data = part.get("image_url") or {}
            url = image_data.get("url") if isinstance(image_data, dict) else None
            if isinstance(url, str) and url:
                converted.append({"type": "input_image", "image_url": url})
    return converted or ""


def chat_payload_to_responses_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Translate a chat.completions body into a no-tools Responses body."""
    messages = payload.get("messages") or []
    if not isinstance(messages, list):
        messages = []

    instructions_parts: list[str] = []
    input_messages: list[dict[str, Any]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "user")
        content = msg.get("content") or ""
        if role == "system":
            instructions_parts.append(content if isinstance(content, str) else str(content))
            continue
        if role == "tool":
            # The proxy is a no-tools boundary. Tool results from a caller are
            # treated as inert user text rather than function-call state.
            role = "user"
        if role not in {"user", "assistant", "developer"}:
            role = "user"
        input_messages.append({
            "role": role,
            "content": _convert_content_for_responses(content),
        })

    out: Dict[str, Any] = {
        "model": str(payload.get("model") or "").strip(),
        "instructions": "\n\n".join(instructions_parts) or "You are a helpful assistant.",
        "input": input_messages or [{"role": "user", "content": ""}],
        "store": False,
        "stream": True,
    }

    reasoning = payload.get("reasoning")
    extra_body = payload.get("extra_body")
    if not isinstance(reasoning, dict) and isinstance(extra_body, dict):
        maybe_reasoning = extra_body.get("reasoning")
        if isinstance(maybe_reasoning, dict):
            reasoning = maybe_reasoning
    if isinstance(reasoning, dict) and reasoning.get("enabled") is not False:
        effort = reasoning.get("effort") or "low"
        if effort == "minimal":
            effort = "low"
        out["reasoning"] = {"effort": effort, "summary": "auto"}
        out["include"] = ["reasoning.encrypted_content"]

    response_format = payload.get("response_format")
    if isinstance(response_format, dict):
        fmt_type = response_format.get("type")
        if fmt_type == "json_object":
            out["text"] = {"format": {"type": "json_object"}}
        elif fmt_type == "json_schema":
            schema_cfg = response_format.get("json_schema")
            if isinstance(schema_cfg, dict):
                fmt: Dict[str, Any] = {
                    "type": "json_schema",
                    "name": schema_cfg.get("name") or "response",
                    "schema": schema_cfg.get("schema") or {},
                }
                if "strict" in schema_cfg:
                    fmt["strict"] = bool(schema_cfg.get("strict"))
                out["text"] = {"format": fmt}

    return out


def responses_stream_to_payload(raw: bytes) -> Dict[str, Any]:
    """Collapse a Codex Responses SSE stream into one Responses payload."""
    text_parts: list[str] = []
    terminal: Optional[Dict[str, Any]] = None
    output_items: list[Any] = []
    usage: Optional[Dict[str, Any]] = None
    response_id = ""

    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            event = json.loads(data)
        except Exception:
            continue
        if not isinstance(event, dict):
            continue
        etype = str(event.get("type") or "")
        if etype == "response.output_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str):
                text_parts.append(delta)
        elif etype == "response.output_item.done":
            item = event.get("item")
            if item is not None:
                output_items.append(item)
        elif etype in {"response.completed", "response.incomplete", "response.failed"}:
            response = event.get("response")
            if isinstance(response, dict):
                terminal = response
                if isinstance(response.get("usage"), dict):
                    usage = response.get("usage")
                if isinstance(response.get("id"), str):
                    response_id = response["id"]
        elif etype == "error":
            message = event.get("message") or event.get("error") or "Codex stream emitted error"
            raise RuntimeError(str(message))

    if terminal is None:
        terminal = {"id": response_id or f"resp-codex-proxy-{int(time.time())}", "object": "response"}
    if output_items and not terminal.get("output"):
        terminal["output"] = output_items
    if text_parts and not terminal.get("output"):
        terminal["output"] = [{
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "".join(text_parts)}],
        }]
    if text_parts and not terminal.get("output_text"):
        terminal["output_text"] = "".join(text_parts)
    if usage is not None and not terminal.get("usage"):
        terminal["usage"] = usage
    return terminal


def responses_payload_to_chat_completion(payload: Dict[str, Any], model: str) -> Dict[str, Any]:
    text_parts: list[str] = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if not isinstance(part, dict):
                continue
            if part.get("type") in {"output_text", "text"}:
                text = part.get("text")
                if isinstance(text, str):
                    text_parts.append(text)
    if not text_parts:
        output_text = payload.get("output_text")
        if isinstance(output_text, str):
            text_parts.append(output_text)

    raw_usage = payload.get("usage")
    usage_in: Dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
    prompt_tokens = _coerce_int(usage_in.get("input_tokens") or usage_in.get("prompt_tokens"))
    completion_tokens = _coerce_int(usage_in.get("output_tokens") or usage_in.get("completion_tokens"))
    total_tokens = _coerce_int(usage_in.get("total_tokens")) or (prompt_tokens + completion_tokens)

    return {
        "id": payload.get("id") or f"chatcmpl-codex-proxy-{int(time.time())}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "".join(text_parts),
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        },
    }


class OpenAICodexAdapter(UpstreamAdapter):
    """Proxy upstream for the OpenAI Codex ChatGPT OAuth backend."""

    @property
    def name(self) -> str:
        return "openai-codex"

    @property
    def display_name(self) -> str:
        return "OpenAI Codex OAuth"

    @property
    def allowed_paths(self) -> FrozenSet[str]:
        return _ALLOWED_PATHS

    def is_authenticated(self) -> bool:
        # Base contract: this must be cheap with no network calls. Resolving
        # with refresh_if_expiring=False checks for a stored token without
        # triggering _refresh_codex_auth_tokens() (network) or taking the
        # cross-process auth-store lock — so `proxy start` and /health polling
        # never block on a hanging refresh or lock contention.
        try:
            creds = self._resolve_credentials(refresh_if_expiring=False)
            return bool(str(creds.get("api_key") or "").strip())
        except Exception:
            return False

    def _resolve_credentials(self, *, refresh_if_expiring: bool = True) -> dict[str, Any]:
        try:
            return resolve_codex_runtime_credentials(refresh_if_expiring=refresh_if_expiring)
        except Exception:
            try:
                return _pooled_codex_credential()
            except Exception as second_exc:
                raise RuntimeError(
                    "Not logged into OpenAI Codex. Run hermes login --provider openai-codex first."
                ) from second_exc

    def get_credential(self) -> UpstreamCredential:
        creds = self._resolve_credentials()
        access_token = str(creds.get("api_key") or "").strip()
        if not access_token:
            raise RuntimeError(
                "OpenAI Codex auth did not return an access token. Run hermes login --provider openai-codex."
            )
        base_url = str(creds.get("base_url") or DEFAULT_CODEX_BASE_URL).strip().rstrip("/")
        return UpstreamCredential(
            bearer=access_token,
            base_url=base_url,
            expires_at=_token_expiry_iso(access_token),
            headers=_codex_headers(access_token),
        )

    def get_retry_credential(
        self,
        *,
        failed_credential: UpstreamCredential,
        status_code: int,
    ) -> Optional[UpstreamCredential]:
        """Rotate to another pooled Codex credential after a 401/429.

        Without this override the adapter inherited the base no-op and the
        proxy server's retry block could never rotate for Codex, so a single
        expired/rate-limited pool entry took down every request. When the
        failed bearer is a pool entry we mark it exhausted (so a stuck/expired
        key isn't reselected) and hand back the next available pooled
        credential.

        The failed credential, however, frequently originates from the
        singleton token store (``resolve_codex_runtime_credentials`` reads the
        singleton first and only falls back to the pool). That bearer is NOT a
        pool entry, so marking it exhausted via ``mark_exhausted_and_rotate``'s
        hint-miss fallthrough would burn an *unrelated* healthy pool entry on a
        1-hour cooldown (or kill a single-entry pool entirely). We therefore
        only mark-and-rotate when the failed bearer actually matches a pool
        entry; for a non-pool failure we offer an available pool credential
        without exhausting anyone.

        This is also reused by the ``/chat/completions`` shim
        (``_handle_codex_chat_completion`` in server.py) so the documented
        Honcho consumer rotates on 401/429 too.
        """
        if status_code not in {401, 429}:
            return None

        try:
            from agent.credential_pool import load_pool
        except Exception as exc:  # pragma: no cover - import/env failure
            logger.warning("proxy: Codex credential pool unavailable for retry: %s", exc)
            return None

        try:
            pool = load_pool("openai-codex")
        except Exception as exc:
            logger.warning("proxy: failed to load Codex credential pool: %s", exc)
            return None
        if pool is None:
            return None

        failed_bearer = failed_credential.bearer or None

        # Determine whether the failed bearer is actually one of this pool's
        # entries. If it is not (the common singleton-store case), rotating via
        # mark_exhausted_and_rotate's hint-miss fallback would exhaust an
        # innocent healthy entry. Only mark/rotate for a true pool-sourced
        # failure; otherwise return a still-available pool credential as-is.
        try:
            pool_entries = list(getattr(pool, "_entries", []) or [])
        except Exception:
            pool_entries = []
        failed_is_pool_entry = bool(failed_bearer) and any(
            str(getattr(e, "runtime_api_key", "") or "") == failed_bearer
            for e in pool_entries
        )

        if failed_is_pool_entry:
            # Mark the failed key exhausted (1-hour cooldown for 429, terminal
            # handling for 401) and rotate to the next available pool entry. The
            # api_key_hint pins rotation to the bearer that actually failed even
            # when this pool was freshly loaded from disk.
            refreshed = pool.mark_exhausted_and_rotate(
                status_code=status_code,
                api_key_hint=failed_bearer,
            )
        else:
            # Non-pool (e.g. singleton) bearer failed. Do NOT exhaust any pool
            # entry — just surface an available pool credential to retry with.
            try:
                refreshed = pool.peek()
            except Exception:
                refreshed = None
        if refreshed is None:
            return None

        bearer = str(getattr(refreshed, "runtime_api_key", "") or "").strip()
        if not bearer or bearer == failed_credential.bearer:
            return None

        base_url = str(
            getattr(refreshed, "runtime_base_url", None) or DEFAULT_CODEX_BASE_URL
        ).strip().rstrip("/")
        logger.info(
            "proxy: Codex upstream returned %s; retrying with rotated pool credential",
            status_code,
        )
        return UpstreamCredential(
            bearer=bearer,
            base_url=base_url or DEFAULT_CODEX_BASE_URL,
            expires_at=_token_expiry_iso(bearer),
            headers=_codex_headers(bearer),
        )


__all__ = [
    "OpenAICodexAdapter",
    "chat_payload_to_responses_payload",
    "responses_payload_to_chat_completion",
    "responses_stream_to_payload",
]
