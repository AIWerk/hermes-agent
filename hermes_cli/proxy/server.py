"""HTTP server that forwards OpenAI-compatible requests to a configured upstream.

Listens on ``http://<host>:<port>/v1/<path>`` and forwards each request to
``<upstream-base-url>/<path>`` with the client's ``Authorization`` header
replaced by a freshly-resolved bearer from the configured adapter. The
response is streamed back unmodified, preserving SSE.

The server is intentionally minimal: it does NOT mediate, log, transform,
or rewrite request/response bodies. It's a credential-attaching forwarder.
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import time
from typing import Any, Optional

try:
    import aiohttp
    from aiohttp import web
    AIOHTTP_AVAILABLE = True
except ImportError:
    aiohttp = None  # type: ignore[assignment]
    web = None  # type: ignore[assignment]
    AIOHTTP_AVAILABLE = False

from hermes_cli.proxy.adapters.base import UpstreamAdapter, UpstreamCredential

logger = logging.getLogger(__name__)
_ACCESS_LOG_NAME = "proxy.log"

# Headers we strip when forwarding to the upstream. ``host``/``content-length``
# are recomputed by aiohttp; ``authorization`` is replaced with our bearer.
# Everything else (content-type, accept, user-agent, x-* headers) passes through.
_HOP_BY_HOP_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "authorization",  # we replace this one
    }
)

DEFAULT_PORT = 8645
DEFAULT_HOST = "127.0.0.1"


def _json_error(status: int, message: str, code: str = "proxy_error") -> "web.Response":
    """Return an OpenAI-style error JSON response."""
    body = {"error": {"message": message, "type": code, "code": code}}
    return web.json_response(body, status=status)


def _filter_request_headers(headers: "aiohttp.typedefs.LooseHeaders") -> dict:
    """Strip hop-by-hop + auth headers from the inbound request."""
    out = {}
    for key, value in headers.items():
        if key.lower() in _HOP_BY_HOP_HEADERS:
            continue
        out[key] = value
    return out


def _filter_response_headers(headers) -> dict:
    """Strip hop-by-hop headers from the upstream response."""
    out = {}
    for key, value in headers.items():
        if key.lower() in _HOP_BY_HOP_HEADERS:
            continue
        # aiohttp recomputes Content-Encoding/Content-Length on stream — let it.
        if key.lower() in {"content-encoding", "content-length"}:
            continue
        out[key] = value
    return out


def _usage_from_payload(payload: Any) -> dict[str, int]:
    if not isinstance(payload, dict):
        return {}
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return {}
    out: dict[str, int] = {}
    mapping = {
        "prompt_tokens": "prompt_tokens",
        "completion_tokens": "completion_tokens",
        "total_tokens": "total_tokens",
        "input_tokens": "input_tokens",
        "output_tokens": "output_tokens",
    }
    for src, dst in mapping.items():
        val = usage.get(src)
        if isinstance(val, (int, float)):
            out[dst] = int(val)
    return out


def _request_id_from_headers(headers: Any) -> Optional[str]:
    for key in ("x-oai-request-id", "openai-request-id", "x-request-id"):
        try:
            val = headers.get(key)
        except Exception:
            val = None
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _safe_model_from_body(body: bytes) -> Optional[str]:
    if not body:
        return None
    try:
        parsed = json.loads(body.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(parsed, dict):
        return None
    model = parsed.get("model")
    return str(model).strip() if model else None


def _log_proxy_event(
    *,
    method: str,
    path: str,
    provider: str,
    status: int,
    start_time: float,
    model: Optional[str] = None,
    usage: Optional[dict[str, int]] = None,
    upstream_request_id: Optional[str] = None,
    error_code: Optional[str] = None,
) -> None:
    """Append one safe JSONL proxy access event without request or token data."""
    try:
        from hermes_constants import get_hermes_home
        log_dir = get_hermes_home() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        event: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "method": method,
            "path": path,
            "provider": provider,
            "status": int(status),
            "latency_ms": int((time.monotonic() - start_time) * 1000),
        }
        if model:
            event["model"] = model
        if usage:
            event["usage"] = usage
        if upstream_request_id:
            event["upstream_request_id"] = upstream_request_id
        if error_code:
            event["error_code"] = error_code
        with (log_dir / _ACCESS_LOG_NAME).open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
    except Exception as exc:
        logger.debug("proxy: failed to write access log: %s", exc)


async def _handle_codex_chat_completion(
    *,
    request: "web.Request",
    upstream_url: str,
    body: bytes,
    headers: dict,
    provider: str,
    start_time: float,
) -> "web.Response":
    """Serve /v1/chat/completions by translating to Codex /responses.

    This path exists for OpenAI-compatible clients such as Honcho. It is a
    strict no-tools shim: tools from the caller are intentionally not forwarded
    to the Codex backend.
    """
    try:
        incoming: Any = json.loads(body.decode("utf-8") if body else "{}")
        if not isinstance(incoming, dict):
            raise ValueError("request JSON must be an object")
    except Exception as exc:
        _log_proxy_event(
            method=request.method,
            path="/chat/completions",
            provider=provider,
            status=400,
            start_time=start_time,
            model=_safe_model_from_body(body),
            error_code="invalid_request",
        )
        return _json_error(400, f"invalid JSON request body: {exc}", code="invalid_request")

    model = str(incoming.get("model") or "").strip() or None
    if incoming.get("stream") is True:
        _log_proxy_event(
            method=request.method,
            path="/chat/completions",
            provider=provider,
            status=400,
            start_time=start_time,
            model=model,
            error_code="stream_not_supported",
        )
        return _json_error(
            400,
            "The OpenAI-compatible Codex proxy chat shim does not support stream=true. Use non-streaming chat.completions or call /v1/responses directly.",
            code="stream_not_supported",
        )

    try:
        from hermes_cli.proxy.adapters.openai_codex import (
            chat_payload_to_responses_payload,
            responses_payload_to_chat_completion,
            responses_stream_to_payload,
        )
        responses_payload = chat_payload_to_responses_payload(incoming)
    except Exception as exc:
        _log_proxy_event(
            method=request.method,
            path="/chat/completions",
            provider=provider,
            status=400,
            start_time=start_time,
            model=model,
            error_code="translation_failed",
        )
        return _json_error(400, f"failed to translate chat request: {exc}", code="translation_failed")

    timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=300)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                upstream_url,
                json=responses_payload,
                headers=headers,
                allow_redirects=False,
            ) as upstream_resp:
                raw = await upstream_resp.read()
                if upstream_resp.status >= 400:
                    _log_proxy_event(
                        method=request.method,
                        path="/chat/completions",
                        provider=provider,
                        status=upstream_resp.status,
                        start_time=start_time,
                        model=model,
                        upstream_request_id=_request_id_from_headers(upstream_resp.headers),
                        error_code="upstream_error",
                    )
                    return web.Response(
                        status=upstream_resp.status,
                        body=raw,
                        headers=_filter_response_headers(upstream_resp.headers),
                    )
                try:
                    content_type = upstream_resp.headers.get("content-type", "")
                    stripped = raw.lstrip()
                    if (
                        "text/event-stream" in content_type.lower()
                        or stripped.startswith(b"event:")
                        or stripped.startswith(b"data:")
                    ):
                        upstream_json = responses_stream_to_payload(raw)
                    else:
                        upstream_json = json.loads(raw.decode("utf-8") if raw else "{}")
                    if not isinstance(upstream_json, dict):
                        raise ValueError("response JSON must be an object")
                except Exception as exc:
                    _log_proxy_event(
                        method=request.method,
                        path="/chat/completions",
                        provider=provider,
                        status=502,
                        start_time=start_time,
                        model=model,
                        upstream_request_id=_request_id_from_headers(upstream_resp.headers),
                        error_code="upstream_invalid_response",
                    )
                    return _json_error(502, f"invalid Codex response: {exc}", code="upstream_invalid_response")
    except aiohttp.ClientError as exc:
        logger.warning("proxy: Codex upstream connection failed: %s", exc)
        _log_proxy_event(
            method=request.method,
            path="/chat/completions",
            provider=provider,
            status=502,
            start_time=start_time,
            model=model,
            error_code="upstream_unreachable",
        )
        return _json_error(502, f"upstream connection failed: {exc}", code="upstream_unreachable")
    except asyncio.TimeoutError:
        _log_proxy_event(
            method=request.method,
            path="/chat/completions",
            provider=provider,
            status=504,
            start_time=start_time,
            model=model,
            error_code="upstream_timeout",
        )
        return _json_error(504, "upstream request timed out", code="upstream_timeout")

    chat_payload = responses_payload_to_chat_completion(
        upstream_json,
        model=str(incoming.get("model") or responses_payload.get("model") or ""),
    )
    _log_proxy_event(
        method=request.method,
        path="/chat/completions",
        provider=provider,
        status=200,
        start_time=start_time,
        model=model,
        usage=_usage_from_payload(chat_payload),
        upstream_request_id=_request_id_from_headers(upstream_resp.headers),
    )
    return web.json_response(chat_payload)


def create_app(adapter: UpstreamAdapter) -> "web.Application":
    """Build the aiohttp application bound to a specific upstream adapter."""
    if not AIOHTTP_AVAILABLE:
        raise RuntimeError(
            "aiohttp is required for `hermes proxy`. Install with: "
            "pip install 'hermes-agent[messaging]' or `pip install aiohttp`."
        )

    app = web.Application()
    # AppKey ensures forward-compat with future aiohttp versions that strip
    # bare-string keys.
    _adapter_key = web.AppKey("adapter", UpstreamAdapter)
    app[_adapter_key] = adapter

    async def handle_health(request: "web.Request") -> "web.Response":
        return web.json_response(
            {
                "status": "ok",
                "upstream": adapter.display_name,
                "authenticated": adapter.is_authenticated(),
            }
        )

    async def handle_proxy(request: "web.Request") -> "web.StreamResponse":
        start_time = time.monotonic()
        # Extract the path *after* /v1
        rel_path = request.match_info.get("tail", "")
        rel_path = "/" + rel_path.lstrip("/")

        if rel_path not in adapter.allowed_paths:
            allowed = ", ".join(sorted(adapter.allowed_paths))
            _log_proxy_event(
                method=request.method,
                path=rel_path,
                provider=adapter.name,
                status=404,
                start_time=start_time,
                error_code="path_not_allowed",
            )
            return _json_error(
                404,
                f"Path /v1{rel_path} is not forwarded by this proxy. "
                f"Allowed: {allowed}",
                code="path_not_allowed",
            )

        try:
            cred = adapter.get_credential()
        except Exception as exc:
            logger.warning("proxy: credential resolution failed: %s", exc)
            _log_proxy_event(
                method=request.method,
                path=rel_path,
                provider=adapter.name,
                status=401,
                start_time=start_time,
                error_code="upstream_auth_failed",
            )
            return _json_error(401, str(exc), code="upstream_auth_failed")

        # Forward body verbatim. Read into memory once — request bodies for
        # chat/completions/embeddings are small (<1MB typically). If we ever
        # need to forward large multipart uploads we'll switch to streaming
        # the request body too.
        body = await request.read()

        timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=300)

        def _headers_for_credential(active_cred: UpstreamCredential) -> dict:
            fwd_headers = _filter_request_headers(request.headers)
            extra_headers = getattr(active_cred, "headers", None)
            if isinstance(extra_headers, dict):
                for key, value in extra_headers.items():
                    if key.lower() != "authorization":
                        fwd_headers[key] = value
            fwd_headers["Authorization"] = f"{active_cred.token_type} {active_cred.bearer}"
            return fwd_headers

        if adapter.name == "openai-codex" and rel_path == "/chat/completions":
            return await _handle_codex_chat_completion(
                request=request,
                upstream_url=f"{cred.base_url.rstrip('/')}/responses",
                body=body,
                headers=_headers_for_credential(cred),
                provider=adapter.name,
                start_time=start_time,
            )

        async def _send_upstream(active_cred: UpstreamCredential):
            upstream_url = f"{active_cred.base_url.rstrip('/')}{rel_path}"
            # Preserve query string verbatim.
            if request.query_string:
                upstream_url = f"{upstream_url}?{request.query_string}"

            fwd_headers = _headers_for_credential(active_cred)

            logger.debug(
                "proxy: forwarding %s %s -> %s (body=%d bytes)",
                request.method, rel_path, upstream_url, len(body),
            )

            try:
                session = aiohttp.ClientSession(timeout=timeout)
            except Exception as exc:  # pragma: no cover - aiohttp setup issue
                raise RuntimeError(f"proxy session init failed: {exc}") from exc

            try:
                upstream_resp = await session.request(
                    request.method,
                    upstream_url,
                    data=body if body else None,
                    headers=fwd_headers,
                    allow_redirects=False,
                )
            except Exception:
                await session.close()
                raise
            return session, upstream_resp

        async def _open_upstream(active_cred: UpstreamCredential):
            try:
                return await _send_upstream(active_cred)
            except RuntimeError as exc:
                _log_proxy_event(
                    method=request.method,
                    path=rel_path,
                    provider=adapter.name,
                    status=500,
                    start_time=start_time,
                    model=_safe_model_from_body(body),
                    error_code="proxy_session_init_failed",
                )
                return _json_error(500, str(exc)), None
            except aiohttp.ClientError as exc:
                logger.warning("proxy: upstream connection failed: %s", exc)
                _log_proxy_event(
                    method=request.method,
                    path=rel_path,
                    provider=adapter.name,
                    status=502,
                    start_time=start_time,
                    model=_safe_model_from_body(body),
                    error_code="upstream_unreachable",
                )
                return (
                    _json_error(
                        502,
                        f"upstream connection failed: {exc}",
                        code="upstream_unreachable",
                    ),
                    None,
                )
            except asyncio.TimeoutError:
                _log_proxy_event(
                    method=request.method,
                    path=rel_path,
                    provider=adapter.name,
                    status=504,
                    start_time=start_time,
                    model=_safe_model_from_body(body),
                    error_code="upstream_timeout",
                )
                return (
                    _json_error(
                        504,
                        "upstream request timed out",
                        code="upstream_timeout",
                    ),
                    None,
                )

        session_or_response, upstream_resp = await _open_upstream(cred)
        if upstream_resp is None:
            return session_or_response
        session = session_or_response

        if upstream_resp.status in {401, 429}:
            try:
                retry_cred = adapter.get_retry_credential(
                    failed_credential=cred,
                    status_code=upstream_resp.status,
                )
            except Exception as exc:
                logger.warning("proxy: retry credential resolution failed: %s", exc)
                retry_cred = None

            if retry_cred is not None:
                upstream_resp.release()
                await session.close()
                session_or_response, upstream_resp = await _open_upstream(retry_cred)
                if upstream_resp is None:
                    return session_or_response
                session = session_or_response

        # Stream response back. Headers first, then chunked body.
        resp = web.StreamResponse(
            status=upstream_resp.status,
            headers=_filter_response_headers(upstream_resp.headers),
        )
        await resp.prepare(request)

        interrupted = False
        try:
            async for chunk in upstream_resp.content.iter_any():
                if chunk:
                    await resp.write(chunk)
        except (aiohttp.ClientError, asyncio.CancelledError) as exc:
            interrupted = True
            logger.warning("proxy: streaming interrupted: %s", exc)
            _log_proxy_event(
                method=request.method,
                path=rel_path,
                provider=adapter.name,
                status=upstream_resp.status,
                start_time=start_time,
                model=_safe_model_from_body(body),
                upstream_request_id=_request_id_from_headers(upstream_resp.headers),
                error_code="streaming_interrupted",
            )
        finally:
            upstream_resp.release()
            await session.close()

        await resp.write_eof()
        if not interrupted:
            _log_proxy_event(
                method=request.method,
                path=rel_path,
                provider=adapter.name,
                status=upstream_resp.status,
                start_time=start_time,
                model=_safe_model_from_body(body),
                upstream_request_id=_request_id_from_headers(upstream_resp.headers),
                error_code="upstream_error" if upstream_resp.status >= 400 else None,
            )
        return resp

    # /health doesn't go through the upstream
    app.router.add_get("/health", handle_health)
    # Catch-all under /v1 — forwards if the path is allowed.
    app.router.add_route("*", "/v1/{tail:.*}", handle_proxy)

    return app


async def run_server(
    adapter: UpstreamAdapter,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    shutdown_event: Optional[asyncio.Event] = None,
) -> None:
    """Run the proxy in the current event loop until shutdown_event is set.

    If shutdown_event is None, runs until cancelled (Ctrl+C or SIGTERM).
    """
    if not AIOHTTP_AVAILABLE:
        raise RuntimeError(
            "aiohttp is required for `hermes proxy`. Install with: "
            "pip install 'hermes-agent[messaging]' or `pip install aiohttp`."
        )

    app = create_app(adapter)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()

    logger.info(
        "proxy: listening on http://%s:%d/v1 -> %s",
        host, port, adapter.display_name,
    )

    stop_event = shutdown_event or asyncio.Event()

    # Wire signal handlers when we own the loop's lifetime.
    if shutdown_event is None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop_event.set)  # windows-footgun: ok
            except NotImplementedError:
                # Windows / restricted environments — Ctrl+C will still
                # raise KeyboardInterrupt and unwind us.
                pass

    try:
        await stop_event.wait()
    finally:
        logger.info("proxy: shutting down")
        await runner.cleanup()


__all__ = [
    "create_app",
    "run_server",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "AIOHTTP_AVAILABLE",
]
