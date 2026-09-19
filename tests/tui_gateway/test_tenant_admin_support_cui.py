"""Authenticated desktop approval round trips for tenant admin support."""
from __future__ import annotations

import json
import threading

import pytest

from tests.plugins.test_tenant_admin_support_plugin import _discover, _dispatch
from tests.profile_authorization_support import ADMIN, install_policy


def _apply_in_gateway(manager, server, token, notify, *, session_key="agent-policy"):
    from agent.cui_actor_context import bind_cui_actor_context, reset_cui_actor_context
    from tools import approval
    from tools.approval_context import reset_current_session_key, set_current_session_key
    server._sessions["ui-policy"] = {"session_key": session_key, "history": []}
    approval.register_gateway_notify(session_key, notify)
    output = []
    def worker():
        actor_token = bind_cui_actor_context(ADMIN)
        session_token = set_current_session_key(session_key)
        try:
            from tools.registry import registry
            output.append(json.loads(registry.dispatch("tenant_admin_support", {
                "operation": "behavior_apply", "target_profile": "employee-home",
                "preview_token": token,
            }, scope=manager.scope_key)))
        finally:
            reset_current_session_key(session_token)
            reset_cui_actor_context(actor_token)
    thread = threading.Thread(target=worker)
    thread.start()
    return thread, output


def test_apply_blocks_on_real_approval_request_and_writes_only_after_exact_accept_once(tmp_path, monkeypatch):
    install_policy(tmp_path, monkeypatch)
    manager, _entry = _discover(tmp_path, monkeypatch)
    preview = _dispatch(manager, "behavior_preview", instructions="Pause and clarify ambiguity.")
    import tui_gateway.server as server
    emitted = []
    thread, output = _apply_in_gateway(manager, server, preview["preview_token"], emitted.append)
    for _ in range(200):
        if emitted:
            break
        threading.Event().wait(0.01)
    assert emitted and emitted[0]["request_id"]
    assert thread.is_alive()
    assert _dispatch(manager, "behavior_history")["items"] == []
    response = server.handle_request({
        "id": "approve-policy", "method": "approval.respond",
        "params": {"session_id": "ui-policy", "request_id": emitted[0]["request_id"], "choice": "once"},
    })
    assert response["result"] == {"resolved": 1}
    thread.join(5)
    assert not thread.is_alive()
    assert output[0]["effective"] == "new_sessions_only"
    assert output[0]["revision"] == 1


@pytest.mark.parametrize("case", ["deny", "timeout", "wrong_request", "missing_notify"])
def test_apply_deny_timeout_wrong_request_and_missing_notify_are_no_write(tmp_path, monkeypatch, case):
    install_policy(tmp_path, monkeypatch)
    manager, _entry = _discover(tmp_path, monkeypatch)
    preview = _dispatch(manager, "behavior_preview", instructions=f"policy {case}")
    import tui_gateway.server as server
    from tools import approval_context
    monkeypatch.setattr(approval_context, "_get_approval_timeout", lambda: 0.05)
    emitted = []
    notify = None if case == "missing_notify" else emitted.append
    if notify is None:
        notify = lambda payload: (_ for _ in ()).throw(RuntimeError("notify unavailable"))
    thread, output = _apply_in_gateway(manager, server, preview["preview_token"], notify,
                                       session_key=f"agent-{case}")
    for _ in range(100):
        if emitted or not thread.is_alive():
            break
        threading.Event().wait(0.01)
    if case in {"deny", "wrong_request"} and emitted:
        request_id = "wrong" if case == "wrong_request" else emitted[0]["request_id"]
        server.handle_request({"id": case, "method": "approval.respond", "params": {
            "session_id": "ui-policy", "request_id": request_id, "choice": "deny",
        }})
    thread.join(2)
    assert not thread.is_alive()
    assert output and output[0].get("revision") is None
    assert _dispatch(manager, "behavior_history")["items"] == []
