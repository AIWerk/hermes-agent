from concurrent.futures import ThreadPoolExecutor
import errno
import hashlib
from pathlib import Path
import threading

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def disable_configured_outbound_shared_folder(monkeypatch, tmp_path):
    from tui_gateway import server

    shared_root = tmp_path / "default-shared"
    shared_root.mkdir()
    monkeypatch.setattr(server, "_resolve_outbound_shared_folder_root", lambda: shared_root)


@pytest.fixture
def client_loopback():
    from hermes_cli import web_server

    prev_host = getattr(web_server.app.state, "bound_host", None)
    prev_port = getattr(web_server.app.state, "bound_port", None)
    web_server.app.state.bound_host = "127.0.0.1"
    web_server.app.state.bound_port = 9119
    client = TestClient(web_server.app, base_url="http://127.0.0.1:9119")
    yield client
    web_server.app.state.bound_host = prev_host
    web_server.app.state.bound_port = prev_port


def test_outbound_image_attachment_payloads_extracts_media_path(tmp_path):
    from tui_gateway import server

    image_path = tmp_path / "answer.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n")

    payloads = server._outbound_image_attachment_payloads(f"Here it is: MEDIA:{image_path}")

    assert len(payloads) == 1
    payload = payloads[0]
    assert payload["name"] == "answer.png"
    assert payload["type"] == "image/png"
    assert payload["is_image"] is True
    assert payload["preview_kind"] == "image"
    assert payload["safe_renderable"] is True
    assert payload["path"] == "shared://Agent-Downloads/answer.png"
    assert payload["open_url"].startswith("/api/assistant/shared-folder/open?path=")
    assert payload["preview_url"] == payload["open_url"]


def test_outbound_image_attachment_payloads_ignores_missing_and_non_images(tmp_path):
    from tui_gateway import server

    assert server._outbound_image_attachment_payloads(f"MEDIA:{tmp_path / 'missing.png'}") == []


def test_outbound_attachment_payloads_extracts_non_image_file(tmp_path):
    from tui_gateway import server

    text_path = tmp_path / "notes.txt"
    text_path.write_text("hello", encoding="utf-8")

    payloads = server._outbound_image_attachment_payloads(f"MEDIA:{text_path}")

    assert len(payloads) == 1
    payload = payloads[0]
    assert payload["name"] == "notes.txt"
    assert payload["type"] == "text/plain"
    assert payload["is_image"] is False
    assert payload["preview_kind"] == "text"
    assert payload["safe_renderable"] is True
    assert payload["path"] == "shared://Agent-Downloads/notes.txt"
    assert payload["open_url"].startswith("/api/assistant/shared-folder/open?path=")
    assert payload["preview_url"] == payload["open_url"]


def test_outbound_json_attachment_is_file_card_without_preview(monkeypatch, tmp_path):
    from tui_gateway import server

    json_path = tmp_path / "data.json"
    json_path.write_text('{"ok": true}', encoding="utf-8")
    shared_root = tmp_path / "Hermes-Shared"
    shared_root.mkdir()
    monkeypatch.setattr(server, "_resolve_outbound_shared_folder_root", lambda: shared_root)

    payloads = server._outbound_image_attachment_payloads(f"MEDIA:{json_path}")

    assert len(payloads) == 1
    payload = payloads[0]
    assert payload["name"] == "data.json"
    assert payload["type"] == "application/json"
    assert payload["preview_kind"] == "file"
    assert payload["safe_renderable"] is False
    assert payload["preview_url"] is None
    assert payload["shared_folder_path"] == "Agent-Downloads/data.json"
    assert payload["download_url"].startswith("/api/assistant/shared-folder/open?path=")


def test_outbound_non_renderable_attachment_copies_to_shared_folder(monkeypatch, tmp_path):
    from tui_gateway import server

    source_root = tmp_path / "safe-output"
    source_root.mkdir()
    shared_root = tmp_path / "Hermes-Shared"
    shared_root.mkdir()
    json_path = source_root / "data.json"
    json_path.write_text('{"ok": true}', encoding="utf-8")
    monkeypatch.setattr(server, "_resolve_outbound_shared_folder_root", lambda: shared_root)
    monkeypatch.setattr(server, "_outbound_source_roots", lambda: (source_root,))

    payloads, text = server._outbound_attachment_payloads_and_text(
        f"Here: MEDIA:{json_path}", append_shared_links=True
    )

    assert len(payloads) == 1
    payload = payloads[0]
    shared_copy = shared_root / "Agent-Downloads" / "data.json"
    assert shared_copy.read_text(encoding="utf-8") == '{"ok": true}'
    assert payload["path"] == "shared://Agent-Downloads/data.json"
    assert payload["open_url"] == "/api/assistant/shared-folder/open?path=Agent-Downloads%2Fdata.json"
    assert payload["download_url"] == payload["open_url"]
    assert payload["preview_url"] is None
    assert payload["shared_folder_path"] == "Agent-Downloads/data.json"
    assert "Im Shared-Ordner unter Agent-Downloads abgelegt:" in text
    assert "/api/assistant/shared-folder/open?path=Agent-Downloads%2Fdata.json" in text


def test_outbound_existing_shared_root_file_is_rehomed_to_agent_downloads(monkeypatch, tmp_path):
    from tui_gateway import server

    shared_root = tmp_path / "Hermes-Shared"
    shared_root.mkdir()
    root_file = shared_root / "root-level.pptx"
    root_file.write_bytes(b"pptx")
    monkeypatch.setattr(server, "_resolve_outbound_shared_folder_root", lambda: shared_root)
    monkeypatch.setattr(server, "_outbound_source_roots", lambda: (shared_root,))

    payloads, text = server._outbound_attachment_payloads_and_text(
        f"Here: MEDIA:{root_file}", append_shared_links=True
    )

    shared_copy = shared_root / "Agent-Downloads" / "root-level.pptx"
    assert shared_copy.read_bytes() == b"pptx"
    assert payloads[0]["path"] == "shared://Agent-Downloads/root-level.pptx"
    assert payloads[0]["shared_folder_path"] == "Agent-Downloads/root-level.pptx"
    assert "Agent-Downloads%2Froot-level.pptx" in text


