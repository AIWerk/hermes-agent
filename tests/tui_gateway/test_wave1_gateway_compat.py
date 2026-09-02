"""Wave 1 compatibility regressions for canonical TUI gateway owners."""

import os
from pathlib import Path
import threading

from agent.cui_actor_context import (
    bind_cui_actor_context,
    current_cui_actor_context,
    reset_cui_actor_context,
)
import tui_gateway.server as server


def test_apply_and_clear_cui_actor_compatibility_wrapper_is_context_local():
    before = {key: os.environ.get(key) for key in os.environ if "CUI_ACTOR" in key}
    token = server._apply_cui_actor_env(
        {
            "tenant_id": "tenant-a",
            "actor_id": "actor-a",
            "role": "customer",
            "untrusted": "drop-me",
        }
    )
    try:
        assert current_cui_actor_context() == {
            "tenant_id": "tenant-a",
            "actor_id": "actor-a",
            "role": "customer",
        }
        assert {key: os.environ.get(key) for key in os.environ if "CUI_ACTOR" in key} == before
    finally:
        server._clear_cui_actor_env(token)
    assert current_cui_actor_context() == {}


def test_display_tool_args_compatibility_wrapper_is_fail_closed():
    projected = server._display_tool_args(
        "browser_type",
        {"text": "Authorization: Bearer top-secret", "ref": "field-1"},
    )
    assert "top-secret" not in repr(projected)
    assert server._display_tool_args("unknown_tool", {"secret": "value"}) == {}


def test_skill_visibility_compatibility_ignores_client_role_spoof():
    token = bind_cui_actor_context(
        {"tenant_id": "tenant-a", "actor_id": "actor-a", "role": "customer"}
    )
    try:
        assert server._cui_actor_role_from_params({"actor_role": "admin"}) == "customer"
        assert server._cui_actor_is_admin({"actor_role": "admin"}) is False
        assert server._skill_visible_for_actor({}, {"actor_role": "admin"}) is False
        assert server._skill_visible_for_actor(
            {"visibility": "customer"}, {"actor_role": "admin"}
        ) is True
    finally:
        reset_cui_actor_context(token)


def test_outbound_fragment_compatibility_buffers_paths_and_preserves_safe_uri():
    emitted, state = server._project_outbound_stream_fragment(
        ("", False), "/home/customer/sec"
    )
    assert emitted == ""
    assert state[0]

    emitted, state = server._project_outbound_stream_fragment(state, "ret.txt\n")
    assert "/home/customer/secret.txt" not in emitted
    assert state == ("", False)

    emitted, state = server._project_outbound_stream_fragment(
        ("", False), "See https://example.com/a"
    )
    assert emitted == "See "
    emitted_tail, state = server._project_outbound_stream_fragment(state, "?x=1 done")
    assert emitted_tail.startswith("https://example.com/a?x=1")


def test_shared_uri_parser_and_materializer_reject_traversal_and_deduplicate(
    monkeypatch,
):
    from hermes_cli import web_server

    calls = []
    monkeypatch.setattr(web_server, "load_config", lambda: {"ok": True})
    monkeypatch.setattr(
        web_server,
        "_create_shared_file_attachment",
        lambda config, item, session_id: calls.append((config, item, session_id))
        or {"path": item["reference_uri"]},
    )

    assert server._shared_uri_relative_path("shared://docs/report.pdf") == "docs/report.pdf"
    assert server._shared_uri_relative_path("shared://../secret") is None
    assert server._shared_uri_relative_path("shared://docs/%2e%2e/secret") is None

    attachments = server._shared_uri_prompt_attachments(
        "Use shared://docs/report.pdf and shared://docs/report.pdf", "session-a"
    )
    assert attachments == [{"path": "shared://docs/report.pdf"}]
    assert len(calls) == 1


def test_inbound_image_preview_stays_inside_upload_root(tmp_path, monkeypatch):
    upload_root = tmp_path / "uploads"
    upload_root.mkdir()
    image = upload_root / "photo.png"
    image.write_bytes(b"png")
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"png")
    monkeypatch.setattr(server, "_DASHBOARD_UPLOAD_ROOT", upload_root.resolve())

    text = (
        f"vision_analyze using image_url: {image}\n"
        f"vision_analyze using image_url: {outside}\n"
    )
    payloads = server._inbound_image_attachment_payloads(text)
    assert [Path(item["path"]) for item in payloads] == [image.resolve()]


def test_history_projection_restores_inbound_image_preview(tmp_path, monkeypatch):
    upload_root = tmp_path / "uploads"
    upload_root.mkdir()
    image = upload_root / "photo.png"
    image.write_bytes(b"png")
    monkeypatch.setattr(server, "_DASHBOARD_UPLOAD_ROOT", upload_root.resolve())

    messages = server._history_to_messages(
        [
            {
                "role": "user",
                "content": f"vision_analyze using image_url: {image}",
            }
        ]
    )

    assert messages[0]["attachments"][0]["path"] == str(image.resolve())


