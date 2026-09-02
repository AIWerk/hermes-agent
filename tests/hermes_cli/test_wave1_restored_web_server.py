"""Restored historical Wave 1 web-server behavior tests."""

import asyncio
import os
import json
import urllib.error
from datetime import datetime, timezone, timedelta
from email.message import Message
import shutil
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest
import yaml

from hermes_cli.config import (
    reload_env,
    redact_key,
    OPTIONAL_ENV_VARS,
    DEFAULT_CONFIG,
)

class TestWave1RestoredTestWebServerEndpoints:
    def test_customer_session_list_projection_sanitizes_values_and_omits_cwd(self):
        import hermes_cli.web_server as ws

        secret = "Authorization: Bearer super-secret-token"
        host_path = "/home/service/tenant-secret/project"
        rows = ws._project_session_list_rows_public(
            [
                {
                    "id": "session-1",
                    "title": f"Work {secret} in {host_path}",
                    "preview": f"Preview {secret}",
                    "summary": {"text": f"Summary at {host_path} {secret}"},
                    "topics": [f"Topic {secret}"],
                    "cwd": host_path,
                    "model_config": {"private": True},
                }
            ]
        )

        rendered = repr(rows)
        assert "super-secret-token" not in rendered
        assert host_path not in rendered
        assert "cwd" not in rows[0]
        assert "model_config" not in rows[0]

    @pytest.fixture(autouse=True)
    def _setup_test_client(self, monkeypatch, _isolate_hermes_home):
        """Create a TestClient and isolate the state DB under the test HERMES_HOME."""
        try:
            from starlette.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi/starlette not installed")

        import hermes_state
        from hermes_constants import get_hermes_home
        import hermes_cli.web_server as web_server
        from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

        web_server._MCP_BRIDGE_SESSIONS.clear()
        web_server._MCP_BRIDGE_REQUEST_IDS.clear()
        monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", get_hermes_home() / "state.db")

        self.client = TestClient(app)
        self.client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN

    def test_cui_actor_full_sessions_still_emit_public_projection(self, monkeypatch):
        import hermes_cli.web_server as ws
        from hermes_state import SessionDB

        actor = {"tenant_id": "tenant-1", "actor_id": "user-1", "role": "user"}
        model_config = {
            "_cui_visibility_scope": "customer",
            "_cui_actor_role": "user",
            "_cui_actor_id": actor["actor_id"],
            "_cui_tenant_id": actor["tenant_id"],
            "private_note": "must-not-cross",
        }
        db = SessionDB()
        try:
            db.create_session(
                session_id="actor-full-row",
                source="cli",
                system_prompt="private system prompt",
                model_config=model_config,
            )
            db.append_message("actor-full-row", role="user", content="customer question")
        finally:
            db.close()

        monkeypatch.setattr(ws, "_cui_actor_context_from_request", lambda request: actor)
        resp = self.client.get("/api/sessions?limit=20&offset=0&full=1")

        assert resp.status_code == 200
        row = next(s for s in resp.json()["sessions"] if s["id"] == "actor-full-row")
        internal_or_secret = {
            key
            for key, visibility in SessionDB._SESSION_COLUMN_VISIBILITY.items()
            if visibility != "public"
        }
        assert internal_or_secret.isdisjoint(row)

    def test_cui_message_projection_preserves_sanitized_compaction_display_content(self):
        import hermes_cli.web_server as ws

        secret = "abcdefghijklmnopqrstuvwxyz123456"
        rows = ws._project_cui_message_rows_public(
            [
                {
                    "id": 1,
                    "role": "user",
                    "content": "physical carrier",
                    "display_content": f"Authorization: Bearer {secret}",
                }
            ]
        )

        assert rows[0]["content"] == "physical carrier"
        assert "display_content" in rows[0]
        assert secret not in rows[0]["display_content"]

    def test_extracted_profile_lists_require_actor_filter_and_public_projection(self):
        from pathlib import Path

        source = (
            Path(__file__).parents[2] / "hermes_cli" / "web_routers" / "profiles.py"
        ).read_text()
        profile_list = source[source.index("def get_profiles_sessions("):source.index("def get_profiles_sessions_sidebar(")]
        sidebar = source[source.index("def get_profiles_sessions_sidebar("):]
        for section in (profile_list, sidebar):
            assert "request: Request" in section
            assert '_cui_actor_context_from_request' in section
            assert '_session_visible_to_cui_actor' in section
            assert '_project_session_list_rows_public' in section
            assert '_list_sessions_rich_all' in section
            assert 'compact_rows": False if actor else' in section
        assert "limit=None if actor" not in profile_list
        assert "min(max(limit + offset, limit), 500)" not in profile_list

    def test_extracted_session_search_and_bulk_admin_routes_are_actor_safe(self):
        from pathlib import Path

        source = (
            Path(__file__).parents[2] / "hermes_cli" / "web_routers" / "sessions.py"
        ).read_text()
        search = source[source.index("async def search_sessions("):source.index("async def bulk_delete_sessions_endpoint(")]
        assert "request: Request" in search
        assert "_session_visible_to_cui_actor" in search
        assert "_project_session_list_rows_public" in search
        assert "_sanitize_public_message_value" in search
        assert "raw_session = db.get_session(raw_sid)" in search
        assert "while len(seen) < safe_limit:" in search
        assert "fetch_limit *= 2" in search
        assert "id_fetch_limit *= 2" in search

        from hermes_cli import web_server as runtime_web_server
        from hermes_cli.web_deps import late

        secret = "abcdefghijklmnopqrstuvwxyz123456"
        sanitizer = runtime_web_server._sanitize_public_message_value
        extracted_sanitizer = late("_sanitize_public_message_value")
        assert callable(extracted_sanitizer)
        for active_sanitizer in (sanitizer, extracted_sanitizer):
            assert secret not in repr(
                active_sanitizer({"snippet": f"Authorization: Bearer {secret}"})
            )

        bulk = source[source.index("async def bulk_delete_sessions_endpoint("):source.index("async def import_sessions_endpoint(")]
        assert "request: Request" in bulk
        assert "_enforce_cui_session_visible" in bulk
        for name in (
            "import_sessions_endpoint",
            "count_empty_sessions_endpoint",
            "delete_empty_sessions_endpoint",
            "get_session_stats",
        ):
            section = source[source.index(f"async def {name}("):]
            assert "request: Request" in section.split("async def ", 1)[0] or "request: Request" in section[:300]
            assert "_cui_actor_context_from_request" in section[:1400]

    def test_profiles_sidebar_messaging_total_is_exact_beyond_display_limit(self):
        from hermes_state import SessionDB

        db = SessionDB()
        try:
            for i in range(7):
                sid = f"sb-message-{i}"
                db.create_session(session_id=sid, source="telegram")
                db.append_message(session_id=sid, role="user", content="hi")
        finally:
            db.close()

        resp = self.client.get(
            "/api/profiles/sessions/sidebar?recents_profile=all&recents_limit=1"
            "&cron_limit=1&messaging_limit=3"
            "&messaging_exclude=cron,cli,codex,desktop,gateway,local,tui"
        )
        assert resp.status_code == 200
        messaging = resp.json()["messaging"]
        assert messaging["total"] == 7
        assert len(messaging["sessions"]) == 3

    def test_assistant_tts_reuses_cached_audio(self, monkeypatch):
        import tools.tts_tool as tts_tool

        calls = []

        def fake_tts(text, output_path):
            calls.append(text)
            Path(output_path).write_bytes(b"ID3cached-audio")
            return json.dumps({
                "success": True,
                "file_path": output_path,
                "provider": "test",
            })

        monkeypatch.setattr(tts_tool, "text_to_speech_tool", fake_tts)

        payload = {"text": "Bitte lies diese Antwort vor.", "session_id": "session-a"}
        first = self.client.post("/api/assistant/tts", json=payload)
        second = self.client.post("/api/assistant/tts", json=payload)

        assert first.status_code == 200
        assert second.status_code == 200
        assert first.content == b"ID3cached-audio"
        assert second.content == b"ID3cached-audio"
        assert first.headers["X-Hermes-TTS-Cache"] == "miss"
        assert second.headers["X-Hermes-TTS-Cache"] == "hit"
        assert calls == [payload["text"]]

    def test_assistant_support_saves_log_and_delivers_telegram(self, monkeypatch):
        import json
        import hermes_cli.web_server as web_server
        from hermes_constants import get_hermes_home

        sent = []

        def fake_deliver(targets, text):
            sent.append((targets, text))
            return True, []

        monkeypatch.setenv("AIWERK_SUPPORT_TELEGRAM_CHAT_ID", "-1001234567890")
        monkeypatch.setattr(web_server, "_deliver_support_message", fake_deliver)

        resp = self.client.post(
            "/api/assistant/support",
            json={
                "category": "E-Mail / Kalender / Dateien",
                "message": "Customer sieht meine neuen Mails nicht.",
                "include_diagnostics": True,
                "session_id": "session-123",
                "session_title": "Mailproblem",
                "connection": "open",
                "diagnostics": {
                    "email": {"status": "auth_required", "summary": "Anmeldung nötig"},
                    "secret": "token=should-not-be-raw-but-is-truncated-only",
                },
            },
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["delivered"] is True
        assert data["support_id"].startswith("sup_")
        assert sent
        assert sent[0][0] == ["telegram:-1001234567890"]
        assert "AIWerk Supportmeldung" in sent[0][1]
        assert "Customer sieht meine neuen Mails nicht." in sent[0][1]
        log_path = get_hermes_home() / "aiwerk-support" / "inbox.jsonl"
        line = log_path.read_text(encoding="utf-8").strip().splitlines()[-1]
        record = json.loads(line)
        assert record["support_id"] == data["support_id"]
        assert record["session_id"] == "session-123"
        assert record["diagnostics"]["email"]["status"] == "auth_required"

    def test_assistant_support_keeps_saved_message_when_delivery_fails(self, monkeypatch):
        import hermes_cli.web_server as web_server
        from hermes_constants import get_hermes_home

        monkeypatch.setenv("AIWERK_SUPPORT_TELEGRAM_CHAT_ID", "-1001234567890")
        monkeypatch.setattr(web_server, "_deliver_support_message", lambda targets, text: (False, ["telegram:-1001234567890: offline"]))

        resp = self.client.post(
            "/api/assistant/support",
            json={"category": "Sonstiges", "message": "Bitte prüfen.", "include_diagnostics": False},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["delivered"] is False
        assert data["queued"] is True
        assert (get_hermes_home() / "aiwerk-support" / "inbox.jsonl").exists()

    def test_assistant_support_does_not_fallback_to_gateway_home_channel(self, monkeypatch):
        import hermes_cli.web_server as web_server

        sent = []
        monkeypatch.setenv("AIWERK_CUI_SUPPORT_TARGET", "telegram")
        monkeypatch.delenv("AIWERK_SUPPORT_TELEGRAM_CHAT_ID", raising=False)
        monkeypatch.delenv("AIWERK_CUI_SUPPORT_TELEGRAM_CHAT_ID", raising=False)
        monkeypatch.setattr(web_server, "_deliver_support_message", lambda targets, text: sent.append(targets) or (False, []))

        resp = self.client.post(
            "/api/assistant/support",
            json={"category": "Sonstiges", "message": "Bitte prüfen.", "include_diagnostics": False},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["delivered"] is False
        assert data["queued"] is True
        assert sent == [[]]

    def test_aiwerk_system_targets_use_explicit_telegram_chat_id(self, monkeypatch):
        import hermes_cli.web_server as web_server

        monkeypatch.setenv("AIWERK_SYSTEM_TELEGRAM_CHAT_ID", "-1009876543210")
        assert getattr(web_server, "_system_delivery_targets")({}) == ["telegram:-1009876543210"]

    def test_aiwerk_system_targets_ignore_bare_telegram_home_channel(self, monkeypatch):
        import hermes_cli.web_server as web_server

        monkeypatch.delenv("AIWERK_SYSTEM_TELEGRAM_CHAT_ID", raising=False)
        assert getattr(web_server, "_system_delivery_targets")({"dashboard": {"notifications": {"delivery_target": "telegram"}}}) == []

    def test_google_workspace_calendar_auth_error_surfaces_reconnect_state(self, monkeypatch):
        import hermes_cli.web_server as web_server

        def bridge_error(config, *, server, tool, params):
            return {
                "result": {
                    "isError": True,
                    "structuredContent": {
                        "result": "Error calling tool 'get_events': **Authentication Required: Token Expired/Revoked for Google Calendar**"
                    },
                }
            }

        monkeypatch.setattr(web_server, "_call_aiwerk_bridge_tool", bridge_error)

        account = {
            "address": "user@example.ch",
            "backend": "google_workspace",
            "mcp_server": "google-workspace-customer",
            "user_google_email": "user@example.ch",
        }
        summary = web_server._google_workspace_calendar_summary({}, account)
        assert summary is not None

        assert summary["status"] == "auth_required"
        assert summary["summary"] == "Google Kalender neu verbinden"
        assert summary["items"] == []

        merged = web_server._merge_calendar_summaries([summary])
        assert merged["status"] == "auth_required"
        assert "neu verbinden" in merged["summary"]

    def test_contacts_summary_derives_safe_fallbacks_from_email_and_calendar(self, monkeypatch):
        import hermes_cli.web_server as web_server

        monkeypatch.delenv("AIWERK_CUI_CONTACTS_JSON", raising=False)
        monkeypatch.setattr(web_server, "_read_manual_contacts", lambda: [])
        email = {"accounts": [{
            "label": "AIWerk",
            "address": "contact@example.test",
            "items": [{"sender": "Max Muster <max@example.ch>"}],
        }]}
        calendar = {"accounts": [{"label": "Kalender", "address": "team@example.ch"}]}

        summary = getattr(web_server, "_contacts_summary")({}, email, calendar)

        emails = {item["email"] for item in summary["relevant"]}
        assert emails == {"max@example.ch"}
        assert "contact@example.test" not in emails
        assert "team@example.ch" not in emails
        assert summary["status"] == "connected"
        assert summary["source_label"] == "Relevante Kontakte"

    def test_cui_contacts_search_queries_google_workspace_bridge(self, monkeypatch):
        import hermes_cli.web_server as web_server

        with web_server._ASSISTANT_RESOURCE_CACHE_LOCK:
            web_server._ASSISTANT_RESOURCE_CACHE.clear()
        calls = []
        monkeypatch.setattr(web_server, "_read_manual_contacts", lambda: [])
        monkeypatch.setattr(web_server, "_email_summary", lambda config: {"status": "connected", "accounts": []})
        monkeypatch.setattr(web_server, "_calendar_summary", lambda config: {"status": "connected", "accounts": []})
        monkeypatch.setattr(web_server, "_shared_folder_summary", lambda config, request=None: {"status": "not_configured"})
        monkeypatch.setattr(web_server, "_vaultwarden_summary", lambda config: {"status": "not_configured"})
        monkeypatch.setattr(web_server, "_todo_summary", lambda config: {"status": "not_configured", "items": []})
        monkeypatch.setattr(web_server, "_connector_summary", lambda config, shared_folder, email, calendar: [])
        monkeypatch.setattr(web_server, "load_config", lambda: {
            "assistant": {"email": {"accounts": [
                {"address": "contact@example.test", "backend": "google_workspace", "mcp_server": "google-workspace-aiwerk", "user_google_email": "contact@example.test"},
                {"address": "owner@example.test", "backend": "google_workspace", "mcp_server": "google-workspace-owner", "user_google_email": "owner@example.test"},
            ]}}
        })

        def fake_bridge_call(config, *, server, tool, params):
            calls.append({"server": server, "tool": tool, "params": params})
            return {"result": {"structuredContent": {"result": f"Contacts for {params['user_google_email']} (1 of 1):\n\nContact ID: c_{server}\nName: Max Treffer\nEmail: max-{server}@example.ch\n\n"}}}

        monkeypatch.setattr(web_server, "_call_aiwerk_bridge_tool", fake_bridge_call)

        resp = self.client.get("/api/cui/contacts/search?q=max")

        assert resp.status_code == 200
        assert resp.json()["total_count"] == 2
        assert {call["server"] for call in calls if call["tool"] == "search_contacts"} == {"google-workspace-aiwerk", "google-workspace-owner"}
        assert all(call["params"].get("query") == "max" for call in calls if call["tool"] == "search_contacts")

    def test_cui_contacts_search_falls_back_to_saved_contacts_and_normalizes_accents(self, monkeypatch):
        import hermes_cli.web_server as web_server

        with web_server._ASSISTANT_RESOURCE_CACHE_LOCK:
            web_server._ASSISTANT_RESOURCE_CACHE.clear()
        monkeypatch.setattr(web_server, "_read_manual_contacts", lambda: [])
        monkeypatch.setattr(web_server, "_email_summary", lambda config: {"status": "connected", "accounts": []})
        monkeypatch.setattr(web_server, "_calendar_summary", lambda config: {"status": "connected", "accounts": []})
        monkeypatch.setattr(web_server, "_shared_folder_summary", lambda config, request=None: {"status": "not_configured"})
        monkeypatch.setattr(web_server, "_vaultwarden_summary", lambda config: {"status": "not_configured"})
        monkeypatch.setattr(web_server, "_todo_summary", lambda config: {"status": "not_configured", "items": []})
        monkeypatch.setattr(web_server, "_connector_summary", lambda config, shared_folder, email, calendar: [])
        monkeypatch.setattr(web_server, "load_config", lambda: {
            "assistant": {"email": {"accounts": [
                {"address": "contact@example.test", "backend": "google_workspace", "mcp_server": "google-workspace-aiwerk", "user_google_email": "contact@example.test"},
            ]}}
        })

        def fake_bridge_call(config, *, server, tool, params):
            if tool == "list_contacts":
                return {"result": {"structuredContent": {"result": "Contacts for contact@example.test (1 of 1):\n\nContact ID: adam\nName: Alex Example\nEmail: adam@example.ch (Work)\n\n"}}}
            if tool == "search_contacts":
                return {"result": {"structuredContent": {"result": "Contacts for contact@example.test (0 of 0):\n\n"}}}
            return {"result": {"structuredContent": {"result": ""}}}

        monkeypatch.setattr(web_server, "_call_aiwerk_bridge_tool", fake_bridge_call)

        resp = self.client.get("/api/cui/contacts/search?q=Alex%20Example")

        assert resp.status_code == 200
        body = resp.json()
        assert body["total_count"] == 1
        assert body["items"][0]["display_name"] == "Alex Example"

    def test_cui_contacts_search_filters_own_and_system_contacts(self, monkeypatch):
        import hermes_cli.web_server as web_server

        with web_server._ASSISTANT_RESOURCE_CACHE_LOCK:
            web_server._ASSISTANT_RESOURCE_CACHE.clear()
        monkeypatch.setattr(web_server, "_read_manual_contacts", lambda: [
            {"display_name": "Self Manual", "email": "contact@example.test", "source_badges": ["Manuell"]},
            {"display_name": "Local Human", "email": "local@example.ch", "source_badges": ["Manuell"]},
        ])
        monkeypatch.setattr(web_server, "_email_summary", lambda config: {"status": "connected", "accounts": [{"address": "contact@example.test", "items": []}]})
        monkeypatch.setattr(web_server, "_calendar_summary", lambda config: {"status": "connected", "accounts": []})
        monkeypatch.setattr(web_server, "_shared_folder_summary", lambda config, request=None: {"status": "not_configured"})
        monkeypatch.setattr(web_server, "_vaultwarden_summary", lambda config: {"status": "not_configured"})
        monkeypatch.setattr(web_server, "_todo_summary", lambda config: {"status": "not_configured", "items": []})
        monkeypatch.setattr(web_server, "_connector_summary", lambda config, shared_folder, email, calendar: [])
        monkeypatch.setattr(web_server, "load_config", lambda: {
            "assistant": {"email": {"accounts": [
                {"address": "contact@example.test", "backend": "google_workspace", "mcp_server": "google-workspace-aiwerk", "user_google_email": "contact@example.test"},
            ]}}
        })

        def fake_bridge_call(config, *, server, tool, params):
            return {"result": {"structuredContent": {"result": "Contacts for contact@example.test (4 of 4):\n\nContact ID: self\nName: Contact Example\nEmail: contact@example.test (Work)\n\nContact ID: noreply\nName: No Reply\nEmail: noreply@example.ch (Work)\n\nContact ID: cf-test\nName: Synthetic+Outlet-CF-Test\nEmail: synthetic-cf-test@example.test (Work)\n\nContact ID: human\nName: Human Match\nEmail: human@example.ch (Work)\n\n"}}}

        monkeypatch.setattr(web_server, "_call_aiwerk_bridge_tool", fake_bridge_call)

        resp = self.client.get("/api/cui/contacts/search?q=example")

        assert resp.status_code == 200
        emails = {item["email"] for item in resp.json()["items"]}
        assert "human@example.ch" in emails
        assert "local@example.ch" in emails
        assert "contact@example.test" not in emails
        assert "noreply@example.ch" not in emails
        assert "synthetic-cf-test@example.test" not in emails

    def test_assistant_ui_locale_resolves_hidden_customer_setting(self, monkeypatch):
        import hermes_cli.web_server as web_server

        resolve_locale = getattr(web_server, "_assistant_ui_locale_from_config")
        monkeypatch.setenv("AIWERK_CUI_LOCALE", "hu")
        assert resolve_locale({}) == "hu"
        monkeypatch.setenv("AIWERK_CUI_LOCALE", "")
        assert resolve_locale({"dashboard": {"locale": "de_CH"}}) == "de"
        assert resolve_locale({"assistant": {"language": "Magyar"}}) == "hu"
        assert resolve_locale({"assistant": {"language": "invalid"}}) == "de"

    def test_shared_folder_open_neutralizes_active_content(self, tmp_path, monkeypatch):
        # Attacker-supplied markup dropped into the shared folder must not be
        # served as renderable content (it would execute in the dashboard origin
        # via the frontend's blob: URL navigation and could steal the session
        # token). Active-content types are forced to a non-renderable download.
        shared = tmp_path / "shared"
        shared.mkdir()
        xhtml_payload = (
            "<html xmlns='http://www.w3.org/1999/xhtml'>"
            "<script>alert(document.cookie)</script></html>"
        )
        (shared / "evil.html").write_text("<script>alert(document.cookie)</script>", encoding="utf-8")
        (shared / "evil.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'><script>1</script></svg>", encoding="utf-8")
        # .xht / .xhtm resolve to application/xhtml+xml — a browser-parsed,
        # script-executing document type that the extension denylist missed.
        (shared / "evil.xht").write_text(xhtml_payload, encoding="utf-8")
        (shared / "evil.xhtm").write_text(xhtml_payload, encoding="utf-8")
        (shared / "report.txt").write_text("hello", encoding="utf-8")
        (shared / "offer.pdf").write_bytes(b"%PDF-1.4 benign")
        (shared / "photo.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        monkeypatch.setenv("AIWERK_CUI_SHARED_FOLDER", str(shared))

        for name in ("evil.html", "evil.svg", "evil.xht", "evil.xhtm"):
            resp = self.client.get(f"/api/assistant/shared-folder/open?path={name}")
            assert resp.status_code == 200, name
            assert resp.headers["content-type"].startswith("application/octet-stream"), name
            assert resp.headers["content-disposition"].startswith("attachment"), name
            # Exactly one Content-Disposition header is set.
            assert resp.headers.get_list("content-disposition") == [
                resp.headers["content-disposition"]
            ], name
            assert resp.headers["x-content-type-options"] == "nosniff", name

        # Benign types remain inline-previewable but still get the nosniff guard.
        for name in ("report.txt", "offer.pdf", "photo.png"):
            resp = self.client.get(f"/api/assistant/shared-folder/open?path={name}")
            assert resp.status_code == 200, name
            assert not resp.headers["content-type"].startswith("application/octet-stream"), name
            assert resp.headers["content-disposition"].startswith("inline"), name
            assert resp.headers["x-content-type-options"] == "nosniff", name

    def test_assistant_shared_folder_allows_sandboxed_agent_downloads_html(self, tmp_path, monkeypatch):
        shared = tmp_path / "shared"
        downloads = shared / "Agent-Downloads"
        downloads.mkdir(parents=True)
        (downloads / "manual.html").write_text("<html><body>Manual</body></html>", encoding="utf-8")
        (shared / "manual.html").write_text("<html><body>Manual</body></html>", encoding="utf-8")
        monkeypatch.setenv("AIWERK_CUI_SHARED_FOLDER", str(shared))

        normal = self.client.get("/api/assistant/shared-folder/open?path=manual.html")
        assert normal.status_code == 200
        assert normal.headers["content-type"].startswith("application/octet-stream")
        assert normal.headers["content-disposition"].startswith("attachment")

        agent_download = self.client.get(
            "/api/assistant/shared-folder/open?path=Agent-Downloads%2Fmanual.html"
        )
        assert agent_download.status_code == 200
        assert agent_download.headers["content-type"].startswith("text/html")
        assert agent_download.headers["content-disposition"].startswith("inline")
        assert agent_download.headers["x-content-type-options"] == "nosniff"
        assert agent_download.headers["content-security-policy"] == "sandbox"

    def test_assistant_resources_lists_shared_folder_and_connectors(self, tmp_path, monkeypatch):
        import hermes_cli.web_server as web_server

        shared = tmp_path / "shared"
        shared.mkdir()
        docs = shared / "docs"
        docs.mkdir()
        juni = docs / "Juni 2026"
        juni.mkdir()
        ebene3 = juni / "Ebene 3"
        ebene3.mkdir()
        ebene4 = ebene3 / "Ebene 4"
        ebene4.mkdir()
        (shared / "offer.pdf").write_bytes(b"pdf")
        (docs / "contract.pdf").write_bytes(b"contract")
        (juni / "planung.txt").write_text("planung", encoding="utf-8")
        (ebene4 / "tief.txt").write_text("tief", encoding="utf-8")
        (shared / ".env").write_text("SECRET=1")
        email_json = tmp_path / "email.json"
        email_json.write_text(json.dumps({
            "unread_count": 2,
            "items": [{"id": "m1", "sender": "Max", "subject": "Offerte", "received_at": "2026-05-30T12:00:00Z"}],
        }))
        calendar_json = tmp_path / "calendar.json"
        calendar_json.write_text(json.dumps({
            "items": [{"id": "e1", "title": "Kundentermin", "starts_at": "2026-05-30T14:30:00Z"}],
        }))
        todo_file = tmp_path / "TODO.md"
        todo_file.write_text("# TODO\n- [ ] Angebot prüfen\n- [x] Alt erledigt\n", encoding="utf-8")
        monkeypatch.setenv("AIWERK_CUI_SHARED_FOLDER", str(shared))
        monkeypatch.setenv("AIWERK_CUI_EMAIL_SUMMARY_JSON", str(email_json))
        monkeypatch.setenv("AIWERK_CUI_CALENDAR_SUMMARY_JSON", str(calendar_json))
        monkeypatch.setenv("AIWERK_CUI_VAULT_SUMMARY_JSON", json.dumps({
            "status": "limited",
            "vault_url": "https://pass.aiwerk.ch",
            "summary": "4 Zugangsdaten · 2 Hinweise",
            "item_count": 4,
            "weak_count": 1,
            "reused_count": 1,
            "compromised_count": None,
            "compromised_supported": False,
        }))
        monkeypatch.setenv("AIWERK_CUI_TODO_PATH", str(todo_file))
        monkeypatch.setattr(web_server, "load_config", lambda: {
            "dashboard": {
                "shared_cloud": {
                    "base_url": "https://cloud.aiwerk.ch",
                    "share_id": "share-123",
                    "path": "/",
                }
            },
            "mcp_servers": {
                "aiwerk_bridge": {"url": "http://127.0.0.1:8000/mcp", "enabled": True},
                "disabled_demo": {"command": "demo", "enabled": False},
                "hermes_neo4j": {"command": "python", "enabled": True},
            },
        })
        monkeypatch.setattr(web_server, "_can_open_system_folder", lambda: True)
        monkeypatch.setattr(web_server, "_aiwerk_bridge_live_subservers", lambda config: [
            {"id": "aiwerk-bridge-coinmarketcap", "label": "CoinMarketCap", "status": "connected", "status_label": "Verbunden", "capabilities": ["Bridge-Subserver"]},
            {"id": "aiwerk-bridge-firecrawl", "label": "Firecrawl", "status": "connected", "status_label": "Verbunden", "capabilities": ["Bridge-Subserver"]},
            {"id": "aiwerk-bridge-google-maps", "label": "Google Maps", "status": "connected", "status_label": "Verbunden", "capabilities": ["Bridge-Subserver"]},
        ])
        opened = []
        monkeypatch.setattr(web_server, "_open_system_folder", lambda path, **kwargs: opened.append(path) or True)

        # can_open_folder is True here via the explicit operator opt-in; a
        # spoofable X-Forwarded-For: 127.0.0.1 no longer grants "local" status
        # (see _request_looks_local / TestRequestLooksLocalSpoofing).
        monkeypatch.setenv("HERMES_CUI_ALLOW_REMOTE_FILE_MANAGER_OPEN", "1")
        resp = self.client.get("/api/assistant/resources?refresh=1", headers={"host": "127.0.0.1:9120"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["email"]["unread_count"] == 2
        assert data["calendar"]["items"][0]["title"] == "Kundentermin"
        assert data["shared_folder"]["status"] == "connected"
        assert data["vault"]["vault_url"] == "https://pass.aiwerk.ch"
        assert data["vault"]["item_count"] == 4
        assert data["vault"]["weak_count"] == 1
        assert data["todos"]["open_count"] == 1
        assert data["todos"]["items"][0]["text"] == "Angebot prüfen"
        assert data["shared_folder"]["can_open_folder"] is True
        assert data["shared_folder"]["cloud_url"] == "https://cloud.aiwerk.ch/web/client/pubshares/share-123/browse?path=%2F"
        shared_items = data["shared_folder"]["items"]
        assert [item["name"] for item in shared_items] == ["docs", "offer.pdf"]
        assert shared_items[0]["kind"] == "folder"
        assert shared_items[0]["cloud_url"] == "https://cloud.aiwerk.ch/web/client/pubshares/share-123/browse?path=%2Fdocs"
        docs_children = shared_items[0]["children"]
        assert [item["name"] for item in docs_children] == ["Juni 2026", "contract.pdf"]
        assert docs_children[0]["kind"] == "folder"
        juni_children = docs_children[0]["children"]
        assert [item["name"] for item in juni_children] == ["Ebene 3", "planung.txt"]
        deep_file = juni_children[0]["children"][0]["children"][0]
        assert deep_file["name"] == "tief.txt"
        assert deep_file["open_url"].startswith("/api/assistant/shared-folder/open?path=")
        assert juni_children[1]["open_url"].startswith("/api/assistant/shared-folder/open?path=")
        assert docs_children[1]["open_url"].startswith("/api/assistant/shared-folder/open?path=")
        assert shared_items[1]["kind"] == "file"
        assert shared_items[1]["open_url"].startswith("/api/assistant/shared-folder/open?path=")
        open_resp = self.client.get(docs_children[1]["open_url"])
        assert open_resp.status_code == 200
        assert open_resp.content == b"contract"
        assert open_resp.headers["content-type"].startswith("application/pdf")
        nested_open_resp = self.client.get(juni_children[1]["open_url"])
        assert nested_open_resp.status_code == 200
        assert nested_open_resp.text == "planung"
        deep_open_resp = self.client.get(deep_file["open_url"])
        assert deep_open_resp.status_code == 200
        assert deep_open_resp.text == "tief"
        assert self.client.get("/api/assistant/shared-folder/open?path=../.env").status_code == 404
        open_folder_resp = self.client.post("/api/assistant/shared-folder/open-folder", headers={"host": "127.0.0.1:9120"})
        assert open_folder_resp.status_code == 200
        assert opened == [shared]
        labels = {connector["label"] for connector in data["connectors"]}
        assert labels == {"AIWerk Bridge", "Wissensbasis"}
        assert all(connector["status"] == "connected" for connector in data["connectors"])
        assert all("MCP" in connector["capabilities"] for connector in data["connectors"])
        bridge = next(connector for connector in data["connectors"] if connector["label"] == "AIWerk Bridge")
        assert [child["label"] for child in bridge["children"]][:3] == ["CoinMarketCap", "Firecrawl", "Google Maps"]
        assert all(child["capabilities"] == ["Bridge-Subserver"] for child in bridge["children"])

    def test_assistant_todo_update_marks_item_done_and_invalidates_cache(self, tmp_path, monkeypatch):
        import hermes_cli.web_server as web_server

        cache_lock = getattr(web_server, "_ASSISTANT_RESOURCE_CACHE_LOCK")
        cache = getattr(web_server, "_ASSISTANT_RESOURCE_CACHE")
        with cache_lock:
            cache.clear()
        todo_file = tmp_path / "TODO.md"
        todo_file.write_text("# TODO\n- [ ] Angebot prüfen\n- [ ] Ölwechsel planen\n", encoding="utf-8")
        monkeypatch.setenv("AIWERK_CUI_TODO_PATH", str(todo_file))
        monkeypatch.setattr(web_server, "load_config", lambda: {})

        first = self.client.get("/api/assistant/resources?refresh=1&resource=todos")
        assert first.status_code == 200
        assert first.json()["todos"]["open_count"] == 2
        item_id = first.json()["todos"]["items"][0]["id"]

        updated = self.client.post("/api/assistant/todos/update", json={"id": item_id, "done": True})
        assert updated.status_code == 200
        assert updated.json()["todos"]["open_count"] == 1
        assert updated.json()["todos"]["done_count"] == 1
        assert "- [x] Angebot prüfen" in todo_file.read_text(encoding="utf-8")

        cached_after_update = self.client.get("/api/assistant/resources?resource=todos")
        assert cached_after_update.status_code == 200
        assert cached_after_update.json()["todos"]["open_count"] == 1
        assert cached_after_update.json()["todos"]["items"][0]["text"] == "Ölwechsel planen"
        assert self.client.post("/api/assistant/todos/update", json={"id": "bad", "done": True}).status_code == 400

    def test_assistant_todo_edit_updates_text_status_and_preserves_metadata(self, tmp_path, monkeypatch):
        import hermes_cli.web_server as web_server

        cache_lock = getattr(web_server, "_ASSISTANT_RESOURCE_CACHE_LOCK")
        cache = getattr(web_server, "_ASSISTANT_RESOURCE_CACHE")
        with cache_lock:
            cache.clear()
        todo_file = tmp_path / "TODO.md"
        todo_file.write_text(
            "# TODO\n- [ ] Angebot prüfen <!-- hermes:id=abc status=pending -->\n- [ ] Ölwechsel planen\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("AIWERK_CUI_TODO_PATH", str(todo_file))
        monkeypatch.setattr(web_server, "load_config", lambda: {})

        first = self.client.get("/api/assistant/resources?refresh=1&resource=todos")
        assert first.status_code == 200
        item = first.json()["todos"]["items"][0]
        assert item["text"] == "Angebot prüfen"
        assert item["full_text"] == "Angebot prüfen"

        edited = self.client.post(
            "/api/assistant/todos/edit",
            json={"id": item["id"], "text": "  Angebot mit Details prüfen  ", "done": False},
        )
        assert edited.status_code == 200
        edited_item = edited.json()["todos"]["items"][0]
        assert edited_item["text"] == "Angebot mit Details prüfen"
        assert edited_item["full_text"] == "Angebot mit Details prüfen"
        todo_text = todo_file.read_text(encoding="utf-8")
        assert "- [ ] Angebot mit Details prüfen <!-- hermes:id=abc status=pending -->" in todo_text

        done = self.client.post(
            "/api/assistant/todos/edit",
            json={"id": item["id"], "text": "Angebot erledigt", "done": True},
        )
        assert done.status_code == 200
        assert done.json()["todos"]["open_count"] == 1
        assert done.json()["todos"]["done_count"] == 1
        assert "- [x] Angebot erledigt <!-- hermes:id=abc status=pending -->" in todo_file.read_text(encoding="utf-8")
        assert self.client.post("/api/assistant/todos/edit", json={"id": item["id"], "text": "   "}).status_code == 400

    def test_assistant_todo_summary_strips_hermes_metadata_from_items(self, tmp_path, monkeypatch):
        import hermes_cli.web_server as web_server

        todo_file = tmp_path / "TODO.md"
        todo_file.write_text(
            "# TODO\n"
            "- [ ] Visible customer task\n"
            "- [ ] Metadata task <!-- hermes:id=1 status=in_progress -->\n"
            "- [x] Completed metadata task <!-- hermes:id=2 status=completed -->\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("AIWERK_CUI_TODO_PATH", str(todo_file))

        summary = getattr(web_server, "_todo_summary")({})

        assert summary["open_count"] == 2
        assert summary["done_count"] == 1
        assert summary["total_count"] == 3
        assert [item["text"] for item in summary["items"]] == [
            "Visible customer task",
            "Metadata task",
        ]

    def test_assistant_todo_add_appends_item_and_invalidates_cache(self, tmp_path, monkeypatch):
        import hermes_cli.web_server as web_server

        cache_lock = getattr(web_server, "_ASSISTANT_RESOURCE_CACHE_LOCK")
        cache = getattr(web_server, "_ASSISTANT_RESOURCE_CACHE")
        with cache_lock:
            cache.clear()
        todo_file = tmp_path / "TODO.md"
        todo_file.write_text("# TODO\n- [ ] Angebot prüfen\n", encoding="utf-8")
        monkeypatch.setenv("AIWERK_CUI_TODO_PATH", str(todo_file))
        monkeypatch.setattr(web_server, "load_config", lambda: {})

        first = self.client.get("/api/assistant/resources?refresh=1&resource=todos")
        assert first.status_code == 200
        assert first.json()["todos"]["open_count"] == 1

        added = self.client.post("/api/assistant/todos/add", json={"text": "  Neue Aufgabe   erfassen  "})
        assert added.status_code == 200
        assert added.json()["todos"]["open_count"] == 2
        assert added.json()["todos"]["items"][-1]["text"] == "Neue Aufgabe erfassen"
        todo_text = todo_file.read_text(encoding="utf-8")
        assert "- [ ] Neue Aufgabe erfassen" in todo_text
        assert "<!-- hermes:id=cui-" in todo_text
        assert "status=pending -->" in todo_text

        cached_after_add = self.client.get("/api/assistant/resources?resource=todos")
        assert cached_after_add.status_code == 200
        assert cached_after_add.json()["todos"]["open_count"] == 2
        assert self.client.post("/api/assistant/todos/add", json={"text": "   "}).status_code == 400

    def test_webdav_shared_folder_items_keep_nested_relative_paths(self, monkeypatch):
        import urllib.parse
        import hermes_cli.web_server as web_server

        root_href = "/Example%20Customer/Customer-Shared/"
        folder_href = "/Example%20Customer/Customer-Shared/Bedienungsanleitungen/"
        file_href = "/Example%20Customer/Customer-Shared/Bedienungsanleitungen/B03900_IM_Kaffeevollautomat_Finessa_0322_WEB.pdf"

        def response_xml(*hrefs: tuple[str, str, bool]) -> bytes:
            responses = []
            for href, name, is_folder in hrefs:
                collection = "<D:collection/>" if is_folder else ""
                responses.append(
                    f"<D:response><D:href>{href}</D:href><D:propstat><D:prop>"
                    f"<D:displayname>{name}</D:displayname><D:getcontentlength>123</D:getcontentlength>"
                    f"<D:resourcetype>{collection}</D:resourcetype>"
                    f"</D:prop></D:propstat></D:response>"
                )
            return ("<?xml version='1.0'?><D:multistatus xmlns:D='DAV:'>" + "".join(responses) + "</D:multistatus>").encode()

        class FakeResponse:
            status = 207
            headers = {}
            def __init__(self, data: bytes):
                self.data = data
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self, *_args):
                return self.data

        def fake_urlopen(request, timeout=None):
            path = urllib.parse.urlparse(request.full_url).path
            if path.rstrip("/") == "/Example%20Customer/Customer-Shared":
                return FakeResponse(response_xml((root_href, "Customer-Shared", True), (folder_href, "Bedienungsanleitungen", True)))
            if path.rstrip("/") == "/Example%20Customer/Customer-Shared/Bedienungsanleitungen":
                return FakeResponse(response_xml((folder_href, "Bedienungsanleitungen", True), (file_href, "B03900_IM_Kaffeevollautomat_Finessa_0322_WEB.pdf", False)))
            raise AssertionError(path)

        monkeypatch.setattr(web_server, "_pass_first_line", lambda entry: "secret")
        monkeypatch.setattr(web_server.urllib.request, "urlopen", fake_urlopen)

        items = web_server._webdav_cloud_items({
            "type": "sftpgo_webdav",
            "base_url": "https://dav.example.test",
            "username": "customer.example",
            "password_pass_entry": "pass/entry",
            "path": "/Example Customer/Customer-Shared",
            "max_depth": 2,
        })

        file_item = items[0]["children"][0]
        assert file_item["name"] == "B03900_IM_Kaffeevollautomat_Finessa_0322_WEB.pdf"
        parsed = urllib.parse.urlparse(file_item["open_url"])
        assert urllib.parse.parse_qs(parsed.query)["path"] == ["Bedienungsanleitungen/B03900_IM_Kaffeevollautomat_Finessa_0322_WEB.pdf"]
        assert file_item["reference_uri"] == "shared://Bedienungsanleitungen/B03900_IM_Kaffeevollautomat_Finessa_0322_WEB.pdf"

    def test_assistant_resource_attachment_writes_calendar_context_without_raw_link(self):
        resp = self.client.post("/api/assistant/attachments/resource", json={
            "kind": "calendar_event",
            "session_id": "session-123",
            "item": {
                "title": "Kundentermin",
                "starts_at": "2026-06-01T10:00:00Z",
                "ends_at": "2026-06-01T10:30:00Z",
                "location_hint": "Bern",
                "account_address": "team@example.ch",
                "html_link": "https://calendar.google.com/event?eid=secret",
            },
        })

        assert resp.status_code == 200
        attachment = resp.json()["attachments"][0]
        text = Path(attachment["path"]).read_text(encoding="utf-8")
        assert "Kundentermin" in text
        assert "team@example.ch" in text
        assert "[LINK]" in text
        assert "calendar.google.com" not in text

    def test_email_reader_strips_invisible_preheader_and_wraps_long_single_line_body(self):
        import hermes_cli.web_server as web_server

        body = (
            "N26 Please log in to your N26 app. "
            + " \u200c" * 175
            + " Don't forget to confirm your details Hey CUSTOMER, "
            "This is a friendly reminder to please log in to your N26 app before August 17, 2026 to confirm your information and answer a few questions about yourself. "
            "This is required to continue using your N26 account, and it should only take a few minutes. "
            "Need to update some details? You can update most of your information via the questionnaire without Customer Support assistance. "
            "Please note, this is required even if your personal information hasn’t changed. "
            "Confirm my details What happens if I don’t confirm your details? "
            "As a fully licensed bank, we’re legally required to regularly ensure information from all our customers is up to date. "
            "Need help? Chat with us N26 Bank SE Voltairestraße 8 | 10179 Berlin | Germany "
            "This email was intended for CUSTOMER."
        )

        cleaned = web_server._strip_email_reader_transport_metadata(body)

        assert "\u200c" not in cleaned
        assert "  " not in cleaned
        assert "Confirm my details\n\nWhat happens" in cleaned
        assert "Need help?\n\nChat with us" in cleaned
        assert cleaned.count("\n\n") >= 4

    def test_assistant_calendar_mirrors_google_workspace_mail_accounts(self, monkeypatch):
        import hermes_cli.web_server as web_server

        seen = []

        def fake_calendar_summary(config, account_cfg, *, now=None):
            address = account_cfg["address"]
            seen.append(address)
            items = [] if address == "contact@example.test" else [{
                "id": "event-1",
                "title": "Kontrolltermin",
                "starts_at": "2026-06-03T13:40:00+02:00",
                "ends_at": "2026-06-03T14:40:00+02:00",
                "account_label": address,
                "account_address": address,
            }]
            return {
                "label": address,
                "address": address,
                "calendar_id": address,
                "source": "google_calendar",
                "status": "connected",
                "summary": f"{len(items)} kommende Termine" if items else "Keine kommenden Termine",
                "items": items,
            }

        monkeypatch.setattr(web_server, "_google_workspace_calendar_summary", fake_calendar_summary)
        summary = getattr(web_server, "_calendar_summary")({
            "dashboard": {
                "email": {
                    "accounts": [
                        {"backend": "google_workspace", "address": "contact@example.test", "mcp_server": "google-workspace-aiwerk", "user_google_email": "contact@example.test"},
                        {"backend": "google_workspace", "address": "user@example.com", "mcp_server": "google-workspace-demo", "user_google_email": "user@example.com"},
                        {"backend": "himalaya", "address": "office@example.ch", "account": "office"},
                    ]
                }
            }
        })

        assert seen == ["contact@example.test", "user@example.com"]
        assert summary["status"] == "connected"
        assert summary["summary"] == "1 kommende Termine in 2 Kalendern"
        assert [account["address"] for account in summary["accounts"]] == ["contact@example.test", "user@example.com"]
        assert summary["items"][0]["title"] == "Kontrolltermin"
        assert summary["items"][0]["account_address"] == "user@example.com"
        assert summary["items"][0]["open_url"].startswith("/api/assistant/calendar/view?")
        assert "account=user%40example.com" in summary["items"][0]["open_url"]
        assert "id=event-1" in summary["items"][0]["open_url"]
        assert summary["accounts"][1]["items"][0]["open_url"].startswith("/api/assistant/calendar/view?")

    def test_microsoft_calendar_summary_reads_graph_events_from_bridge(self, monkeypatch):
        import hermes_cli.web_server as web_server

        calls = []
        event_a = "AQMk-long-shared-prefix-AAAA"
        event_b = "AQMk-long-shared-prefix-BBBB"

        def fake_bridge_call(config, *, server, tool, params):
            calls.append({"server": server, "tool": tool, "params": params})
            return {
                "result": {
                    "content": [{
                        "type": "text",
                        "text": json.dumps({
                            "value": [
                                {
                                    "id": event_b,
                                    "subject": "Zweiter Termin",
                                    "start": {"dateTime": "2026-07-15T11:00:00.0000000", "timeZone": "UTC"},
                                    "end": {"dateTime": "2026-07-15T11:30:00.0000000", "timeZone": "UTC"},
                                    "location": {"displayName": "Bern"},
                                    "organizer": {"emailAddress": {"name": "Example Owner", "address": "owner@example.test"}},
                                    "webLink": "https://outlook.live.com/calendar/item-b",
                                },
                                {
                                    "id": event_a,
                                    "subject": "Erster Termin",
                                    "start": {"dateTime": "2026-07-14T10:00:00.0000000", "timeZone": "UTC"},
                                    "end": {"dateTime": "2026-07-14T10:30:00.0000000", "timeZone": "UTC"},
                                    "location": {"displayName": "Zürich"},
                                    "webLink": "https://outlook.live.com/calendar/item-a",
                                },
                            ]
                        }),
                    }],
                }
            }

        monkeypatch.setattr(web_server, "_call_aiwerk_bridge_tool", fake_bridge_call)

        summary = getattr(web_server, "_calendar_summary")({
            "calendar": {
                "accounts": [{
                    "backend": "microsoft_calendar",
                    "label": "Example Owner Outlook",
                    "address": "owner@example.test",
                    "mcp_server": "microsoft-calendar",
                    "max_results": 5,
                }]
            }
        })

        assert calls == [{
            "server": "microsoft-calendar",
            "tool": "get-calendar-view",
            "params": {
                "startDateTime": calls[0]["params"]["startDateTime"],
                "endDateTime": calls[0]["params"]["endDateTime"],
            },
        }]
        assert summary["status"] == "connected"
        assert summary["summary"] == "2 kommende Termine"
        assert summary["accounts"][0]["source"] == "microsoft_calendar"
        assert summary["accounts"][0]["label"] == "Example Owner Outlook"
        assert [item["title"] for item in summary["items"]] == ["Erster Termin", "Zweiter Termin"]
        assert [item["id"] for item in summary["items"]] == [event_a, event_b]
        assert len({item["id"] for item in summary["items"]}) == 2
        assert summary["items"][0]["account_address"] == "owner@example.test"
        assert "account=owner%40example.test" in summary["items"][0]["open_url"]
        assert "id=AQMk-long-shared-prefix-AAAA" in summary["items"][0]["open_url"]

    def test_microsoft_calendar_summary_preserves_graph_timezone_context(self):
        import hermes_cli.web_server as web_server

        assert web_server._microsoft_graph_datetime({"dateTime": "2026-07-14T10:15:00.0000000", "timeZone": "UTC"}) == "2026-07-14T10:15:00.0000000Z"
        assert web_server._microsoft_graph_datetime({"dateTime": "2026-07-14T10:15:00.0000000", "timeZone": "W. Europe Standard Time"}) == "2026-07-14T10:15:00.0000000 [W. Europe Standard Time]"
        assert web_server._microsoft_graph_datetime({"date": "2026-07-14", "timeZone": "UTC"}) == "2026-07-14"

    def test_microsoft_calendar_account_lookup_accepts_user_principal_name(self):
        import hermes_cli.web_server as web_server

        account = web_server._calendar_account_config_for_ref({
            "calendar": {
                "accounts": [{
                    "backend": "microsoft_calendar",
                    "label": "Example Owner Outlook",
                    "user_principal_name": "owner@example.test",
                    "mcp_server": "microsoft-calendar",
                }]
            }
        }, "owner@example.test")

        assert account is not None
        assert account["backend"] == "microsoft_calendar"

    def test_microsoft_calendar_summary_surfaces_bridge_auth_error(self, monkeypatch):
        import hermes_cli.web_server as web_server

        def fake_bridge_call(config, *, server, tool, params):
            return {"isError": True, "result": {"content": [{"type": "text", "text": "Authentication required: token expired"}]}}

        monkeypatch.setattr(web_server, "_call_aiwerk_bridge_tool", fake_bridge_call)

        summary = getattr(web_server, "_microsoft_calendar_summary")(
            {},
            {"backend": "outlook", "address": "owner@example.test"},
            now=datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc),
        )

        assert summary is not None
        assert summary["status"] == "auth_required"
        assert summary["summary"] == "Outlook Kalender neu verbinden"
        assert summary["source"] == "microsoft_calendar"
        assert summary["items"] == []

    def test_assistant_calendar_viewer_fetches_microsoft_detail(self, monkeypatch):
        import hermes_cli.web_server as web_server

        monkeypatch.setattr(web_server, "_assistant_resources_payload", lambda request, force_refresh=False, **kwargs: {
            "calendar": {
                "accounts": [{
                    "address": "owner@example.test",
                    "label": "Example Owner Outlook",
                    "items": [{
                        "id": "ms-event-1",
                        "event_id": "ms-event-1",
                        "title": "BPW",
                        "starts_at": "2026-07-14T10:15:00Z",
                        "ends_at": "2026-07-14T10:45:00Z",
                        "source": "microsoft_calendar",
                    }],
                }]
            }
        })
        monkeypatch.setattr(web_server, "load_config", lambda: {
            "calendar": {
                "accounts": [{
                    "backend": "microsoft_calendar",
                    "label": "Example Owner Outlook",
                    "address": "owner@example.test",
                    "mcp_server": "microsoft-calendar",
                }]
            }
        })
        calls = []

        def fake_bridge_tool(config, *, server, tool, params):
            calls.append({"server": server, "tool": tool, "params": params})
            return {
                "result": {
                    "content": [{
                        "type": "text",
                        "text": json.dumps({
                            "id": "ms-event-1",
                            "subject": "BPW Detail",
                            "bodyPreview": "Bitte vorbereiten <script>alert(1)</script>",
                            "start": {"dateTime": "2026-07-14T10:15:00Z", "timeZone": "UTC"},
                            "end": {"dateTime": "2026-07-14T10:45:00Z", "timeZone": "UTC"},
                            "location": {"displayName": "Bern<script>alert(1)</script>"},
                            "webLink": "https://outlook.live.com/calendar/item",
                        }),
                    }]
                }
            }

        monkeypatch.setattr(web_server, "_call_aiwerk_bridge_tool", fake_bridge_tool)

        resp = self.client.get("/api/assistant/calendar/view?account=owner%40example.test&id=ms-event-1")

        assert resp.status_code == 200
        text = resp.text
        assert "BPW Detail" in text
        assert "Bern&lt;script&gt;alert(1)&lt;/script&gt;" in text
        assert "Bitte vorbereiten" in text
        assert "<script>alert(1)</script>" not in text
        assert "outlook.live.com" not in text
        assert "[LINK]" in text
        assert calls == [{"server": "microsoft-calendar", "tool": "get-calendar-event", "params": {"eventId": "ms-event-1"}}]

    def test_google_workspace_calendar_summary_requests_default_event_type_filter(self, monkeypatch):
        import hermes_cli.web_server as web_server

        calls = []

        def fake_bridge_call(config, *, server, tool, params):
            calls.append({"server": server, "tool": tool, "params": params})
            return {
                "result": {
                    "content": [{
                        "type": "text",
                        "text": 'Successfully retrieved 1 events from calendar \'primary\':\n- "Kundentermin" (Starts: 2026-06-02T10:00:00+02:00, Ends: 2026-06-02T10:30:00+02:00) ID: event-1 | Link: https://calendar.google.com/event?eid=secret',
                    }],
                }
            }

        monkeypatch.setattr(web_server, "_call_aiwerk_bridge_tool", fake_bridge_call)

        summary = getattr(web_server, "_google_workspace_calendar_summary")(
            {},
            {"backend": "google_workspace", "address": "team@example.ch", "user_google_email": "team@example.ch"},
            now=datetime(2026, 6, 1, 8, 0, tzinfo=timezone.utc),
        )

        assert calls[0]["tool"] == "get_events"
        assert calls[0]["params"]["event_types"] == ["default"]
        assert summary is not None
        assert summary["items"][0]["title"] == "Kundentermin"

    def test_assistant_calendar_viewer_reads_cached_event_as_sanitized_html(self, monkeypatch):
        import hermes_cli.web_server as web_server

        monkeypatch.setattr(web_server, "_assistant_resources_payload", lambda request, force_refresh=False, **kwargs: {
            "calendar": {
                "accounts": [{
                    "address": "team@example.ch",
                    "label": "team@example.ch",
                    "items": [{
                        "id": "event-1",
                        "event_id": "event-1",
                        "title": "<Kundentermin>",
                        "starts_at": "2026-06-01T10:00:00Z",
                        "ends_at": "2026-06-01T10:30:00Z",
                        "location_hint": "Bern<script>alert(1)</script>",
                        "description": "SEO Webseite Strub Lucarnum<br>Review &amp; Planung<div>Bitte vorbereiten.</div><script>alert(1)</script>",
                        "html_link": "https://calendar.google.com/event?eid=secret",
                    }],
                }]
            }
        })

        resp = self.client.get("/api/assistant/calendar/view?account=team%40example.ch&id=event-1")

        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert "no-store" in resp.headers["cache-control"]
        assert "default-src 'none'" in resp.headers["content-security-policy"]
        text = resp.text
        assert "Nur-Leseansicht" in text
        assert "&lt;Kundentermin&gt;" in text
        assert "01.06.2026, 12:00 Uhr" in text
        assert "01.06.2026, 12:30 Uhr" in text
        assert "2026-06-01T10:00:00Z" not in text
        assert "Bern&lt;script&gt;alert(1)&lt;/script&gt;" in text
        assert "SEO Webseite Strub Lucarnum" in text
        assert "Review &amp; Planung" in text
        assert "Bitte vorbereiten." in text
        assert "&lt;br&gt;" not in text
        assert "<br>" not in text
        assert "Titel: &lt;Kundentermin&gt;" not in text
        assert "Ort: Bern" not in text
        assert "calendar.google.com" not in text
        assert "[LINK]" in text
        assert "<script>alert(1)</script>" not in text

    def test_assistant_calendar_viewer_fetches_detailed_location_when_cache_lacks_it(self, monkeypatch):
        import hermes_cli.web_server as web_server

        monkeypatch.setattr(web_server, "_assistant_resources_payload", lambda request, force_refresh=False, **kwargs: {
            "calendar": {
                "accounts": [{
                    "address": "user@example.com",
                    "label": "user@example.com",
                    "items": [{
                        "id": "event-1",
                        "event_id": "event-1",
                        "title": "Kontrolltermin",
                        "starts_at": "2026-06-03T13:40:00+02:00",
                        "ends_at": "2026-06-03T14:40:00+02:00",
                    }],
                }]
            }
        })
        monkeypatch.setattr(web_server, "load_config", lambda: {
            "dashboard": {
                "email": {
                    "accounts": [{
                        "backend": "google_workspace",
                        "address": "user@example.com",
                        "mcp_server": "google-workspace-demo",
                        "user_google_email": "user@example.com",
                    }]
                }
            }
        })
        calls = []

        def fake_bridge_tool(config, *, server, tool, params):
            calls.append({"server": server, "tool": tool, "params": params})
            return {
                "result": {
                    "structuredContent": {
                        "result": "Event Details:\n"
                        "- Title: Kontrolltermin\n"
                        "- Starts: 2026-06-03T13:40:00+02:00\n"
                        "- Ends: 2026-06-03T14:40:00+02:00\n"
                        "- Description: No Description\n"
                        "- Location: Bahnhofplatz 1, Bern<script>alert(1)</script>\n"
                        "- Event ID: event-1\n"
                        "- Link: https://calendar.google.com/event?eid=secret"
                    }
                }
            }

        monkeypatch.setattr(web_server, "_call_aiwerk_bridge_tool", fake_bridge_tool)

        resp = self.client.get("/api/assistant/calendar/view?account=user%40example.com&id=event-1")

        assert resp.status_code == 200
        text = resp.text
        assert "Bahnhofplatz 1, Bern&lt;script&gt;alert(1)&lt;/script&gt;" in text
        assert "Titel: Kieferorthopäde" not in text
        assert "Ort: Bahnhofplatz" not in text
        assert "calendar.google.com" not in text
        assert "[LINK]" in text
        assert calls == [{
            "server": "google-workspace-demo",
            "tool": "get_events",
            "params": {
                "calendar_id": "user@example.com",
                "user_google_email": "user@example.com",
                "event_id": "event-1",
                "max_results": 1,
                "detailed": True,
            },
        }]

    def test_assistant_calendar_viewer_is_token_gated(self):
        import hermes_cli.web_server as web_server

        from starlette.testclient import TestClient
        assert "GET" in web_server._ASSISTANT_ALLOWED_HTTP[
            "/api/assistant/calendar/view"
        ]
        resp = TestClient(web_server.app).get("/api/assistant/calendar/view?account=team%40example.ch&id=event-1")
        assert resp.status_code == 401

    def test_shared_folder_file_manager_open_is_disabled_for_remote_dashboard_requests(self, tmp_path, monkeypatch):
        import hermes_cli.web_server as web_server

        shared = tmp_path / "shared"
        shared.mkdir()
        (shared / "offer.pdf").write_bytes(b"pdf")
        monkeypatch.setenv("AIWERK_CUI_SHARED_FOLDER", str(shared))
        monkeypatch.delenv("HERMES_CUI_ALLOW_REMOTE_FILE_MANAGER_OPEN", raising=False)
        monkeypatch.setattr(web_server, "load_config", lambda: {
            "dashboard": {
                "shared_cloud": {
                    "base_url": "https://cloud.aiwerk.ch",
                    "share_id": "share-123",
                    "path": "/",
                }
            }
        })
        monkeypatch.setattr(web_server, "_can_open_system_folder", lambda: True)
        monkeypatch.setattr(web_server.subprocess, "Popen", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("xdg-open must not run for remote CUI requests")))
        remote_headers = {"host": "customer.example.test", "x-forwarded-for": "203.0.113.42"}

        resp = self.client.get("/api/assistant/resources?refresh=1&resource=shared_folder", headers=remote_headers)

        assert resp.status_code == 200
        shared_folder = resp.json()["shared_folder"]
        assert shared_folder["can_open_folder"] is False
        assert shared_folder["cloud_url"] == "https://cloud.aiwerk.ch/web/client/pubshares/share-123/browse?path=%2F"
        open_folder_resp = self.client.post("/api/assistant/shared-folder/open-folder", headers=remote_headers)
        assert open_folder_resp.status_code == 409

    def test_shared_folder_file_manager_open_can_be_explicitly_enabled_for_remote_requests(self, tmp_path, monkeypatch):
        import hermes_cli.web_server as web_server

        shared = tmp_path / "shared"
        shared.mkdir()
        monkeypatch.setenv("AIWERK_CUI_SHARED_FOLDER", str(shared))
        monkeypatch.setenv("HERMES_CUI_ALLOW_REMOTE_FILE_MANAGER_OPEN", "true")
        monkeypatch.setattr(web_server, "_can_open_system_folder", lambda: True)
        opened = []
        monkeypatch.setattr(web_server.subprocess, "Popen", lambda args, **kwargs: opened.append(args))
        remote_headers = {"host": "customer.example.test", "x-forwarded-for": "203.0.113.42"}

        resp = self.client.get("/api/assistant/resources?refresh=1&resource=shared_folder", headers=remote_headers)

        assert resp.status_code == 200
        assert resp.json()["shared_folder"]["can_open_folder"] is True
        open_folder_resp = self.client.post("/api/assistant/shared-folder/open-folder", headers=remote_headers)
        assert open_folder_resp.status_code == 200
        assert any(args and args[0] == "xdg-open" for args in opened)

    def test_assistant_attachment_upload_extracts_text_and_sanitizes_path(self):
        from hermes_constants import get_hermes_home

        resp = self.client.post(
            "/api/assistant/attachments",
            data={"session_id": "abc/../unsafe"},
            files=[
                ("files", ("../../note.txt", b"hello customer file", "text/plain")),
                ("files", ("photo.png", b"\x89PNG\r\n\x1a\n", "image/png")),
            ],
        )

        assert resp.status_code == 200
        attachments = resp.json()["attachments"]
        assert len(attachments) == 2
        note = attachments[0]
        assert note["name"] == "note.txt"
        assert note["extracted_text"] == "hello customer file"
        assert note["extraction"] == "text"
        note_path = Path(note["path"]).resolve()
        assert get_hermes_home().resolve() / "dashboard_uploads" in note_path.parents
        assert note_path.name.endswith("note.txt")
        image = attachments[1]
        assert image["is_image"] is True
        assert image["extraction"] == "image"

    def test_assistant_attachment_upload_rejects_unsupported_extension(self):
        resp = self.client.post(
            "/api/assistant/attachments",
            files={"files": ("bad.exe", b"nope", "application/octet-stream")},
        )

        assert resp.status_code == 415

    def test_custom_endpoint_rollback_prefers_actual_dotenv_over_inherited_value(self, monkeypatch):
        import hermes_cli.web_server as ws
        from hermes_cli.config import custom_endpoint_key_env, load_env

        env_var = custom_endpoint_key_env("conflict-proxy")
        ws.save_env_value(env_var, "dotenv-old")
        assert load_env().get(env_var) == "dotenv-old"
        monkeypatch.setenv(env_var, "inherited-old")
        monkeypatch.setattr(ws, "save_config", lambda cfg: (_ for _ in ()).throw(OSError("disk full")))

        resp = self.client.post(
            "/api/providers/custom-endpoints",
            json={
                "id": "conflict-proxy",
                "name": "Conflict Proxy",
                "base_url": "https://proxy.example/v1",
                "model": "proxy/model",
                "api_key": "new-key",
            },
        )

        assert resp.status_code == 500
        assert load_env()[env_var] == "dotenv-old"
        assert os.environ[env_var] == "inherited-old"

    def test_custom_endpoint_rollback_preserves_inherited_only_absence_from_dotenv(self, monkeypatch):
        import hermes_cli.web_server as ws
        from hermes_cli.config import custom_endpoint_key_env, load_env

        env_var = custom_endpoint_key_env("inherited-proxy")
        monkeypatch.setenv(env_var, "inherited-only")
        assert env_var not in load_env()
        monkeypatch.setattr(ws, "save_config", lambda cfg: (_ for _ in ()).throw(OSError("disk full")))

        resp = self.client.post(
            "/api/providers/custom-endpoints",
            json={
                "id": "inherited-proxy",
                "name": "Inherited Proxy",
                "base_url": "https://proxy.example/v1",
                "model": "proxy/model",
                "api_key": "new-key",
            },
        )

        assert resp.status_code == 500
        assert env_var not in load_env()
        assert os.environ[env_var] == "inherited-only"

class TestWave1RestoredTestAdminApiPermissionEnforcement:
    def test_admin_action_map_maps_to_admin_only_actions(self):
        import hermes_cli.web_server as web_server
        from hermes_cli.dashboard_auth.permissions import is_admin_only_action

        for _method, _prefix, action in web_server._ADMIN_API_ACTIONS:
            assert is_admin_only_action(action), action

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("POST", "/api/providers/custom-endpoints"),
            ("POST", "/api/providers/custom-endpoints/acme/activate"),
            ("POST", "/api/providers/custom-endpoints/validate"),
            ("DELETE", "/api/providers/custom-endpoints/acme"),
            ("POST", "/api/providers/validate"),
        ],
    )
    def test_custom_provider_mutations_and_network_probes_are_admin_actions(self, method, path):
        import hermes_cli.web_server as web_server

        assert web_server._admin_api_action_for(method, path) is not None