def test_outbound_plain_vps_path_is_replaced_with_filename(monkeypatch, tmp_path):
    from tui_gateway import server
    from hermes_cli import web_server

    source_root = tmp_path / "safe-output"
    source_root.mkdir()
    shared_root = tmp_path / "Hermes-Shared"
    shared_root.mkdir()
    image_path = source_root / "generated.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n")
    monkeypatch.setattr(server, "_resolve_outbound_shared_folder_root", lambda: shared_root)
    monkeypatch.setattr(server, "_outbound_source_roots", lambda: (source_root,))
    monkeypatch.setattr(web_server, "load_config", lambda: {})
    monkeypatch.setattr(web_server, "_create_shared_file_public_link", lambda *args, **kwargs: None)

    payloads, text = server._outbound_attachment_payloads_and_text(
        f"Created file: {image_path}", append_shared_links=True
    )

    assert len(payloads) == 1
    assert str(image_path) not in text
    assert "Created file: generated.png" in text
    assert "Agent-Downloads%2Fgenerated.png" in text


def test_outbound_rejected_local_path_is_redacted_from_customer_text(monkeypatch, tmp_path):
    from tui_gateway import server

    hermes_root = tmp_path / "hermes-home"
    hermes_root.mkdir()
    config = hermes_root / "config.yaml"
    config.write_text("provider_api_key: sk-secret\n", encoding="utf-8")
    monkeypatch.setattr(server, "_hermes_home", hermes_root)
    monkeypatch.setattr(server.tempfile, "gettempdir", lambda: str(tmp_path))

    payloads, text = server._outbound_attachment_payloads_and_text(
        f"Created file: MEDIA:{config}", append_shared_links=True
    )

    assert payloads == []
    assert str(config) not in text
    assert "Created file: config.yaml" in text


def test_outbound_unrecognized_absolute_paths_are_redacted_from_customer_text():
    from tui_gateway import server

    raw_paths = (
        "/home/alice/private.bin",
        "/workspace/project/private",
        "/data/reports/private",
        "/Volumes/Shared/private",
        "/nix/store/abc/private",
        "C:/Users/Alice/private",
        r"\\server\share\private",
        "/home/alice/My File/report.pdf",
        "C:/Users/Alice/My File/report.pdf",
        r"\\server\share\My File\report.pdf",
    )
    payloads, text = server._outbound_attachment_payloads_and_text(
        "Hidden files: " + " and ".join(raw_paths),
        append_shared_links=True,
    )

    assert payloads == []
    assert all(raw_path not in text for raw_path in raw_paths)
    assert "private.bin" in text
    assert "private" in text


def test_outbound_path_redaction_preserves_safe_urls_and_shared_references():
    from tui_gateway import server

    original = (
        "Open https://example.com/report.pdf and "
        "https://example.com/home/user/report or shared://Agent-Downloads/a.txt "
        "or https://example.com/x?next=/home/alice/private "
        "or https://example.com/#/home/alice/private"
    )

    payloads, text = server._outbound_attachment_payloads_and_text(original)

    assert payloads == []
    assert text == original


def test_history_to_messages_redacts_outbound_host_paths(tmp_path):
    from tui_gateway import server

    source = tmp_path / "report.pdf"
    source.write_bytes(b"PDF")

    messages = server._history_to_messages(
        [{
            "role": "assistant",
            "content": f"Created: MEDIA:{source}",
            "reasoning": "used /tmp/private/secret.txt",
            "reasoning_details": [{"summary": "/home/alice/private.log"}],
        }]
    )

    assert len(messages) == 1
    assert str(source) not in messages[0]["text"]
    assert messages[0]["text"] == "Created: report.pdf"
    assert messages[0]["attachments"][0]["path"] == "shared://Agent-Downloads/report.pdf"
    assert "/tmp/private" not in messages[0]["reasoning"]
    assert "/home/alice" not in messages[0]["reasoning_details"][0]["summary"]


def test_inflight_and_terminal_error_redact_outbound_host_paths(monkeypatch):
    from tui_gateway import server

    session = {
        "history_lock": threading.RLock(),
        "inflight_turn": {
            "assistant": "Created MEDIA:/tmp/private/report.pdf",
            "error": "failed at /home/alice/private.log",
            "recoverable": True,
            "status": "error",
            "streaming": False,
            "user": "make it",
        },
        "cols": 80,
    }
    emitted = []
    monkeypatch.setattr(server, "_emit", lambda event, sid, payload: emitted.append((event, sid, payload)))
    monkeypatch.setattr(server, "_retire_turn_marker", lambda _session: None)
    monkeypatch.setattr(server, "_fail_inflight_turn", lambda _session, _error: None)

    snapshot = server._inflight_snapshot(session)
    server._emit_terminal_turn_error("session", session, "failed")

    assert snapshot is not None
    assert snapshot["assistant"] == "Created report.pdf"
    assert "/home/alice" not in snapshot["error"]
    assert emitted[-1][2]["text"] == "Created report.pdf"
    assert "/home/alice" not in emitted[-1][2]["error"]


def test_emit_redacts_host_paths_from_live_message_frames(monkeypatch):
    from tui_gateway import server

    frames = []
    monkeypatch.setattr(server, "write_json", frames.append)

    server._emit("message.start", "session")
    server._emit(
        "message.delta",
        "session",
        {"text": "Created MEDIA:/tmp/private/report.pdf\n", "rendered": "unsafe /tmp/private/report.pdf"},
    )
    server._emit(
        "message.complete",
        "session",
        {
            "text": "done",
            "error": "failed at /home/alice/private.log",
            "reasoning": {"summary": "read /tmp/private/notes.txt"},
            "rendered": "done from /var/private/result.txt",
        },
    )

    delta_payload = frames[1]["params"]["payload"]
    complete_payload = frames[2]["params"]["payload"]
    assert delta_payload["text"] == "Created report.pdf\n"
    assert "/tmp/private" not in delta_payload["rendered"]
    assert "/home/alice" not in complete_payload["error"]
    assert "/tmp/private" not in complete_payload["reasoning"]["summary"]
    assert "/var/private" not in complete_payload["rendered"]


