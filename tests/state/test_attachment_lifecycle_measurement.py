from hermes_state import SessionDB


def test_measure_workspace_attachment_survives_session_delete(tmp_path):
    """Rank 8 measurement: canonical upstream files are not session-owned."""
    workspace = tmp_path / "workspace"
    attachment = workspace / ".hermes" / "desktop-attachments" / "same.txt"
    attachment.parent.mkdir(parents=True)
    attachment.write_text("payload")
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("conversation-a", "desktop", cwd=str(workspace))
        assert db.delete_session("conversation-a") is True
        assert attachment.exists()
        assert attachment.read_text() == "payload"
    finally:
        db.close()