def test_linked_gateway_row_is_visible_only_to_its_configured_actor(monkeypatch):
    actor = {
        "tenant_id": "tenant-a",
        "actor_id": "actor-a",
        "user_id": "alice",
        "role": "user",
    }
    monkeypatch.setattr(
        server,
        "_load_dashboard_user_config",
        lambda: {
            "dashboard": {
                "basic_auth": {
                    "users": [
                        {
                            "username": "alice",
                            "actor_id": "actor-a",
                            "telegram_user_ids": ["123"],
                        }
                    ]
                }
            }
        },
    )
    row = {"source": "telegram", "user_id": "123"}

    assert server._cui_actor_owns_gateway_row(row, actor) is True
    assert server._row_visible_to_cui_actor(row, actor) is True
    assert (
        server._row_visible_to_cui_actor(
            row, {**actor, "actor_id": "actor-b", "user_id": "bob"}
        )
        is False
    )


def test_live_session_compatibility_lookup_preserves_visibility(monkeypatch):
    owned = {
        "session_key": "stored-a",
        "cui_actor_context": {
            "tenant_id": "tenant-a",
            "actor_id": "actor-a",
            "role": "user",
        },
    }
    server._sessions["runtime-a"] = owned
    token = server.bind_cui_actor_context(
        {"tenant_id": "tenant-a", "actor_id": "actor-a", "role": "user"}
    )
    try:
        assert server._find_live_session("stored-a") == ("runtime-a", owned)
        assert server._live_session_visible_to_cui_actor(
            owned, server.current_cui_actor_context()
        ) is True
    finally:
        server.reset_cui_actor_context(token)
        server._sessions.pop("runtime-a", None)


def test_cui_reset_bg_process_zero_blocks_forever(monkeypatch):
    calls = []
    old_key = "20260701_010000_old"

    class FakeDB:
        def get_session(self, session_id):
            return (
                {"id": old_key, "started_at": 1_000.0, "ended_at": None}
                if session_id == old_key
                else None
            )

        def get_messages(self, _session_id):
            return []

    import tools.process_registry as process_registry_module

    monkeypatch.setattr(
        server,
        "_load_cfg",
        lambda: {
            "session_reset": {
                "mode": "idle",
                "idle_minutes": 1,
                "bg_process_max_age_hours": 0,
            }
        },
    )
    monkeypatch.setattr(server.time, "time", lambda: 2_000.0)
    monkeypatch.setattr(
        process_registry_module.process_registry,
        "has_active_for_session",
        lambda session_key, max_active_age=None: calls.append(
            (session_key, max_active_age)
        )
        or True,
    )

    assert server._cui_session_reset_reason(FakeDB(), old_key) is None
    assert calls == [(old_key, None)]


def test_cui_expired_session_rotates_in_place(monkeypatch):
    ended = []
    closed_workers = []
    old_key = "20260701_010000_old"

    class FakeDB:
        def get_session(self, session_id):
            return (
                {"id": old_key, "started_at": 1_000.0, "ended_at": None}
                if session_id == old_key
                else None
            )

        def get_messages(self, _session_id):
            return [{"timestamp": 1_000.0}]

        def end_session(self, session_id, reason):
            ended.append((session_id, reason))

    class Worker:
        def close(self):
            closed_workers.append(True)

    class DbContext:
        def __enter__(self):
            return FakeDB()

        def __exit__(self, *_args):
            return False

    session = {
        "agent": None,
        "history": [{"role": "user", "content": "old"}],
        "history_lock": threading.Lock(),
        "session_key": old_key,
        "slash_worker": Worker(),
        "cwd": os.getcwd(),
        "source": "tui",
        "running": False,
        "active_session_lease": None,
    }
    server._sessions["live-old"] = session
    try:
        monkeypatch.setattr(
            server,
            "_load_cfg",
            lambda: {"session_reset": {"mode": "idle", "idle_minutes": 1}},
        )
        monkeypatch.setattr(server.time, "time", lambda: 2_000.0)
        monkeypatch.setattr(server, "_session_db", lambda _session: DbContext())
        monkeypatch.setattr(
            server, "_transfer_active_session_slot", lambda *a, **k: True
        )
        monkeypatch.setattr(server, "_register_session_cwd", lambda *a, **k: None)
        monkeypatch.setattr(server, "_notify_session_boundary", lambda *a, **k: None)
        monkeypatch.setattr(server, "_emit", lambda *a, **k: None)

        rotated = server._rotate_cui_session_if_expired("live-old", session)

        assert rotated is not None
        assert ended == [(old_key, "session_reset")]
        assert closed_workers == [True]
        assert session["session_key"] != old_key
        assert session["history"] == []
        assert session["auto_reset_from"] == old_key
        assert session["auto_reset_reason"] == "idle"
    finally:
        server._sessions.pop("live-old", None)


def test_cap_rejected_prompt_does_not_rotate_or_process_attachments(monkeypatch):
    session = {}
    touched = []
    monkeypatch.setattr(server, "_sess_nowait", lambda _params, _rid: (session, None))
    monkeypatch.setattr(
        server,
        "_ensure_active_session_slot",
        lambda _sid, _session: "active session limit (1/1)",
    )
    monkeypatch.setattr(
        server,
        "_rotate_cui_session_if_expired",
        lambda *_args: touched.append("rotate"),
    )
    monkeypatch.setattr(
        server,
        "_shared_uri_prompt_attachments",
        lambda *_args: touched.append("shared-uri") or [],
    )
    monkeypatch.setattr(
        server,
        "_process_prompt_attachments",
        lambda *_args: touched.append("attachments") or ([], ""),
    )

    response = server._methods["prompt.submit"](
        "r-cap",
        {"session_id": "live-expired", "text": "hello", "attachments": [{}]},
    )

    assert response["error"]["code"] == 4090
    assert touched == []