def test_emit_buffers_fragmented_host_path_deltas(monkeypatch):
    from tui_gateway import server

    frames = []
    monkeypatch.setattr(server, "write_json", frames.append)

    server._emit("message.start", "fragmented")
    server._emit("message.delta", "fragmented", {"text": "/"})
    server._emit("message.delta", "fragmented", {"text": "home/alice/private"})
    server._emit("message.complete", "fragmented", {"text": "/home/alice/private"})

    visible = "".join(
        frame["params"]["payload"].get("text", "")
        for frame in frames
        if frame["params"].get("payload")
    )
    assert "/home/alice/private" not in visible

    frames.clear()
    server._emit("message.start", "encoded-fragmented")
    for fragment in ("%", "2", "Fhome%2Falice%2Fsecret"):
        server._emit("message.delta", "encoded-fragmented", {"text": fragment})
    server._emit(
        "message.complete",
        "encoded-fragmented",
        {"text": "%2Fhome%2Falice%2Fsecret"},
    )
    encoded_visible = "".join(
        frame["params"]["payload"].get("text", "")
        for frame in frames
        if frame["params"].get("payload")
    )
    assert "%2Fhome%2Falice%2Fsecret" not in encoded_visible

    frames.clear()
    server._emit("message.start", "cap-fragmented")
    server._emit(
        "message.delta",
        "cap-fragmented",
        {"text": "A" * 65530 + " /tmp/%"},
    )
    server._emit(
        "message.delta",
        "cap-fragmented",
        {"text": "2Fhome%2Falice%2Fsecret"},
    )
    cap_visible = "".join(
        frame["params"]["payload"].get("text", "")
        for frame in frames
        if frame["params"].get("payload")
    )
    assert "%2Fhome%2Falice%2Fsecret" not in cap_visible


def test_emit_preserves_safe_uri_across_fragment_boundaries(monkeypatch):
    from tui_gateway import server

    uri = "https://example.test/open?path=%252Fhome%252Falice%252Fsecret#x"
    for split in (1, 5, 8, 20, len(uri) - 3):
        frames = []
        monkeypatch.setattr(server, "write_json", frames.append)
        sid = f"uri-{split}"
        server._emit("message.start", sid)
        server._emit("message.delta", sid, {"text": uri[:split]})
        server._emit("message.delta", sid, {"text": f"{uri[split:]}\n"})
        visible = "".join(
            frame["params"]["payload"].get("text", "")
            for frame in frames
            if frame["params"].get("payload")
        )
        assert visible == f"{uri}\n"


def test_emit_sanitizes_reasoning_delta_and_streams_harmless_slashes(monkeypatch):
    from tui_gateway import server

    frames = []
    monkeypatch.setattr(server, "write_json", frames.append)

    server._emit("message.start", "reasoning")
    server._emit("reasoning.delta", "reasoning", {"text": "read /home/alice/private.log\n"})
    server._emit("message.delta", "reasoning", {"text": "ratio 1/2 is safe. More text."})

    visible = [
        frame["params"]["payload"]["text"]
        for frame in frames
        if frame["params"].get("payload")
    ]
    assert all("/home/alice" not in text for text in visible)
    assert "ratio 1/2 is safe. More text." in visible


def test_safe_uri_sanitizer_has_no_placeholder_collision():
    from tui_gateway import server

    marker = "\x00HERMES_SAFE_URI_0\x00"
    original = f"{marker} https://example.com/x?next=/home/alice/private"

    assert server._sanitize_outbound_text_references(original) == original


def test_sanitizer_redacts_encoded_path_without_mutating_safe_uri_or_collision():
    from tui_gateway import server

    local = "/tmp/collision.pdf"
    original = (
        f"Open https://example.test/open?path={local} and {local} "
        "or encoded %2Fhome%2Falice%2Fprivate "
        "and double %252Fhome%252Falice%252Fprivate"
    )

    sanitized = server._sanitize_outbound_text_references(original)

    assert f"https://example.test/open?path={local}" in sanitized
    assert sanitized.count(local) == 1
    assert "%2Fhome%2Falice%2Fprivate" not in sanitized
    assert "%252Fhome%252Falice%252Fprivate" not in sanitized


def test_outbound_resolution_failure_is_redacted_from_customer_text(tmp_path):
    from tui_gateway import server

    loop = tmp_path / "loop"
    loop.symlink_to(loop.name)
    raw_path = loop / "private-report.pdf"

    payloads, text = server._outbound_attachment_payloads_and_text(
        f"Created file: MEDIA:{raw_path}", append_shared_links=True
    )

    assert payloads == []
    assert str(raw_path) not in text
    assert "private-report.pdf" in text


def test_outbound_oversized_path_is_redacted_from_customer_text(monkeypatch, tmp_path):
    from tui_gateway import server

    source_root = tmp_path / "safe-output"
    source_root.mkdir()
    source = source_root / "large.pdf"
    source.write_bytes(b"oversized")
    monkeypatch.setattr(server, "_outbound_source_roots", lambda: (source_root,))
    monkeypatch.setattr(server, "_OUTBOUND_ATTACHMENT_MAX_BYTES", 1)

    payloads, text = server._outbound_attachment_payloads_and_text(
        f"Created file: {source}", append_shared_links=True
    )

    assert payloads == []
    assert str(source) not in text
    assert "Created file: large.pdf" in text


