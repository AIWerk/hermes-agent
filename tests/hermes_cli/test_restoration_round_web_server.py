import io
import json
import threading
import time
import urllib.parse
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient


_RESTORED_ENV_KEYS = (
    "AIWERK_CUI_AGENT_NAME",
    "AIWERK_CUI_CALENDAR_HORIZON_DAYS",
    "AIWERK_CUI_CALENDAR_MAX_RESULTS",
    "AIWERK_CUI_CALENDAR_SUMMARY_JSON",
    "AIWERK_CUI_CONTACTS_DISABLE_AIWERK_BRIDGE",
    "AIWERK_CUI_CONTACTS_DISABLE_GMAIL_INTERACTIONS",
    "AIWERK_CUI_CONTACTS_DISABLE_HIMALAYA_INTERACTIONS",
    "AIWERK_CUI_CONTACTS_HIMALAYA_INBOX_FOLDER",
    "AIWERK_CUI_CONTACTS_HIMALAYA_SENT_FOLDER",
    "AIWERK_CUI_CONTACTS_INBOX_QUERY",
    "AIWERK_CUI_CONTACTS_INTERACTION_SCAN_LIMIT",
    "AIWERK_CUI_CONTACTS_PAGE_SIZE",
    "AIWERK_CUI_CONTACTS_RELEVANCE_WINDOW_DAYS",
    "AIWERK_CUI_CONTACTS_SAVED_TOP_UP_TARGET",
    "AIWERK_CUI_CONTACTS_SENT_QUERY",
    "AIWERK_CUI_EMAIL_ACCOUNT",
    "AIWERK_CUI_EMAIL_BACKEND",
    "AIWERK_CUI_EMAIL_DISABLE_AIWERK_BRIDGE",
    "AIWERK_CUI_EMAIL_DISABLE_HIMALAYA",
    "AIWERK_CUI_EMAIL_FOLDER",
    "AIWERK_CUI_EMAIL_SUMMARY_JSON",
    "AIWERK_CUI_GMAIL_LATEST_QUERY",
    "AIWERK_CUI_GMAIL_UNREAD_QUERY",
    "AIWERK_CUI_GOOGLE_EMAIL",
    "AIWERK_CUI_GOOGLE_WORKSPACE_SERVER",
    "AIWERK_CUI_LANGUAGE",
    "AIWERK_CUI_MAILDIR",
    "AIWERK_CUI_SUPPORT_LOG",
    "AIWERK_CUI_SUPPORT_TARGET",
    "AIWERK_CUI_USER_DISPLAY_NAME",
    "AIWERK_CUI_USER_NAME",
    "AIWERK_CUI_VAULT_SUMMARY_JSON",
    "AIWERK_CUI_VAULT_URL",
    "AIWERK_SHARED_FOLDER",
    "AIWERK_SYSTEM_TARGET",
    "HERMES_CUI_LOCALE",
    "HERMES_SHARED_DIR",
    "HERMES_SHARED_FOLDER",
    "HERMES_USER_DISPLAY_NAME",
    "HIMALAYA_ACCOUNT",
    "HIMALAYA_FOLDER",
    "MAILDIR",
    "WHATSAPP_MODE",
)


