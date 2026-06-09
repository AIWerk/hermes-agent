from pathlib import Path
import urllib.parse

import pytest
from fastapi.testclient import TestClient


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
    assert payload["open_url"].startswith("/api/assistant/artifacts/open?path=")


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
    assert payload["open_url"].startswith("/api/assistant/artifacts/open?path=")
    assert payload["preview_url"].startswith("/api/assistant/artifacts/open?path=")


def test_outbound_json_attachment_is_file_card_without_preview(tmp_path):
    from tui_gateway import server

    json_path = tmp_path / "data.json"
    json_path.write_text('{"ok": true}', encoding="utf-8")

    payloads = server._outbound_image_attachment_payloads(f"MEDIA:{json_path}")

    assert len(payloads) == 1
    payload = payloads[0]
    assert payload["name"] == "data.json"
    assert payload["type"] == "application/json"
    assert payload["preview_kind"] == "file"
    assert payload["safe_renderable"] is False
    assert payload["preview_url"] is None
    assert payload["download_url"].startswith("/api/assistant/artifacts/open?path=")


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


def test_outbound_attachment_payloads_materializes_external_audio(monkeypatch, tmp_path):
    from tui_gateway import server

    source_root = tmp_path / "outside-artifact-roots"
    source_root.mkdir()
    source = source_root / "Jaro.mp3"
    source.write_bytes(b"ID3 playable test bytes")
    upload_root = tmp_path / "dashboard_uploads"
    hermes_root = tmp_path / "hermes-home"
    fake_temp_root = tmp_path / "fake-temp"
    monkeypatch.setattr(server, "_DASHBOARD_UPLOAD_ROOT", upload_root)
    monkeypatch.setattr(server, "_hermes_home", hermes_root)
    monkeypatch.setattr(server.tempfile, "gettempdir", lambda: str(fake_temp_root))

    payloads = server._outbound_image_attachment_payloads(f"MEDIA:{source}")
    payloads_again = server._outbound_image_attachment_payloads(f"MEDIA:{source}")

    assert len(payloads) == 1
    payload = payloads[0]
    materialized = Path(urllib.parse.parse_qs(urllib.parse.urlparse(payload["open_url"]).query)["path"][0])
    materialized_again = Path(urllib.parse.parse_qs(urllib.parse.urlparse(payloads_again[0]["open_url"]).query)["path"][0])
    assert materialized != source
    assert materialized_again == materialized
    assert upload_root in materialized.parents
    assert materialized.read_bytes() == source.read_bytes()
    assert payload["path"] == str(materialized)
    assert payload["preview_kind"] == "audio"
    assert payload["safe_renderable"] is True


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


def test_outbound_attachment_payloads_extracts_active_content_as_non_preview_file(tmp_path):
    from tui_gateway import server

    html_path = tmp_path / "artifact.html"
    html_path.write_text("<script>alert(1)</script>", encoding="utf-8")

    payloads = server._outbound_image_attachment_payloads(f"MEDIA:{html_path}")

    assert len(payloads) == 1
    payload = payloads[0]
    assert payload["name"] == "artifact.html"
    assert payload["preview_kind"] == "file"
    assert payload["safe_renderable"] is False
    assert payload["preview_url"] is None
    assert payload["download_url"].startswith("/api/assistant/artifacts/open?path=")


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