def test_outbound_shared_materialization_failure_fails_closed(monkeypatch, tmp_path):
    from tui_gateway import server

    source_root = tmp_path / "safe-output"
    source_root.mkdir()
    source = source_root / "report.pdf"
    source.write_bytes(b"%PDF-1.4\n")
    monkeypatch.setattr(server, "_outbound_source_roots", lambda: (source_root,))
    monkeypatch.setattr(
        server,
        "_materialize_outbound_shared_artifact",
        lambda path: (None, None, None),
    )

    payloads, text = server._outbound_attachment_payloads_and_text(
        f"Created file: MEDIA:{source}", append_shared_links=True
    )

    assert payloads == []
    assert str(source) not in text
    assert "report.pdf" in text
    assert "nicht unter Agent-Downloads bereitgestellt" in text


def test_outbound_shared_delivery_survives_ephemeral_preview_failure(monkeypatch, tmp_path):
    from tui_gateway import server
    from hermes_cli import web_server

    source_root = tmp_path / "safe-output"
    source_root.mkdir()
    shared_root = tmp_path / "Hermes-Shared"
    shared_root.mkdir()
    source = source_root / "report.pdf"
    source.write_bytes(b"%PDF-1.4\n")
    monkeypatch.setattr(server, "_outbound_source_roots", lambda: (source_root,))
    monkeypatch.setattr(server, "_resolve_outbound_shared_folder_root", lambda: shared_root)
    def forbidden_ephemeral_materialization(_path):
        raise AssertionError("shared delivery must not create a second preview snapshot")

    monkeypatch.setattr(server, "_materialize_outbound_artifact", forbidden_ephemeral_materialization)
    monkeypatch.setattr(web_server, "_create_shared_file_public_link", lambda *args, **kwargs: None)

    payloads, text = server._outbound_attachment_payloads_and_text(
        f"Created file: MEDIA:{source}", append_shared_links=True
    )

    assert len(payloads) == 1
    assert payloads[0]["shared_folder_path"] == "Agent-Downloads/report.pdf"
    assert payloads[0]["open_url"].startswith("/api/assistant/shared-folder/open?path=")
    assert payloads[0]["preview_url"] == payloads[0]["open_url"]
    assert "nicht unter Agent-Downloads bereitgestellt" not in text


def test_outbound_local_shared_payload_exposes_only_shared_identity(monkeypatch, tmp_path):
    from tui_gateway import server
    from hermes_cli import web_server

    source_root = tmp_path / "safe-output"
    source_root.mkdir()
    shared_root = tmp_path / "Hermes-Shared"
    shared_root.mkdir()
    source = source_root / "report.pdf"
    source.write_bytes(b"%PDF-1.4\n")
    monkeypatch.setattr(server, "_outbound_source_roots", lambda: (source_root,))
    monkeypatch.setattr(server, "_resolve_outbound_shared_folder_root", lambda: shared_root)
    monkeypatch.setattr(web_server, "_create_shared_file_public_link", lambda *args, **kwargs: None)

    payloads, _text = server._outbound_attachment_payloads_and_text(
        f"Created file: MEDIA:{source}", append_shared_links=True
    )

    payload = payloads[0]
    assert payload["path"] == "shared://Agent-Downloads/report.pdf"
    assert payload["preview_url"] == payload["open_url"]
    customer_fields = "\n".join(str(value) for value in payload.values())
    assert str(source_root) not in customer_fields
    assert str(shared_root) not in customer_fields


def test_outbound_shared_concurrent_same_basename_keeps_each_content(monkeypatch, tmp_path):
    from tui_gateway import server

    source_root_a = tmp_path / "safe-a"
    source_root_b = tmp_path / "safe-b"
    source_root_a.mkdir()
    source_root_b.mkdir()
    source_a = source_root_a / "same.pdf"
    source_b = source_root_b / "same.pdf"
    source_a.write_bytes(b"AAAA")
    source_b.write_bytes(b"BBBB")
    shared_root = tmp_path / "Hermes-Shared"
    shared_root.mkdir()
    monkeypatch.setattr(server, "_resolve_outbound_shared_folder_root", lambda: shared_root)
    monkeypatch.setattr(server, "_outbound_source_roots", lambda: (source_root_a, source_root_b))

    original_copy = server._copy_outbound_source_to_snapshot
    barrier = threading.Barrier(2)

    def synchronized_copy(source, target):
        result = original_copy(source, target)
        barrier.wait(timeout=5)
        return result

    monkeypatch.setattr(server, "_copy_outbound_source_to_snapshot", synchronized_copy)
    with ThreadPoolExecutor(max_workers=2) as pool:
        future_a = pool.submit(server._materialize_outbound_shared_artifact, source_a)
        future_b = pool.submit(server._materialize_outbound_shared_artifact, source_b)
        target_a, rel_a, size_a = future_a.result(timeout=10)
        target_b, rel_b, size_b = future_b.result(timeout=10)

    assert target_a is not None
    assert target_b is not None
    assert rel_a is not None
    assert rel_b is not None
    assert size_a == 4
    assert size_b == 4
    assert target_a != target_b
    assert rel_a != rel_b
    assert target_a.read_bytes() == b"AAAA"
    assert target_b.read_bytes() == b"BBBB"


def test_outbound_shared_local_target_digest_matches_copied_snapshot(monkeypatch, tmp_path):
    from tui_gateway import server

    source_root = tmp_path / "safe-output"
    source_root.mkdir()
    source = source_root / "report.pdf"
    source.write_bytes(b"AAAA")
    shared_root = tmp_path / "Hermes-Shared"
    shared_root.mkdir()
    (shared_root / "Agent-Downloads").mkdir()
    (shared_root / "Agent-Downloads" / source.name).write_bytes(b"occupied")
    monkeypatch.setattr(server, "_resolve_outbound_shared_folder_root", lambda: shared_root)
    monkeypatch.setattr(server, "_outbound_source_roots", lambda: (source_root,))

    original_copy = server._copy_outbound_source_to_snapshot

    def mutate_then_copy(path, target):
        source.write_bytes(b"BBBB")
        return original_copy(path, target)

    monkeypatch.setattr(server, "_copy_outbound_source_to_snapshot", mutate_then_copy)
    target, rel, delivered_size = server._materialize_outbound_shared_artifact(source)

    expected_digest = hashlib.sha256(b"BBBB").hexdigest()
    assert target is not None
    assert rel is not None
    assert delivered_size == 4
    assert target.read_bytes() == b"BBBB"
    assert expected_digest[:12] in target.name or expected_digest in target.name