class TestRestorationRoundWebServer:
    def _cloud_response_xml(self, *hrefs: tuple[str, str, bool, int]) -> bytes:
        responses = []
        for href, name, is_folder, size in hrefs:
            collection = "<D:collection/>" if is_folder else ""
            responses.append(
                f"<D:response><D:href>{href}</D:href><D:propstat><D:prop>"
                f"<D:displayname>{name}</D:displayname>"
                f"<D:getcontentlength>{size}</D:getcontentlength>"
                f"<D:getlastmodified>Thu, 10 Sep 2026 12:00:00 GMT</D:getlastmodified>"
                f"<D:resourcetype>{collection}</D:resourcetype>"
                f"</D:prop></D:propstat></D:response>"
            )
        return (
            "<?xml version='1.0'?><D:multistatus xmlns:D='DAV:'>"
            + "".join(responses)
            + "</D:multistatus>"
        ).encode()

    def _cloud_config(self, **overrides):
        cloud = {
            "base_url": "https://cloud.example.test",
            "share_id": "share-123",
            "path": "/Customer Shared",
            "password_pass_entry": "customers/shared",
            "max_depth": 2,
        }
        cloud.update(overrides)
        return {"dashboard": {"shared_cloud": cloud}}

    def test_cloud_only_sftpgo_pubshare_summary_open_and_attachment(self, monkeypatch, tmp_path):
        import hermes_cli.web_server as ws

        monkeypatch.delenv("AIWERK_CUI_SHARED_FOLDER", raising=False)
        monkeypatch.delenv("AIWERK_SHARED_FOLDER", raising=False)
        monkeypatch.delenv("HERMES_SHARED_FOLDER", raising=False)
        monkeypatch.delenv("HERMES_SHARED_DIR", raising=False)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
        monkeypatch.setattr(ws, "load_config", lambda: self._cloud_config())
        monkeypatch.setattr(ws, "_pass_first_line", lambda entry: "share-password")
        monkeypatch.setattr(ws, "_discover_dav_shared_folder_root", lambda config: None)

        class FakeResponse:
            status = 200

            def __init__(self, body, content_type="application/json"):
                self._body = body if isinstance(body, bytes) else body.encode()
                self.headers = {"content-type": content_type}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, *_args):
                return self._body

        class FakeOpener:
            def open(self, request, timeout=None):
                assert timeout in {20, 30}
                url = request.full_url
                if "/login?" in url and request.data is None:
                    return FakeResponse('<input name="_form_token" value="token-1">', "text/html")
                if "/login?" in url and request.data is not None:
                    return FakeResponse("'X-CSRF-TOKEN': 'csrf-1'", "text/html")
                if "/dirs?" in url:
                    path = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["path"][0]
                    if path == "/Customer Shared":
                        return FakeResponse(json.dumps([
                            {"name": "docs", "type": "dir", "modified_time": "2026-09-10T12:00:00Z"},
                            {"name": "overview.txt", "type": "file", "size": 5},
                            {"name": ".env", "type": "file", "size": 10},
                        ]))
                    if path == "/Customer Shared/docs":
                        return FakeResponse(json.dumps([
                            {"name": "manual.html", "type": "file", "size": 17},
                            {"name": "plan.pdf", "type": "file", "size": 7},
                        ]))
                if "/browse?" in url:
                    path = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["path"][0]
                    if path == "/Customer Shared/overview.txt":
                        return FakeResponse(b"hello", "text/plain")
                    if path == "/Customer Shared/docs/manual.html":
                        return FakeResponse(b"<script>bad()</script>", "text/html")
                raise AssertionError(url)

        monkeypatch.setattr(ws.urllib.request, "build_opener", lambda *_args: FakeOpener())
        monkeypatch.setattr(ws.urllib.request, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("urlopen should not be used")))
        client = TestClient(ws.app)
        headers = {ws._SESSION_HEADER_NAME: ws._SESSION_TOKEN}

        resources = client.get("/api/assistant/resources?refresh=1&resource=shared_folder", headers=headers)
        assert resources.status_code == 200
        shared = resources.json()["shared_folder"]
        assert shared["status"] == "connected"
        assert shared["source"] == "cloud"
        assert shared["can_open_folder"] is False
        assert shared["cloud_url"] == "https://cloud.example.test/web/client/pubshares/share-123/browse?path=%2FCustomer%20Shared"
        assert [item["name"] for item in shared["items"]] == ["docs", "overview.txt"]
        assert shared["items"][0]["cloud_url"].endswith("path=%2FCustomer%20Shared%2Fdocs")
        manual = shared["items"][0]["children"][0]
        assert manual["open_url"] == "/api/assistant/shared-folder/open?path=docs%2Fmanual.html"
        assert manual["reference_uri"] == "shared://docs/manual.html"

        file_open = client.get("/api/assistant/shared-folder/open?path=overview.txt", headers=headers)
        assert file_open.status_code == 200
        assert file_open.content == b"hello"
        assert file_open.headers["content-type"].startswith("text/plain")

        attachment = client.post(
            "/api/assistant/attachments/resource",
            headers=headers,
            json={
                "kind": "shared_file",
                "session_id": "session/with unsafe chars",
                "item": {"open_url": "/api/assistant/shared-folder/open?path=overview.txt"},
            },
        )
        assert attachment.status_code == 200
        uploaded = attachment.json()["attachments"][0]
        copied = Path(uploaded["path"])
        assert copied.read_bytes() == b"hello"
        assert copied.name.endswith("overview.txt")
        assert {"session_with_unsafe_chars", "session-with-unsafe-chars"} & set(copied.parts)
        assert str(copied).startswith(str(tmp_path / "home" / "dashboard_uploads"))

    def test_cloud_only_webdav_summary_open_and_attachment(self, monkeypatch, tmp_path):
        import hermes_cli.web_server as ws

        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
        monkeypatch.setattr(ws, "load_config", lambda: self._cloud_config(
            type="webdav",
            webdav_url="https://dav.example.test/dav/files/customer",
            username="customer",
        ))
        monkeypatch.setattr(ws, "_pass_first_line", lambda entry: "secret")
        monkeypatch.setattr(ws, "_discover_dav_shared_folder_root", lambda config: None)

        class FakeResponse:
            status = 207

            def __init__(self, body, content_type="application/xml", status=207):
                self._body = body if isinstance(body, bytes) else body.encode()
                self.headers = {"content-type": content_type}
                self.status = status

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, *_args):
                return self._body

        def fake_urlopen(request, timeout=None):
            assert "Authorization" in request.headers
            assert "secret" not in repr(request.headers)
            url_path = urllib.parse.unquote(urllib.parse.urlparse(request.full_url).path).rstrip("/")
            if request.get_method() == "PROPFIND" and url_path.endswith("/Customer Shared"):
                return FakeResponse(self._cloud_response_xml(
                    ("/Customer%20Shared/", "Customer Shared", True, 0),
                    ("/Customer%20Shared/docs/", "docs", True, 0),
                    ("/Customer%20Shared/readme.txt", "readme.txt", False, 6),
                    ("/Customer%20Shared/team-token.txt", "team-token.txt", False, 6),
                ))
            if request.get_method() == "PROPFIND" and url_path.endswith("/Customer Shared/docs"):
                return FakeResponse(self._cloud_response_xml(
                    ("/Customer%20Shared/docs/", "docs", True, 0),
                    ("/Customer%20Shared/docs/guide.pdf", "guide.pdf", False, 7),
                ))
            if request.get_method() == "GET" and url_path.endswith("/Customer Shared/docs/guide.pdf"):
                return FakeResponse(b"%PDF-1\n", "application/pdf", status=200)
            raise AssertionError(f"{request.get_method()} {request.full_url}")

        monkeypatch.setattr(ws.urllib.request, "urlopen", fake_urlopen)
        client = TestClient(ws.app)
        headers = {ws._SESSION_HEADER_NAME: ws._SESSION_TOKEN}

        resources = client.get("/api/assistant/resources?refresh=1&resource=shared_folder", headers=headers)
        assert resources.status_code == 200
        shared = resources.json()["shared_folder"]
        assert shared["status"] == "connected"
        assert shared["source"] == "cloud"
        assert [item["name"] for item in shared["items"]] == ["docs", "readme.txt"]
        guide = shared["items"][0]["children"][0]
        assert guide["open_url"] == "/api/assistant/shared-folder/open?path=docs%2Fguide.pdf"

        opened = client.get(guide["open_url"], headers=headers)
        assert opened.status_code == 200
        assert opened.content == b"%PDF-1\n"
        assert opened.headers["content-type"].startswith("application/pdf")

        attached = client.post(
            "/api/assistant/attachments/resource",
            headers=headers,
            json={"kind": "shared_file", "session_id": "s1", "item": {"open_url": guide["open_url"]}},
        )
        assert attached.status_code == 200
        assert Path(attached.json()["attachments"][0]["path"]).read_bytes() == b"%PDF-1\n"

    def test_shared_folder_local_mount_precedes_cloud(self, monkeypatch, tmp_path):
        import hermes_cli.web_server as ws

        shared = tmp_path / "shared"
        shared.mkdir()
        (shared / "local.txt").write_text("local", encoding="utf-8")
        monkeypatch.setenv("AIWERK_SHARED_FOLDER", str(shared))
        monkeypatch.setattr(ws, "load_config", lambda: self._cloud_config())

        def fail_cloud(*_args, **_kwargs):
            raise AssertionError("cloud must not be queried when local mount is present")

        monkeypatch.setattr(ws, "_webdav_cloud_items", fail_cloud, raising=False)
        monkeypatch.setattr(ws, "_sftpgo_pubshare_items", fail_cloud, raising=False)
        monkeypatch.setattr(ws, "_download_webdav_cloud_file", fail_cloud, raising=False)
        monkeypatch.setattr(ws, "_download_sftpgo_pubshare_file", fail_cloud, raising=False)
        client = TestClient(ws.app)
        headers = {ws._SESSION_HEADER_NAME: ws._SESSION_TOKEN}

        resources = client.get("/api/assistant/resources?refresh=1&resource=shared_folder", headers=headers)
        assert resources.status_code == 200
        shared_folder = resources.json()["shared_folder"]
        assert shared_folder["source"] == "local"
        assert [item["name"] for item in shared_folder["items"]] == ["local.txt"]
        assert client.get("/api/assistant/shared-folder/open?path=local.txt", headers=headers).text == "local"

    def test_cloud_shared_folder_open_rejects_unsafe_and_oversized_files(self, monkeypatch, tmp_path):
        import hermes_cli.web_server as ws

        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
        monkeypatch.setattr(ws, "load_config", lambda: self._cloud_config(
            type="webdav",
            webdav_url="https://dav.example.test/dav/files/customer",
            username="customer",
        ))
        monkeypatch.setattr(ws, "_pass_first_line", lambda entry: "secret")
        monkeypatch.setattr(ws, "_discover_dav_shared_folder_root", lambda config: None)
        too_large = b"x" * (ws._ASSISTANT_SHARED_FILE_OPEN_MAX_BYTES + 1)

        class FakeResponse:
            status = 200
            headers = {"content-type": "text/plain"}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, limit=-1):
                return too_large[:limit]

        monkeypatch.setattr(ws.urllib.request, "urlopen", lambda *_args, **_kwargs: FakeResponse())
        client = TestClient(ws.app)
        headers = {ws._SESSION_HEADER_NAME: ws._SESSION_TOKEN}

        assert client.get("/api/assistant/shared-folder/open?path=../secret.txt", headers=headers).status_code == 404
        assert client.get("/api/assistant/shared-folder/open?path=team-token.txt", headers=headers).status_code == 404
        assert client.get("/api/assistant/shared-folder/open?path=big.txt", headers=headers).status_code == 404

    def test_hidden_shared_names_are_rejected_by_shared_folder_listing(self, monkeypatch, tmp_path):
        import hermes_cli.web_server as ws

        shared = tmp_path / "shared"
        shared.mkdir()
        (shared / "visible.txt").write_text("ok", encoding="utf-8")
        for name in ("config.yaml", "auth.json", "id_rsa", "known_hosts", "team-token.txt"):
            (shared / name).write_text("secret", encoding="utf-8")
        monkeypatch.setenv("AIWERK_CUI_SHARED_FOLDER", str(shared))

        summary = ws._shared_folder_summary({}, request=None)
        names = {item["name"] for item in summary["items"]}

        assert "visible.txt" in names
        assert names.isdisjoint({"config.yaml", "auth.json", "id_rsa", "known_hosts", "team-token.txt"})

    def test_active_content_media_types_force_attachment_for_extensionless_files(self):
        import hermes_cli.web_server as ws

        for media_type in (
            "text/html",
            "application/xhtml+xml",
            "image/svg+xml",
            "application/xml",
            "text/xml",
            "application/xslt+xml",
            "text/javascript",
            "application/javascript",
            "application/ecmascript",
            "text/ecmascript",
            "text/x-component",
            "message/rfc822",
        ):
            assert ws._is_active_shared_media_type("download", media_type)
            assert ws._safe_shared_open_disposition("download", media_type) == (
                "application/octet-stream",
                "attachment",
            )

    def test_cui_message_projection_restores_tool_reasoning_display_fields_and_redacts_metadata(self):
        import hermes_cli.web_server as ws

        secret = "Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456"
        host_path = "/home/customer/private/report.csv"
        row = ws._project_cui_message_rows_public([
            {
                "id": 7,
                "role": "assistant",
                "content": "done",
                "tool_call_id": "call-1",
                "tool_name": "read_file",
                "reasoning": f"used {secret}",
                "reasoning_content": "compact",
                "reasoning_details": [{"text": secret}],
                "display_kind": "tool",
                "display_metadata": {
                    "safe_label": "Report",
                    "nested": {"path": host_path, "token": secret},
                },
                "model_config": {"private": True},
            }
        ])[0]

        for key in (
            "tool_call_id",
            "tool_name",
            "reasoning",
            "reasoning_content",
            "reasoning_details",
            "display_kind",
            "display_metadata",
        ):
            assert key in row
        rendered = repr(row)
        assert "safe_label" in rendered
        assert "Report" in rendered
        assert "abcdefghijklmnopqrstuvwxyz123456" not in rendered
        assert host_path not in rendered
        assert "model_config" not in rendered

    def test_session_list_projection_restores_profile_and_activity_fields(self):
        import hermes_cli.web_server as ws

        row = ws._project_session_list_rows_public([
            {
                "id": "s1",
                "title": "Customer",
                "is_active": True,
                "profile": "default",
                "is_default_profile": True,
                "cwd": "/home/customer/private",
            }
        ])[0]

        assert row["is_active"] is True
        assert row["profile"] == "default"
        assert row["is_default_profile"] is True
        assert "cwd" not in row

    def test_session_detail_projection_preserves_default_profile_stamp(self):
        from hermes_cli.web_routers.sessions import _project_session_detail_public

        row = _project_session_detail_public({
            "id": "s1",
            "title": "Customer",
            "profile": "default",
            "is_default_profile": True,
            "cwd": "/home/customer/private",
            "model_config": {"secret": "raw"},
        })

        assert row["profile"] == "default"
        assert row["is_default_profile"] is True
        assert "cwd" not in row
        assert "model_config" not in row

    def test_aiwerk_bridge_child_catalog_uses_restored_labels_descriptions_and_slugs(self, monkeypatch):
        import hermes_cli.web_server as ws

        config = {"mcp_servers": {"aiwerk_bridge": {"enabled": True, "subservers": {
            "google-workspace-aiwerk": {},
            "google-workspace-demo": {},
            "serpapi": {},
            "smallinvoice": {},
        }}}}
        monkeypatch.setattr(ws, "_call_aiwerk_bridge_tool", lambda *args, **kwargs: {})

        children = ws._aiwerk_bridge_subservers(config)
        by_id = {child["name"]: child for child in children}

        assert by_id["google-workspace-aiwerk"]["label"] == "Google Workspace AIWerk"
        assert by_id["google-workspace-aiwerk"]["description"] == "Gmail, Kalender und Drive"
        assert by_id["google-workspace-aiwerk"]["catalog_slug"] == "google-workspace"
        assert by_id["google-workspace-demo"]["label"] == "Google Workspace Demo"
        assert by_id["google-workspace-demo"]["catalog_slug"] == "google-workspace"
        assert by_id["serpapi"]["label"] == "SerpAPI"
        assert by_id["serpapi"]["description"] == "Websuche und SERP-Daten"
        assert by_id["smallinvoice"]["label"] == "Smallinvoice"

    def test_shared_folder_env_precedence_dav_discovery_limits_and_visible_summary(self, monkeypatch, tmp_path):
        import hermes_cli.web_server as ws

        roots = {}
        for key in ("AIWERK_CUI_SHARED_FOLDER", "AIWERK_SHARED_FOLDER", "HERMES_SHARED_FOLDER", "HERMES_SHARED_DIR"):
            root = tmp_path / key
            root.mkdir()
            (root / f"{key}.txt").write_text(key, encoding="utf-8")
            roots[key] = root
            monkeypatch.setenv(key, str(root))

        assert ws._resolve_shared_folder_root({}) == roots["AIWERK_CUI_SHARED_FOLDER"]
        monkeypatch.delenv("AIWERK_CUI_SHARED_FOLDER")
        assert ws._resolve_shared_folder_root({}) == roots["AIWERK_SHARED_FOLDER"]
        monkeypatch.delenv("AIWERK_SHARED_FOLDER")
        assert ws._resolve_shared_folder_root({}) == roots["HERMES_SHARED_FOLDER"]
        monkeypatch.delenv("HERMES_SHARED_FOLDER")
        assert ws._resolve_shared_folder_root({}) == roots["HERMES_SHARED_DIR"]

        for key in ("HERMES_SHARED_DIR",):
            monkeypatch.delenv(key)
        runtime = tmp_path / "runtime"
        dav = runtime / "gvfs" / "dav:host=dav.aiwerk.ch,ssl=true" / "Hermes-Shared"
        dav.mkdir(parents=True)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
        assert ws._resolve_shared_folder_root({}) == dav

        for index in range(45):
            (dav / f"file-{index:02d}.txt").write_text("x", encoding="utf-8")
        nested = dav / "nested"
        current = nested
        for depth in range(7):
            current.mkdir(exist_ok=True)
            (current / f"depth-{depth}.txt").write_text("x", encoding="utf-8")
            current = current / "next"

        summary = ws._shared_folder_summary({}, request=None)
        assert len(summary["items"]) <= 40
        assert summary["visible_items"] == 12
        assert summary["max_depth"] == 5

    def test_attachment_upload_contract_accepts_restored_file_families_and_enforces_batch_limits(self, monkeypatch, tmp_path):
        from starlette.testclient import TestClient
        import hermes_cli.web_server as ws

        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
        client = TestClient(ws.app)
        headers = {ws._SESSION_HEADER_NAME: ws._SESSION_TOKEN}
        files = [
            ("files", ("config.yaml", b"name: value\n", "application/x-yaml")),
            ("files", ("doc.docx", _minimal_docx_bytes("hello"), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")),
            ("files", ("voice.mp3", b"ID3", "audio/mpeg")),
            ("files", ("clip.mp4", b"\x00\x00\x00\x18ftypmp42", "video/mp4")),
        ]

        response = client.post("/api/assistant/attachments", headers=headers, files=files)

        assert response.status_code == 200
        attachments = response.json()["attachments"]
        assert {item["name"] for item in attachments} == {"config.yaml", "doc.docx", "voice.mp3", "clip.mp4"}
        assert next(item for item in attachments if item["name"] == "config.yaml")["extraction"] == "text"
        assert next(item for item in attachments if item["name"] == "doc.docx")["extraction"] == "docx"

        too_many = [("files", (f"{index}.txt", b"x", "text/plain")) for index in range(11)]
        response = client.post("/api/assistant/attachments", headers=headers, files=too_many)
        assert response.status_code == 413

    def test_text_and_docx_extraction_restored_bounds(self, tmp_path):
        import hermes_cli.web_server as ws

        text_path = tmp_path / "large.txt"
        text_path.write_text("a" * 70_000, encoding="utf-8")
        extracted, kind = ws._extract_uploaded_text(text_path, "text/plain")
        assert kind == "text"
        assert len(extracted) == 60_000

        docx_path = tmp_path / "large.docx"
        docx_path.write_bytes(_minimal_docx_bytes("b" * 30_000))
        extracted, kind = ws._extract_uploaded_text(docx_path, "")
        assert kind == "docx"
        assert len(extracted) == 30_000

    def test_contact_routes_are_registered_and_return_resource_backed_items(self, monkeypatch):
        from starlette.testclient import TestClient
        import hermes_cli.web_server as ws

        payload = {
            "contacts": {
                "relevant": [{"display_name": "Relevant Person", "email": "relevant@example.com"}],
                "frequent": [{"display_name": "Frequent Person", "email": "frequent@example.com"}],
                "total_count": 2,
            }
        }
        monkeypatch.setattr(ws, "_assistant_resources_payload", lambda *args, **kwargs: payload)
        client = TestClient(ws.app)
        headers = {ws._SESSION_HEADER_NAME: ws._SESSION_TOKEN}

        context = client.get("/api/cui/context/contacts?session_id=s1", headers=headers)
        frequent = client.get("/api/cui/contacts/frequent", headers=headers)

        assert context.status_code == 200
        assert context.json()["items"] == payload["contacts"]["relevant"]
        assert context.json()["session_id"] == "s1"
        assert frequent.status_code == 200
        assert frequent.json()["items"] == payload["contacts"]["frequent"]
        assert frequent.json()["total_count"] == 2

    def test_explicit_contact_search_restores_gmail_interaction_enrichment(self, monkeypatch):
        import hermes_cli.web_server as ws

        config = {
            "assistant": {
                "contacts": {
                    "accounts": [
                        {
                            "backend": "google_workspace",
                            "address": "me@example.com",
                            "mcp_server": "google-workspace-aiwerk",
                            "server": "google-workspace-aiwerk",
                            "user_google_email": "me@example.com",
                        }
                    ]
                },
                "email": {
                    "accounts": [
                        {
                            "backend": "google_workspace",
                            "address": "me@example.com",
                            "mcp_server": "google-workspace-aiwerk",
                            "server": "google-workspace-aiwerk",
                            "user_google_email": "me@example.com",
                        },
                    ]
                }
            }
        }
        bridge_calls = []

        def bridge(_config, *, server, tool, params):
            bridge_calls.append({"server": server, "tool": tool, "params": dict(params)})
            if tool == "search_contacts":
                return {"contacts": []}
            if tool == "list_contacts":
                return {"contacts": []}
            if tool == "search_gmail_messages":
                message_id = "ada-message" if params["query"] == "Ada" else "other-message"
                return {
                    "messages": [{"id": message_id}],
                    "result": {"structuredContent": {"result": f"Message ID: {message_id}"}},
                }
            if tool == "get_gmail_messages_content_batch":
                message_id = params["message_ids"][0]
                if message_id == "ada-message":
                    sender = "Ada Lovelace <ada@example.com>"
                else:
                    sender = "Other Person <other@example.com>"
                return {
                    "messages": [
                        {
                            "id": message_id,
                            "from": sender,
                            "to": "Me <me@example.com>",
                            "date": "2026-09-10T10:00:00Z",
                        }
                    ],
                    "result": {
                        "structuredContent": {
                            "result": (
                                f"Message ID: {message_id}\n"
                                f"From: {sender}\n"
                                "To: Me <me@example.com>\n"
                                "Date: Thu, 10 Sep 2026 10:00:00 +0000"
                            )
                        }
                    },
                }
            raise AssertionError(tool)

        monkeypatch.setattr(ws, "load_config", lambda: config)
        monkeypatch.setattr(ws, "_call_aiwerk_bridge_tool", bridge)
        monkeypatch.setattr(ws, "_email_summary", lambda _config: {"accounts": [{"address": "me@example.com"}]})
        monkeypatch.setattr(ws, "_calendar_summary", lambda _config: {"accounts": []})
        monkeypatch.setattr(ws, "_read_manual_contacts", lambda: [])
        monkeypatch.setattr(ws, "_read_contacts_store_payload", lambda: {"hidden": []})

        payload = ws._search_contacts_payload("Ada")

        assert [item["email"] for item in payload["items"]] == ["ada@example.com"]
        gmail_searches = [call for call in bridge_calls if call["tool"] == "search_gmail_messages"]
        assert any(call["params"] == {
            "query": "Ada",
            "user_google_email": "me@example.com",
            "page_size": 200,
        } for call in gmail_searches)
        content_calls = [call for call in bridge_calls if call["tool"] == "get_gmail_messages_content_batch"]
        assert content_calls
        assert any(call["params"] == {
            "message_ids": ["ada-message"],
            "user_google_email": "me@example.com",
            "format": "metadata",
        } for call in content_calls)

        bridge_calls.clear()
        monkeypatch.setenv("AIWERK_CUI_CONTACTS_DISABLE_GMAIL_INTERACTIONS", "1")
        assert ws._search_contacts_payload("Ada")["items"] == []
        assert not any(call["tool"] == "search_gmail_messages" for call in bridge_calls)

    def test_explicit_contact_search_filters_own_email_from_gmail_interactions(self, monkeypatch):
        import hermes_cli.web_server as ws

        config = {
            "assistant": {
                "contacts": {
                    "accounts": [
                        {
                            "backend": "google_workspace",
                            "address": "me@example.com",
                            "mcp_server": "google-workspace-aiwerk",
                            "server": "google-workspace-aiwerk",
                            "user_google_email": "me@example.com",
                        }
                    ]
                },
                "email": {"accounts": [{"backend": "google_workspace", "address": "me@example.com"}]},
            }
        }

        def bridge(_config, *, server, tool, params):
            if tool in {"search_contacts", "list_contacts"}:
                return {"contacts": []}
            if tool == "search_gmail_messages":
                return {
                    "messages": [{"id": "own-message"}],
                    "result": {"structuredContent": {"result": "Message ID: own-message"}},
                }
            return {
                "messages": [{"from": "Me <me@example.com>", "to": "Me <me@example.com>"}],
                "result": {
                    "structuredContent": {
                        "result": "Message ID: own-message\nFrom: Me <me@example.com>\nTo: Me <me@example.com>"
                    }
                },
            }

        monkeypatch.setattr(ws, "load_config", lambda: config)
        monkeypatch.setattr(ws, "_call_aiwerk_bridge_tool", bridge)
        monkeypatch.setattr(ws, "_email_summary", lambda _config: {"accounts": [{"address": "me@example.com"}]})
        monkeypatch.setattr(ws, "_calendar_summary", lambda _config: {"accounts": []})
        monkeypatch.setattr(ws, "_read_manual_contacts", lambda: [])
        monkeypatch.setattr(ws, "_read_contacts_store_payload", lambda: {"hidden": []})

        assert ws._search_contacts_payload("me")["items"] == []

    def test_explicit_contact_search_uses_saved_fallback_sorts_name_expansion_and_final_filters(self, monkeypatch):
        import hermes_cli.web_server as ws

        calls = []
        config = {
            "assistant": {
                "email": {"accounts": [{"backend": "google_workspace", "address": "me@example.com"}]},
                "contacts": {"accounts": [{"backend": "google_workspace", "address": "me@example.com"}]},
            }
        }

        def google_contacts(_config, query="", limit=None, sort_order=None):
            calls.append({"query": query, "limit": limit, "sort_order": sort_order})
            if query:
                return []
            if sort_order == "FIRST_NAME_ASCENDING":
                return [
                    {"display_name": "Ádám Smith", "email": "adam.smith@example.com", "source_badges": ["Google"]},
                    {"display_name": "Hidden Smith", "email": "hidden@example.com", "key": "hide-me"},
                    {"display_name": "Me Smith", "email": "me@example.com"},
                    {"display_name": "Newsletter Smith", "email": "newsletter@example.com"},
                ]
            return [{"display_name": "No Reply Smith", "email": "noreply@example.com"}]

        monkeypatch.setattr(ws, "load_config", lambda: config)
        monkeypatch.setattr(ws, "_assistant_resources_payload", lambda force_refresh=False: {
            "email": {"accounts": [{"address": "me@example.com"}]},
            "calendar": {"accounts": []},
            "contacts": {"items": [{"display_name": "Cached Smith", "email": "cached@example.com"}]},
        })
        monkeypatch.setattr(ws, "_contacts_from_google_workspace", google_contacts)
        monkeypatch.setattr(ws, "_contacts_from_google_workspace_query_interactions", lambda *args, **kwargs: [])
        monkeypatch.setattr(ws, "_read_manual_contacts", lambda: [{"display_name": "Manual Smith", "email": "manual@example.com"}])
        monkeypatch.setattr(ws, "_read_contacts_store_payload", lambda: {"hidden": ["hide-me"]})

        payload = ws._search_contacts_payload("Smith", limit=3)

        assert [item["email"] for item in payload["items"]] == [
            "adam.smith@example.com",
            "manual@example.com",
            "cached@example.com",
        ]
        assert payload["total_count"] == 3
        assert {"query": "", "limit": 1000, "sort_order": None} in calls
        assert {"query": "", "limit": 1000, "sort_order": "FIRST_NAME_ASCENDING"} in calls
        expansion_queries = [call["query"] for call in calls if call["query"]]
        assert expansion_queries == [
            "Smith",
            "Adam Smith",
            "Smith Adam",
            "Ádám Smith",
            "Smith Ádám",
        ]

    def test_explicit_contact_search_dedupes_exact_query_variants(self, monkeypatch):
        import hermes_cli.web_server as ws

        calls = []

        def google_contacts(_config, query="", limit=None, sort_order=None):
            calls.append((query, sort_order))
            return []

        monkeypatch.setattr(ws, "load_config", lambda: {})
        monkeypatch.setattr(ws, "_assistant_resources_payload", lambda force_refresh=False: {
            "email": {"accounts": []},
            "calendar": {"accounts": []},
            "contacts": {"items": []},
        })
        monkeypatch.setattr(ws, "_contacts_from_google_workspace", google_contacts)
        monkeypatch.setattr(ws, "_contacts_from_google_workspace_query_interactions", lambda *args, **kwargs: [])
        monkeypatch.setattr(ws, "_read_manual_contacts", lambda: [])
        monkeypatch.setattr(ws, "_read_contacts_store_payload", lambda: {"hidden": []})

        ws._search_contacts_payload("Ádám", limit=2)

        searched = [query for query, sort_order in calls if query and sort_order is None]
        assert searched.count("Ádám") == 1
        assert searched.count("adam") == 1

    def test_assistant_resources_caches_connectors_with_env_sensitive_identity(self, monkeypatch):
        import hermes_cli.web_server as ws

        calls = []

        def connector(config, shared_folder, email, calendar, *, include_live_bridge_children=True):
            calls.append({
                "env": ws.os.environ.get("AIWERK_CUI_GOOGLE_EMAIL", ""),
                "include_live_bridge_children": include_live_bridge_children,
                "shared_status": shared_folder["status"],
                "email_status": email["status"],
                "calendar_status": calendar["status"],
            })
            return [{"id": f"connector-{len(calls)}"}]

        monkeypatch.setattr(ws, "load_config", lambda: {"assistant": {}})
        monkeypatch.setattr(ws, "_email_summary", lambda _config: {"status": "email", "items": []})
        monkeypatch.setattr(ws, "_calendar_summary", lambda _config: {"status": "calendar", "items": []})
        monkeypatch.setattr(ws, "_shared_folder_summary", lambda _config, request=None: {"status": "shared_folder", "items": []})
        monkeypatch.setattr(ws, "_vaultwarden_summary", lambda _config: {"status": "vault", "items": []})
        monkeypatch.setattr(ws, "_todo_summary", lambda _config: {"status": "todos", "items": []})
        monkeypatch.setattr(ws, "_contacts_summary", lambda _config, _email, _calendar: {"status": "contacts", "items": []})
        monkeypatch.setattr(ws, "_connector_summary", connector)
        lock = getattr(ws, "_ASSISTANT_RESOURCE_LOCK", getattr(ws, "_ASSISTANT_RESOURCE_CACHE_LOCK", None))
        assert lock is not None
        with lock:
            ws._ASSISTANT_RESOURCE_CACHE.clear()
            if hasattr(ws, "_ASSISTANT_RESOURCE_REFRESHING"):
                ws._ASSISTANT_RESOURCE_REFRESHING.clear()

        first = ws._assistant_resources_payload(force_refresh=True, refresh_resource="connectors")
        second = ws._assistant_resources_payload(force_refresh=False)

        assert first["connectors"] == [{"id": "connector-1"}]
        assert first["cache"]["resources"]["connectors"]["cached"] is False
        assert first["cache"]["resources"]["connectors"]["ttl_seconds"] == ws._ASSISTANT_RESOURCE_CACHE_TTLS["connectors"]
        assert second["connectors"] == [{"id": "connector-1"}]
        assert second["cache"]["resources"]["connectors"]["cached"] is True
        assert len(calls) == 1

        monkeypatch.setenv("AIWERK_CUI_EMAIL_SUMMARY_JSON", '{"summary":"env"}')
        changed_env = ws._assistant_resources_payload(force_refresh=False)
        changed_env_used_placeholder = changed_env["connectors"] == []
        if changed_env["connectors"] == []:
            assert changed_env["cache"]["resources"]["connectors"]["stale"] is True
            deadline = time.monotonic() + 5
            while changed_env["connectors"] == []:
                assert time.monotonic() < deadline
                time.sleep(0.01)
                changed_env = ws._assistant_resources_payload(force_refresh=False)

        assert changed_env["connectors"] == [{"id": "connector-2"}]
        assert changed_env["cache"]["resources"]["connectors"]["cached"] is changed_env_used_placeholder
        assert [call["env"] for call in calls] == ["", ""]

        refreshed = ws._assistant_resources_payload(force_refresh=True, refresh_resource="connectors")

        assert refreshed["connectors"] == [{"id": "connector-3"}]
        assert refreshed["cache"]["resources"]["connectors"]["cached"] is False
        assert calls[-1]["include_live_bridge_children"] is True

    def _resource_cache_lock(self, ws):
        return getattr(ws, "_ASSISTANT_RESOURCE_LOCK", getattr(ws, "_ASSISTANT_RESOURCE_CACHE_LOCK", None))

    def test_assistant_resources_returns_connectors_placeholder_and_publishes_background_refresh(self, monkeypatch):
        import hermes_cli.web_server as ws

        release = threading.Event()
        calls = []

        def connector(config, shared_folder, email, calendar, *, include_live_bridge_children=True):
            calls.append(include_live_bridge_children)
            assert release.wait(timeout=5)
            return [{"id": "live-connector"}]

        monkeypatch.setattr(ws, "load_config", lambda: {"assistant": {}})
        monkeypatch.setattr(ws, "_email_summary", lambda _config: {"status": "email", "items": []})
        monkeypatch.setattr(ws, "_calendar_summary", lambda _config: {"status": "calendar", "items": []})
        monkeypatch.setattr(ws, "_shared_folder_summary", lambda _config, request=None: {"status": "shared_folder", "items": []})
        monkeypatch.setattr(ws, "_vaultwarden_summary", lambda _config: {"status": "vault", "items": []})
        monkeypatch.setattr(ws, "_todo_summary", lambda _config: {"status": "todos", "items": []})
        monkeypatch.setattr(ws, "_contacts_summary", lambda _config, _email, _calendar: {"status": "contacts", "items": []})
        monkeypatch.setattr(ws, "_connector_summary", connector)
        with self._resource_cache_lock(ws):
            ws._ASSISTANT_RESOURCE_CACHE.clear()
            ws._ASSISTANT_RESOURCE_REFRESHING.clear()

        first = ws._assistant_resources_payload(force_refresh=False, refresh_resource=None)

        assert first["connectors"] == []
        assert first["cache"]["resources"]["connectors"]["cached"] is False
        assert first["cache"]["resources"]["connectors"]["stale"] is True
        assert first["cache"]["resources"]["connectors"]["refreshing"] is True

        release.set()
        deadline = time.monotonic() + 5
        while calls and ws._ASSISTANT_RESOURCE_REFRESHING:
            assert time.monotonic() < deadline
            time.sleep(0.01)

        second = ws._assistant_resources_payload(force_refresh=False, refresh_resource=None)

        assert second["connectors"] == [{"id": "live-connector"}]
        assert second["cache"]["resources"]["connectors"]["cached"] is True
        assert calls == [True]

    def test_assistant_resource_cache_deepcopies_payloads_on_write_and_read(self):
        import hermes_cli.web_server as ws

        full_key = "contacts:deepcopy"
        with self._resource_cache_lock(ws):
            ws._ASSISTANT_RESOURCE_CACHE.pop(full_key, None)
            ws._ASSISTANT_RESOURCE_REFRESHING.discard(full_key)

        source = {"items": [{"email": "ada@example.com"}]}
        ws._assistant_write_resource_cache(full_key, source, ttl_seconds=60)
        source["items"][0]["email"] = "mutated@example.com"

        first, _ = ws._assistant_cached_resource("contacts", 60, "deepcopy", lambda: {"items": []})
        first["items"][0]["email"] = "returned-mutated@example.com"
        second, _ = ws._assistant_cached_resource("contacts", 60, "deepcopy", lambda: {"items": []})

        assert second["items"][0]["email"] == "ada@example.com"

    def test_assistant_resource_cache_returns_stale_payload_with_last_error_on_builder_exception(self):
        import hermes_cli.web_server as ws

        full_key = "email:stale-error"
        with self._resource_cache_lock(ws):
            ws._ASSISTANT_RESOURCE_CACHE.pop(full_key, None)
            ws._ASSISTANT_RESOURCE_REFRESHING.discard(full_key)
        ws._assistant_write_resource_cache(full_key, {"summary": "seed"}, ttl_seconds=0)

        payload, meta = ws._assistant_cached_resource(
            "email",
            60,
            "stale-error",
            lambda: (_ for _ in ()).throw(RuntimeError("boom")),
            force_refresh=True,
        )

        assert payload == {"summary": "seed", "last_error": "Aktualisierung fehlgeschlagen"}
        assert meta["cached"] is True
        assert meta["stale"] is True
        assert meta["last_error"] == "Aktualisierung fehlgeschlagen"

    def test_assistant_resource_cache_force_refresh_generation_blocks_stale_publication(self):
        import hermes_cli.web_server as ws

        full_key = "email:generation"
        with self._resource_cache_lock(ws):
            ws._ASSISTANT_RESOURCE_CACHE.pop(full_key, None)
            ws._ASSISTANT_RESOURCE_REFRESHING.discard(full_key)
        ws._assistant_write_resource_cache(full_key, {"summary": "seed"}, ttl_seconds=0)

        old_started = threading.Event()
        release_old = threading.Event()

        def old_builder():
            old_started.set()
            assert release_old.wait(timeout=5)
            return {"summary": "old"}

        payload, meta = ws._assistant_cached_resource(
            "email",
            60,
            "generation",
            old_builder,
            stale_while_revalidate=True,
        )
        assert payload["summary"] == "seed"
        assert meta["stale"] is True
        assert old_started.wait(timeout=5)

        fresh, _ = ws._assistant_cached_resource(
            "email",
            60,
            "generation",
            lambda: {"summary": "fresh"},
            force_refresh=True,
        )
        release_old.set()
        deadline = time.monotonic() + 5
        while full_key in ws._ASSISTANT_RESOURCE_REFRESHING:
            assert time.monotonic() < deadline
            time.sleep(0.01)

        assert fresh["summary"] == "fresh"
        assert ws._ASSISTANT_RESOURCE_CACHE[full_key]["payload"]["summary"] == "fresh"

    def test_wave1_restored_route_paths_census_matches_registered_routes(self):
        import hermes_cli.web_server as ws

        route_paths = {
            getattr(route, "path", "")
            for route in ws.app.routes
            if getattr(route, "path", "").startswith("/api/")
        }

        required = set(ws._WAVE1_RESTORED_ROUTE_PATHS)
        assert "/api/cui/context/contacts" in required
        assert "/api/cui/contacts/frequent" in required
        assert "/api/assistant/resources" in required
        assert "/api/cui/contacts/search" in required
        assert "/api/cui/contacts/hide" in required
        assert "/api/assistant/support" in required
        assert "/api/assistant/todos/add" in required
        assert "/api/assistant/todos/update" in required
        assert required <= route_paths

    def test_cache_ttls_env_signature_and_managed_autonomy_policy_are_restored(self, monkeypatch):
        import hermes_cli.web_server as ws
        import hermes_cli.web_server_chat as chat

        assert ws._ASSISTANT_RESOURCE_CACHE_TTLS == {
            "email": 3600,
            "calendar": 1800,
            "shared_folder": 3600,
            "vault": 900,
            "todos": 60,
            "contacts": 1800,
            "connectors": 3600,
        }
        assert ws._CUI_MANAGED_AUTONOMY_FEATURE_ENABLED is True
        assert ws._CUI_MANAGED_AUTONOMY_ROLES == frozenset({"admin", "operator"})
        assert chat._CUI_MANAGED_TRUST_ENV == frozenset(ws._CUI_MANAGED_AUTONOMY_ENV_KEYS)

        baseline = ws._assistant_resource_config_signature({"dashboard": {}}, None)
        for key in _RESTORED_ENV_KEYS:
            monkeypatch.setenv(key, f"value-for-{key}")
            changed = ws._assistant_resource_config_signature({"dashboard": {}}, None)
            assert changed != baseline, key
            monkeypatch.delenv(key)

    def test_read_tool_aliases_and_admin_action_policy_are_preserved(self):
        import hermes_cli.web_server as ws

        assert {"get-specific-calendar-view", "get-specific-calendar-event", "health_check"} <= ws._AIWERK_BRIDGE_READ_TOOLS
        assert ws._admin_api_action_for("POST", "/api/providers/custom-endpoints") == "mcp.policy.change"
        assert ws._admin_api_action_for("POST", "/api/sessions/owner-backfill") == "tenant.cross_access"

    def test_email_env_overrides_drive_google_himalaya_read_and_maildir_producers(self, monkeypatch, tmp_path):
        import hermes_cli.web_server as ws

        bridge_calls = []

        def bridge(_config, *, server, tool, params):
            bridge_calls.append({"server": server, "tool": tool, "params": dict(params)})
            if tool == "search_gmail_messages":
                return {"messages": [{"id": f"{params['query']}-id"}]}
            return {"messages": [{"id": params["message_ids"][0], "sender": "Sender <sender@example.com>"}]}

        monkeypatch.setattr(ws, "_call_aiwerk_bridge_tool", bridge)
        monkeypatch.setenv("AIWERK_CUI_EMAIL_BACKEND", "google_workspace")
        monkeypatch.setenv("AIWERK_CUI_GOOGLE_WORKSPACE_SERVER", "env-server")
        monkeypatch.setenv("AIWERK_CUI_GOOGLE_EMAIL", "env@example.com")
        monkeypatch.setenv("AIWERK_CUI_GMAIL_UNREAD_QUERY", "label:unread")
        monkeypatch.setenv("AIWERK_CUI_GMAIL_LATEST_QUERY", "label:latest")

        summary = ws._google_workspace_email_summary({"assistant": {"email": {}}})

        assert summary["status"] == "connected"
        assert {call["server"] for call in bridge_calls} == {"env-server"}
        search_calls = [call for call in bridge_calls if call["tool"] == "search_gmail_messages"]
        assert [call["params"]["query"] for call in search_calls] == ["label:unread", "label:latest"]
        content_calls = [call for call in bridge_calls if call["tool"] == "get_gmail_messages_content_batch"]
        assert content_calls
        assert all(call["params"]["user_google_email"] == "env@example.com" for call in content_calls)

        bridge_calls.clear()
        assert ws._run_google_workspace_message_read({}, {}, "read-1")
        assert bridge_calls == [
            {
                "server": "env-server",
                "tool": "get_gmail_messages_content_batch",
                "params": {"message_ids": ["read-1"], "user_google_email": "env@example.com", "format": "full"},
            }
        ]

        bridge_calls.clear()
        monkeypatch.setenv("AIWERK_CUI_EMAIL_DISABLE_AIWERK_BRIDGE", "1")
        assert ws._google_workspace_email_summary({}) is None
        assert bridge_calls == []
        monkeypatch.delenv("AIWERK_CUI_EMAIL_DISABLE_AIWERK_BRIDGE")

        himalaya_commands = []

        def run_himalaya(cmd, **_kwargs):
            himalaya_commands.append(list(cmd))
            if "envelope" in cmd:
                stdout = json.dumps([
                    {"id": "h1", "subject": "Hello", "from": "Sender <sender@example.com>"}
                ])
            else:
                stdout = "Message ID: h1\nSubject: Hello\n\nBody"
            return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

        monkeypatch.setattr(ws.shutil, "which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(ws.subprocess, "run", run_himalaya)
        monkeypatch.setenv("AIWERK_CUI_EMAIL_ACCOUNT", "env-account")
        monkeypatch.setenv("HIMALAYA_ACCOUNT", "fallback-account")
        monkeypatch.setenv("AIWERK_CUI_EMAIL_FOLDER", "EnvInbox")
        monkeypatch.setenv("HIMALAYA_FOLDER", "FallbackInbox")

        assert ws._run_himalaya_envelope_list(page_size=7)
        assert ws._run_himalaya_message_read("h1") == "Message ID: h1\nSubject: Hello\n\nBody"
        assert himalaya_commands[0][himalaya_commands[0].index("--account") + 1] == "env-account"
        assert himalaya_commands[0][himalaya_commands[0].index("--folder") + 1] == "EnvInbox"
        assert himalaya_commands[1][himalaya_commands[1].index("--account") + 1] == "env-account"
        assert himalaya_commands[1][himalaya_commands[1].index("--folder") + 1] == "EnvInbox"

        himalaya_commands.clear()
        monkeypatch.delenv("AIWERK_CUI_EMAIL_ACCOUNT")
        monkeypatch.delenv("AIWERK_CUI_EMAIL_FOLDER")
        ws._run_himalaya_envelope_list(page_size=7)
        assert himalaya_commands[0][himalaya_commands[0].index("--account") + 1] == "fallback-account"
        assert himalaya_commands[0][himalaya_commands[0].index("--folder") + 1] == "FallbackInbox"

        monkeypatch.setenv("AIWERK_CUI_EMAIL_DISABLE_HIMALAYA", "1")
        assert ws._himalaya_email_summary({}) is None

        maildir = tmp_path / "maildir"
        (maildir / "new").mkdir(parents=True)
        (maildir / "new" / "1").write_text("one", encoding="utf-8")
        (maildir / "new" / "2").write_text("two", encoding="utf-8")
        monkeypatch.delenv("AIWERK_CUI_EMAIL_DISABLE_HIMALAYA")
        monkeypatch.setenv("AIWERK_CUI_EMAIL_DISABLE_AIWERK_BRIDGE", "1")
        monkeypatch.setenv("AIWERK_CUI_MAILDIR", str(maildir))
        assert ws._email_summary({})["unread_count"] == 2
        monkeypatch.delenv("AIWERK_CUI_MAILDIR")
        monkeypatch.setenv("MAILDIR", str(maildir))
        assert ws._email_summary({})["unread_count"] == 2

    def test_email_reader_get_fetches_sanitizes_and_items_expose_open_url(self, monkeypatch):
        import hermes_cli.web_server as ws

        config = {
            "assistant": {
                "email": {
                    "accounts": [
                        {
                            "backend": "google_workspace",
                            "address": "owner@example.test",
                            "mcp_server": "google-workspace-aiwerk",
                            "user_google_email": "owner@example.test",
                        }
                    ]
                }
            }
        }
        monkeypatch.setattr(ws, "load_config", lambda: config)
        monkeypatch.setattr(
            ws,
            "_assistant_resources_payload",
            lambda *args, **kwargs: {
                "email": {
                    "accounts": [
                        {
                            "address": "owner@example.test",
                            "items": [
                                {
                                    "id": "msg-1",
                                    "message_id": "msg-1",
                                    "sender": "Sender <sender@example.test>",
                                    "subject": "Rendered subject",
                                    "received_at": "2026-09-10T12:00:00Z",
                                }
                            ],
                        }
                    ]
                }
            },
        )
        reads = []

        def read_message(_config, account, message_id):
            reads.append((account["address"], message_id))
            return (
                "From: Sender <sender@example.test>\n"
                "Subject: Rendered subject\n"
                "\n"
                "<b>Hello</b><script>alert('x')</script> "
                "https://secret.example/path?token=abc /home/customer/private.txt"
            )

        monkeypatch.setattr(ws, "_run_google_workspace_message_read", read_message)
        client = TestClient(ws.app)
        headers = {ws._SESSION_HEADER_NAME: ws._SESSION_TOKEN}

        response = client.get(
            "/api/assistant/email/view?account=owner%40example.test&id=msg-1",
            headers=headers,
        )

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert reads == [("owner@example.test", "msg-1")]
        body = response.text
        assert "Rendered subject" in body
        assert "Hello" in body
        assert "[LINK]" in body
        assert "<script" not in body
        assert "secret.example" not in body

        if hasattr(ws, "_attach_email_open_urls"):
            account = {"address": "owner@example.test"}
            ws._attach_email_open_urls(account, [{"id": "msg-1"}, {"message_id": "msg-2"}])
            assert account["items"][0]["open_url"] == "/api/assistant/email/view?account=owner%40example.test&id=msg-1"
            assert account["items"][1]["open_url"] == "/api/assistant/email/view?account=owner%40example.test&id=msg-2"

    def test_email_reader_himalaya_missing_binary_surfaces_backend_unavailable(self, monkeypatch):
        import hermes_cli.web_server as ws

        config = {
            "assistant": {
                "email": {
                    "accounts": [
                        {
                            "backend": "himalaya",
                            "address": "office@example.test",
                            "account": "office",
                            "folder": "INBOX",
                        }
                    ]
                }
            }
        }
        monkeypatch.setattr(ws, "load_config", lambda: config)
        monkeypatch.setattr(
            ws,
            "_assistant_resources_payload",
            lambda *args, **kwargs: {
                "email": {
                    "accounts": [
                        {
                            "address": "office@example.test",
                            "items": [{"id": "h1", "message_id": "h1", "subject": "Himalaya subject"}],
                        }
                    ]
                }
            },
        )
        monkeypatch.setattr(ws.shutil, "which", lambda name: None)
        client = TestClient(ws.app)
        headers = {ws._SESSION_HEADER_NAME: ws._SESSION_TOKEN}

        response = client.get(
            "/api/assistant/email/view?account=office%40example.test&id=h1",
            headers=headers,
        )

        assert response.status_code == 503
        assert response.json()["detail"] == "Himalaya is not installed"

    def test_resource_attachment_shared_file_copies_real_image_artifact(self, monkeypatch, tmp_path):
        import hermes_cli.web_server as ws

        shared_root = tmp_path / "shared"
        source_dir = shared_root / "images"
        source_dir.mkdir(parents=True)
        source = source_dir / "photo.png"
        source.write_bytes(b"\x89PNG\r\n\x1a\nreal image bytes")
        monkeypatch.setenv("AIWERK_SHARED_FOLDER", str(shared_root))
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
        client = TestClient(ws.app)
        headers = {ws._SESSION_HEADER_NAME: ws._SESSION_TOKEN}

        response = client.post(
            "/api/assistant/attachments/resource",
            headers=headers,
            json={
                "kind": "shared_file",
                "session_id": "session/with unsafe chars",
                "item": {
                    "name": "photo.png",
                    "open_url": "/api/assistant/shared-folder/open?path=images/photo.png",
                    "mime": "image/png",
                },
            },
        )

        assert response.status_code == 200
        attachment = response.json()["attachments"][0]
        copied = Path(attachment["path"])
        assert copied.read_bytes() == source.read_bytes()
        assert copied != source
        assert {"session_with_unsafe_chars", "session-with-unsafe-chars"} & set(copied.parts)
        assert attachment["name"] == "photo.png"
        assert attachment["type"] == "image/png"
        assert attachment["size"] == source.stat().st_size
        assert attachment["is_image"] is True
        assert attachment["extraction"] == "image"
        assert not attachment.get("extracted_text")

    def test_assistant_mode_allows_artifact_open_safe_methods_and_denies_unlisted_route(
        self, monkeypatch, tmp_path
    ):
        import hermes_cli.web_server as ws

        monkeypatch.setattr(ws, "_DASHBOARD_MODE", "assistant")
        ws.app.state.auth_required = False
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
        artifact = ws._assistant_upload_root() / "artifact.txt"
        artifact.write_text("safe artifact", encoding="utf-8")
        client = TestClient(ws.app)
        headers = {ws._SESSION_HEADER_NAME: ws._SESSION_TOKEN}

        path = "/api/assistant/artifacts/open"
        params = {"path": str(artifact)}
        assert client.get(path, params=params, headers=headers).status_code == 200
        route_methods = {
            method
            for route in ws.app.routes
            if getattr(route, "path", "") == path
            for method in getattr(route, "methods", set())
        }
        if "HEAD" in route_methods:
            assert client.head(path, params=params, headers=headers).status_code == 200
        if "OPTIONS" in route_methods:
            assert client.options(path, params=params, headers=headers).status_code != 404
        assert client.get("/api/config", headers=headers).status_code == 404

    def test_assistant_transcribe_multipart_uses_bounded_read_and_real_transcription(self, monkeypatch, tmp_path):
        import hermes_cli.web_server as ws
        import tools.transcription_tools as transcription_tools

        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
        calls = []

        def transcribe(path):
            calls.append(Path(path))
            return {"success": True, "transcript": "real words", "provider": "test-stt"}

        monkeypatch.setattr(transcription_tools, "transcribe_audio", transcribe)
        client = TestClient(ws.app)
        headers = {ws._SESSION_HEADER_NAME: ws._SESSION_TOKEN}

        response = client.post(
            "/api/assistant/transcribe",
            headers=headers,
            data={"session_id": "voice-session"},
            files={"file": ("voice.webm", b"audio bytes", "audio/webm")},
        )

        assert response.status_code == 200
        assert response.json() == {"text": "real words", "provider": "test-stt"}
        assert len(calls) == 1
        assert calls[0].read_bytes() == b"audio bytes"
        assert "voice-session" in str(calls[0])

    def test_read_upload_reads_only_limit_plus_one_and_callers_reject_before_retaining(self, monkeypatch, tmp_path):
        import hermes_cli.web_server as ws

        class BoundedUpload:
            filename = "large.txt"
            content_type = "text/plain"

            def __init__(self):
                self.requested = []

            async def read(self, size=-1):
                self.requested.append(size)
                return b"x" * size

        if hasattr(ws, "_read_upload"):
            upload = BoundedUpload()
            data = _run_async(ws._read_upload(upload, ws._ASSISTANT_UPLOAD_MAX_FILE_BYTES))

            assert upload.requested == [ws._ASSISTANT_UPLOAD_MAX_FILE_BYTES + 1]
            assert len(data) == ws._ASSISTANT_UPLOAD_MAX_FILE_BYTES + 1

        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
        client = TestClient(ws.app)
        headers = {ws._SESSION_HEADER_NAME: ws._SESSION_TOKEN}
        response = client.post(
            "/api/assistant/attachments",
            headers=headers,
            files={"files": ("large.txt", b"x" * (ws._ASSISTANT_UPLOAD_MAX_FILE_BYTES + 1), "text/plain")},
        )

        assert response.status_code == 413
        assert not any(ws._assistant_upload_root().rglob("large.txt"))

    def test_bridge_subserver_missing_status_defaults_connected_and_projects_consumer_fields(self):
        import hermes_cli.web_server as ws

        item = ws._aiwerk_bridge_subserver_item({"id": "google-workspace-demo"})
        if item.get("id") == "aiwerk-bridge-id-:-google-workspace-demo":
            item = ws._aiwerk_bridge_subserver_item("google-workspace-demo", {})

        assert item["id"] == "aiwerk-bridge-google-workspace-demo"
        assert item["label"] == "Google Workspace Demo"
        assert item["description"] == "Gmail, Kalender und Drive"
        assert item["status"] == "connected"
        assert item["status_label"] == "Verbunden"
        assert item["capabilities"] == ["Bridge-Subserver"]
        assert item["open_url"] == "https://aiwerkmcp.com/#/catalog/google-workspace"
        if "catalog_slug" in item:
            assert item["catalog_slug"] == "google-workspace"

    def test_contact_env_overrides_drive_bridge_himalaya_and_interaction_sources(self, monkeypatch):
        import hermes_cli.web_server as ws

        config = {
            "assistant": {
                "email": {
                    "accounts": [
                        {"backend": "google_workspace"},
                        {"backend": "himalaya", "account": "configured-himalaya"},
                    ]
                }
            }
        }
        gmail_searches = []

        def search_ids(_config, query="", *, server=None, user_google_email=None, page_size=None):
            gmail_searches.append({
                "query": query,
                "server": server,
                "user_google_email": user_google_email,
                "page_size": page_size,
            })
            return []

        monkeypatch.setattr(ws, "_gmail_bridge_search_message_ids", search_ids)
        monkeypatch.setenv("AIWERK_CUI_CONTACTS_INTERACTION_SCAN_LIMIT", "999")
        monkeypatch.setenv("AIWERK_CUI_CONTACTS_SENT_QUERY", "sent-env")
        monkeypatch.setenv("AIWERK_CUI_CONTACTS_INBOX_QUERY", "inbox-env")
        monkeypatch.setenv("AIWERK_CUI_GOOGLE_WORKSPACE_SERVER", "contact-server")
        monkeypatch.setenv("AIWERK_CUI_GOOGLE_EMAIL", "contact@example.com")

        assert ws._contacts_from_google_workspace_interactions(config, set()) == []
        assert gmail_searches == [
            {"query": "sent-env", "server": "contact-server", "user_google_email": "contact@example.com", "page_size": 100},
            {"query": "inbox-env", "server": "contact-server", "user_google_email": "contact@example.com", "page_size": 100},
        ]

        gmail_searches.clear()
        monkeypatch.setenv("AIWERK_CUI_CONTACTS_DISABLE_GMAIL_INTERACTIONS", "1")
        assert ws._contacts_from_google_workspace_interactions(config, set()) == []
        assert gmail_searches == []
        monkeypatch.delenv("AIWERK_CUI_CONTACTS_DISABLE_GMAIL_INTERACTIONS")
        monkeypatch.setenv("AIWERK_CUI_EMAIL_DISABLE_AIWERK_BRIDGE", "1")
        assert ws._contacts_from_google_workspace_interactions(config, set()) == []
        assert gmail_searches == []
        monkeypatch.delenv("AIWERK_CUI_EMAIL_DISABLE_AIWERK_BRIDGE")

        bridge_calls = []

        def contacts_bridge(_config, *, server, tool, params):
            bridge_calls.append({"server": server, "tool": tool, "params": dict(params)})
            return {"contacts": [{"display_name": "Bridge Person", "email": "person@example.com"}]}

        monkeypatch.setattr(ws, "_call_aiwerk_bridge_tool", contacts_bridge)
        monkeypatch.setenv("AIWERK_CUI_CONTACTS_PAGE_SIZE", "9999")
        assert ws._contacts_from_google_workspace(config, query="", limit=None)
        assert bridge_calls[-1]["tool"] == "list_contacts"
        assert bridge_calls[-1]["params"]["page_size"] == 1000
        assert bridge_calls[-1]["params"]["user_google_email"] == "contact@example.com"

        assert ws._contacts_from_google_workspace(config, query="person", limit=999)
        assert bridge_calls[-1]["tool"] == "search_contacts"
        assert bridge_calls[-1]["params"]["page_size"] == 30

        bridge_calls.clear()
        monkeypatch.setenv("AIWERK_CUI_CONTACTS_DISABLE_AIWERK_BRIDGE", "1")
        assert ws._contacts_from_google_workspace(config, query="person") == []
        assert bridge_calls == []
        monkeypatch.delenv("AIWERK_CUI_CONTACTS_DISABLE_AIWERK_BRIDGE")

        himalaya_calls = []

        def himalaya_list(*, query=None, page_size=0, account=None, folder=None):
            himalaya_calls.append({"query": query, "page_size": page_size, "account": account, "folder": folder})
            return []

        monkeypatch.setattr(ws, "_run_himalaya_envelope_list", himalaya_list)
        monkeypatch.setenv("AIWERK_CUI_CONTACTS_INTERACTION_SCAN_LIMIT", "77")
        monkeypatch.setenv("AIWERK_CUI_CONTACTS_HIMALAYA_SENT_FOLDER", "SentEnv")
        monkeypatch.setenv("AIWERK_CUI_CONTACTS_HIMALAYA_INBOX_FOLDER", "InboxEnv")

        assert ws._contacts_from_himalaya_interactions(config, set()) == []
        assert himalaya_calls == [
            {"query": None, "page_size": 77, "account": "configured-himalaya", "folder": "SentEnv"},
            {"query": None, "page_size": 77, "account": "configured-himalaya", "folder": "InboxEnv"},
        ]

        himalaya_calls.clear()
        monkeypatch.setenv("AIWERK_CUI_CONTACTS_DISABLE_HIMALAYA_INTERACTIONS", "1")
        assert ws._contacts_from_himalaya_interactions(config, set()) == []
        assert himalaya_calls == []
        monkeypatch.delenv("AIWERK_CUI_CONTACTS_DISABLE_HIMALAYA_INTERACTIONS")
        monkeypatch.setenv("AIWERK_CUI_EMAIL_DISABLE_HIMALAYA", "1")
        assert ws._contacts_from_himalaya_interactions(config, set()) == []
        assert himalaya_calls == []

    def test_calendar_locale_vault_support_whatsapp_env_consumers(self, monkeypatch, tmp_path):
        import hermes_cli.web_server as ws

        bridge_calls = []

        def calendar_bridge(_config, *, server, tool, params):
            bridge_calls.append({"server": server, "tool": tool, "params": dict(params)})
            if tool == "get-calendar-view":
                return {
                    "value": [
                        {"id": str(index), "subject": f"Outlook {index}", "start": {"dateTime": f"2026-01-0{index + 1}T10:00:00", "timeZone": "UTC"}, "end": {"dateTime": f"2026-01-0{index + 1}T11:00:00", "timeZone": "UTC"}}
                        for index in range(5)
                    ]
                }
            return {"text": "- \"Google 1\" (Starts: 2026-01-01T10:00:00Z, Ends: 2026-01-01T11:00:00Z) ID: g1"}

        monkeypatch.setattr(ws, "_call_aiwerk_bridge_tool", calendar_bridge)
        monkeypatch.setenv("AIWERK_CUI_CALENDAR_HORIZON_DAYS", "21")
        monkeypatch.setenv("AIWERK_CUI_CALENDAR_MAX_RESULTS", "3")
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)

        google = ws._google_workspace_calendar_summary(
            {},
            {"backend": "google_workspace", "address": "g@example.com", "mcp_server": "google-server"},
            now=now,
        )
        google_call = next(call for call in bridge_calls if call["tool"] == "get_events")
        assert google["status"] == "connected"
        assert google_call["params"]["max_results"] == 3
        assert google_call["params"]["time_min"] == "2026-01-01T00:00:00Z"
        assert google_call["params"]["time_max"] == "2026-01-22T00:00:00Z"

        microsoft = ws._microsoft_calendar_summary(
            {},
            {"backend": "microsoft_calendar", "address": "m@example.com", "mcp_server": "ms-server"},
            now=now,
        )
        microsoft_call = next(call for call in bridge_calls if call["tool"] == "get-calendar-view")
        assert microsoft_call["params"]["endDateTime"] == "2026-01-22T00:00:00Z"
        assert len(microsoft["items"]) == 3

        monkeypatch.setenv("AIWERK_CUI_LANGUAGE", "Hungarian")
        assert ws._assistant_ui_locale_from_config({}) == "hu"

        monkeypatch.setenv("AIWERK_CUI_VAULT_URL", "https://vault.example")
        assert ws._vault_url_from_config({}) == "https://vault.example"
        assert ws._vaultwarden_summary({})["url"] == "https://vault.example"

        vault_json = tmp_path / "vault.json"
        vault_json.write_text(json.dumps({"status": "connected", "summary": "from vault json"}), encoding="utf-8")
        monkeypatch.setenv("AIWERK_CUI_VAULT_SUMMARY_JSON", str(vault_json))
        assert ws._vaultwarden_summary({})["summary"] == "from vault json"

        calendar_json = tmp_path / "calendar.json"
        calendar_json.write_text(json.dumps({"summary": "from calendar json", "items": []}), encoding="utf-8")
        monkeypatch.setenv("AIWERK_CUI_CALENDAR_SUMMARY_JSON", str(calendar_json))
        assert ws._calendar_summary({})["summary"] == "from calendar json"

        support_log = tmp_path / "support.jsonl"
        monkeypatch.setenv("AIWERK_CUI_SUPPORT_LOG", str(support_log))
        assert ws._support_log_path() == support_log
        monkeypatch.setenv("AIWERK_CUI_SUPPORT_TARGET", "mailto:support@example.com")
        assert ws._support_delivery_targets({}) == [{"target": "mailto:support@example.com"}]
        monkeypatch.setenv("AIWERK_SYSTEM_TARGET", "mailto:ops@example.com")
        assert ws._system_delivery_targets({}) == [{"target": "mailto:ops@example.com"}]

        import hermes_cli.web_routers.messaging as messaging

        monkeypatch.setenv("WHATSAPP_MODE", "self-chat")
        payload = messaging._messaging_platform_payload(
            {
                "id": "whatsapp",
                "name": "WhatsApp",
                "description": "",
                "docs_url": "",
                "required_env": set(),
                "env_vars": ["WHATSAPP_MODE"],
            },
            {},
            {"platforms": {"whatsapp": {"state": "running"}}},
        )
        assert payload["whatsapp_setup"]["mode"] == "self-chat"

    def test_environment_overrides_restore_precedence_bounds_disable_switches_and_cache_identity(self, monkeypatch, tmp_path):
        import hermes_cli.web_server as ws

        assert set(_RESTORED_ENV_KEYS) <= set(ws._ASSISTANT_RESOURCE_CACHE_ENV_KEYS)

        monkeypatch.setenv("AIWERK_CUI_AGENT_NAME", "Env Hermes")
        assert ws._assistant_display_name_from_config({"display": {"agent_name": "Config Hermes"}}) == "Env Hermes"

        monkeypatch.setenv("AIWERK_CUI_USER_DISPLAY_NAME", "Display User")
        monkeypatch.setenv("AIWERK_CUI_USER_NAME", "Raw User")
        monkeypatch.setenv("HERMES_USER_DISPLAY_NAME", "Hermes User")
        assert ws._assistant_user_display_name_from_config({}) == "Display User"

        monkeypatch.setenv("AIWERK_CUI_CONTACTS_PAGE_SIZE", "999")
        assert ws._contacts_page_size({}) == 500
        monkeypatch.setenv("AIWERK_CUI_CONTACTS_PAGE_SIZE", "0")
        assert ws._contacts_page_size({}) == 1

        monkeypatch.setenv("AIWERK_CUI_CONTACTS_RELEVANCE_WINDOW_DAYS", "999")
        assert ws._contacts_relevance_window_days() == 90
        monkeypatch.setenv("AIWERK_CUI_CONTACTS_SAVED_TOP_UP_TARGET", "999")
        assert ws._contacts_saved_top_up_target() == 50
        monkeypatch.setenv("AIWERK_CUI_CONTACTS_SAVED_TOP_UP_TARGET", "0")
        assert ws._contacts_saved_top_up_target() == 20

        monkeypatch.setenv("AIWERK_CUI_EMAIL_DISABLE_AIWERK_BRIDGE", "1")
        assert ws._email_summary({"assistant": {"email": {"accounts": [{"backend": "google_workspace"}]}}})["status"] == "not_configured"

        email_json = tmp_path / "email.json"
        email_json.write_text(json.dumps({"summary": "from env", "accounts": []}), encoding="utf-8")
        monkeypatch.delenv("AIWERK_CUI_EMAIL_DISABLE_AIWERK_BRIDGE")
        monkeypatch.setenv("AIWERK_CUI_EMAIL_SUMMARY_JSON", str(email_json))
        assert ws._email_summary({})["summary"] == "from env"


def _minimal_docx_bytes(text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr(
            "word/document.xml",
            (
                "<?xml version='1.0' encoding='UTF-8'?>"
                "<w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'>"
                f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body>"
                "</w:document>"
            ),
        )
    return buf.getvalue()


def _run_async(coro):
    import asyncio

    return asyncio.run(coro)
