from collections import deque
from types import SimpleNamespace

import pytest

from acp_adapter import events
from acp_adapter import server as server_module
from acp_adapter.server import HermesACPAgent
from acp_adapter.tools import build_tool_complete


def test_acp_tool_start_exports_projection_but_retains_full_args_server_side(monkeypatch):
    captured = {}
    emitted = []

    monkeypatch.setattr(events, "make_tool_call_id", lambda: "tc-1")
    monkeypatch.setattr(
        events,
        "build_tool_start",
        lambda tool_call_id, name, args, edit_diff=None: captured.update(
            tool_call_id=tool_call_id,
            name=name,
            args=args,
        ) or "projected-update",
    )
    monkeypatch.setattr(events, "_send_update", lambda *_args: emitted.append(_args[-1]))

    ids: dict[str, deque[str]] = {}
    meta = {}
    callback = events.make_tool_progress_cb(object(), "sid", object(), ids, meta)
    full_args = {"token": "internal-secret", "path": "/private/customer.txt"}

    callback("tool.started", "future_secret_tool", "", full_args)

    assert captured == {"tool_call_id": "tc-1", "name": "future_secret_tool", "args": {}}
    assert emitted == ["projected-update"]
    assert meta["tc-1"]["args"] == full_args


def test_acp_auto_approved_edit_diff_is_redacted_before_live_render(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(events, "make_tool_call_id", lambda: "tc-edit")
    monkeypatch.setattr(
        events,
        "build_tool_start",
        lambda tool_call_id, name, args, edit_diff=None: captured.update(
            args=args,
            edit_diff=edit_diff,
        ) or "update",
    )
    monkeypatch.setattr(events, "_send_update", lambda *_args: None)
    target = tmp_path / "safe.txt"
    credential = "Bearer sk-abcdefghijklmnopqrstuvwxyz123456"
    full_args = {"path": str(target), "content": f"Authorization: {credential}\n"}
    callback = events.make_tool_progress_cb(
        object(),
        "sid",
        object(),
        {},
        {},
        edit_approval_policy_getter=lambda: ("session", str(tmp_path)),
    )

    callback("tool.started", "write_file", "", full_args)

    assert captured["args"] == {"path": str(target)}
    assert captured["edit_diff"] is not None
    assert "abcdefghijklmnopqrstuvwxyz123456" not in repr(captured["edit_diff"])


def test_acp_skill_edit_completion_redacts_credentials_from_diff(monkeypatch):
    credential = "Bearer sk-abcdefghijklmnopqrstuvwxyz123456"
    monkeypatch.setattr(
        "agent.display.extract_edit_diff",
        lambda *_args, **_kwargs: (
            "--- a/SKILL.md\n"
            "+++ b/SKILL.md\n"
            "@@ -1 +1 @@\n"
            f"-Authorization: {credential}\n"
            f"+Authorization: {credential}\n"
        ),
    )

    update = build_tool_complete(
        "tc-skill",
        "skill_manage",
        result='{"ok": true}',
        function_args={"action": "patch", "name": "demo"},
        snapshot=object(),
    )

    assert "abcdefghijklmnopqrstuvwxyz123456" not in repr(update)


def test_generic_completion_result_redacts_credentials():
    secret = "abcdefghijklmnopqrstuvwxyz123456"
    update = build_tool_complete(
        "tc-result",
        "terminal",
        {"command": "safe"},
        '{"output":"Authorization: Bearer sk-' + secret + '","exit_code":0}',
    )

    assert secret not in repr(update)


def test_acp_tool_complete_exports_projection_from_full_server_side_args(monkeypatch):
    captured = {}
    emitted = []
    monkeypatch.setattr(
        events,
        "build_tool_complete",
        lambda tool_call_id, name, *, result, function_args, snapshot: captured.update(
            tool_call_id=tool_call_id,
            name=name,
            function_args=function_args,
        ) or "complete-update",
    )
    monkeypatch.setattr(events, "_send_update", lambda *_args: emitted.append(_args[-1]))
    ids = {"future_secret_tool": deque(["tc-2"])}
    full_args = {"token": "internal-secret", "path": "/private/customer.txt"}
    meta = {"tc-2": {"args": full_args, "snapshot": None}}
    callback = events.make_step_cb(object(), "sid", object(), ids, meta)

    callback(1, [{"name": "future_secret_tool", "result": "ok"}])

    assert captured == {
        "tool_call_id": "tc-2",
        "name": "future_secret_tool",
        "function_args": {},
    }
    assert emitted == ["complete-update"]
    assert meta == {}


@pytest.mark.asyncio
async def test_acp_history_replay_projects_unknown_tool_args_before_rendering(monkeypatch):
    captured = []

    class _Conn:
        async def session_update(self, **kwargs):
            captured.append(("send", kwargs["update"]))

    monkeypatch.setattr(
        server_module,
        "build_tool_start",
        lambda tool_call_id, name, args: captured.append(("start", args)) or "start-update",
    )
    monkeypatch.setattr(
        server_module,
        "build_tool_complete",
        lambda tool_call_id, name, *, result, function_args: captured.append(
            ("complete", function_args)
        ) or "complete-update",
    )
    agent = object.__new__(HermesACPAgent)
    agent._conn = _Conn()
    state = SimpleNamespace(
        session_id="sid",
        history=[
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "tc-history",
                        "type": "function",
                        "function": {
                            "name": "future_secret_tool",
                            "arguments": '{"token":"internal-secret","path":"/private/customer.txt"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "tc-history",
                "tool_name": "future_secret_tool",
                "content": "ok",
            },
        ],
    )

    await agent._replay_session_history(state)

    assert ("start", {}) in captured
    assert ("complete", {}) in captured
    assert all("internal-secret" not in repr(item) for item in captured)