def test_outbound_shared_cloud_name_matches_uploaded_snapshot(monkeypatch, tmp_path):
    from tui_gateway import server
    from hermes_cli import web_server

    source_root = tmp_path / "safe-output"
    source_root.mkdir()
    source = source_root / "report.pdf"
    source.write_bytes(b"AAAA")
    monkeypatch.setattr(server, "_resolve_outbound_shared_folder_root", lambda: None)
    monkeypatch.setattr(server, "_outbound_source_roots", lambda: (source_root,))
    original_digest = server._file_sha256_digest

    def hash_then_mutate(path):
        digest = original_digest(path)
        source.write_bytes(b"BBBB")
        return digest

    captured: dict[str, object] = {}

    def upload(_config, path, rel):
        captured["bytes"] = path.read_bytes()
        captured["rel"] = rel
        return rel

    monkeypatch.setattr(server, "_file_sha256_digest", hash_then_mutate)
    monkeypatch.setattr(web_server, "load_config", lambda: {})
    monkeypatch.setattr(web_server, "_upload_shared_file_to_cloud", upload)

    shared_path, shared_rel, delivered_size = server._materialize_outbound_shared_artifact(source)

    uploaded = captured["bytes"]
    assert isinstance(uploaded, bytes)
    expected_digest = hashlib.sha256(uploaded).hexdigest()
    assert shared_path is None
    assert shared_rel == captured["rel"]
    assert delivered_size == len(uploaded)
    assert expected_digest in str(shared_rel)


def test_outbound_shared_existing_agent_download_is_snapshotted(monkeypatch, tmp_path):
    from tui_gateway import server

    shared_root = tmp_path / "Hermes-Shared"
    source = shared_root / "Agent-Downloads" / "report.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"AAAA")
    monkeypatch.setattr(server, "_resolve_outbound_shared_folder_root", lambda: shared_root)
    monkeypatch.setattr(server, "_outbound_source_roots", lambda: (shared_root,))

    target, rel, delivered_size = server._materialize_outbound_shared_artifact(source)
    source.write_bytes(b"BBBB")

    assert target is not None
    assert rel is not None
    assert delivered_size == 4
    assert target != source.resolve()
    assert target.read_bytes() == b"AAAA"


def test_outbound_shared_source_symlink_swap_fails_closed(monkeypatch, tmp_path):
    from tui_gateway import server

    source_root = tmp_path / "safe"
    source_root.mkdir()
    shared_root = tmp_path / "Shared"
    shared_root.mkdir()
    source = source_root / "report.pdf"
    source.write_bytes(b"SAFE")
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"SECRET-OUTSIDE-ALLOWLIST")
    monkeypatch.setattr(server, "_outbound_source_roots", lambda: (source_root,))
    monkeypatch.setattr(server, "_resolve_outbound_shared_folder_root", lambda: shared_root)
    original_copy = server._copy_outbound_source_to_snapshot

    def swap_before_copy(src, dst):
        source.unlink()
        source.symlink_to(outside)
        return original_copy(src, dst)

    monkeypatch.setattr(server, "_copy_outbound_source_to_snapshot", swap_before_copy)

    target, rel, delivered_size = server._materialize_outbound_shared_artifact(source)

    assert target is None
    assert rel is None
    assert delivered_size is None
    assert not list((shared_root / "Agent-Downloads").glob("*.pdf"))


def test_outbound_shared_delivery_does_not_require_hardlinks(monkeypatch, tmp_path):
    from tui_gateway import server

    source_root = tmp_path / "safe"
    source_root.mkdir()
    shared_root = tmp_path / "Shared"
    shared_root.mkdir()
    source = source_root / "report.pdf"
    source.write_bytes(b"PDF")
    monkeypatch.setattr(server, "_outbound_source_roots", lambda: (source_root,))
    monkeypatch.setattr(server, "_resolve_outbound_shared_folder_root", lambda: shared_root)

    def hardlinks_unsupported(*_args, **_kwargs):
        raise OSError(errno.EOPNOTSUPP, "hard links unsupported")

    monkeypatch.setattr(server.os, "link", hardlinks_unsupported)

    target, rel, delivered_size = server._materialize_outbound_shared_artifact(source)

    assert target is not None
    assert rel is not None
    assert delivered_size == 3
    assert target.read_bytes() == b"PDF"


def test_outbound_payload_size_matches_delivered_snapshot(monkeypatch, tmp_path):
    from tui_gateway import server
    from hermes_cli import web_server

    source_root = tmp_path / "safe-output"
    source_root.mkdir()
    source = source_root / "report.pdf"
    source.write_bytes(b"AAAA")
    shared_root = tmp_path / "Hermes-Shared"
    shared_root.mkdir()
    monkeypatch.setattr(server, "_resolve_outbound_shared_folder_root", lambda: shared_root)
    monkeypatch.setattr(server, "_outbound_source_roots", lambda: (source_root,))
    monkeypatch.setattr(web_server, "_create_shared_file_public_link", lambda *args, **kwargs: None)
    original_copy = server._copy_outbound_source_to_snapshot

    def mutate_then_copy(path, target):
        source.write_bytes(b"BBBBBBBB")
        return original_copy(path, target)

    monkeypatch.setattr(server, "_copy_outbound_source_to_snapshot", mutate_then_copy)
    payloads, _text = server._outbound_attachment_payloads_and_text(
        f"Created file: MEDIA:{source}", append_shared_links=True
    )

    delivered_path = shared_root / payloads[0]["shared_folder_path"]
    assert delivered_path.read_bytes() == b"BBBBBBBB"
    assert payloads[0]["size"] == 8


