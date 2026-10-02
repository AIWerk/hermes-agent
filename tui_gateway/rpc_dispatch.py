"""JSON-RPC admission and worker dispatch. Rebound onto the server namespace."""


from __future__ import annotations

from .method_ctx import bind_module


def handle_request(req: dict) -> dict | None:
    from hermes_cli.backend_retirement import retirement

    with retirement.work() as admitted:
        if not admitted:
            return _err(req.get("id"), 5035, "backend is retiring; reconnect to continue")  # noqa: F821
        return _handle_admitted_request(req)


def _handle_admitted_request(req: dict) -> dict | None:
    normalized = _normalize_request(req)  # noqa: F821
    if isinstance(normalized, dict):
        return normalized
    rid, method, params = normalized
    if isinstance(params, dict):
        params = {
            key: value for key, value in params.items()
            if key != "actor_context" and not key.startswith("_cui_")
        }
    if not (fn := _methods.get(method)):  # noqa: F821
        return _err(rid, -32601, f"unknown method: {method} — the client and the Hermes backend are out of sync "  # noqa: F821
                    "(different versions); run `hermes update` and restart both")
    # Test doubles register straight into ``_methods`` without a contract; every production
    # handler comes through ``register_method`` and therefore has one.
    contract = _contracts.METHODS.get(method)  # noqa: F821
    token = _current_rpc_method.set(method)  # noqa: F821
    try:
        response = fn(rid, params)
    except ProfileUnavailableError as exc:  # noqa: F821
        return _err(rid, 4064, str(exc))  # noqa: F821
    finally:
        _current_rpc_method.reset(token)  # noqa: F821
    if contract is not None and isinstance(response, dict) and isinstance(response.get("result"), dict):
        _contracts.check_result(contract, response["result"])  # noqa: F821
    return response


def dispatch(
    req: dict, transport: Optional[Transport] = None, actor_context: dict | None = None  # noqa: F821
) -> dict | None:
    """Route inbound RPCs — long handlers to the pool (returns None; the worker writes its own
    response via the bound transport), everything else inline (returns the response dict).
    *transport* pins every write of this request — events included — to that transport;
    omitted → the module stdio transport (``tui_gateway.entry`` behaviour)."""
    t = transport or _stdio_transport  # noqa: F821
    token = bind_transport(t)  # noqa: F821
    actor_token = _apply_cui_actor_env(actor_context)  # noqa: F821
    try:
        from tui_gateway import server_requests
        if server_requests.is_response_frame(req):
            # The renderer answering one of OUR requests (clarify, approval, …): no response frame goes back.
            if not server_requests.resolve_response(req) and not _relay_compute_host_response(req):  # noqa: F821
                logger.debug("dropping response for unknown server request id=%r", req.get("id"))  # noqa: F821
            return None
        normalized = _normalize_request(req)  # noqa: F821
        if isinstance(normalized, dict):
            return normalized
        if normalized[1] not in _LONG_HANDLERS:  # noqa: F821
            response = handle_request(req)
            _store_created_session_actor(req, response)  # noqa: F821
            return response
        from hermes_cli.backend_retirement import retirement

        # Reserve BEFORE enqueueing: a queued handler has accepted work even though no worker runs yet.
        if not retirement.acquire():
            return _err(req.get("id"), 5035, "backend is retiring; reconnect to continue")  # noqa: F821
        try:
            ctx = contextvars.copy_context()  # the pool worker must see the bound transport  # noqa: F821
            owner = normalized[2].get("owner")
            if normalized[1] in _CONNECTOR_RPC_METHODS and isinstance(owner, dict) and owner.get("type") == "session":  # noqa: F821
                ctx.run(_capture_connector_rpc_owner, normalized[2])  # noqa: F821

            def run():
                try:
                    resp = _handle_admitted_request(req)
                    _store_created_session_actor(req, resp)  # noqa: F821
                except Exception as exc:
                    resp = _err(req.get("id"), -32000, f"handler error: {exc}")  # noqa: F821
                if resp is not None:
                    t.write(resp)
            future = _pool.submit(lambda: ctx.run(run))  # noqa: F821
        except BaseException:
            retirement.release()
            raise
        # Also releases cancelled queued futures; the worker's own finally would never execute.
        future.add_done_callback(lambda _: retirement.release())
        return None
    finally:
        _clear_cui_actor_env(actor_token)  # noqa: F821
        reset_transport(token)  # noqa: F821


def register(server):
    bind_module(globals(), server)
