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