def test_outbound_shared_collision_avoids_equal_size_stale_copy(monkeypatch, tmp_path):
    from tui_gateway import server

    source_root = tmp_path / "safe-output"
    source_root.mkdir()
    shared_root = tmp_path / "Hermes-Shared"
    shared_root.mkdir()
    source = source_root / "report.pdf"
    source.write_bytes(b"AAAA")
    target_dir = shared_root / "Agent-Downloads"
    target_dir.mkdir()
    (target_dir / source.name).write_bytes(b"BBBB")
    monkeypatch.setattr(server, "_resolve_outbound_shared_folder_root", lambda: shared_root)
    monkeypatch.setattr(server, "_outbound_source_roots", lambda: (source_root,))

    first_target, first_rel, first_size = server._materialize_outbound_shared_artifact(source)
    assert first_target is not None
    assert first_rel is not None
    assert first_size == 4
    first_target.write_bytes(b"CCCC")

    refreshed_target, refreshed_rel, refreshed_size = server._materialize_outbound_shared_artifact(source)

    assert refreshed_target is not None
    assert refreshed_size == 4
    assert refreshed_target != first_target
    assert refreshed_rel != first_rel
    assert first_target.read_bytes() == b"CCCC"
    assert refreshed_target.read_bytes() == b"AAAA"


def test_outbound_previewable_image_uses_shared_pubshare_for_open_and_download(monkeypatch, tmp_path):
    from tui_gateway import server
    from hermes_cli import web_server

    source_root = tmp_path / "safe-output"
    source_root.mkdir()
    shared_root = tmp_path / "Hermes-Shared"
    shared_root.mkdir()
    image_path = source_root / "answer.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n")
    monkeypatch.setattr(server, "_resolve_outbound_shared_folder_root", lambda: shared_root)
    monkeypatch.setattr(server, "_outbound_source_roots", lambda: (source_root,))
    monkeypatch.setattr(web_server, "load_config", lambda: {"dashboard": {"shared_cloud": {}}})
    monkeypatch.setattr(
        web_server,
        "_create_shared_file_public_link",
        lambda config, rel_path, *, name=None: {
            "url": "https://cloud.aiwerk.ch/web/client/pubshares/image123?compress=false",
            "download_url": "https://cloud.aiwerk.ch/web/client/pubshares/image123/download",
        },
    )

    payloads, text = server._outbound_attachment_payloads_and_text(
        f"Here: MEDIA:{image_path}", append_shared_links=True
    )

    shared_copy = shared_root / "Agent-Downloads" / "answer.png"
    assert shared_copy.read_bytes() == b"\x89PNG\r\n\x1a\n"
    assert payloads[0]["path"] == "shared://Agent-Downloads/answer.png"
    assert payloads[0]["shared_folder_path"] == "Agent-Downloads/answer.png"
    assert payloads[0]["open_url"] == "https://cloud.aiwerk.ch/web/client/pubshares/image123?compress=false"
    assert payloads[0]["preview_url"] == payloads[0]["open_url"]
    assert payloads[0]["download_url"] == "https://cloud.aiwerk.ch/web/client/pubshares/image123/download"
    assert payloads[0]["public_url"] == payloads[0]["open_url"]
    assert payloads[0]["public_download_url"] == payloads[0]["download_url"]
    assert "Web-Link: https://cloud.aiwerk.ch/web/client/pubshares/image123?compress=false" in text
    assert "Download: https://cloud.aiwerk.ch/web/client/pubshares/image123/download" in text


def test_outbound_previewable_file_falls_back_to_shared_open_url(monkeypatch, tmp_path):
    from tui_gateway import server
    from hermes_cli import web_server

    source_root = tmp_path / "safe-output"
    source_root.mkdir()
    shared_root = tmp_path / "Hermes-Shared"
    shared_root.mkdir()
    pdf_path = source_root / "report.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")
    monkeypatch.setattr(server, "_resolve_outbound_shared_folder_root", lambda: shared_root)
    monkeypatch.setattr(server, "_outbound_source_roots", lambda: (source_root,))
    monkeypatch.setattr(web_server, "load_config", lambda: {})
    monkeypatch.setattr(web_server, "_create_shared_file_public_link", lambda *args, **kwargs: None)

    payloads, text = server._outbound_attachment_payloads_and_text(
        f"Here: MEDIA:{pdf_path}", append_shared_links=True
    )

    shared_open_url = "/api/assistant/shared-folder/open?path=Agent-Downloads%2Freport.pdf"
    assert payloads[0]["shared_folder_path"] == "Agent-Downloads/report.pdf"
    assert payloads[0]["open_url"] == shared_open_url
    assert payloads[0]["download_url"] == shared_open_url
    assert payloads[0]["preview_url"] == shared_open_url
    assert shared_open_url in text


def test_outbound_shared_folder_link_includes_public_cloud_link(monkeypatch, tmp_path):
    from tui_gateway import server
    from hermes_cli import web_server

    source_root = tmp_path / "safe-output"
    source_root.mkdir()
    shared_root = tmp_path / "Hermes-Shared"
    shared_root.mkdir()
    pptx_path = source_root / "slides.pptx"
    pptx_path.write_bytes(b"pptx")
    monkeypatch.setattr(server, "_resolve_outbound_shared_folder_root", lambda: shared_root)
    monkeypatch.setattr(server, "_outbound_source_roots", lambda: (source_root,))
    monkeypatch.setattr(web_server, "load_config", lambda: {"dashboard": {"shared_cloud": {}}})
    monkeypatch.setattr(
        web_server,
        "_create_shared_file_public_link",
        lambda config, rel_path, *, name=None: {
            "url": "https://cloud.aiwerk.ch/web/client/pubshares/share123?compress=false",
            "download_url": "https://cloud.aiwerk.ch/web/client/pubshares/share123/download",
        },
    )

    payloads, text = server._outbound_attachment_payloads_and_text(
        f"Here: MEDIA:{pptx_path}", append_shared_links=True
    )

    assert payloads[0]["shared_folder_path"] == "Agent-Downloads/slides.pptx"
    assert payloads[0]["public_url"] == "https://cloud.aiwerk.ch/web/client/pubshares/share123?compress=false"
    assert payloads[0]["public_download_url"] == "https://cloud.aiwerk.ch/web/client/pubshares/share123/download"
    assert "Web-Link: https://cloud.aiwerk.ch/web/client/pubshares/share123?compress=false" in text
    assert "Download: https://cloud.aiwerk.ch/web/client/pubshares/share123/download" in text


