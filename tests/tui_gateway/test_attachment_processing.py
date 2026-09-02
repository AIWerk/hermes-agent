from pathlib import Path

from tui_gateway import server


def test_process_prompt_attachments_routes_images_and_inlines_text(tmp_path, monkeypatch):
    upload_root = tmp_path / "dashboard_uploads"
    session_dir = upload_root / "session" / "batch"
    session_dir.mkdir(parents=True)
    image = session_dir / "photo.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    note = session_dir / "note.txt"
    note.write_text("important customer text", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("must not leak", encoding="utf-8")

    monkeypatch.setattr(server, "_DASHBOARD_UPLOAD_ROOT", upload_root.resolve())

    images, context = server._process_prompt_attachments([
        {"name": "photo.png", "path": str(image), "type": "image/png"},
        {"name": "note.txt", "path": str(note), "type": "text/plain"},
        {"name": "outside.txt", "path": str(outside), "type": "text/plain"},
    ])

    assert images == [str(image)]
    assert "important customer text" in context
    assert "outside.txt" not in context
    assert "must not leak" not in context


def test_process_prompt_attachments_denies_cross_session_reference(tmp_path, monkeypatch):
    # The upload root is shared across sessions: dashboard_uploads/<sid>/<batch>/.
    # A client controls the attachment "path" field, so without per-session
    # scoping one session could reference another session's uploaded file by
    # absolute path. The session-scoped check must deny the cross-session path
    # while still accepting the requesting session's own file.
    upload_root = tmp_path / "dashboard_uploads"
    monkeypatch.setattr(server, "_DASHBOARD_UPLOAD_ROOT", upload_root.resolve())

    mine_dir = upload_root / server._session_upload_component("sess-A") / "batch"
    mine_dir.mkdir(parents=True)
    mine = mine_dir / "mine.txt"
    mine.write_text("my own upload", encoding="utf-8")

    theirs_dir = upload_root / server._session_upload_component("sess-B") / "batch"
    theirs_dir.mkdir(parents=True)
    theirs = theirs_dir / "theirs.txt"
    theirs.write_text("another customer's secret", encoding="utf-8")

    images, context = server._process_prompt_attachments(
        [
            {"name": "mine.txt", "path": str(mine), "type": "text/plain"},
            {"name": "theirs.txt", "path": str(theirs), "type": "text/plain"},
        ],
        "sess-A",
    )

    assert images == []
    assert "my own upload" in context
    assert "theirs.txt" not in context
    assert "another customer's secret" not in context


def test_attachment_path_allowed_scopes_to_session(tmp_path, monkeypatch):
    upload_root = tmp_path / "dashboard_uploads"
    monkeypatch.setattr(server, "_DASHBOARD_UPLOAD_ROOT", upload_root.resolve())

    a_file = upload_root / server._session_upload_component("sess-A") / "batch" / "f.png"
    a_file.parent.mkdir(parents=True)
    a_file.write_bytes(b"\x89PNG")
    b_file = upload_root / server._session_upload_component("sess-B") / "batch" / "g.png"
    b_file.parent.mkdir(parents=True)
    b_file.write_bytes(b"\x89PNG")

    # Same session: allowed. Cross session: denied.
    assert server._attachment_path_allowed(a_file, "sess-A") is True
    assert server._attachment_path_allowed(b_file, "sess-A") is False
    # No session id supplied: falls back to upload-root boundary (both allowed).
    assert server._attachment_path_allowed(a_file) is True
    assert server._attachment_path_allowed(b_file) is True
    # Outside the upload root entirely: denied regardless.
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"\x89PNG")
    assert server._attachment_path_allowed(outside, "sess-A") is False
    assert server._attachment_path_allowed(outside) is False


def test_process_prompt_attachments_uses_extracted_text_for_document(tmp_path, monkeypatch):
    upload_root = tmp_path / "dashboard_uploads"
    session_dir = upload_root / "session" / "batch"
    session_dir.mkdir(parents=True)
    doc = session_dir / "brief.pdf"
    doc.write_bytes(b"%PDF")

    monkeypatch.setattr(server, "_DASHBOARD_UPLOAD_ROOT", upload_root.resolve())

    images, context = server._process_prompt_attachments([
        {
            "name": "brief.pdf",
            "path": str(doc),
            "type": "application/pdf",
            "extracted_text": "extracted pdf text",
            "extraction": "pdf",
        }
    ])

    assert images == []
    assert "brief.pdf" in context
    assert "extracted pdf text" in context