def test_outbound_non_renderable_attachment_uploads_to_cloud_without_local_mount(monkeypatch, tmp_path):
    from tui_gateway import server
    from hermes_cli import web_server

    source_root = tmp_path / "safe-output"
    source_root.mkdir()
    pptx_path = source_root / "cloud-only.pptx"
    pptx_path.write_bytes(b"pptx")
    monkeypatch.setattr(server, "_resolve_outbound_shared_folder_root", lambda: None)
    monkeypatch.setattr(server, "_outbound_source_roots", lambda: (source_root,))
    monkeypatch.setattr(web_server, "load_config", lambda: {"dashboard": {"shared_cloud": {}}})
    monkeypatch.setattr(
        web_server,
        "_upload_shared_file_to_cloud",
        lambda config, source, rel_path: rel_path,
    )
    monkeypatch.setattr(
        web_server,
        "_create_shared_file_public_link",
        lambda config, rel_path, *, name=None: {
            "url": "https://cloud.aiwerk.ch/web/client/pubshares/cloud123?compress=false",
            "download_url": "https://cloud.aiwerk.ch/web/client/pubshares/cloud123/download",
        },
    )

    payloads, text = server._outbound_attachment_payloads_and_text(
        f"Here: MEDIA:{pptx_path}", append_shared_links=True
    )

    assert payloads[0]["path"].startswith("shared://Agent-Downloads/")
    assert str(pptx_path.resolve()) not in payloads[0]["path"]
    assert payloads[0]["shared_folder_path"].startswith("Agent-Downloads/cloud-only-")
    assert payloads[0]["shared_folder_path"].endswith(".pptx")
    assert payloads[0]["open_url"] == "https://cloud.aiwerk.ch/web/client/pubshares/cloud123?compress=false"
    assert payloads[0]["download_url"] == "https://cloud.aiwerk.ch/web/client/pubshares/cloud123/download"
    assert "Web-Link: https://cloud.aiwerk.ch/web/client/pubshares/cloud123?compress=false" in text


def test_assistant_preview_kind_keeps_json_as_file_even_with_text_mime():
    from hermes_cli import web_server

    preview_kind = getattr(web_server, "_assistant_preview_kind")
    assert preview_kind("data.json", "application/json") == "file"
    assert preview_kind("data.json", "text/plain") == "file"


def test_outbound_attachment_payloads_extracts_pdf_audio_and_video(tmp_path):
    from tui_gateway import server

    pdf_path = tmp_path / "report.pdf"
    audio_path = tmp_path / "clip.mp3"
    video_path = tmp_path / "demo.mp4"
    pdf_path.write_bytes(b"%PDF-1.4\n")
    audio_path.write_bytes(b"ID3")
    video_path.write_bytes(b"\x00\x00\x00\x18ftypmp42")

    payloads = server._outbound_image_attachment_payloads(
        f"MEDIA:{pdf_path}\nMEDIA:{audio_path}\nMEDIA:{video_path}"
    )

    assert [payload["preview_kind"] for payload in payloads] == ["pdf", "audio", "video"]
    assert all(payload["safe_renderable"] is True for payload in payloads)
    assert all(payload["preview_url"] for payload in payloads)


def test_outbound_attachment_payloads_rejects_external_source(monkeypatch, tmp_path):
    # SECURITY: the materializer must NOT copy a file that lives outside the
    # safe-output allowlist (dashboard upload root + process temp dir) into the
    # customer-servable area. The assistant text is customer-influenceable, and
    # the extension allowlist also matches config/credential files, so an
    # external source path must be refused before any copy happens. (This test
    # previously asserted the insecure copy-anything behavior.)
    from tui_gateway import server

    source_root = tmp_path / "outside-artifact-roots"
    source_root.mkdir()
    source = source_root / "Jaro.mp3"
    source.write_bytes(b"ID3 playable test bytes")
    upload_root = tmp_path / "dashboard_uploads"
    hermes_root = tmp_path / "hermes-home"
    fake_temp_root = tmp_path / "fake-temp"
    fake_temp_root.mkdir()
    monkeypatch.setattr(server, "_DASHBOARD_UPLOAD_ROOT", upload_root)
    monkeypatch.setattr(server, "_hermes_home", hermes_root)
    monkeypatch.setattr(server.tempfile, "gettempdir", lambda: str(fake_temp_root))

    assert server._outbound_image_attachment_payloads(f"MEDIA:{source}") == []
    assert server._materialize_outbound_artifact(source) is None
    # Nothing was laundered into the served upload root.
    assert not (upload_root / "outbound_artifacts").exists()


def test_outbound_attachment_payloads_snapshots_temp_dir_source_to_shared(monkeypatch, tmp_path):
    from tui_gateway import server

    fake_temp_root = tmp_path / "fake-temp"
    fake_temp_root.mkdir()
    source = fake_temp_root / "Jaro.mp3"
    source.write_bytes(b"ID3 playable test bytes")
    shared_root = tmp_path / "Hermes-Shared"
    shared_root.mkdir()
    hermes_root = tmp_path / "hermes-home"
    monkeypatch.setattr(server, "_hermes_home", hermes_root)
    monkeypatch.setattr(server.tempfile, "gettempdir", lambda: str(fake_temp_root))
    monkeypatch.setattr(server, "_resolve_outbound_shared_folder_root", lambda: shared_root)

    payloads = server._outbound_image_attachment_payloads(f"MEDIA:{source}")
    payloads_again = server._outbound_image_attachment_payloads(f"MEDIA:{source}")

    assert len(payloads) == 1
    payload = payloads[0]
    shared_copy = shared_root / "Agent-Downloads" / "Jaro.mp3"
    assert shared_copy.read_bytes() == source.read_bytes()
    assert payloads_again[0]["shared_folder_path"] == payload["shared_folder_path"]
    assert payload["path"] == "shared://Agent-Downloads/Jaro.mp3"
    assert payload["preview_url"] == payload["open_url"]
    assert payload["preview_kind"] == "audio"
    assert payload["safe_renderable"] is True


def test_outbound_attachment_payloads_rejects_hermes_home_config(monkeypatch, tmp_path):
    # The concrete attack from the brief: assistant text references
    # ~/.hermes/config.yaml (provider API keys). Even though .yaml is in the
    # extension allowlist, the materializer must refuse a HERMES_HOME source.
    from tui_gateway import server

    hermes_root = tmp_path / "hermes-home"
    hermes_root.mkdir()
    config = hermes_root / "config.yaml"
    config.write_text("provider_api_key: sk-secret\n", encoding="utf-8")
    upload_root = tmp_path / "dashboard_uploads"
    monkeypatch.setattr(server, "_DASHBOARD_UPLOAD_ROOT", upload_root)
    monkeypatch.setattr(server, "_hermes_home", hermes_root)
    # Even if HERMES_HOME were (mis)configured under the temp allowlist, the
    # explicit HERMES_HOME / dotfile denylist still rejects it.
    monkeypatch.setattr(server.tempfile, "gettempdir", lambda: str(tmp_path))

    assert server._outbound_image_attachment_payloads(f"MEDIA:{config}") == []
    assert server._materialize_outbound_artifact(config) is None


def test_assistant_artifact_endpoint_serves_upload_root_image(client_loopback, tmp_path, monkeypatch):
    from hermes_cli import web_server

    monkeypatch.setattr(web_server, "get_hermes_home", lambda: tmp_path / "hermes-home")
    image_path = web_server._assistant_upload_root() / "artifact.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n")

    response = client_loopback.get(
        "/api/assistant/artifacts/open",
        params={"path": str(image_path)},
        headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/png")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.content.startswith(b"\x89PNG")


def test_assistant_artifact_endpoint_serves_non_image_as_safe_attachment(client_loopback, tmp_path, monkeypatch):
    from hermes_cli import web_server

    monkeypatch.setattr(web_server, "get_hermes_home", lambda: tmp_path / "hermes-home")
    text_path = web_server._assistant_upload_root() / "artifact.txt"
    text_path.write_text("not an image", encoding="utf-8")

    response = client_loopback.get(
        "/api/assistant/artifacts/open",
        params={"path": str(text_path)},
        headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "inline" in response.headers["content-disposition"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.text == "not an image"


def test_assistant_artifact_endpoint_forces_json_download(client_loopback, tmp_path, monkeypatch):
    from hermes_cli import web_server

    monkeypatch.setattr(web_server, "get_hermes_home", lambda: tmp_path / "hermes-home")
    json_path = web_server._assistant_upload_root() / "artifact.json"
    json_path.write_text('{"not": "previewed"}', encoding="utf-8")

    response = client_loopback.get(
        "/api/assistant/artifacts/open",
        params={"path": str(json_path)},
        headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert "attachment" in response.headers["content-disposition"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.text == '{"not": "previewed"}'


def test_outbound_attachment_payloads_extracts_active_content_as_non_preview_file(monkeypatch, tmp_path):
    from tui_gateway import server

    html_path = tmp_path / "artifact.html"
    html_path.write_text("<script>alert(1)</script>", encoding="utf-8")
    shared_root = tmp_path / "Hermes-Shared"
    shared_root.mkdir()
    monkeypatch.setattr(server, "_resolve_outbound_shared_folder_root", lambda: shared_root)

    payloads = server._outbound_image_attachment_payloads(f"MEDIA:{html_path}")

    assert len(payloads) == 1
    payload = payloads[0]
    assert payload["name"] == "artifact.html"
    assert payload["preview_kind"] == "file"
    assert payload["safe_renderable"] is False
    assert payload["preview_url"] is None
    assert payload["shared_folder_path"] == "Agent-Downloads/artifact.html"
    assert payload["download_url"].startswith("/api/assistant/shared-folder/open?path=")


def test_assistant_artifact_endpoint_forces_active_content_download(client_loopback, tmp_path, monkeypatch):
    from hermes_cli import web_server

    monkeypatch.setattr(web_server, "get_hermes_home", lambda: tmp_path / "hermes-home")
    html_path = web_server._assistant_upload_root() / "artifact.html"
    html_path.write_text("<script>alert(1)</script>", encoding="utf-8")

    response = client_loopback.get(
        "/api/assistant/artifacts/open",
        params={"path": str(html_path)},
        headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/octet-stream")
    assert "attachment" in response.headers["content-disposition"]
    assert response.headers["x-content-type-options"] == "nosniff"


def test_assistant_artifact_endpoint_rejects_non_upload_root_paths(client_loopback, tmp_path, monkeypatch):
    from hermes_cli import web_server

    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    monkeypatch.setattr(web_server, "get_hermes_home", lambda: hermes_home)
    config_path = hermes_home / "config.yaml"
    config_path.write_text("model: test\n", encoding="utf-8")

    response = client_loopback.get(
        "/api/assistant/artifacts/open",
        params={"path": str(config_path)},
        headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
    )

    assert response.status_code == 404
