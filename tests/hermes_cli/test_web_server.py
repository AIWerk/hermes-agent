"""Tests for hermes_cli.web_server and related config utilities."""

import os
import json
import urllib.error
from datetime import datetime, timezone, timedelta
from email.message import Message
import shutil
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from hermes_cli.config import (
    reload_env,
    redact_key,
    OPTIONAL_ENV_VARS,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


# Path to the test-only example-dashboard plugin. Lives under
# tests/fixtures/ so the bundled-plugins directory stays clean — stock
# installs no longer ship a dummy "Example" sidebar tab. Tests that
# depend on its routes opt in via the `_install_example_plugin` fixture
# below.
_EXAMPLE_PLUGIN_FIXTURE = (
    Path(__file__).resolve().parent.parent / "fixtures" / "plugins" / "example-dashboard"
)


@pytest.fixture
def _install_example_plugin(_isolate_hermes_home):
    """Drop the example-dashboard fixture into the per-test HERMES_HOME
    user-plugins directory and force the web_server's dashboard plugin
    cache + API mount to rediscover it.

    The plugin used to live under ``<repo>/plugins/example-dashboard/``
    and was loaded for every install, putting an "Example" tab in every
    user's sidebar. It is now a tests-only fixture: any test that needs
    ``/api/plugins/example/hello`` or ``/dashboard-plugins/example/...``
    requests this fixture so the plugin appears only for that test's
    isolated ``HERMES_HOME``.

    The user-plugin source is preferred over a transient
    ``HERMES_BUNDLED_PLUGINS`` override because the bundled dir is
    resolved per-call (other tests in the suite implicitly rely on the
    real bundled plugins — kanban, hermes-achievements, model providers
    — being available, and globally swapping that root would yank them
    all). User plugins are first in the discovery search order, so
    laying down the fixture here is enough.
    """
    from hermes_constants import get_hermes_home
    from hermes_cli import web_server

    user_plugins_dir = get_hermes_home() / "plugins"
    user_plugins_dir.mkdir(parents=True, exist_ok=True)
    dst = user_plugins_dir / "example-dashboard"
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(_EXAMPLE_PLUGIN_FIXTURE, dst)

    # Snapshot the existing routes BEFORE mounting so we can:
    #   1. Identify the routes the mount call appends.
    #   2. Restore the original list on teardown — otherwise leftover
    #      ``/api/plugins/example/*`` routes leak into subsequent tests
    #      and start serving requests against a torn-down HERMES_HOME.
    app = web_server.app
    original_routes = list(app.router.routes)

    # Bust the module-level cache and re-discover so the example plugin
    # shows up in `_get_dashboard_plugins()`. `_mount_plugin_api_routes`
    # imports the plugin's `plugin_api.py` and ``include_router``s its
    # FastAPI router under ``/api/plugins/example/*``. The static-asset
    # route at ``/dashboard-plugins/<name>/<path>`` reads the plugins
    # list dynamically per request, so the rescan alone is enough for
    # the static-asset tests; the API auth tests additionally need the
    # route reorder below.
    web_server._dashboard_plugins_cache = None
    web_server._get_dashboard_plugins(force_rescan=True)
    web_server._mount_plugin_api_routes()

    # ``include_router`` appends the new routes to the END of
    # ``app.router.routes``. That works fine at import time — the SPA
    # catch-all ``mount_spa(app)`` registers AFTER the initial mount
    # call — but when we mount mid-flight the catch-all is already in
    # place, so the new ``/api/plugins/example/*`` route loses the
    # match-order race and we get a 404. Move the newly-appended routes
    # to the front of the list so FastAPI matches them first. They're
    # path-prefixed to ``/api/plugins/example/`` and can't shadow
    # anything else.
    new_routes = [r for r in app.router.routes if r not in original_routes]
    for route in new_routes:
        app.router.routes.remove(route)
    for offset, route in enumerate(new_routes):
        app.router.routes.insert(offset, route)

    try:
        yield
    finally:
        # Restore the original route list — drops the example plugin's
        # routes so the next test sees a clean app — and clear the
        # cache for the same reason.
        app.router.routes[:] = original_routes
        web_server._dashboard_plugins_cache = None


# ---------------------------------------------------------------------------
# reload_env tests
# ---------------------------------------------------------------------------


class TestReloadEnv:
    """Tests for reload_env() — re-reads .env into os.environ."""

    def test_adds_new_vars(self, tmp_path):
        """reload_env() adds vars from .env that are not in os.environ."""
        env_file = tmp_path / ".env"
        env_file.write_text("TEST_RELOAD_VAR=hello123\n")
        with patch.dict(reload_env.__globals__, {"get_env_path": lambda: env_file}):
            os.environ.pop("TEST_RELOAD_VAR", None)
            count = reload_env()
            assert count >= 1
            assert os.environ.get("TEST_RELOAD_VAR") == "hello123"
        os.environ.pop("TEST_RELOAD_VAR", None)

    def test_updates_changed_vars(self, tmp_path):
        """reload_env() updates vars whose value changed on disk."""
        env_file = tmp_path / ".env"
        env_file.write_text("TEST_RELOAD_VAR=old_value\n")
        with patch.dict(reload_env.__globals__, {"get_env_path": lambda: env_file}):
            os.environ["TEST_RELOAD_VAR"] = "old_value"
            # Now change the file
            env_file.write_text("TEST_RELOAD_VAR=new_value\n")
            count = reload_env()
            assert count >= 1
            assert os.environ.get("TEST_RELOAD_VAR") == "new_value"
        os.environ.pop("TEST_RELOAD_VAR", None)

    def test_removes_deleted_known_vars(self, tmp_path):
        """reload_env() removes known Hermes vars not present in .env."""
        env_file = tmp_path / ".env"
        env_file.write_text("")  # empty .env
        # Pick a known key from OPTIONAL_ENV_VARS
        known_key = next(iter(OPTIONAL_ENV_VARS.keys()))
        with patch.dict(reload_env.__globals__, {"get_env_path": lambda: env_file}):
            os.environ[known_key] = "stale_value"
            count = reload_env()
            assert known_key not in os.environ
            assert count >= 1

    def test_does_not_remove_unknown_vars(self, tmp_path):
        """reload_env() preserves non-Hermes env vars even when absent from .env."""
        env_file = tmp_path / ".env"
        env_file.write_text("")
        with patch.dict(reload_env.__globals__, {"get_env_path": lambda: env_file}):
            os.environ["MY_CUSTOM_UNRELATED_VAR"] = "keep_me"
            reload_env()
            assert os.environ.get("MY_CUSTOM_UNRELATED_VAR") == "keep_me"
        os.environ.pop("MY_CUSTOM_UNRELATED_VAR", None)


# ---------------------------------------------------------------------------
# redact_key tests
# ---------------------------------------------------------------------------


class TestRedactKey:
    def test_long_key_shows_prefix_suffix(self):
        result = redact_key("sk-1234567890abcdef")
        assert result.startswith("sk-1")
        assert result.endswith("cdef")
        assert "..." in result

    def test_short_key_fully_masked(self):
        assert redact_key("short") == "***"

    def test_empty_key(self):
        result = redact_key("")
        assert "not set" in result.lower() or result == "***" or "\x1b" in result


class TestSessionTokenInjection:
    """The desktop shell mints HERMES_DASHBOARD_SESSION_TOKEN and signs its
    /api + /api/ws calls with it. The backend must adopt that token, else every
    desktop request 401s ("gateway is offline"). A main-merge once silently
    dropped this read — this guards the contract, not a literal value.
    """

    def test_honors_injected_token(self, monkeypatch):
        import importlib
        import hermes_cli.web_server as ws

        monkeypatch.setenv("HERMES_DASHBOARD_SESSION_TOKEN", "desktop-seeded-token")
        try:
            importlib.reload(ws)
            assert ws._SESSION_TOKEN == "desktop-seeded-token"
        finally:
            monkeypatch.delenv("HERMES_DASHBOARD_SESSION_TOKEN", raising=False)
            importlib.reload(ws)

    def test_falls_back_to_random_token(self, monkeypatch):
        import importlib
        import hermes_cli.web_server as ws

        monkeypatch.delenv("HERMES_DASHBOARD_SESSION_TOKEN", raising=False)
        importlib.reload(ws)

        assert ws._SESSION_TOKEN and len(ws._SESSION_TOKEN) >= 32


class TestAssistantUserDisplayName:
    def test_resolves_user_display_name_from_customer_config(self, monkeypatch):
        import hermes_cli.web_server as ws

        monkeypatch.delenv("AIWERK_CUI_USER_DISPLAY_NAME", raising=False)
        cfg = {
            "dashboard": {"agent_name": "Customer", "user_name": "Customer Example"},
        }

        assert ws._assistant_user_display_name_from_config(cfg) == "Customer"

    def test_env_user_display_name_overrides_config(self, monkeypatch):
        import hermes_cli.web_server as ws

        monkeypatch.setenv("AIWERK_CUI_USER_DISPLAY_NAME", "Jordan Example")
        cfg = {"dashboard": {"user_name": "Customer Example"}}

        assert ws._assistant_user_display_name_from_config(cfg) == "Jordan"

    def test_user_display_name_prefers_config_over_memory_fallback(self, tmp_path, monkeypatch):
        import hermes_cli.web_server as ws

        home = tmp_path / "home"
        memories = home / "memories"
        memories.mkdir(parents=True)
        (memories / "USER.md").write_text("User's name is Legacy.\n", encoding="utf-8")
        monkeypatch.setattr(ws, "get_hermes_home", lambda: home)
        monkeypatch.setattr(ws, "load_config", lambda: {"dashboard": {"user_name": "Customer Example"}})

        assert ws._assistant_user_display_name() == "Customer"

    def test_prefers_explicit_user_name_over_assistant_identity(self, tmp_path, monkeypatch):
        import hermes_cli.web_server as ws

        home = tmp_path / "home"
        memories = home / "memories"
        memories.mkdir(parents=True)
        (memories / "USER.md").write_text(
            "User wants to call the assistant golem.\n"
            "User's name is Attila.\n"
            "golem is Attila's AIWerk test base agent.\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(ws, "get_hermes_home", lambda: home)

        assert ws._assistant_user_display_name() == "Attila"

    def test_ignores_generic_assistant_identity_names(self, tmp_path, monkeypatch):
        import hermes_cli.web_server as ws

        home = tmp_path / "home"
        memories = home / "memories"
        memories.mkdir(parents=True)
        (memories / "USER.md").write_text(
            "golem is a test assistant.\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(ws, "get_hermes_home", lambda: home)

        assert ws._assistant_user_display_name() is None


# ---------------------------------------------------------------------------
# web_server tests (FastAPI endpoints)
# ---------------------------------------------------------------------------


class TestWebServerEndpoints:
    """Test the FastAPI REST endpoints using Starlette TestClient."""

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

    def test_get_status(self):
        resp = self.client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "version" in data
        assert "hermes_home" in data
        assert "active_sessions" in data

    def test_get_sessions_uses_only_persisted_cwd(self, monkeypatch):
        """Session rows without persisted cwd must not inherit TERMINAL_CWD.

        /api/sessions should reflect per-session DB state, not process/global
        cwd settings, so workspace grouping stays stable and deterministic.
        """
        from hermes_state import SessionDB

        monkeypatch.setenv("TERMINAL_CWD", "/tmp/global-default")

        db = SessionDB()
        try:
            db.create_session(session_id="session-no-cwd", source="cli")
        finally:
            db.close()

        resp = self.client.get("/api/sessions?limit=20&offset=0")
        assert resp.status_code == 200

        rows = resp.json()["sessions"]
        row = next(s for s in rows if s["id"] == "session-no-cwd")
        assert row["cwd"] is None

    def test_get_sessions_forwards_min_messages(self, monkeypatch):
        """The ?min_messages= filter must reach SessionDB.

        The desktop session picker calls /api/sessions?...&min_messages=N to
        hide empty sessions. The param was silently dropped from the handler
        in a merge once (SessionDB still supported it); guard the wiring.
        """
        captured = {}

        class _FakeDB:
            def __init__(self, *args, **kwargs):
                pass

            def list_sessions_rich(self, limit, offset, min_message_count=0, **kwargs):
                captured["list"] = min_message_count
                return []

            def session_count(self, min_message_count=0, **kwargs):
                captured["count"] = min_message_count
                return 0

            def close(self):
                pass

        monkeypatch.setattr("hermes_state.SessionDB", _FakeDB)

        resp = self.client.get("/api/sessions?limit=5&offset=0&min_messages=3")
        assert resp.status_code == 200
        assert captured["list"] == 3
        assert captured["count"] == 3

    def test_rename_session_updates_title(self):
        """PATCH /api/sessions/{id} renames a session (regression: the route
        was missing entirely, so the desktop rename dialog got a 405)."""
        from hermes_state import SessionDB

        db = SessionDB()
        try:
            db.create_session(session_id="rename-me", source="cli")
        finally:
            db.close()

        resp = self.client.patch("/api/sessions/rename-me", json={"title": "My Chat"})
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "title": "My Chat"}

        db = SessionDB()
        try:
            assert db.get_session_title("rename-me") == "My Chat"
        finally:
            db.close()

    def test_rename_session_clears_title_when_empty(self):
        from hermes_state import SessionDB

        db = SessionDB()
        try:
            db.create_session(session_id="clear-me", source="cli")
            db.set_session_title("clear-me", "Has A Title")
        finally:
            db.close()

        resp = self.client.patch("/api/sessions/clear-me", json={"title": ""})
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "title": ""}

        db = SessionDB()
        try:
            assert db.get_session_title("clear-me") is None
        finally:
            db.close()

    def test_rename_session_not_found(self):
        resp = self.client.patch("/api/sessions/does-not-exist", json={"title": "x"})
        assert resp.status_code == 404

    def test_archive_session_via_patch(self):
        """PATCH archived=true soft-hides a session; archived=false restores it."""
        from hermes_state import SessionDB

        db = SessionDB()
        try:
            db.create_session(session_id="arch-me", source="cli")
            db.append_message(session_id="arch-me", role="user", content="hi")
        finally:
            db.close()

        resp = self.client.patch("/api/sessions/arch-me", json={"archived": True})
        assert resp.status_code == 200
        assert resp.json()["archived"] is True

        # Hidden from the default list, surfaced by archived=only.
        listed = self.client.get("/api/sessions").json()
        assert all(s["id"] != "arch-me" for s in listed["sessions"])
        only = self.client.get("/api/sessions?archived=only").json()
        assert any(s["id"] == "arch-me" for s in only["sessions"])

        resp = self.client.patch("/api/sessions/arch-me", json={"archived": False})
        assert resp.status_code == 200
        restored = self.client.get("/api/sessions").json()
        assert any(s["id"] == "arch-me" for s in restored["sessions"])

    def test_patch_session_without_fields_is_400(self):
        """An existing session + empty body is a bad request, not a 404."""
        from hermes_state import SessionDB

        db = SessionDB()
        try:
            db.create_session(session_id="no-fields", source="cli")
        finally:
            db.close()

        resp = self.client.patch("/api/sessions/no-fields", json={})
        assert resp.status_code == 400

    def test_profiles_sessions_tags_default_profile(self):
        """The cross-profile aggregator returns the default profile's rows
        tagged profile="default" (single-profile parity with /api/sessions)."""
        from hermes_state import SessionDB

        db = SessionDB()
        try:
            db.create_session(session_id="agg-me", source="cli")
            db.append_message(session_id="agg-me", role="user", content="hi")
        finally:
            db.close()

        resp = self.client.get("/api/profiles/sessions?limit=20&min_messages=0")
        assert resp.status_code == 200
        data = resp.json()
        row = next(s for s in data["sessions"] if s["id"] == "agg-me")
        assert row["profile"] == "default"
        assert row["is_default_profile"] is True
        assert isinstance(data.get("errors"), list)

    def test_profiles_sessions_rejects_unknown_archived_value(self):
        resp = self.client.get("/api/profiles/sessions?archived=bogus")
        assert resp.status_code == 400

    def test_get_sessions_rejects_unknown_archived_value(self):
        resp = self.client.get("/api/sessions?archived=bogus")
        assert resp.status_code == 400

    def test_get_sessions_rejects_unknown_order_value(self):
        resp = self.client.get("/api/sessions?order=sideways")
        assert resp.status_code == 400

    def test_get_sessions_order_recent_surfaces_compression_tip(self):
        """A long-running conversation that auto-compresses must stay on the
        first page by recency, listed under its live continuation id."""
        import time as _time

        from hermes_state import SessionDB

        db = SessionDB()
        try:
            old = _time.time() - 86_400
            # Old conversation that later compresses into a fresh continuation.
            # The continuation must start at/after the parent's ended_at to be
            # recognised as a compression tip (not a sub-agent/branch).
            db.create_session(session_id="root-old", source="cli")
            db.append_message(session_id="root-old", role="user", content="kickoff")
            db.end_session("root-old", "compression")
            db._conn.execute(
                "UPDATE sessions SET started_at = ?, ended_at = ? WHERE id = ?",
                (old, old + 10, "root-old"),
            )
            db.create_session(session_id="tip-new", source="cli", parent_session_id="root-old")
            db._conn.execute("UPDATE sessions SET started_at = ? WHERE id = ?", (old + 10, "tip-new"))
            db.append_message(session_id="tip-new", role="user", content="continued just now")
            # A brand-new unrelated session started after the root but before now.
            db.create_session(session_id="mid", source="cli")
            db._conn.execute("UPDATE sessions SET started_at = ? WHERE id = ?", (_time.time() - 3600, "mid"))
            db.append_message(session_id="mid", role="user", content="hello")
            db._conn.commit()
        finally:
            db.close()

        rows = self.client.get("/api/sessions?order=recent&limit=5").json()["sessions"]
        ids = [r["id"] for r in rows]
        # The compressed conversation surfaces under its live tip id...
        assert "tip-new" in ids
        # ...carrying the durable lineage root so the desktop can match pins.
        tip = next(r for r in rows if r["id"] == "tip-new")
        assert tip.get("_lineage_root_id") == "root-old"

    def test_search_dedupes_compression_lineage_to_tip(self):
        """A conversation that auto-compresses leaves the matched term in both
        the root segment and the continuation. Search must collapse them to a
        single result keyed by the lineage root and pointing at the live tip,
        so the sidebar stops showing the same chat several times."""
        import time as _time

        from hermes_state import SessionDB

        db = SessionDB()
        try:
            db.create_session(session_id="search-root", source="cli")
            db.append_message(session_id="search-root", role="user", content="distinctneedle in the root")
            db.end_session("search-root", "compression")
            now = _time.time()
            db._conn.execute(
                "UPDATE sessions SET started_at = ?, ended_at = ? WHERE id = ?",
                (now - 100, now - 90, "search-root"),
            )
            db.create_session(session_id="search-tip", source="cli", parent_session_id="search-root")
            db._conn.execute("UPDATE sessions SET started_at = ? WHERE id = ?", (now - 90, "search-tip"))
            db.append_message(session_id="search-tip", role="user", content="distinctneedle again in the tip")
            db._conn.commit()
        finally:
            db.close()

        resp = self.client.get("/api/sessions/search?q=distinctneedle")
        assert resp.status_code == 200
        results = resp.json()["results"]

        lineage_hits = [r for r in results if r.get("lineage_root") == "search-root"]
        # One conversation -> exactly one result despite two FTS hits.
        assert len(lineage_hits) == 1
        hit = lineage_hits[0]
        # Surfaced under the live tip so clicking resumes the current session.
        assert hit["session_id"] == "search-tip"
        assert hit["lineage_root"] == "search-root"

    def test_search_keeps_branch_specific_hits_on_branch(self):
        """Branch sessions share parent_session_id, but they are not compression
        continuations. A query that only exists in the branch must open the
        branch instead of being collapsed back to the parent/root."""
        import time as _time

        from hermes_state import SessionDB

        db = SessionDB()
        try:
            now = _time.time()
            db.create_session(session_id="branch-parent", source="cli")
            db.append_message(session_id="branch-parent", role="user", content="ancestor context")
            db.end_session("branch-parent", "branched")
            db._conn.execute(
                "UPDATE sessions SET started_at = ?, ended_at = ? WHERE id = ?",
                (now - 100, now - 90, "branch-parent"),
            )
            db.create_session(session_id="branch-child", source="cli", parent_session_id="branch-parent")
            db._conn.execute("UPDATE sessions SET started_at = ? WHERE id = ?", (now - 80, "branch-child"))
            db.append_message(session_id="branch-child", role="user", content="branchspecificneedle only here")
            db._conn.commit()
        finally:
            db.close()

        resp = self.client.get("/api/sessions/search?q=branchspecificneedle")
        assert resp.status_code == 200
        results = resp.json()["results"]

        assert any(
            r["session_id"] == "branch-child" and r.get("lineage_root") == "branch-child"
            for r in results
        )

    def test_get_sessions_archived_is_boolean(self):
        from hermes_state import SessionDB

        db = SessionDB()
        try:
            db.create_session(session_id="bool-arch", source="cli")
            db.append_message(session_id="bool-arch", role="user", content="hi")
        finally:
            db.close()

        row = next(s for s in self.client.get("/api/sessions").json()["sessions"] if s["id"] == "bool-arch")
        assert row["archived"] is False

    def test_rename_response_omits_archived_when_not_set(self):
        """Title-only PATCH keeps its legacy {ok, title} response shape."""
        from hermes_state import SessionDB

        db = SessionDB()
        try:
            db.create_session(session_id="title-only", source="cli")
        finally:
            db.close()

        resp = self.client.patch("/api/sessions/title-only", json={"title": "Hi"})
        assert resp.status_code == 200
        assert "archived" not in resp.json()

    def test_audio_transcription_endpoint(self, monkeypatch):
        import tools.transcription_tools as transcription_tools

        captured = {}

        def fake_transcribe_audio(path):
            captured["path"] = path
            return {
                "success": True,
                "transcript": "hello from voice mode",
                "provider": "test",
            }

        monkeypatch.setattr(transcription_tools, "transcribe_audio", fake_transcribe_audio)

        resp = self.client.post(
            "/api/audio/transcribe",
            json={
                "data_url": "data:audio/webm;base64,aGVsbG8=",
                "mime_type": "audio/webm",
            },
        )

        assert resp.status_code == 200
        assert resp.json() == {
            "ok": True,
            "transcript": "hello from voice mode",
            "provider": "test",
        }
        assert captured["path"].endswith(".webm")
        assert not Path(captured["path"]).exists()

    def test_audio_transcription_rejects_invalid_base64(self):
        resp = self.client.post(
            "/api/audio/transcribe",
            json={
                "data_url": "data:audio/webm;base64,not base64",
                "mime_type": "audio/webm",
            },
        )

        assert resp.status_code == 400
        assert "base64" in resp.json()["detail"]

    def test_desktop_audio_routes_registered(self):
        """All three desktop voice endpoints must exist.

        The renderer (apps/desktop) calls /api/audio/transcribe, /speak, and
        /elevenlabs/voices. /speak + /voices were silently dropped in a merge
        once; this guards the contract so a future merge can't lose them
        without failing CI.
        """
        from hermes_cli.web_server import app

        paths = {getattr(r, "path", None) for r in app.routes}
        assert "/api/audio/transcribe" in paths
        assert "/api/audio/speak" in paths
        assert "/api/audio/elevenlabs/voices" in paths

    def test_elevenlabs_voices_unavailable_without_key(self, monkeypatch):
        import hermes_cli.web_server as web_server

        monkeypatch.setattr(web_server, "load_env", lambda: {})
        monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)

        resp = self.client.get("/api/audio/elevenlabs/voices")
        assert resp.status_code == 200
        assert resp.json() == {"available": False, "voices": []}

    def test_speak_text_returns_base64_data_url(self, monkeypatch, tmp_path):
        import tools.tts_tool as tts_tool

        audio_file = tmp_path / "speech.mp3"
        audio_file.write_bytes(b"ID3fake-audio-bytes")

        def fake_tts(text):
            return json.dumps({
                "success": True,
                "file_path": str(audio_file),
                "provider": "test",
            })

        monkeypatch.setattr(tts_tool, "text_to_speech_tool", fake_tts)

        resp = self.client.post("/api/audio/speak", json={"text": "hello there"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["mime_type"] == "audio/mpeg"
        assert body["data_url"].startswith("data:audio/mpeg;base64,")
        assert body["provider"] == "test"
        # The handler streams the bytes back and removes the temp file.
        assert not audio_file.exists()

    def test_speak_text_requires_nonempty_text(self):
        resp = self.client.post("/api/audio/speak", json={"text": "   "})
        assert resp.status_code == 400

    def test_update_hermes_returns_docker_guidance_without_spawning(self, monkeypatch):
        import hermes_cli.web_server as web_server

        spawned = False

        def fail_spawn(*_args, **_kwargs):
            nonlocal spawned
            spawned = True
            raise AssertionError("docker update guard should not spawn hermes update")

        monkeypatch.setattr(web_server, "detect_install_method", lambda _root: "docker")
        monkeypatch.setattr(web_server, "_spawn_hermes_action", fail_spawn)
        web_server._ACTION_PROCS.pop("hermes-update", None)
        web_server._ACTION_RESULTS.pop("hermes-update", None)

        resp = self.client.post("/api/hermes/update")

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert data["name"] == "hermes-update"
        assert data["pid"] is None
        assert data["error"] == "docker_update_unsupported"
        assert "docker pull nousresearch/hermes-agent:latest" in data["message"]
        assert spawned is False

        status = self.client.get("/api/actions/hermes-update/status")
        assert status.status_code == 200
        status_data = status.json()
        assert status_data["running"] is False
        assert status_data["exit_code"] == 1
        assert status_data["pid"] is None
        assert any("docker pull nousresearch/hermes-agent:latest" in line for line in status_data["lines"])

    def test_update_hermes_spawns_on_non_docker_install(self, monkeypatch):
        import hermes_cli.web_server as web_server

        class Proc:
            pid = 12345

            def poll(self):
                return None

        calls = []

        def fake_spawn(subcommand, name):
            calls.append((subcommand, name))
            return Proc()

        monkeypatch.setattr(web_server, "detect_install_method", lambda _root: "git")
        monkeypatch.setattr(web_server, "_spawn_hermes_action", fake_spawn)
        web_server._ACTION_PROCS.pop("hermes-update", None)
        web_server._ACTION_RESULTS.pop("hermes-update", None)

        resp = self.client.post("/api/hermes/update")

        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "pid": 12345, "name": "hermes-update"}
        assert calls == [(["update"], "hermes-update")]

    def test_get_status_filters_unconfigured_gateway_platforms(self, monkeypatch):
        import gateway.config as gateway_config
        import hermes_cli.web_server as web_server

        class _Platform:
            def __init__(self, value):
                self.value = value

        class _GatewayConfig:
            def get_connected_platforms(self):
                return [_Platform("telegram")]

        monkeypatch.setattr(web_server, "get_running_pid", lambda: 1234)
        monkeypatch.setattr(
            web_server,
            "read_runtime_status",
            lambda: {
                "gateway_state": "running",
                "updated_at": "2026-04-12T00:00:00+00:00",
                "platforms": {
                    "telegram": {"state": "connected", "updated_at": "2026-04-12T00:00:00+00:00"},
                    "whatsapp": {"state": "retrying", "updated_at": "2026-04-12T00:00:00+00:00"},
                    "feishu": {"state": "connected", "updated_at": "2026-04-12T00:00:00+00:00"},
                },
            },
        )
        monkeypatch.setattr(web_server, "check_config_version", lambda: (1, 1))
        monkeypatch.setattr(gateway_config, "load_gateway_config", lambda: _GatewayConfig())

        resp = self.client.get("/api/status")

        assert resp.status_code == 200
        assert resp.json()["gateway_platforms"] == {
            "telegram": {"state": "connected", "updated_at": "2026-04-12T00:00:00+00:00"},
        }

    def test_get_status_hides_stale_platforms_when_gateway_not_running(self, monkeypatch):
        import gateway.config as gateway_config
        import hermes_cli.web_server as web_server

        class _GatewayConfig:
            def get_connected_platforms(self):
                return []

        monkeypatch.setattr(web_server, "get_running_pid", lambda: None)
        monkeypatch.setattr(
            web_server,
            "read_runtime_status",
            lambda: {
                "gateway_state": "startup_failed",
                "updated_at": "2026-04-12T00:00:00+00:00",
                "platforms": {
                    "whatsapp": {"state": "retrying", "updated_at": "2026-04-12T00:00:00+00:00"},
                    "feishu": {"state": "connected", "updated_at": "2026-04-12T00:00:00+00:00"},
                },
            },
        )
        monkeypatch.setattr(web_server, "check_config_version", lambda: (1, 1))
        monkeypatch.setattr(gateway_config, "load_gateway_config", lambda: _GatewayConfig())

        resp = self.client.get("/api/status")

        assert resp.status_code == 200
        assert resp.json()["gateway_state"] == "startup_failed"
        assert resp.json()["gateway_platforms"] == {}


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

    def test_cron_delivery_targets_lists_configured_platforms(self, monkeypatch):
        """The cron dropdown endpoint returns Local + configured platforms dynamically."""
        import gateway.config as gateway_config

        class _Platform:
            def __init__(self, value):
                self.value = value

        class _GatewayConfig:
            def get_connected_platforms(self):
                return [_Platform("matrix")]

        monkeypatch.setattr(
            gateway_config, "load_gateway_config", lambda: _GatewayConfig()
        )
        monkeypatch.setenv("MATRIX_HOME_ROOM", "!room:matrix.org")

        resp = self.client.get("/api/cron/delivery-targets")

        assert resp.status_code == 200
        targets = {t["id"]: t for t in resp.json()["targets"]}
        # Local is always offered; matrix appears because its gateway is configured.
        assert "local" in targets
        assert "matrix" in targets
        assert targets["matrix"]["home_target_set"] is True
        # No hardcoded telegram/discord/slack/email when they aren't configured.
        assert "telegram" not in targets

    def test_get_config_schema(self):
        resp = self.client.get("/api/config/schema")
        assert resp.status_code == 200
        data = resp.json()
        assert "fields" in data
        assert "category_order" in data
        schema = data["fields"]
        assert len(schema) > 100  # Should have 150+ fields
        assert "model" in schema
        # Verify category_order is a non-empty list
        assert isinstance(data["category_order"], list)
        assert len(data["category_order"]) > 0
        assert "general" in data["category_order"]

    def test_get_config_defaults(self):
        resp = self.client.get("/api/config/defaults")
        assert resp.status_code == 200
        defaults = resp.json()
        assert "model" in defaults

    def test_get_env_vars(self):
        resp = self.client.get("/api/env")
        assert resp.status_code == 200
        data = resp.json()
        # Should contain known env var names
        assert any(k.endswith("_API_KEY") or k.endswith("_TOKEN") for k in data.keys())

    def test_assistant_mode_allows_chat_safe_api_and_blocks_admin_api(self, monkeypatch):
        import hermes_cli.web_server as web_server

        monkeypatch.setattr(web_server, "_DASHBOARD_MODE", "assistant")

        assert self.client.get("/api/status").status_code == 200
        assert self.client.get("/api/sessions").status_code == 200
        assert self.client.get("/api/model/info").status_code == 200
        assert self.client.get("/api/assistant/resources").status_code == 200
        assert self.client.post("/api/assistant/attachments", files={"files": ("note.txt", b"hello", "text/plain")}).status_code == 200
        assert self.client.post("/api/assistant/attachments/resource", json={"kind": "unknown", "item": {}}).status_code == 400
        assert self.client.get("/api/env").status_code == 404
        assert self.client.get("/api/config").status_code == 404
        assert self.client.get("/api/logs").status_code == 404

    def test_assistant_resources_cache_ttl_and_manual_refresh(self, monkeypatch):
        import hermes_cli.web_server as web_server

        cache_lock = getattr(web_server, "_ASSISTANT_RESOURCE_CACHE_LOCK")
        cache = getattr(web_server, "_ASSISTANT_RESOURCE_CACHE")
        with cache_lock:
            cache.clear()
        calls = {"email": 0, "calendar": 0, "shared": 0, "vault": 0, "todos": 0, "contacts": 0, "connectors": 0}
        monkeypatch.setattr(web_server, "load_config", lambda: {"mcp_servers": {"hermes_neo4j": {"enabled": True}}})

        def email_summary(config):
            calls["email"] += 1
            return {"status": "connected", "unread_count": calls["email"], "summary": "Mail", "items": []}

        def calendar_summary(config=None):
            calls["calendar"] += 1
            return {"status": "connected", "summary": "Kalender", "items": []}

        def shared_summary(config, request=None):
            calls["shared"] += 1
            return {"status": "connected", "root_label": "Shared", "summary": "1 Datei", "items": [], "total_count": 1}

        def vault_summary(config):
            calls["vault"] += 1
            return {"status": "connected", "vault_url": "https://pass.aiwerk.ch", "summary": "Tresor", "item_count": calls["vault"], "weak_count": 0, "reused_count": 0, "compromised_count": None}

        def todo_summary(config):
            calls["todos"] += 1
            return {"status": "connected", "summary": "Aufgaben", "items": [], "open_count": calls["todos"], "done_count": 0, "total_count": calls["todos"]}

        def contacts_summary(config, email, calendar):
            calls["contacts"] += 1
            return {"status": "connected", "summary": "Kontakte", "items": [], "relevant": [], "frequent": [], "total_count": calls["contacts"], "manual_count": 0, "connected_count": calls["contacts"]}

        def connector_summary(config, shared_folder, email, calendar):
            calls["connectors"] += 1
            return [{"id": "mcp-hermes_neo4j", "label": "Wissensbasis", "status": "connected"}]

        monkeypatch.setattr(web_server, "_email_summary", email_summary)
        monkeypatch.setattr(web_server, "_calendar_summary", calendar_summary)
        monkeypatch.setattr(web_server, "_shared_folder_summary", shared_summary)
        monkeypatch.setattr(web_server, "_vaultwarden_summary", vault_summary)
        monkeypatch.setattr(web_server, "_todo_summary", todo_summary)
        monkeypatch.setattr(web_server, "_contacts_summary", contacts_summary)
        monkeypatch.setattr(web_server, "_connector_summary", connector_summary)

        first = self.client.get("/api/assistant/resources")
        second = self.client.get("/api/assistant/resources")
        forced = self.client.get("/api/assistant/resources?refresh=1")
        email_forced = self.client.get("/api/assistant/resources?refresh=1&resource=email")

        assert first.status_code == 200
        assert second.status_code == 200
        assert forced.status_code == 200
        assert email_forced.status_code == 200
        assert calls == {"email": 3, "calendar": 2, "shared": 2, "vault": 2, "todos": 2, "contacts": 2, "connectors": 2}
        assert first.json()["cache"]["resources"]["email"]["ttl_seconds"] == 3600
        assert first.json()["cache"]["resources"]["calendar"]["ttl_seconds"] == 1800
        assert first.json()["cache"]["resources"]["vault"]["ttl_seconds"] == 900
        assert first.json()["cache"]["resources"]["todos"]["ttl_seconds"] == 60
        assert first.json()["cache"]["resources"]["contacts"]["ttl_seconds"] == 1800
        assert second.json()["cache"]["cached"] is True
        assert second.json()["email"]["unread_count"] == 1
        assert forced.json()["cache"]["cached"] is False
        assert forced.json()["email"]["unread_count"] == 2
        assert email_forced.json()["email"]["unread_count"] == 2
        assert email_forced.json()["cache"]["resources"]["email"]["cached"] is True
        assert email_forced.json()["cache"]["resources"]["email"]["stale"] is True
        assert email_forced.json()["cache"]["resources"]["calendar"]["cached"] is True

    def test_contacts_resource_refresh_returns_stale_and_revalidates_in_background(self, monkeypatch):
        import hermes_cli.web_server as web_server

        cache_lock = getattr(web_server, "_ASSISTANT_RESOURCE_CACHE_LOCK")
        cache = getattr(web_server, "_ASSISTANT_RESOURCE_CACHE")
        refreshing_lock = getattr(web_server, "_ASSISTANT_RESOURCE_REFRESHING_LOCK")
        refreshing = getattr(web_server, "_ASSISTANT_RESOURCE_REFRESHING")
        with cache_lock:
            cache.clear()
        with refreshing_lock:
            refreshing.clear()

        calls = {"email": 0, "calendar": 0, "shared": 0, "vault": 0, "todos": 0, "contacts": 0, "connectors": 0}
        monkeypatch.setattr(web_server, "load_config", lambda: {})
        monkeypatch.setattr(web_server, "_email_summary", lambda config: {"status": "connected", "summary": "Mail", "items": []})
        monkeypatch.setattr(web_server, "_calendar_summary", lambda config=None: {"status": "connected", "summary": "Kalender", "items": []})
        monkeypatch.setattr(web_server, "_shared_folder_summary", lambda config, request=None: {"status": "connected", "summary": "Shared", "items": []})
        monkeypatch.setattr(web_server, "_vaultwarden_summary", lambda config: {"status": "disabled", "summary": "Tresor", "items": []})
        monkeypatch.setattr(web_server, "_todo_summary", lambda config: {"status": "connected", "summary": "Aufgaben", "items": [], "open_count": 0, "done_count": 0, "total_count": 0})
        monkeypatch.setattr(web_server, "_connector_summary", lambda config, shared_folder, email, calendar: [])

        def contacts_summary(config, email, calendar):
            calls["contacts"] += 1
            contact = {"id": f"contact-{calls['contacts']}", "display_name": "Jane Kontakt", "email": "jane@example.ch"}
            return {"status": "connected", "summary": f"Kontakte {calls['contacts']}", "items": [contact], "relevant": [contact], "frequent": [], "total_count": calls["contacts"], "manual_count": 0, "connected_count": calls["contacts"]}

        monkeypatch.setattr(web_server, "_contacts_summary", contacts_summary)

        first = self.client.get("/api/assistant/resources?refresh=1&resource=contacts")
        forced_contacts = self.client.get("/api/assistant/resources?refresh=1&resource=contacts")

        assert first.status_code == 200
        assert forced_contacts.status_code == 200
        assert forced_contacts.json()["contacts"]["total_count"] == 1
        assert forced_contacts.json()["cache"]["resources"]["contacts"]["cached"] is True
        assert forced_contacts.json()["cache"]["resources"]["contacts"]["stale"] is True
        assert forced_contacts.json()["cache"]["resources"]["contacts"]["refreshing"] is True

    def test_cui_contacts_deduplicates_source_badges(self):
        import hermes_cli.web_server as web_server

        contact = getattr(web_server, "_normalize_contact")({
            "display_name": "Rustan Khayrov",
            "email": "rustan@example.ch",
            "source_badges": ["Google Contacts", "bergsmann@gmail.com", "bergsmann@gmail.com", "Aus E-Mail"],
            "source": "google contacts",
        })
        assert contact is not None
        assert contact["source_badges"] == ["Google Contacts", "bergsmann@gmail.com", "Aus E-Mail"]

        decoded = getattr(web_server, "_contact_from_address")(
            "=?utf-8?b?w4lydGVzw610w6lzIEvDtnpwb250aSBSZW5kc3plcnTFkWw=?= <ertesites@kozpontirendszer.gov.hu>",
            source="Aus E-Mail",
            relevance="relevant",
        )
        assert decoded is not None
        assert decoded["display_name"] == "Értesítés Központi Rendszertől"
        assert getattr(web_server, "_is_probably_system_contact")(decoded, own_emails=set()) is True

        merged = getattr(web_server, "_dedupe_contacts")([
            {"display_name": "Rustan Khayrov", "email": "rustan@example.ch", "source_badges": ["Google Contacts", "bergsmann@gmail.com"]},
            {"display_name": "Rustan Khayrov", "email": "rustan@example.ch", "source_badges": ["bergsmann@gmail.com", "Aus E-Mail"]},
        ])
        assert merged[0]["source_badges"] == ["Google Contacts", "bergsmann@gmail.com", "Aus E-Mail"]

    def test_cui_contacts_create_search_and_resource_summary(self, tmp_path, monkeypatch):
        import hermes_cli.web_server as web_server

        with web_server._ASSISTANT_RESOURCE_CACHE_LOCK:
            web_server._ASSISTANT_RESOURCE_CACHE.clear()
        monkeypatch.delenv("AIWERK_CUI_CONTACTS_JSON", raising=False)
        monkeypatch.setattr(web_server, "load_config", lambda: {})
        monkeypatch.setattr(web_server, "_assistant_contacts_store_path", lambda: tmp_path / "cui_contacts.json")

        created = self.client.post("/api/cui/contacts", json={
            "name": "Anna Meier",
            "organization": "Beispiel AG",
            "role": "Geschäftsführerin",
            "email": "Anna.Meier@Example.CH",
            "phone": "+41 31 555 12 12",
            "note": "Nur kurze Notiz",
        })
        assert created.status_code == 200
        assert created.json()["contact"]["email"] == "anna.meier@example.ch"
        assert "Manuell" in created.json()["contact"]["source_badges"]

        resources = self.client.get("/api/assistant/resources?refresh=1&resource=contacts")
        assert resources.status_code == 200
        contacts = resources.json()["contacts"]
        assert contacts["status"] == "connected"
        assert contacts["total_count"] == 1
        assert contacts["frequent"][0]["display_name"] == "Anna Meier"

        search = self.client.get("/api/cui/contacts/search?q=beispiel")
        assert search.status_code == 200
        assert search.json()["total_count"] == 1
        assert search.json()["items"][0]["organization"] == "Beispiel AG"

        hidden = self.client.post("/api/cui/contacts/hide", json={"id": created.json()["contact"]["id"], "email": created.json()["contact"]["email"]})
        assert hidden.status_code == 200
        assert "email:anna.meier@example.ch" in hidden.json()["hidden"]

        resources = self.client.get("/api/assistant/resources?refresh=1&resource=contacts")
        assert resources.status_code == 200
        assert resources.json()["contacts"]["total_count"] == 0
        search = self.client.get("/api/cui/contacts/search?q=beispiel")
        assert search.status_code == 200
        assert search.json()["total_count"] == 0

    def test_contacts_summary_derives_safe_fallbacks_from_email_and_calendar(self, monkeypatch):
        import hermes_cli.web_server as web_server

        monkeypatch.delenv("AIWERK_CUI_CONTACTS_JSON", raising=False)
        monkeypatch.setattr(web_server, "_read_manual_contacts", lambda: [])
        email = {"accounts": [{
            "label": "AIWerk",
            "address": "kontakt@aiwerk.ch",
            "items": [{"sender": "Max Muster <max@example.ch>"}],
        }]}
        calendar = {"accounts": [{"label": "Kalender", "address": "team@example.ch"}]}

        summary = getattr(web_server, "_contacts_summary")({}, email, calendar)

        emails = {item["email"] for item in summary["relevant"]}
        assert emails == {"max@example.ch"}
        assert "kontakt@aiwerk.ch" not in emails
        assert "team@example.ch" not in emails
        assert summary["status"] == "connected"
        assert summary["source_label"] == "Relevante Kontakte"

    def test_contacts_summary_uses_google_workspace_bridge_accounts(self, monkeypatch):
        import hermes_cli.web_server as web_server

        calls = []
        monkeypatch.delenv("AIWERK_CUI_CONTACTS_JSON", raising=False)
        monkeypatch.setattr(web_server, "_read_manual_contacts", lambda: [])

        def fake_bridge_call(config, *, server, tool, params):
            calls.append({"server": server, "tool": tool, "params": params})
            if tool == "search_gmail_messages":
                if "in:sent" in params.get("query", ""):
                    return {"result": {"structuredContent": {"result": "Message ID: sent-1"}}}
                return {"result": {"structuredContent": {"result": "Message ID: inbox-1"}}}
            if tool == "get_gmail_messages_content_batch":
                if server == "google-workspace-aiwerk":
                    return {"result": {"structuredContent": {"result": "Retrieved 1 messages:\n\nMessage ID: sent-1\nSubject: Angebot\nFrom: Kontakt <kontakt@aiwerk.ch>\nDate: Tue, 2 Jun 2026 10:00:00 +0000\nTo: Anna AIWerk <anna@aiwerk.ch>\n"}}}
                return {"result": {"structuredContent": {"result": "Retrieved 2 messages:\n\nMessage ID: sent-1\nSubject: Hallo\nFrom: Attila <bergsmann@gmail.com>\nDate: Tue, 2 Jun 2026 11:00:00 +0000\nTo: Bela Privat <bela@example.ch>\n\nMessage ID: inbox-1\nSubject: Analytics\nFrom: Google Analytics <analytics-noreply@google.com>\nDate: Tue, 2 Jun 2026 12:00:00 +0000\nTo: bergsmann@gmail.com\nPrecedence: bulk\nList-Unsubscribe: <https://example.com/unsub>\n"}}}
            if tool == "list_contacts" and server == "google-workspace-aiwerk":
                return {"result": {"structuredContent": {"result": "Contacts for kontakt@aiwerk.ch (1 of 1):\n\nContact ID: c_aiwerk\nName: Anna AIWerk\nEmail: anna@aiwerk.ch (Work)\nPhone: +41 31 555 12 12 (Work)\nOrganization: CEO at AIWerk AG\n\n"}}}
            if tool == "list_contacts":
                return {"result": {"structuredContent": {"result": "Contacts for bergsmann@gmail.com (1 of 1):\n\nContact ID: c_private\nName: Bela Privat\nEmail: bela@example.ch (Other)\n\n"}}}
            return {}

        monkeypatch.setattr(web_server, "_call_aiwerk_bridge_tool", fake_bridge_call)
        config = {
            "assistant": {
                "email": {
                    "accounts": [
                        {"address": "kontakt@aiwerk.ch", "backend": "google_workspace", "mcp_server": "google-workspace-aiwerk", "user_google_email": "kontakt@aiwerk.ch"},
                        {"address": "bergsmann@gmail.com", "backend": "google_workspace", "mcp_server": "google-workspace-bergsmann", "user_google_email": "bergsmann@gmail.com"},
                    ]
                }
            }
        }
        email = {"accounts": [{"address": "kontakt@aiwerk.ch", "items": [{"sender": "Anna AIWerk <anna@aiwerk.ch>"}]}]}

        summary = getattr(web_server, "_contacts_summary")(config, email, {})

        assert {call["server"] for call in calls if call["tool"] == "search_gmail_messages"} == {"google-workspace-aiwerk", "google-workspace-bergsmann"}
        assert any(call["params"].get("query") == "in:sent newer_than:10d" for call in calls if call["tool"] == "search_gmail_messages")
        assert any(call["params"].get("query") == "newer_than:10d -in:sent" for call in calls if call["tool"] == "search_gmail_messages")
        emails = {item["email"] for item in summary["items"]}
        assert {"anna@aiwerk.ch", "bela@example.ch"}.issubset(emails)
        assert "analytics-noreply@google.com" not in emails
        assert "kontakt@aiwerk.ch" not in emails
        assert "bergsmann@gmail.com" not in emails
        assert summary["google_count"] == 2
        assert summary["summary"] == "2 relevante Kontakte"
        assert summary["source_label"] == "Relevante Kontakte"
        assert summary["relevance_window_days"] == 10
        assert summary["interaction_count"] == 2
        anna = next(item for item in summary["items"] if item["email"] == "anna@aiwerk.ch")
        assert anna["organization"] == "AIWerk AG"
        assert anna["role"] == "CEO"
        assert "Google Contacts" in anna["source_badges"]

    def test_contacts_summary_scans_himalaya_sent_and_inbox_accounts(self, monkeypatch):
        import hermes_cli.web_server as web_server

        calls = []
        now = datetime.now(timezone.utc)
        recent = (now - timedelta(days=1)).isoformat().replace("T", " ").replace("+00:00", "+00:00")
        old = (now - timedelta(days=30)).isoformat().replace("T", " ").replace("+00:00", "+00:00")

        class _Proc:
            returncode = 0
            stderr = ""

            def __init__(self, stdout):
                self.stdout = stdout

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            folder = cmd[cmd.index("--folder") + 1]
            if folder == "Gesendet":
                return _Proc(json.dumps([
                    {
                        "id": "s1",
                        "subject": "Offerte",
                        "from": {"name": "Attila", "addr": "user@example.ch"},
                        "to": [{"name": "Client Sent", "addr": "client@example.ch"}],
                        "cc": [{"name": "Self", "addr": "user@example.ch"}],
                        "date": recent,
                    },
                    {
                        "id": "old",
                        "subject": "Alt",
                        "to": [{"name": "Old", "addr": "old@example.ch"}],
                        "date": old,
                    },
                ]))
            return _Proc(json.dumps([
                {
                    "id": "i1",
                    "subject": "Antwort",
                    "from": {"name": "Inbox Human", "addr": "inbox@example.ch"},
                    "date": recent,
                },
                {
                    "id": "self",
                    "subject": "Self",
                    "from": {"name": "Self", "addr": "user@example.ch"},
                    "date": recent,
                },
            ]))

        monkeypatch.delenv("AIWERK_CUI_CONTACTS_JSON", raising=False)
        monkeypatch.delenv("AIWERK_CUI_EMAIL_DISABLE_HIMALAYA", raising=False)
        monkeypatch.setattr(web_server, "_read_manual_contacts", lambda: [])
        monkeypatch.setattr("hermes_cli.web_server.shutil.which", lambda name: "/usr/bin/himalaya" if name == "himalaya" else None)
        monkeypatch.setattr(web_server.subprocess, "run", fake_run)

        summary = getattr(web_server, "_contacts_summary")({
            "assistant": {"email": {"accounts": [
                {"backend": "himalaya", "address": "user@example.ch", "account": "demo", "folder": "INBOX", "sent_folder": "Gesendet"},
            ]}}
        }, {"accounts": []}, {})

        assert any("Gesendet" in call for call in calls)
        assert any("INBOX" in call for call in calls)
        emails = {item["email"] for item in summary["items"]}
        assert {"client@example.ch", "inbox@example.ch"}.issubset(emails)
        assert "user@example.ch" not in emails
        assert "old@example.ch" not in emails
        assert summary["interaction_count"] == 2
        assert summary["summary"] == "2 relevante Kontakte"

    def test_contacts_summary_tops_up_with_recent_saved_google_contacts(self, monkeypatch):
        import hermes_cli.web_server as web_server

        calls = []
        monkeypatch.delenv("AIWERK_CUI_CONTACTS_JSON", raising=False)
        monkeypatch.setenv("AIWERK_CUI_CONTACTS_SAVED_TOP_UP_TARGET", "3")
        monkeypatch.setattr(web_server, "_read_manual_contacts", lambda: [])
        monkeypatch.setattr(web_server, "_contacts_from_google_workspace_interactions", lambda config, *, own_emails: [
            getattr(web_server, "_contact_from_address")("Active Client <active@example.ch>", source="Gesendet", score=5.0, last_interaction_at="2026-06-03T10:00:00Z", relevance="relevant")
        ])
        monkeypatch.setattr(web_server, "_contacts_from_himalaya_interactions", lambda config, *, own_emails: [])

        def fake_bridge_call(config, *, server, tool, params):
            calls.append({"server": server, "tool": tool, "params": params})
            if tool == "list_contacts":
                return {"result": {"structuredContent": {"result": "Contacts for user@example.ch (3 of 3):\n\nContact ID: active\nName: Active Client\nEmail: active@example.ch (Work)\n\nContact ID: saved1\nName: Saved One\nEmail: saved1@example.ch (Work)\n\nContact ID: saved2\nName: Saved Two\nEmail: saved2@example.ch (Work)\n\n"}}}
            return {}

        monkeypatch.setattr(web_server, "_call_aiwerk_bridge_tool", fake_bridge_call)
        summary = getattr(web_server, "_contacts_summary")({
            "assistant": {"email": {"accounts": [
                {"address": "user@example.ch", "backend": "google_workspace", "mcp_server": "google-workspace-demo", "user_google_email": "user@example.ch"},
            ]}}
        }, {"accounts": []}, {})

        emails = [item["email"] for item in summary["items"]]
        assert emails[:3] == ["active@example.ch", "saved2@example.ch", "saved1@example.ch"]
        assert summary["interaction_count"] == 1
        assert summary["saved_count"] == 2
        assert summary["total_count"] == 3
        assert any(call["tool"] == "list_contacts" and call["params"].get("sort_order") == "LAST_MODIFIED_DESCENDING" for call in calls)

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
                {"address": "kontakt@aiwerk.ch", "backend": "google_workspace", "mcp_server": "google-workspace-aiwerk", "user_google_email": "kontakt@aiwerk.ch"},
                {"address": "bergsmann@gmail.com", "backend": "google_workspace", "mcp_server": "google-workspace-bergsmann", "user_google_email": "bergsmann@gmail.com"},
            ]}}
        })

        def fake_bridge_call(config, *, server, tool, params):
            calls.append({"server": server, "tool": tool, "params": params})
            return {"result": {"structuredContent": {"result": f"Contacts for {params['user_google_email']} (1 of 1):\n\nContact ID: c_{server}\nName: Max Treffer\nEmail: max-{server}@example.ch\n\n"}}}

        monkeypatch.setattr(web_server, "_call_aiwerk_bridge_tool", fake_bridge_call)

        resp = self.client.get("/api/cui/contacts/search?q=max")

        assert resp.status_code == 200
        assert resp.json()["total_count"] == 2
        assert {call["server"] for call in calls if call["tool"] == "search_contacts"} == {"google-workspace-aiwerk", "google-workspace-bergsmann"}
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
                {"address": "kontakt@aiwerk.ch", "backend": "google_workspace", "mcp_server": "google-workspace-aiwerk", "user_google_email": "kontakt@aiwerk.ch"},
            ]}}
        })

        def fake_bridge_call(config, *, server, tool, params):
            if tool == "list_contacts":
                return {"result": {"structuredContent": {"result": "Contacts for kontakt@aiwerk.ch (1 of 1):\n\nContact ID: adam\nName: Ádám Bergsmann\nEmail: adam@example.ch (Work)\n\n"}}}
            if tool == "search_contacts":
                return {"result": {"structuredContent": {"result": "Contacts for kontakt@aiwerk.ch (0 of 0):\n\n"}}}
            return {"result": {"structuredContent": {"result": ""}}}

        monkeypatch.setattr(web_server, "_call_aiwerk_bridge_tool", fake_bridge_call)

        resp = self.client.get("/api/cui/contacts/search?q=Adam%20Bergsmann")

        assert resp.status_code == 200
        body = resp.json()
        assert body["total_count"] == 1
        assert body["items"][0]["display_name"] == "Ádám Bergsmann"

    def test_cui_contacts_search_filters_own_and_system_contacts(self, monkeypatch):
        import hermes_cli.web_server as web_server

        with web_server._ASSISTANT_RESOURCE_CACHE_LOCK:
            web_server._ASSISTANT_RESOURCE_CACHE.clear()
        monkeypatch.setattr(web_server, "_read_manual_contacts", lambda: [
            {"display_name": "Self Manual", "email": "kontakt@aiwerk.ch", "source_badges": ["Manuell"]},
            {"display_name": "Local Human", "email": "local@example.ch", "source_badges": ["Manuell"]},
        ])
        monkeypatch.setattr(web_server, "_email_summary", lambda config: {"status": "connected", "accounts": [{"address": "kontakt@aiwerk.ch", "items": []}]})
        monkeypatch.setattr(web_server, "_calendar_summary", lambda config: {"status": "connected", "accounts": []})
        monkeypatch.setattr(web_server, "_shared_folder_summary", lambda config, request=None: {"status": "not_configured"})
        monkeypatch.setattr(web_server, "_vaultwarden_summary", lambda config: {"status": "not_configured"})
        monkeypatch.setattr(web_server, "_todo_summary", lambda config: {"status": "not_configured", "items": []})
        monkeypatch.setattr(web_server, "_connector_summary", lambda config, shared_folder, email, calendar: [])
        monkeypatch.setattr(web_server, "load_config", lambda: {
            "assistant": {"email": {"accounts": [
                {"address": "kontakt@aiwerk.ch", "backend": "google_workspace", "mcp_server": "google-workspace-aiwerk", "user_google_email": "kontakt@aiwerk.ch"},
            ]}}
        })

        def fake_bridge_call(config, *, server, tool, params):
            return {"result": {"structuredContent": {"result": "Contacts for kontakt@aiwerk.ch (4 of 4):\n\nContact ID: self\nName: Kontakt AIWerk\nEmail: kontakt@aiwerk.ch (Work)\n\nContact ID: noreply\nName: No Reply\nEmail: noreply@example.ch (Work)\n\nContact ID: cf-test\nName: Mewo+Outlet-CF-Test\nEmail: mewo-cf-test@example.ch (Work)\n\nContact ID: human\nName: Human Match\nEmail: human@example.ch (Work)\n\n"}}}

        monkeypatch.setattr(web_server, "_call_aiwerk_bridge_tool", fake_bridge_call)

        resp = self.client.get("/api/cui/contacts/search?q=example")

        assert resp.status_code == 200
        emails = {item["email"] for item in resp.json()["items"]}
        assert "human@example.ch" in emails
        assert "local@example.ch" in emails
        assert "kontakt@aiwerk.ch" not in emails
        assert "noreply@example.ch" not in emails
        assert "mewo-cf-test@example.ch" not in emails

    def test_cui_contacts_payload_final_guard_filters_cached_own_contacts(self):
        import hermes_cli.web_server as web_server

        payload = {
            "status": "connected",
            "summary": "3 Kontakte verfügbar",
            "items": [
                {"display_name": "Kontakt AIWerk", "email": "kontakt@aiwerk.ch", "source_badges": ["Google Contacts", "kontakt@aiwerk.ch"]},
                {"display_name": "Attila", "email": "bergsmann@gmail.com", "source_badges": ["Google Contacts", "bergsmann@gmail.com"]},
                {"display_name": "Human", "email": "human@example.ch", "source_badges": ["Google Contacts", "kontakt@aiwerk.ch", "bergsmann@gmail.com"]},
            ],
            "frequent": [
                {"display_name": "Kontakt AIWerk", "email": "kontakt@aiwerk.ch", "source_badges": ["kontakt@aiwerk.ch"]},
                {"display_name": "Human", "email": "human@example.ch", "source_badges": ["Google Contacts", "kontakt@aiwerk.ch"]},
            ],
            "relevant": [{"display_name": "Attila", "email": "bergsmann@gmail.com", "source_badges": ["bergsmann@gmail.com"]}],
            "total_count": 3,
        }

        filtered = getattr(web_server, "_filter_contacts_payload")(
            payload,
            own_emails={"kontakt@aiwerk.ch", "bergsmann@gmail.com"},
        )

        assert [item["email"] for item in filtered["items"]] == ["human@example.ch"]
        assert filtered["items"][0]["source_badges"] == ["Google Contacts"]
        assert [item["email"] for item in filtered["frequent"]] == ["human@example.ch"]
        assert filtered["frequent"][0]["source_badges"] == ["Google Contacts"]
        assert filtered["relevant"] == []

    def test_assistant_api_allowed_is_method_aware(self):
        import hermes_cli.web_server as web_server

        allowed = web_server._assistant_api_allowed
        # Read-only session reads stay reachable under the /api/sessions/ prefix.
        assert allowed("/api/sessions/stats", "GET") is True
        assert allowed("/api/sessions/abc/messages", "GET") is True
        # Destructive verbs under the same prefix are refused.
        assert allowed("/api/sessions/bulk-delete", "POST") is False
        assert allowed("/api/sessions/empty", "DELETE") is False
        assert allowed("/api/sessions/prune", "POST") is False
        assert allowed("/api/sessions/abc", "DELETE") is False
        assert allowed("/api/sessions/abc", "PATCH") is False
        # Exact-match entries remain allowed for their (non-GET) methods.
        assert allowed("/api/assistant/todos/add", "POST") is True

    def test_assistant_mode_blocks_destructive_session_http(self, monkeypatch):
        import hermes_cli.web_server as web_server

        monkeypatch.setattr(web_server, "_DASHBOARD_MODE", "assistant")
        # bulk-delete with a valid body would be 200/422 in admin mode; a 404
        # here proves the assistant-mode gate refused the destructive verb.
        assert self.client.post("/api/sessions/bulk-delete", json={"ids": ["x"]}).status_code == 404
        assert self.client.delete("/api/sessions/empty").status_code == 404
        assert self.client.post("/api/sessions/prune", json={}).status_code == 404
        # A read-only session endpoint is not blocked by the gate.
        assert self.client.get("/api/sessions/stats").status_code != 404
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
        assert "- [ ] Neue Aufgabe erfassen" in todo_file.read_text(encoding="utf-8")

        cached_after_add = self.client.get("/api/assistant/resources?resource=todos")
        assert cached_after_add.status_code == 200
        assert cached_after_add.json()["todos"]["open_count"] == 2
        assert self.client.post("/api/assistant/todos/add", json={"text": "   "}).status_code == 400

    def test_vault_summary_prefers_aiwerk_bridge_health_check(self, monkeypatch):
        import hermes_cli.web_server as web_server

        calls = []

        def fake_bridge_call(config, *, server, tool, params):
            calls.append({"server": server, "tool": tool, "params": params})
            return {
                "status": "ok",
                "vault_url": "https://pass.aiwerk.ch/api",
                "authenticated": True,
                "exposed_collection_visible": True,
                "agent_created_collection_visible": True,
                "items_in_exposed": 7,
                "items_in_agent_created": 2,
            }

        monkeypatch.setattr(web_server, "_call_aiwerk_bridge_tool", fake_bridge_call)
        monkeypatch.setattr(web_server.shutil, "which", lambda name: None)

        data = getattr(web_server, "_vaultwarden_summary")({
            "mcp_servers": {"aiwerk_bridge": {"url": "https://bridge.aiwerk.ch/u/demo/mcp", "enabled": True}},
        })

        assert calls == [{"server": "vault", "tool": "health_check", "params": {}}]
        assert data["status"] == "connected"
        assert data["source"] == "aiwerk_bridge"
        assert data["vault_url"] == "https://pass.aiwerk.ch"
        assert data["item_count"] == 9
        assert data["exposed_count"] == 7
        assert data["agent_created_count"] == 2
        assert data["weak_count"] is None
        assert data["reused_count"] is None
        assert "freigegebene Zugangsdaten" in data["summary"]

    def test_aiwerk_bridge_live_subservers_use_router_status(self, monkeypatch):
        import hermes_cli.web_server as web_server

        monkeypatch.setattr(web_server, "_mcp_bridge_initialize", lambda config, **kwargs: "session-1")

        def fake_rpc(config, method, params, *, session_id=None, request_id=1):
            assert method == "tools/call"
            assert params == {"name": "mcp", "arguments": {"action": "status"}}
            assert session_id == "session-1"
            return {
                "result": {
                    "content": [{
                        "type": "text",
                        "text": json.dumps({
                            "action": "status",
                            "mode": "router",
                            "servers": [
                                {"name": "coinmarketcap", "transport": "streamable-http", "status": "disconnected", "tools": 0},
                                {"name": "google-workspace-aiwerk", "transport": "stdio", "status": "disconnected", "tools": 0},
                                {"name": "firecrawl", "transport": "stdio", "status": "disconnected", "tools": 0},
                            ],
                        }),
                    }]
                }
            }, "session-1"

        monkeypatch.setattr(web_server, "_mcp_bridge_rpc", fake_rpc)

        children = getattr(web_server, "_aiwerk_bridge_live_subservers")({"mcp_servers": {"aiwerk_bridge": {"url": "https://bridge.example/mcp"}}})

        assert [child["label"] for child in children] == ["CoinMarketCap", "Google Workspace AIWerk", "Firecrawl"]
        assert children[0]["open_url"] == "https://aiwerkmcp.com/#/catalog/coinmarketcap"
        assert children[1]["open_url"] == "https://aiwerkmcp.com/#/catalog/google-workspace"
        assert children[1]["description"] == "Gmail, Kalender und Drive"
        assert all(child["capabilities"] == ["Bridge-Subserver"] for child in children)

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

    def test_webdav_download_uses_file_url_without_trailing_slash(self, monkeypatch):
        import hermes_cli.web_server as web_server

        requested_urls: list[str] = []

        class FakeResponse:
            status = 200
            headers = {"content-type": "application/pdf"}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, *_args):
                return b"%PDF-1.4\n"

        def fake_urlopen(request, timeout=None):
            requested_urls.append(request.full_url)
            return FakeResponse()

        monkeypatch.setattr(web_server, "_pass_first_line", lambda entry: "secret")
        monkeypatch.setattr(web_server.urllib.request, "urlopen", fake_urlopen)

        downloaded = web_server._download_webdav_cloud_file({
            "type": "sftpgo_webdav",
            "base_url": "https://dav.example.test",
            "username": "customer.example",
            "password_pass_entry": "pass/entry",
            "path": "/Example Customer/Customer-Shared",
        }, "Bedienungsanleitungen/manual.pdf")

        assert downloaded is not None
        assert requested_urls == ["https://dav.example.test/Example%20Customer/Customer-Shared/Bedienungsanleitungen/manual.pdf"]

    def test_assistant_resource_attachment_copies_shared_file(self, tmp_path, monkeypatch):
        import hermes_cli.web_server as web_server

        shared = tmp_path / "shared"
        shared.mkdir()
        image = shared / "photo.png"
        image.write_bytes(b"fake-png")
        monkeypatch.setenv("AIWERK_CUI_SHARED_FOLDER", str(shared))
        monkeypatch.setattr(web_server, "load_config", lambda: {"dashboard": {}})

        resp = self.client.post("/api/assistant/attachments/resource", json={
            "kind": "shared_file",
            "session_id": "session-123",
            "item": {
                "name": "photo.png",
                "kind": "file",
                "mime": "image/png",
                "open_url": "/api/assistant/shared-folder/open?path=photo.png",
            },
        })

        assert resp.status_code == 200
        attachment = resp.json()["attachments"][0]
        assert attachment["name"] == "photo.png"
        assert attachment["type"] == "image/png"
        assert attachment["is_image"] is True
        copied = Path(attachment["path"])
        assert copied.read_bytes() == b"fake-png"
        assert web_server.get_hermes_home() / "dashboard_uploads" in copied.parents

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

    def test_assistant_resource_attachment_writes_contact_context(self):
        resp = self.client.post("/api/assistant/attachments/resource", json={
            "kind": "contact",
            "session_id": "session-123",
            "item": {
                "display_name": "Anna Meier",
                "organization": "Beispiel AG",
                "role": "CEO",
                "email": "anna@example.ch",
                "phone": "+41 31 000 00 00",
                "source_badges": ["Gmail", "Calendar"],
                "raw_connector_metadata": "must-not-leak",
            },
        })

        assert resp.status_code == 200
        attachment = resp.json()["attachments"][0]
        assert attachment["name"].startswith("contact-Anna-Meier")
        text = Path(attachment["path"]).read_text(encoding="utf-8")
        assert "Attached contact context" in text
        assert "Anna Meier" in text
        assert "Beispiel AG" in text
        assert "anna@example.ch" in text
        assert "Gmail, Calendar" in text
        assert "raw_connector_metadata" not in text
        assert "must-not-leak" not in text

    def test_assistant_resource_attachment_rejects_shared_traversal(self, tmp_path, monkeypatch):
        import hermes_cli.web_server as web_server

        shared = tmp_path / "shared"
        shared.mkdir()
        monkeypatch.setenv("AIWERK_CUI_SHARED_FOLDER", str(shared))
        monkeypatch.setattr(web_server, "load_config", lambda: {"dashboard": {}})

        resp = self.client.post("/api/assistant/attachments/resource", json={
            "kind": "shared_file",
            "item": {"open_url": "/api/assistant/shared-folder/open?path=../secret.png"},
        })

        assert resp.status_code == 400

    def test_assistant_resources_can_read_himalaya_mailbox(self, monkeypatch):
        import hermes_cli.web_server as web_server

        calls = []

        class _Proc:
            returncode = 0
            stderr = ""

            def __init__(self, stdout):
                self.stdout = stdout

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            assert kwargs["timeout"] == 12
            if cmd[-3:] == ["not", "flag", "Seen"]:
                return _Proc(json.dumps([
                    {
                        "id": "42",
                        "flags": [],
                        "subject": "Neue Offerte",
                        "from": {"name": "Max Muster", "addr": "max@example.com"},
                        "date": "2026-05-30 16:57+00:00",
                        "has_attachment": True,
                    }
                ]))
            return _Proc("[]")

        monkeypatch.delenv("AIWERK_CUI_EMAIL_SUMMARY_JSON", raising=False)
        monkeypatch.delenv("AIWERK_CUI_EMAIL_DISABLE_HIMALAYA", raising=False)
        monkeypatch.setattr("hermes_cli.web_server.shutil.which", lambda name: "/usr/bin/himalaya" if name == "himalaya" else None)
        monkeypatch.setattr(web_server.subprocess, "run", fake_run)
        monkeypatch.setattr(web_server, "load_config", lambda: {
            "dashboard": {"email": {"backend": "himalaya", "account": "demo", "folder": "INBOX"}},
        })

        resp = self.client.get("/api/assistant/resources?refresh=1")

        assert resp.status_code == 200
        email = resp.json()["email"]
        assert email["status"] == "connected"
        assert email["unread_count"] == 1
        assert email["summary"] == "1 neue Nachrichten"
        assert email["items"][0]["subject"] == "Neue Offerte"
        assert email["items"][0]["sender"] == "Max Muster <max@example.com>"
        assert email["items"][0]["received_at"] == "2026-05-30T16:57:00Z"
        assert email["items"][0]["unread"] is True
        assert email["items"][0]["has_attachment"] is True
        assert email["items"][0]["message_id"] == "42"
        assert email["items"][0]["open_url"].startswith("/api/assistant/email/view?")
        assert "account=demo" in email["items"][0]["open_url"]
        assert "id=42" in email["items"][0]["open_url"]
        assert calls[0][:9] == ["himalaya", "envelope", "list", "--account", "demo", "--folder", "INBOX", "--page-size", "50"]

    def test_assistant_email_summary_hides_obvious_dashboard_spam(self):
        import hermes_cli.web_server as web_server

        merged = web_server._merge_email_summaries([
            {
                "status": "connected",
                "account_label": "user@example.com",
                "account_address": "user@example.com",
                "source": "imap",
                "unread_count": 2,
                "items": [
                    {
                        "id": "9687",
                        "sender": "Migros <info@attractivewedding.info>",
                        "subject": "Es gibt ein Update zu Ihrem kürzlich getätigten Kauf!!",
                        "received_at": "2026-06-01T14:52:00Z",
                        "unread": True,
                    },
                    {
                        "id": "9688",
                        "sender": "Max Muster <max@example.com>",
                        "subject": "Neue Offerte",
                        "received_at": "2026-06-01T14:53:00Z",
                        "unread": True,
                    },
                ],
            }
        ])

        assert merged is not None
        assert merged["unread_count"] == 1
        assert merged["filtered_count"] == 1
        assert [item["id"] for item in merged["items"]] == ["9688"]
        assert merged["accounts"][0]["unread_count"] == 1
        assert merged["accounts"][0]["filtered_count"] == 1

    def test_assistant_email_items_show_all_unread_then_fill_to_five(self):
        import hermes_cli.web_server as web_server

        unread = [
            {"id": "u1", "message_id": "u1", "subject": "Unread 1", "received_at": "2026-06-02T10:00:00Z"},
            {"id": "u2", "message_id": "u2", "subject": "Unread 2", "received_at": "2026-06-02T11:00:00Z"},
        ]
        latest = [
            {"id": "u2", "message_id": "u2", "subject": "Unread duplicate", "received_at": "2026-06-02T11:00:00Z"},
            {"id": "r1", "message_id": "r1", "subject": "Read 1", "received_at": "2026-06-02T09:00:00Z"},
            {"id": "spam", "message_id": "spam", "sender": "Migros <info@attractivewedding.info>", "subject": "Migros fake", "received_at": "2026-06-02T08:30:00Z"},
            {"id": "r2", "message_id": "r2", "subject": "Read 2", "received_at": "2026-06-02T08:00:00Z"},
            {"id": "r3", "message_id": "r3", "subject": "Read 3", "received_at": "2026-06-02T07:00:00Z"},
        ]

        items = web_server._unread_first_email_items(unread, latest)

        assert [item["message_id"] for item in items] == ["u2", "u1", "r1", "r2", "r3"]
        assert [item["unread"] for item in items] == [True, True, False, False, False]

    def test_assistant_email_items_keep_more_than_five_unread_without_latest_fill(self):
        import hermes_cli.web_server as web_server

        unread = [
            {"id": f"u{index}", "message_id": f"u{index}", "subject": f"Unread {index}", "received_at": f"2026-06-02T10:{index:02d}:00Z"}
            for index in range(7)
        ]

        items = web_server._unread_first_email_items(unread, [{"id": "read", "message_id": "read"}])

        assert len(items) == 7
        assert all(item["unread"] is True for item in items)
        assert "read" not in {item["message_id"] for item in items}

    def test_assistant_email_viewer_reads_himalaya_message_as_sanitized_html(self, monkeypatch):
        import hermes_cli.web_server as web_server

        monkeypatch.setattr(web_server, "load_config", lambda: {
            "dashboard": {"email": {"backend": "himalaya", "account": "demo", "address": "user@example.com", "folder": "INBOX"}},
        })
        monkeypatch.setattr(web_server, "_assistant_resources_payload", lambda request, force_refresh=False, **kwargs: {
            "email": {
                "accounts": [{
                    "address": "user@example.com",
                    "label": "user@example.com",
                    "items": [{"id": "42", "message_id": "42", "sender": "Max <max@example.com>", "subject": "<Hello>", "received_at": "2026-05-31T18:00:00Z"}],
                }]
            }
        })
        seen = {}

        def fake_read(**kwargs):
            seen.update(kwargs)
            return "--- BODY ---\nHallo<script>alert(1)</script>\nhttps://example.com/really/long/tracking/url?token=abc123\n<img src=x>"

        monkeypatch.setattr(web_server, "_run_himalaya_message_read", fake_read)

        resp = self.client.get("/api/assistant/email/view?account=user%40example.com&id=42")

        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert "no-store" in resp.headers["cache-control"]
        assert "default-src 'none'" in resp.headers["content-security-policy"]
        assert seen == {"message_id": "42", "account": "demo", "folder": "INBOX"}
        text = resp.text
        assert "Nur-Leseansicht" in text
        assert "&lt;Hello&gt;" in text
        assert "Hallo&lt;script&gt;alert(1)&lt;/script&gt;" in text
        assert "--- BODY ---" not in text
        assert "https://example.com" not in text
        assert "[LINK]" in text
        assert "<script>alert(1)</script>" not in text

    def test_assistant_email_viewer_reads_google_workspace_message_as_sanitized_html(self, monkeypatch):
        import hermes_cli.web_server as web_server

        seen = {}
        monkeypatch.setattr(web_server, "load_config", lambda: {
            "dashboard": {
                "email": {
                    "accounts": [
                        {
                            "backend": "aiwerk_bridge",
                            "address": "kontakt@aiwerk.ch",
                            "mcp_server": "google-workspace-aiwerk",
                            "user_google_email": "kontakt@aiwerk.ch",
                        }
                    ]
                }
            },
            "mcp_servers": {"aiwerk_bridge": {"url": "https://bridge.aiwerk.ch/u/demo/mcp", "enabled": True}},
        })
        monkeypatch.setattr(web_server, "_assistant_resources_payload", lambda request, force_refresh=False, **kwargs: {
            "email": {
                "accounts": [{
                    "address": "kontakt@aiwerk.ch",
                    "items": [{
                        "message_id": "gmail-1",
                        "sender": "Website <web@example.com>",
                        "subject": "Google Anfrage",
                        "received_at": "2026-05-30T19:10:00Z",
                    }],
                }]
            }
        })

        def fake_google_read(config, account_cfg, *, message_id):
            seen["message_id"] = message_id
            seen["account"] = account_cfg["address"]
            return """Retrieved 1 messages:

Message ID: gmail-1
Subject: Google Anfrage
From: Website <web@example.com>
Date: Sat, 30 May 2026 19:10:00 +0000
Message-ID: <gmail-1@example.com>
To: kontakt@aiwerk.ch
List-Unsubscribe: <https://example.com/unsubscribe>
Web Link: https://mail.google.com/mail/u/0/#all/gmail-1

--- BODY ---
Google body<script>alert(1)</script>
Link: https://example.com/a/very/long/link?with=query&and=tracking
www.example.org/landing

--- ATTACHMENTS ---
1. conv_0201kszsyfvpe8f8vhag6cfyd196.json (application/json, 4.8 KB)
   Attachment ID: ANGjdJ9X4YSaYhNIGonrXR7S34YjmYZR8acEwKVANQ80UZurma5q7pbvsvirvd5CXIWizCZzvszLCAwFkFYrnXYfHiQeKHOlnDH9IoQgntGt7BgwXP_7mxY
   Use get_gmail_attachment_content(message_id='gmail-1', attachment_id='ANGjdJ9X4YSaYhNIGonrXR7S34YjmYZR8acEwKVANQ80UZurma5q7pbvsvirvd5CXIWizCZzvszLCAwFkFYrnXYfHiQeKHOlnDH9IoQgntGt7BgwXP_7mxY') to download
2. angebot.pdf (application/pdf, 128 KB)
   Attachment ID: secret-attachment-id
<img src=x>"""

        monkeypatch.setattr(web_server, "_run_google_workspace_message_read", fake_google_read)

        resp = self.client.get("/api/assistant/email/view?account=kontakt%40aiwerk.ch&id=gmail-1")

        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert resp.headers["cache-control"] == "no-store"
        assert "default-src 'none'" in resp.headers["content-security-policy"]
        assert seen == {"message_id": "gmail-1", "account": "kontakt@aiwerk.ch"}
        text = resp.text
        assert "Google Anfrage" in text
        assert "Google body&lt;script&gt;alert(1)&lt;/script&gt;" in text
        assert "Retrieved 1 messages" not in text
        assert "Message ID: gmail-1" not in text
        assert "List-Unsubscribe" not in text
        assert "mail.google.com" not in text
        assert "--- BODY ---" not in text
        assert "https://example.com/a/very/long" not in text
        assert "www.example.org" not in text
        assert text.count("[LINK]") >= 2
        assert "--- ATTACHMENTS ---" not in text
        assert "Attachment ID" not in text
        assert "get_gmail_attachment_content" not in text
        assert "ANGjdJ9X4YSa" not in text
        assert "Anhänge:" in text
        assert "conv_0201kszsyfvpe8f8vhag6cfyd196.json (application/json, 4.8 KB)" in text
        assert "angebot.pdf (application/pdf, 128 KB)" in text
        assert "<script>alert(1)</script>" not in text

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

    def test_assistant_resources_himalaya_preview_falls_back_to_latest_when_no_unread(self, monkeypatch):
        import hermes_cli.web_server as web_server

        calls = []

        class _Proc:
            returncode = 0
            stderr = ""

            def __init__(self, stdout):
                self.stdout = stdout

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if cmd[-3:] == ["not", "flag", "Seen"]:
                return _Proc("[]")
            return _Proc(json.dumps([
                {
                    "id": "99",
                    "flags": ["Seen"],
                    "subject": "Letzte Nachricht",
                    "from": {"addr": "info@example.com"},
                    "date": "2026-05-30 08:00+02:00",
                }
            ]))

        monkeypatch.delenv("AIWERK_CUI_EMAIL_SUMMARY_JSON", raising=False)
        monkeypatch.setattr("hermes_cli.web_server.shutil.which", lambda name: "/usr/bin/himalaya" if name == "himalaya" else None)
        monkeypatch.setattr(web_server.subprocess, "run", fake_run)
        monkeypatch.setattr(web_server, "load_config", lambda: {"dashboard": {"email": {"enabled": True}}})

        resp = self.client.get("/api/assistant/resources?refresh=1")

        assert resp.status_code == 200
        email = resp.json()["email"]
        assert email["unread_count"] == 0
        assert email["summary"] == "Keine neuen Nachrichten"
        assert email["items"][0]["subject"] == "Letzte Nachricht"
        assert email["items"][0]["unread"] is False
        assert len(calls) == 2

    def test_assistant_resources_can_read_google_workspace_mail_via_aiwerk_bridge(self, monkeypatch):
        import hermes_cli.web_server as web_server

        calls = []

        class _Response:
            def __init__(self, body, headers=None):
                self._body = body
                self.headers = headers or {}

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return self._body.encode("utf-8")

        def mcp_tool_payload(text):
            return json.dumps({
                "jsonrpc": "2.0",
                "id": 2,
                "result": {
                    "content": [{"type": "text", "text": json.dumps({
                        "result": {
                            "structuredContent": {"result": text},
                            "content": [{"type": "text", "text": text}],
                            "isError": False,
                        }
                    })}],
                },
            })

        def fake_urlopen(req, timeout):
            payload = json.loads(req.data.decode("utf-8"))
            calls.append(payload)
            assert timeout == 30
            if payload["method"] == "initialize":
                return _Response(
                    json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-06-18"}}),
                    {"MCP-Session-Id": "session-1"},
                )
            arguments = payload["params"]["arguments"]
            assert arguments["server"] == "google-workspace-aiwerk"
            if arguments["tool"] == "search_gmail_messages":
                return _Response(mcp_tool_payload("""
Found 1 messages matching 'in:inbox is:unread':

📧 MESSAGES:
  1. Message ID: 19e70ea1de7486ee
     Web Link: https://mail.google.com/mail/u/0/#all/19e70ea1de7486ee
     Thread ID: 19e70ea1de7486ee
"""))
            return _Response(mcp_tool_payload("""
Retrieved 1 messages:

Message ID: 19e70ea1de7486ee
Subject: Kontaktformular Anfrage
From: AIWerk Website <kontakt@aiwerk.ch>
Date: Sat, 30 May 2026 19:10:00 +0000
To: <kontakt@aiwerk.ch>
Web Link: https://mail.google.com/mail/u/0/#all/19e70ea1de7486ee
"""))

        monkeypatch.delenv("AIWERK_CUI_EMAIL_SUMMARY_JSON", raising=False)
        monkeypatch.setattr(web_server.urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setattr(web_server, "load_config", lambda: {
            "dashboard": {"email": {"backend": "aiwerk_bridge", "mcp_server": "google-workspace-aiwerk", "user_google_email": "me"}},
            "mcp_servers": {"aiwerk_bridge": {"url": "https://bridge.aiwerk.ch/u/demo/mcp", "enabled": True, "headers": {"Authorization": "Bearer test"}}},
        })

        resp = self.client.get("/api/assistant/resources?refresh=1")

        assert resp.status_code == 200
        email = resp.json()["email"]
        assert email["status"] == "connected"
        assert email["unread_count"] == 1
        assert email["summary"] == "1 neue Nachrichten"
        assert email["items"][0]["subject"] == "Kontaktformular Anfrage"
        assert email["items"][0]["sender"] == "AIWerk Website <kontakt@aiwerk.ch>"
        assert email["items"][0]["received_at"] == "2026-05-30T19:10:00Z"
        assert email["items"][0]["unread"] is True
        assert email["items"][0]["message_id"] == "19e70ea1de7486ee"
        assert email["items"][0]["gmail_web_url"].startswith("https://mail.google.com/")
        assert email["items"][0]["open_url"].startswith("/api/assistant/email/view?")
        assert "account=Google+Workspace" in email["items"][0]["open_url"]
        assert "id=19e70ea1de7486ee" in email["items"][0]["open_url"]
        tools = [
            call.get("params", {}).get("arguments", {}).get("tool")
            for call in calls[1:]
            if call.get("params", {}).get("arguments", {}).get("tool")
        ]
        assert tools.count("search_gmail_messages") >= 1
        assert "get_gmail_messages_content_batch" in tools

    def test_aiwerk_bridge_reuses_session_across_cui_tool_calls(self, monkeypatch):
        import hermes_cli.web_server as web_server

        calls = []

        def fake_rpc(_config, method, params, *, session_id=None, request_id=1):
            calls.append({"method": method, "session_id": session_id, "request_id": request_id, "params": params})
            if method == "initialize":
                return {"result": {}}, "session-123"
            return {"result": {"content": [{"text": json.dumps({"ok": True})}]}}, session_id

        config = {"mcp_servers": {"aiwerk_bridge": {"url": "https://bridge.aiwerk.ch/u/demo/mcp"}}}
        monkeypatch.setattr(web_server, "_mcp_bridge_rpc", fake_rpc)

        first = web_server._call_aiwerk_bridge_tool(config, server="google-workspace-aiwerk", tool="one", params={})
        second = web_server._call_aiwerk_bridge_tool(config, server="google-workspace-aiwerk", tool="two", params={})

        assert first == {"ok": True}
        assert second == {"ok": True}
        assert [call["method"] for call in calls] == ["initialize", "tools/call", "tools/call"]
        assert calls[1]["session_id"] == "session-123"
        assert calls[2]["session_id"] == "session-123"
        assert [call["request_id"] for call in calls] == [1, 2, 3]

    def test_aiwerk_bridge_config_expands_header_env_refs_from_dotenv(self, monkeypatch):
        import hermes_cli.web_server as web_server

        monkeypatch.delenv("AIWERK_BRIDGE_MCP_TOKEN", raising=False)
        monkeypatch.setattr(web_server, "load_env", lambda: {"AIWERK_BRIDGE_MCP_TOKEN": "secret-token"})

        bridge = web_server._mcp_bridge_config({
            "mcp_servers": {
                "aiwerk_bridge": {
                    "url": "https://bridge.example/${AIWERK_BRIDGE_MCP_TOKEN}/mcp",
                    "headers": {"Authorization": "Bearer ${AIWERK_BRIDGE_MCP_TOKEN}"},
                }
            }
        })

        assert bridge["url"] == "https://bridge.example/secret-token/mcp"
        assert bridge["headers"]["Authorization"] == "Bearer secret-token"

    def test_google_workspace_email_summary_keeps_account_visible_on_bridge_error(self, monkeypatch):
        import hermes_cli.web_server as web_server

        def fake_bridge_call(_config, *, server, tool, params):
            raise urllib.error.HTTPError("https://bridge.example/mcp", 403, "Forbidden", Message(), None)

        monkeypatch.setattr(web_server, "_call_aiwerk_bridge_tool", fake_bridge_call)
        summary = web_server._google_workspace_email_summary(
            {"mcp_servers": {"aiwerk_bridge": {"url": "https://bridge.example/mcp"}}},
            {"backend": "google_workspace", "address": "kontakt@example.ch", "user_google_email": "kontakt@example.ch"},
        )

        assert summary is not None
        assert summary["status"] == "error"
        assert summary["account_address"] == "kontakt@example.ch"
        assert summary["items"] == []
        assert "403" in summary["error"]

    def test_google_workspace_email_summary_fetches_unread_and_fills_with_latest(self, monkeypatch):
        import hermes_cli.web_server as web_server

        calls = []

        def fake_bridge_call(_config, *, server, tool, params):
            calls.append({"server": server, "tool": tool, "params": params})
            if tool == "search_gmail_messages" and params["query"] == "in:inbox is:unread":
                return {"result": {"content": [{"text": "Message ID: unread-1\nMessage ID: unread-2\n"}]}}
            if tool == "search_gmail_messages" and params["query"] == "in:inbox":
                return {"result": {"content": [{"text": "Message ID: latest-1\n"}]}}
            if tool == "get_gmail_messages_content_batch" and params["message_ids"] == ["unread-1", "unread-2"]:
                return {"result": {"content": [{"text": "Message ID: unread-1\nSubject: U1\nFrom: Sender <s@example.com>\nDate: Tue, 02 Jun 2026 11:00:00 +0000\n\nMessage ID: unread-2\nSubject: U2\nFrom: Sender <s@example.com>\nDate: Tue, 02 Jun 2026 10:00:00 +0000\n"}]}}
            if tool == "get_gmail_messages_content_batch" and params["message_ids"] == ["latest-1"]:
                return {"result": {"content": [{"text": "Message ID: latest-1\nSubject: Hello\nFrom: Sender <s@example.com>\nDate: Tue, 02 Jun 2026 09:00:00 +0000\n"}]}}
            raise AssertionError(f"unexpected bridge call: {tool} {params}")

        monkeypatch.setattr(web_server, "_call_aiwerk_bridge_tool", fake_bridge_call)
        summary = web_server._google_workspace_email_summary(
            {"mcp_servers": {"aiwerk_bridge": {"url": "https://bridge.aiwerk.ch/u/demo/mcp"}}},
            {
                "backend": "google_workspace",
                "mcp_server": "google-workspace-aiwerk",
                "user_google_email": "me@example.com",
                "unread_query": "in:inbox is:unread",
                "latest_query": "in:inbox",
            },
        )

        assert summary is not None
        assert summary["unread_count"] == 2
        assert [item["message_id"] for item in summary["items"]] == ["unread-1", "unread-2", "latest-1"]
        assert [item["unread"] for item in summary["items"]] == [True, True, False]
        assert [call["tool"] for call in calls] == [
            "search_gmail_messages",
            "get_gmail_messages_content_batch",
            "search_gmail_messages",
            "get_gmail_messages_content_batch",
        ]

    def test_google_workspace_email_summary_reuses_search_when_queries_match(self, monkeypatch):
        import hermes_cli.web_server as web_server

        calls = []

        def fake_bridge_call(_config, *, server, tool, params):
            calls.append({"server": server, "tool": tool, "params": params})
            if tool == "search_gmail_messages":
                return {"result": {"content": [{"text": "Message ID: unread-1\n"}]}}
            if tool == "get_gmail_messages_content_batch":
                assert params["message_ids"] == ["unread-1"]
                return {"result": {"content": [{"text": "Message ID: unread-1\nSubject: Hi\nFrom: Sender <s@example.com>\nDate: Tue, 02 Jun 2026 10:00:00 +0000\n"}]}}
            raise AssertionError(f"unexpected bridge call: {tool}")

        monkeypatch.setattr(web_server, "_call_aiwerk_bridge_tool", fake_bridge_call)
        summary = web_server._google_workspace_email_summary(
            {"mcp_servers": {"aiwerk_bridge": {"url": "https://bridge.aiwerk.ch/u/demo/mcp"}}},
            {
                "backend": "google_workspace",
                "mcp_server": "google-workspace-aiwerk",
                "user_google_email": "me@example.com",
                "unread_query": "in:inbox is:unread",
                "latest_query": "in:inbox is:unread",
            },
        )

        assert summary is not None
        assert summary["unread_count"] == 1
        assert summary["items"][0]["unread"] is True
        assert [call["tool"] for call in calls] == [
            "search_gmail_messages",
            "get_gmail_messages_content_batch",
        ]

    def test_assistant_resources_aggregates_google_workspace_and_imap_accounts(self, monkeypatch):
        import hermes_cli.web_server as web_server

        bridge_calls = []
        himalaya_calls = []

        class _Response:
            def __init__(self, body, headers=None):
                self._body = body
                self.headers = headers or {}

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return self._body.encode("utf-8")

        class _Proc:
            returncode = 0
            stderr = ""

            def __init__(self, stdout):
                self.stdout = stdout

        def mcp_tool_payload(text):
            return json.dumps({
                "jsonrpc": "2.0",
                "id": 2,
                "result": {
                    "content": [{"type": "text", "text": json.dumps({
                        "result": {
                            "structuredContent": {"result": text},
                            "content": [{"type": "text", "text": text}],
                            "isError": False,
                        }
                    })}],
                },
            })

        def fake_urlopen(req, timeout):
            payload = json.loads(req.data.decode("utf-8"))
            bridge_calls.append(payload)
            if payload["method"] == "initialize":
                return _Response(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}), {"MCP-Session-Id": "session-1"})
            arguments = payload["params"]["arguments"]
            assert arguments["server"] == "google-workspace-aiwerk"
            assert arguments["params"]["user_google_email"] == "kontakt@aiwerk.ch"
            if arguments["tool"] == "search_gmail_messages":
                return _Response(mcp_tool_payload("Message ID: gmail-1"))
            return _Response(mcp_tool_payload("""
Message ID: gmail-1
Subject: Gmail Anfrage
From: Website <web@example.com>
Date: Sat, 30 May 2026 19:10:00 +0000
Web Link: https://mail.google.com/mail/u/0/#all/gmail-1
"""))

        def fake_run(cmd, **kwargs):
            himalaya_calls.append(cmd)
            assert "--account" in cmd
            assert cmd[cmd.index("--account") + 1] == "info-imap"
            if cmd[-3:] == ["not", "flag", "Seen"]:
                return _Proc(json.dumps([{
                    "id": "imap-1",
                    "flags": [],
                    "subject": "IMAP Anfrage",
                    "from": {"addr": "kunde@example.com"},
                    "date": "2026-05-30 18:00+00:00",
                }]))
            return _Proc("[]")

        monkeypatch.delenv("AIWERK_CUI_EMAIL_SUMMARY_JSON", raising=False)
        monkeypatch.delenv("AIWERK_CUI_EMAIL_BACKEND", raising=False)
        monkeypatch.setattr(web_server.urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setattr("hermes_cli.web_server.shutil.which", lambda name: "/usr/bin/himalaya" if name == "himalaya" else None)
        monkeypatch.setattr(web_server.subprocess, "run", fake_run)
        monkeypatch.setattr(web_server, "load_config", lambda: {
            "dashboard": {
                "email": {
                    "accounts": [
                        {"backend": "google_workspace", "address": "kontakt@aiwerk.ch", "mcp_server": "google-workspace-aiwerk", "user_google_email": "kontakt@aiwerk.ch"},
                        {"backend": "imap", "address": "info@example.ch", "account": "info-imap", "folder": "INBOX"},
                    ]
                }
            },
            "mcp_servers": {"aiwerk_bridge": {"url": "https://bridge.aiwerk.ch/u/demo/mcp", "enabled": True}},
        })

        resp = self.client.get("/api/assistant/resources?refresh=1")

        assert resp.status_code == 200
        email = resp.json()["email"]
        assert email["status"] == "connected"
        assert email["unread_count"] == 2
        assert email["summary"] == "2 neue Nachrichten in 2 Konten"
        assert {account["label"] for account in email["accounts"]} == {"kontakt@aiwerk.ch", "info@example.ch"}
        assert {account["address"] for account in email["accounts"]} == {"kontakt@aiwerk.ch", "info@example.ch"}
        assert {item["account_label"] for item in email["items"]} == {"kontakt@aiwerk.ch", "info@example.ch"}
        assert {item["account_address"] for item in email["items"]} == {"kontakt@aiwerk.ch", "info@example.ch"}
        account_items = {account["address"]: account["items"] for account in email["accounts"]}
        assert account_items["kontakt@aiwerk.ch"][0]["subject"] == "Gmail Anfrage"
        assert account_items["info@example.ch"][0]["subject"] == "IMAP Anfrage"
        assert any(item["subject"] == "Gmail Anfrage" for item in email["items"])
        assert any(item["subject"] == "IMAP Anfrage" for item in email["items"])
        assert bridge_calls
        assert himalaya_calls

    def test_assistant_calendar_mirrors_google_workspace_mail_accounts(self, monkeypatch):
        import hermes_cli.web_server as web_server

        seen = []

        def fake_calendar_summary(config, account_cfg, *, now=None):
            address = account_cfg["address"]
            seen.append(address)
            items = [] if address == "kontakt@aiwerk.ch" else [{
                "id": "event-1",
                "title": "Kieferorthopäde — Anika",
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
                        {"backend": "google_workspace", "address": "kontakt@aiwerk.ch", "mcp_server": "google-workspace-aiwerk", "user_google_email": "kontakt@aiwerk.ch"},
                        {"backend": "google_workspace", "address": "user@example.com", "mcp_server": "google-workspace-demo", "user_google_email": "user@example.com"},
                        {"backend": "himalaya", "address": "office@example.ch", "account": "office"},
                    ]
                }
            }
        })

        assert seen == ["kontakt@aiwerk.ch", "user@example.com"]
        assert summary["status"] == "connected"
        assert summary["summary"] == "1 kommende Termine in 2 Kalendern"
        assert [account["address"] for account in summary["accounts"]] == ["kontakt@aiwerk.ch", "user@example.com"]
        assert summary["items"][0]["title"] == "Kieferorthopäde — Anika"
        assert summary["items"][0]["account_address"] == "user@example.com"
        assert summary["items"][0]["open_url"].startswith("/api/assistant/calendar/view?")
        assert "account=user%40example.com" in summary["items"][0]["open_url"]
        assert "id=event-1" in summary["items"][0]["open_url"]
        assert summary["accounts"][1]["items"][0]["open_url"].startswith("/api/assistant/calendar/view?")

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
                        "title": "Kieferorthopäde — Anika",
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
                        "- Title: Kieferorthopäde — Anika\n"
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
        assert "/api/assistant/calendar/view" in web_server._ASSISTANT_ALLOWED_API_EXACT
        resp = TestClient(web_server.app).get("/api/assistant/calendar/view?account=team%40example.ch&id=event-1")
        assert resp.status_code == 401

    def test_google_workspace_preview_shows_unread_before_latest_fill(self, monkeypatch):
        import hermes_cli.web_server as web_server

        calls = []

        def fake_search_ids(config, *, server, user_google_email, query, page_size):
            calls.append({"query": query, "page_size": page_size, "kind": "search"})
            if query == "in:inbox is:unread":
                return ["old-unread"]
            return ["fresh-read"]

        def fake_items(config, *, server, user_google_email, query, page_size, unread):
            calls.append({"query": query, "page_size": page_size, "unread": unread, "kind": "items"})
            return [{
                "id": "fresh-read",
                "message_id": "fresh-read",
                "sender": "Fresh <fresh@example.com>",
                "subject": "Fresh latest",
                "received_at": "2026-05-31T20:00:00Z",
                "unread": False,
            }]

        def fake_metadata_items(config, *, server, user_google_email, message_ids, unread, page_size):
            calls.append({"message_ids": message_ids, "page_size": page_size, "unread": unread, "kind": "metadata"})
            return [{
                "id": "old-unread",
                "message_id": "old-unread",
                "sender": "Old <old@example.com>",
                "subject": "Old unread",
                "received_at": "2026-05-31T19:00:00Z",
                "unread": True,
            }]

        monkeypatch.setattr(web_server, "_gmail_bridge_search_message_ids", fake_search_ids)
        monkeypatch.setattr(web_server, "_gmail_bridge_message_items", fake_items)
        monkeypatch.setattr(web_server, "_gmail_bridge_metadata_items_for_ids", fake_metadata_items)
        summary = web_server._google_workspace_email_summary({"mcp_servers": {"aiwerk_bridge": {"url": "https://bridge.example/mcp"}}}, {
            "backend": "google_workspace",
            "address": "user@example.com",
            "mcp_server": "google-workspace-demo",
            "user_google_email": "user@example.com",
        })

        assert summary is not None
        assert summary["unread_count"] == 1
        assert summary["items"][0]["subject"] == "Old unread"
        assert summary["items"][0]["unread"] is True
        assert summary["items"][1]["subject"] == "Fresh latest"
        assert summary["items"][1]["unread"] is False
        assert [call["kind"] for call in calls] == ["search", "metadata", "items"]
        assert [call.get("query") for call in calls if call["kind"] == "search"] == ["in:inbox is:unread"]

    def test_assistant_resources_account_items_keep_all_scanned_unread_messages(self):
        import hermes_cli.web_server as web_server

        unread_items = [
            {
                "id": f"mail-{index}",
                "subject": f"Neue Nachricht {index}",
                "received_at": f"2026-05-30T12:{index:02d}:00Z",
                "unread": True,
            }
            for index in range(7)
        ]

        merged = web_server._merge_email_summaries([{
            "status": "connected",
            "account_label": "info@example.ch",
            "account_address": "info@example.ch",
            "source": "imap",
            "unread_count": len(unread_items),
            "summary": "7 neue Nachrichten",
            "items": unread_items,
        }])

        assert merged is not None
        assert merged["unread_count"] == 7
        assert len(merged["accounts"][0]["items"]) == 7
        assert len(merged["items"]) == 5

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
        remote_headers = {"host": "rocky.aiwerk.ch", "x-forwarded-for": "203.0.113.42"}

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
        remote_headers = {"host": "rocky.aiwerk.ch", "x-forwarded-for": "203.0.113.42"}

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
    def test_get_env_vars_marks_channel_managed_keys(self):
        from hermes_cli.web_server import _channel_managed_env_keys

        data = self.client.get("/api/env").json()
        # Every entry carries the classification the Keys page relies on.
        assert all("channel_managed" in info for info in data.values())

        channel_keys = _channel_managed_env_keys()
        # Messaging-platform credentials owned by the Channels page are flagged;
        # everything else stays visible on the Keys page.
        for key, info in data.items():
            assert info["channel_managed"] is (key in channel_keys)

    def test_platform_scoped_messaging_env_vars_are_channel_managed(self):
        from hermes_cli.web_server import (
            _MESSAGING_KEYS_PAGE_KEYS,
            _build_catalog_entry,
            _channel_managed_env_keys,
        )

        discord = _build_catalog_entry("discord")
        assert "DISCORD_HOME_CHANNEL" in discord["env_vars"]
        assert "DISCORD_ALLOW_ALL_USERS" in discord["env_vars"]

        managed = _channel_managed_env_keys()
        assert "DISCORD_HOME_CHANNEL" in managed
        assert "BLUEBUBBLES_ALLOW_ALL_USERS" in managed
        assert "MATTERMOST_ALLOW_ALL_USERS" in managed
        assert "GATEWAY_PROXY_URL" not in managed
        assert "GATEWAY_PROXY_URL" in _MESSAGING_KEYS_PAGE_KEYS

    def test_reveal_env_var(self, tmp_path):
        """POST /api/env/reveal should return the real unredacted value."""
        from hermes_cli.config import save_env_value
        from hermes_cli.web_server import _SESSION_HEADER_NAME, _SESSION_TOKEN
        save_env_value("TEST_REVEAL_KEY", "super-secret-value-12345")
        resp = self.client.post(
            "/api/env/reveal",
            json={"key": "TEST_REVEAL_KEY"},
            headers={_SESSION_HEADER_NAME: _SESSION_TOKEN},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["key"] == "TEST_REVEAL_KEY"
        assert data["value"] == "super-secret-value-12345"

    def test_reveal_env_var_not_found(self):
        """POST /api/env/reveal should 404 for unknown keys."""
        from hermes_cli.web_server import _SESSION_HEADER_NAME, _SESSION_TOKEN
        resp = self.client.post(
            "/api/env/reveal",
            json={"key": "NONEXISTENT_KEY_XYZ"},
            headers={_SESSION_HEADER_NAME: _SESSION_TOKEN},
        )
        assert resp.status_code == 404

    def test_reveal_env_var_no_token(self, tmp_path):
        """POST /api/env/reveal without token should return 401."""
        from starlette.testclient import TestClient
        from hermes_cli.web_server import app
        from hermes_cli.config import save_env_value
        save_env_value("TEST_REVEAL_NOAUTH", "secret-value")
        # Use a fresh client WITHOUT the dashboard session header
        unauth_client = TestClient(app)
        resp = unauth_client.post(
            "/api/env/reveal",
            json={"key": "TEST_REVEAL_NOAUTH"},
        )
        assert resp.status_code == 401

    def test_reveal_env_var_bad_token(self, tmp_path):
        """POST /api/env/reveal with wrong token should return 401."""
        from hermes_cli.config import save_env_value
        from hermes_cli.web_server import _SESSION_HEADER_NAME
        save_env_value("TEST_REVEAL_BADAUTH", "secret-value")
        resp = self.client.post(
            "/api/env/reveal",
            json={"key": "TEST_REVEAL_BADAUTH"},
            headers={_SESSION_HEADER_NAME: "wrong-token-here"},
        )
        assert resp.status_code == 401

    def test_reveal_env_var_custom_session_header_ignores_proxy_authorization(self, tmp_path):
        """A valid dashboard session header should coexist with proxy auth."""
        from hermes_cli.config import save_env_value
        from hermes_cli.web_server import _SESSION_HEADER_NAME, _SESSION_TOKEN

        save_env_value("TEST_REVEAL_PROXY_AUTH", "secret-value")
        resp = self.client.post(
            "/api/env/reveal",
            json={"key": "TEST_REVEAL_PROXY_AUTH"},
            headers={
                _SESSION_HEADER_NAME: _SESSION_TOKEN,
                "Authorization": "Basic dXNlcjpwYXNz",
            },
        )

        assert resp.status_code == 200
        assert resp.json()["value"] == "secret-value"

    def test_reveal_env_var_legacy_authorization_header_still_works(self, tmp_path):
        """Keep old dashboard bundles working while the new header rolls out."""
        from hermes_cli.config import save_env_value
        from hermes_cli.web_server import _SESSION_TOKEN

        save_env_value("TEST_REVEAL_LEGACY_AUTH", "secret-value")
        resp = self.client.post(
            "/api/env/reveal",
            json={"key": "TEST_REVEAL_LEGACY_AUTH"},
            headers={"Authorization": f"Bearer {_SESSION_TOKEN}"},
        )

        assert resp.status_code == 200

    def test_get_messaging_platforms(self):
        resp = self.client.get("/api/messaging/platforms")

        assert resp.status_code == 200
        platforms = resp.json()["platforms"]
        telegram = next(platform for platform in platforms if platform["id"] == "telegram")
        assert telegram["name"] == "Telegram"
        assert telegram["enabled"] is False
        assert any(field["key"] == "TELEGRAM_BOT_TOKEN" and field["required"] for field in telegram["env_vars"])

    def test_messaging_catalog_covers_gateway_platforms(self):
        """Catalog is derived from the Platform enum, so every built-in shows up."""
        from gateway.config import Platform

        resp = self.client.get("/api/messaging/platforms")
        platforms = {entry["id"] for entry in resp.json()["platforms"]}

        for member in Platform.__members__.values():
            if member.value == "local":
                continue
            assert member.value in platforms, f"Missing gateway platform {member.value} from /api/messaging/platforms"

    def test_messaging_catalog_includes_plugin_platforms(self, monkeypatch):
        """Plugin-registered adapters appear in the catalog without per-platform code."""
        from gateway.platform_registry import PlatformEntry, platform_registry

        entry = PlatformEntry(
            name="ircfake",
            label="IRC (test)",
            adapter_factory=lambda cfg: None,
            check_fn=lambda: True,
            required_env=["IRC_SERVER"],
            install_hint="Connect to IRC.",
            source="plugin",
        )
        platform_registry.register(entry)
        try:
            resp = self.client.get("/api/messaging/platforms")
            ids = {row["id"]: row for row in resp.json()["platforms"]}
            assert "ircfake" in ids
            assert ids["ircfake"]["name"] == "IRC (test)"
            assert any(field["key"] == "IRC_SERVER" and field["required"] for field in ids["ircfake"]["env_vars"])
        finally:
            platform_registry.unregister("ircfake")

    def test_update_messaging_platform_saves_env_and_enablement(self):
        from hermes_cli.config import load_config, load_env

        resp = self.client.put(
            "/api/messaging/platforms/telegram",
            json={
                "enabled": False,
                "env": {"TELEGRAM_BOT_TOKEN": "1234567890abcdef"},
            },
        )

        assert resp.status_code == 200
        assert load_env()["TELEGRAM_BOT_TOKEN"] == "1234567890abcdef"
        assert load_config()["platforms"]["telegram"]["enabled"] is False

        status = self.client.get("/api/messaging/platforms").json()["platforms"]
        telegram = next(platform for platform in status if platform["id"] == "telegram")
        assert telegram["enabled"] is False

    def test_messaging_platform_test_reports_missing_required_setup(self):
        resp = self.client.put("/api/messaging/platforms/discord", json={"enabled": True})
        assert resp.status_code == 200

        resp = self.client.post("/api/messaging/platforms/discord/test")

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert data["state"] == "not_configured"
        assert "DISCORD_BOT_TOKEN" in data["message"]

    def test_telegram_onboarding_start_strips_poll_token(self, monkeypatch):
        import hermes_cli.web_server as ws

        with ws._telegram_onboarding_lock:
            ws._telegram_onboarding_pairings.clear()

        calls = []

        def fake_request(method, path, *, body=None, bearer_token=None):
            calls.append((method, path, body, bearer_token))
            return {
                "pairing_id": "pair123",
                "poll_token": "poll-secret",
                "suggested_username": "hermes_pair123_bot",
                "deep_link": "https://t.me/newbot/HermesSetupBot/hermes_pair123_bot",
                "qr_payload": "https://t.me/newbot/HermesSetupBot/hermes_pair123_bot",
                "expires_at": "2027-05-18T00:00:00.000Z",
            }

        monkeypatch.setattr(ws, "_telegram_onboarding_request_sync", fake_request)

        resp = self.client.post(
            "/api/messaging/telegram/onboarding/start",
            json={"bot_name": "Hosted Hermes"},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["pairing_id"] == "pair123"
        assert "poll_token" not in data
        assert calls == [
            (
                "POST",
                "/v1/telegram/pairings",
                {"bot_name": "Hosted Hermes"},
                None,
            )
        ]

    def test_telegram_onboarding_ready_and_apply_never_returns_bot_token(self, monkeypatch):
        import hermes_cli.web_server as ws
        from hermes_cli.config import load_config, load_env

        with ws._telegram_onboarding_lock:
            ws._telegram_onboarding_pairings.clear()

        def fake_request(method, path, *, body=None, bearer_token=None):
            if method == "POST":
                return {
                    "pairing_id": "pair-ready",
                    "poll_token": "poll-secret",
                    "suggested_username": "hermes_pair_ready_bot",
                    "deep_link": "https://t.me/newbot/HermesSetupBot/hermes_pair_ready_bot",
                    "qr_payload": "https://t.me/newbot/HermesSetupBot/hermes_pair_ready_bot",
                    "expires_at": "2027-05-18T00:00:00.000Z",
                }
            assert method == "GET"
            assert path == "/v1/telegram/pairings/pair-ready"
            assert bearer_token == "poll-secret"
            return {
                "status": "ready",
                "bot_username": "hermes_pair_ready_bot",
                "owner_user_id": 123456789,
                "token": "123456:SECRET",
            }

        monkeypatch.setattr(ws, "_telegram_onboarding_request_sync", fake_request)

        start = self.client.post("/api/messaging/telegram/onboarding/start", json={})
        assert start.status_code == 200

        ready = self.client.get("/api/messaging/telegram/onboarding/pair-ready")
        assert ready.status_code == 200
        ready_data = ready.json()
        assert ready_data["status"] == "ready"
        assert ready_data["owner_user_id"] == "123456789"
        assert "token" not in ready_data

        applied = self.client.post(
            "/api/messaging/telegram/onboarding/pair-ready/apply",
            json={"allowed_user_ids": ["123456789", "123456789"]},
        )
        assert applied.status_code == 200
        applied_data = applied.json()
        assert applied_data == {
            "ok": True,
            "platform": "telegram",
            "bot_username": "hermes_pair_ready_bot",
            "needs_restart": True,
        }
        env = load_env()
        assert env["TELEGRAM_BOT_TOKEN"] == "123456:SECRET"
        assert env["TELEGRAM_ALLOWED_USERS"] == "123456789"
        assert load_config()["platforms"]["telegram"]["enabled"] is True

    def test_telegram_onboarding_apply_requires_ready_pairing(self, monkeypatch):
        import hermes_cli.web_server as ws

        with ws._telegram_onboarding_lock:
            ws._telegram_onboarding_pairings.clear()

        def fake_request(method, path, *, body=None, bearer_token=None):
            return {
                "pairing_id": "pair-waiting",
                "poll_token": "poll-secret",
                "suggested_username": "hermes_pair_waiting_bot",
                "deep_link": "https://t.me/newbot/HermesSetupBot/hermes_pair_waiting_bot",
                "qr_payload": "https://t.me/newbot/HermesSetupBot/hermes_pair_waiting_bot",
                "expires_at": "2027-05-18T00:00:00.000Z",
            }

        monkeypatch.setattr(ws, "_telegram_onboarding_request_sync", fake_request)

        start = self.client.post("/api/messaging/telegram/onboarding/start", json={})
        assert start.status_code == 200

        resp = self.client.post(
            "/api/messaging/telegram/onboarding/pair-waiting/apply",
            json={"allowed_user_ids": ["123456789"]},
        )

        assert resp.status_code == 409
        assert "not ready" in resp.json()["detail"]

    def test_telegram_onboarding_cancel_clears_local_session(self, monkeypatch):
        import hermes_cli.web_server as ws

        with ws._telegram_onboarding_lock:
            ws._telegram_onboarding_pairings.clear()

        def fake_request(method, path, *, body=None, bearer_token=None):
            return {
                "pairing_id": "pair-cancel",
                "poll_token": "poll-secret",
                "suggested_username": "hermes_pair_cancel_bot",
                "deep_link": "https://t.me/newbot/HermesSetupBot/hermes_pair_cancel_bot",
                "qr_payload": "https://t.me/newbot/HermesSetupBot/hermes_pair_cancel_bot",
                "expires_at": "2027-05-18T00:00:00.000Z",
            }

        monkeypatch.setattr(ws, "_telegram_onboarding_request_sync", fake_request)

        start = self.client.post("/api/messaging/telegram/onboarding/start", json={})
        assert start.status_code == 200

        cancel = self.client.delete("/api/messaging/telegram/onboarding/pair-cancel")
        assert cancel.status_code == 200

        status = self.client.get("/api/messaging/telegram/onboarding/pair-cancel")
        assert status.status_code == 404

    def test_session_token_endpoint_removed(self):
        """GET /api/auth/session-token should no longer exist (token injected via HTML)."""
        resp = self.client.get("/api/auth/session-token")
        # The endpoint is gone — the catch-all SPA route serves index.html
        # or the middleware returns 401 for unauthenticated /api/ paths.
        assert resp.status_code in {200, 404}
        # Either way, it must NOT return the token as JSON
        try:
            data = resp.json()
            assert "token" not in data
        except Exception:
            pass  # Not JSON — that's fine (SPA HTML)

    def test_unauthenticated_api_blocked(self):
        """API requests without the session token should be rejected."""
        from starlette.testclient import TestClient
        from hermes_cli.web_server import app
        # Create a client WITHOUT the dashboard session header
        unauth_client = TestClient(app)
        resp = unauth_client.get("/api/env")
        assert resp.status_code == 401
        resp = unauth_client.get("/api/config")
        assert resp.status_code == 401
        # Public endpoints should still work
        resp = unauth_client.get("/api/status")
        assert resp.status_code == 200
        resp = unauth_client.get("/api/dashboard/plugins")
        assert resp.status_code == 200
        resp = unauth_client.get("/api/dashboard/plugins/rescan")
        assert resp.status_code == 401
        resp = self.client.get("/api/dashboard/plugins/rescan")
        assert resp.status_code == 200

    def test_path_traversal_blocked(self):
        """Verify URL-encoded path traversal is blocked."""
        # %2e%2e = ..
        resp = self.client.get("/%2e%2e/%2e%2e/etc/passwd")
        # Should return 200 with index.html (SPA fallback), not the actual file
        assert resp.status_code in {200, 404}
        if resp.status_code == 200:
            # Should be the SPA fallback, not the system file
            assert "root:" not in resp.text

    def test_path_traversal_dotdot_blocked(self):
        """Direct .. path traversal via encoded sequences."""
        resp = self.client.get("/%2e%2e/hermes_cli/web_server.py")
        assert resp.status_code in {200, 404}
        if resp.status_code == 200:
            assert "FastAPI" not in resp.text  # Should not serve the actual source

    def test_set_model_main_nous_applies_gateway_defaults(self, monkeypatch):
        """Switching the main provider to Nous calls apply_nous_managed_defaults
        (mirroring the CLI's post-model-selection Tool Gateway routing) and
        surfaces the routed tools in the response."""
        import hermes_cli.nous_subscription as ns

        called = {}

        def fake_apply(config, *, enabled_toolsets=None, force_fresh=False):
            called["enabled"] = set(enabled_toolsets or ())
            called["force_fresh"] = force_fresh
            # Simulate routing the unconfigured web tool through the gateway.
            web = config.setdefault("web", {})
            web["backend"] = "firecrawl"
            return {"web"}

        monkeypatch.setattr(ns, "apply_nous_managed_defaults", fake_apply)

        resp = self.client.post(
            "/api/model/set",
            json={"scope": "main", "provider": "nous", "model": "hermes-4"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["provider"] == "nous"
        assert data["gateway_tools"] == ["web"]
        assert called["force_fresh"] is True

    def test_set_model_main_non_nous_skips_gateway_defaults(self, monkeypatch):
        """Non-Nous providers must NOT trigger Tool Gateway auto-routing."""
        import hermes_cli.nous_subscription as ns

        def boom(*args, **kwargs):  # pragma: no cover - must not be called
            raise AssertionError("apply_nous_managed_defaults called for non-nous provider")

        monkeypatch.setattr(ns, "apply_nous_managed_defaults", boom)

        resp = self.client.post(
            "/api/model/set",
            json={"scope": "main", "provider": "openrouter", "model": "anthropic/claude-opus-4.8"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data.get("gateway_tools", []) == []

    def test_apply_main_model_assignment_base_url_and_context_reconcile(self):
        """The shared main-slot assignment helper must persist base_url only for
        custom providers, clear stale base_url for hosted ones, and always drop
        a hardcoded context_length override. Both POST /api/model/set and
        profile-model writes route through this, so the contract is pinned here."""
        from hermes_cli.web_server import _apply_main_model_assignment

        # Custom + base_url → persisted; stale context_length dropped.
        out = _apply_main_model_assignment(
            {"context_length": 8192}, "custom", "llama-3.1-8b", "http://127.0.0.1:8000/v1"
        )
        assert out["provider"] == "custom"
        assert out["default"] == "llama-3.1-8b"
        assert out["base_url"] == "http://127.0.0.1:8000/v1"
        assert "context_length" not in out

        # Hosted provider → stale base_url cleared (no base_url supplied).
        out = _apply_main_model_assignment(
            {"base_url": "http://127.0.0.1:8000/v1"}, "openrouter", "anthropic/claude-opus-4.8"
        )
        assert out["provider"] == "openrouter"
        assert out["base_url"] == ""

        # Custom WITHOUT a base_url → don't invent one, clear any stale value.
        out = _apply_main_model_assignment(
            {"base_url": "http://stale:1/v1"}, "custom", "m"
        )
        assert out["base_url"] == ""

        # Non-dict input is coerced to a fresh dict (never raises).
        out = _apply_main_model_assignment("not-a-dict", "custom", "m", "http://x/v1")
        assert out == {"provider": "custom", "default": "m", "base_url": "http://x/v1"}

    def test_parse_model_ids_handles_openai_and_bare_shapes(self):
        """Model discovery must tolerate the common /v1/models shapes and
        never raise (so a slightly non-standard local endpoint still works)."""
        from hermes_cli.web_server import _parse_model_ids

        class FakeResp:
            def __init__(self, payload, ok=True):
                self._payload = payload
                self.is_success = ok

            def json(self):
                if isinstance(self._payload, Exception):
                    raise self._payload
                return self._payload

        # OpenAI / vLLM / llama.cpp shape.
        assert _parse_model_ids(
            FakeResp({"data": [{"id": "llama-3.1-8b"}, {"id": "qwen2.5-7b"}]})
        ) == ["llama-3.1-8b", "qwen2.5-7b"]
        # Bare list of ids.
        assert _parse_model_ids(FakeResp({"data": ["m1", "m2"]})) == ["m1", "m2"]
        # Top-level list.
        assert _parse_model_ids(FakeResp([{"id": "x"}])) == ["x"]
        # Non-success / malformed / exception → [] (never raises).
        assert _parse_model_ids(FakeResp({"data": []}, ok=False)) == []
        assert _parse_model_ids(FakeResp({"nope": 1})) == []
        assert _parse_model_ids(FakeResp(ValueError("bad json"))) == []

    def test_set_model_main_custom_persists_base_url(self):
        """Custom/local providers must persist model.base_url so the runtime
        resolver (which ignores OPENAI_BASE_URL) can route to a self-hosted
        endpoint without an API key. Regression for the desktop onboarding bug
        where 'Local / custom endpoint' could never be configured."""
        from hermes_cli.config import load_config

        resp = self.client.post(
            "/api/model/set",
            json={
                "scope": "main",
                "provider": "custom",
                "model": "llama-3.1-8b",
                "base_url": "http://127.0.0.1:8000/v1",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["provider"] == "custom"
        assert data["base_url"] == "http://127.0.0.1:8000/v1"

        model_cfg = load_config().get("model")
        assert isinstance(model_cfg, dict)
        assert model_cfg["provider"] == "custom"
        assert model_cfg["default"] == "llama-3.1-8b"
        assert model_cfg["base_url"] == "http://127.0.0.1:8000/v1"

    def test_set_model_main_non_custom_clears_stale_base_url(self):
        """Switching to a hosted provider must clear a stale base_url so the
        resolver picks that provider's own default endpoint."""
        from hermes_cli.config import load_config, save_config

        cfg = load_config()
        cfg["model"] = {
            "provider": "custom",
            "default": "llama-3.1-8b",
            "base_url": "http://127.0.0.1:8000/v1",
        }
        save_config(cfg)

        resp = self.client.post(
            "/api/model/set",
            json={"scope": "main", "provider": "openrouter", "model": "anthropic/claude-opus-4.8"},
        )
        assert resp.status_code == 200
        assert resp.json()["base_url"] == ""

    def test_set_model_main_reports_stale_auxiliary_pins(self):
        """Switching the main provider must report auxiliary slots still pinned
        to a *different* provider so the UI can warn the user their helper tasks
        aren't following the switch (the silent credit-burn path)."""
        from hermes_cli.config import load_config, save_config

        cfg = load_config()
        cfg["model"] = {"provider": "nous", "default": "hermes-4"}
        cfg["auxiliary"] = {
            # Pinned to nous — same as the OLD main, becomes stale after switch.
            "compression": {"provider": "nous", "model": "anthropic/claude-sonnet-4.6"},
            # Auto — follows main, never stale.
            "vision": {"provider": "auto", "model": ""},
            # Pinned to a third provider — also stale vs the new main.
            "curator": {"provider": "deepseek", "model": "deepseek-chat"},
        }
        save_config(cfg)

        resp = self.client.post(
            "/api/model/set",
            json={"scope": "main", "provider": "openrouter", "model": "anthropic/claude-opus-4.8"},
        )
        assert resp.status_code == 200
        stale = resp.json()["stale_aux"]
        stale_tasks = {entry["task"] for entry in stale}
        assert stale_tasks == {"compression", "curator"}
        # auto slot must never appear.
        assert "vision" not in stale_tasks
        # Provider/model echoed back for the UI label.
        comp = next(e for e in stale if e["task"] == "compression")
        assert comp["provider"] == "nous"
        assert comp["model"] == "anthropic/claude-sonnet-4.6"

    def test_set_model_main_no_stale_when_aux_matches_new_provider(self):
        """Aux slots pinned to the SAME provider as the new main are not stale."""
        from hermes_cli.config import load_config, save_config

        cfg = load_config()
        cfg["model"] = {"provider": "nous", "default": "hermes-4"}
        cfg["auxiliary"] = {
            "compression": {"provider": "openrouter", "model": "google/gemini-2.5-flash"},
            "vision": {"provider": "auto", "model": ""},
        }
        save_config(cfg)

        resp = self.client.post(
            "/api/model/set",
            json={"scope": "main", "provider": "openrouter", "model": "anthropic/claude-opus-4.8"},
        )
        assert resp.status_code == 200
        assert resp.json()["stale_aux"] == []

        model_cfg = load_config().get("model")
        assert model_cfg["provider"] == "openrouter"
        assert model_cfg.get("base_url", "") == ""

    def test_set_model_main_gateway_failure_does_not_block_save(self, monkeypatch):
        """A Portal/gateway hiccup must never prevent saving the model."""
        import hermes_cli.nous_subscription as ns

        def boom(*args, **kwargs):
            raise RuntimeError("portal unreachable")

        monkeypatch.setattr(ns, "apply_nous_managed_defaults", boom)

        resp = self.client.post(
            "/api/model/set",
            json={"scope": "main", "provider": "nous", "model": "hermes-4"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data.get("gateway_tools", []) == []

    def test_recommended_default_nous_honors_free_tier(self, monkeypatch):
        """For a free-tier Nous user, the recommended default must be a free
        model (mirroring `hermes model`), not the first curated paid entry."""
        import hermes_cli.models as models_mod

        monkeypatch.setattr(models_mod, "get_curated_nous_model_ids", lambda: ["paid/expensive", "free/cheap"])
        monkeypatch.setattr(
            models_mod, "get_pricing_for_provider",
            lambda provider: {"paid/expensive": {"input": "1"}, "free/cheap": {"input": "0"}},
        )
        monkeypatch.setattr(models_mod, "check_nous_free_tier", lambda *, force_fresh=False: True)
        monkeypatch.setattr(
            models_mod, "union_with_portal_free_recommendations",
            lambda ids, pricing, url: (ids, pricing),
        )
        # Free partition keeps only the free model selectable.
        monkeypatch.setattr(
            models_mod, "partition_nous_models_by_tier",
            lambda ids, pricing, free_tier: (["free/cheap"], ["paid/expensive"]),
        )

        resp = self.client.get("/api/model/recommended-default?provider=nous")
        assert resp.status_code == 200
        data = resp.json()
        assert data["provider"] == "nous"
        assert data["model"] == "free/cheap"
        assert data["free_tier"] is True

    def test_recommended_default_nous_paid_uses_curated_default(self, monkeypatch):
        """A paid Nous user gets the first curated/paid-augmented model."""
        import hermes_cli.models as models_mod

        monkeypatch.setattr(models_mod, "get_curated_nous_model_ids", lambda: ["top/model", "other/model"])
        monkeypatch.setattr(models_mod, "get_pricing_for_provider", lambda provider: {})
        monkeypatch.setattr(models_mod, "check_nous_free_tier", lambda *, force_fresh=False: False)
        monkeypatch.setattr(
            models_mod, "union_with_portal_paid_recommendations",
            lambda ids, pricing, url: (ids, pricing),
        )

        resp = self.client.get("/api/model/recommended-default?provider=nous")
        assert resp.status_code == 200
        data = resp.json()
        assert data["provider"] == "nous"
        assert data["model"] == "top/model"
        assert data["free_tier"] is False

    def test_recommended_default_handles_failure_gracefully(self, monkeypatch):
        """Endpoint never 500s — returns empty model on internal error."""
        import hermes_cli.models as models_mod

        def boom():
            raise RuntimeError("portal down")

        monkeypatch.setattr(models_mod, "get_curated_nous_model_ids", boom)

        resp = self.client.get("/api/model/recommended-default?provider=nous")
        assert resp.status_code == 200
        data = resp.json()
        assert data["model"] == ""
        assert data["free_tier"] is None


# ---------------------------------------------------------------------------
# _build_schema_from_config tests
# ---------------------------------------------------------------------------


class TestBuildSchemaFromConfig:
    def test_produces_expected_field_count(self):
        from hermes_cli.web_server import CONFIG_SCHEMA
        # DEFAULT_CONFIG has ~150+ leaf fields
        assert len(CONFIG_SCHEMA) > 100

    def test_schema_entries_have_required_fields(self):
        from hermes_cli.web_server import CONFIG_SCHEMA
        for key, entry in list(CONFIG_SCHEMA.items())[:10]:
            assert "type" in entry, f"Missing type for {key}"
            assert "category" in entry, f"Missing category for {key}"

    def test_overrides_applied(self):
        from hermes_cli.web_server import CONFIG_SCHEMA
        # terminal.backend should be a select with options
        if "terminal.backend" in CONFIG_SCHEMA:
            entry = CONFIG_SCHEMA["terminal.backend"]
            assert entry["type"] == "select"
            assert "options" in entry
            assert "local" in entry["options"]

    def test_empty_prefix_produces_correct_keys(self):
        from hermes_cli.web_server import _build_schema_from_config
        test_config = {"model": "test", "nested": {"key": "val"}}
        schema = _build_schema_from_config(test_config)
        assert "model" in schema
        assert "nested.key" in schema

    def test_top_level_scalars_get_general_category(self):
        """Top-level scalar fields should be in 'general' category."""
        from hermes_cli.web_server import CONFIG_SCHEMA
        assert CONFIG_SCHEMA["model"]["category"] == "general"

    def test_nested_keys_get_parent_category(self):
        """Nested fields should use the top-level parent as their category."""
        from hermes_cli.web_server import CONFIG_SCHEMA
        if "agent.max_turns" in CONFIG_SCHEMA:
            assert CONFIG_SCHEMA["agent.max_turns"]["category"] == "agent"

    def test_category_merge_applied(self):
        """Small categories should be merged into larger ones."""
        from hermes_cli.web_server import CONFIG_SCHEMA
        categories = {e["category"] for e in CONFIG_SCHEMA.values()}
        # These should be merged away
        assert "privacy" not in categories  # merged into security
        assert "context" not in categories  # merged into agent

    def test_no_single_field_categories(self):
        """After merging, no category should have just 1 field."""
        from hermes_cli.web_server import CONFIG_SCHEMA
        from collections import Counter
        cats = Counter(e["category"] for e in CONFIG_SCHEMA.values())
        for cat, count in cats.items():
            assert count >= 2, f"Category '{cat}' has only {count} field(s) — should be merged"


# ---------------------------------------------------------------------------
# Config round-trip tests
# ---------------------------------------------------------------------------


class TestConfigRoundTrip:
    """Verify config survives GET → edit → PUT without data loss."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        try:
            from starlette.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi/starlette not installed")
        from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN
        self.client = TestClient(app)
        self.client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN

    def test_get_config_no_internal_keys(self):
        """GET /api/config should not expose _config_version or _model_meta."""
        config = self.client.get("/api/config").json()
        internal = [k for k in config if k.startswith("_")]
        assert not internal, f"Internal keys leaked to frontend: {internal}"

    def test_get_config_model_is_string(self):
        """GET /api/config should normalize model dict to a string."""
        config = self.client.get("/api/config").json()
        assert isinstance(config.get("model"), str), \
            f"model should be string, got {type(config.get('model'))}"

    def test_round_trip_preserves_model_subkeys(self):
        """Save and reload should not lose model.provider, model.base_url, etc."""
        from hermes_cli.config import load_config, save_config

        # Set up a config with model as a dict (the common user config form)
        save_config({
            "model": {
                "default": "anthropic/claude-sonnet-4",
                "provider": "openrouter",
                "base_url": "https://openrouter.ai/api/v1",
                "api_mode": "openai",
            }
        })

        before = load_config()
        assert isinstance(before.get("model"), dict)
        original_keys = set(before["model"].keys())

        # GET → PUT unchanged
        web_config = self.client.get("/api/config").json()
        assert isinstance(web_config.get("model"), str), "GET should normalize model to string"

        self.client.put("/api/config", json={"config": web_config})

        after = load_config()
        assert isinstance(after.get("model"), dict), "model should still be a dict after save"
        assert set(after["model"].keys()) >= original_keys, \
            f"Lost model subkeys: {original_keys - set(after['model'].keys())}"

    def test_edit_model_name_preserved(self):
        """Changing the model string should update model.default on disk."""
        from hermes_cli.config import load_config

        web_config = self.client.get("/api/config").json()
        original_model = web_config["model"]

        # Change model
        web_config["model"] = "test/editing-model"
        self.client.put("/api/config", json={"config": web_config})

        after = load_config()
        if isinstance(after.get("model"), dict):
            assert after["model"]["default"] == "test/editing-model"
        else:
            assert after["model"] == "test/editing-model"

        # Restore
        web_config["model"] = original_model
        self.client.put("/api/config", json={"config": web_config})

    def test_edit_nested_value(self):
        """Editing a nested config value should persist correctly."""
        from hermes_cli.config import load_config

        web_config = self.client.get("/api/config").json()
        original_turns = web_config.get("agent", {}).get("max_turns")

        # Change max_turns
        if "agent" not in web_config:
            web_config["agent"] = {}
        web_config["agent"]["max_turns"] = 42

        self.client.put("/api/config", json={"config": web_config})

        after = load_config()
        assert after.get("agent", {}).get("max_turns") == 42

        # Restore
        web_config["agent"]["max_turns"] = original_turns
        self.client.put("/api/config", json={"config": web_config})

    def test_schema_types_match_config_values(self):
        """Every schema field should have a matching-type value in the config."""
        config = self.client.get("/api/config").json()
        schema_resp = self.client.get("/api/config/schema").json()
        schema = schema_resp["fields"]

        def get_nested(obj, path):
            parts = path.split(".")
            cur = obj
            for p in parts:
                if cur is None or not isinstance(cur, dict):
                    return None
                cur = cur.get(p)
            return cur

        mismatches = []
        for key, entry in schema.items():
            val = get_nested(config, key)
            if val is None:
                continue  # not set in user config — fine
            expected = entry["type"]
            if expected in {"string", "select"} and not isinstance(val, str):
                mismatches.append(f"{key}: expected str, got {type(val).__name__}")
            elif expected == "number" and not isinstance(val, (int, float)):
                mismatches.append(f"{key}: expected number, got {type(val).__name__}")
            elif expected == "boolean" and not isinstance(val, bool):
                mismatches.append(f"{key}: expected bool, got {type(val).__name__}")
            elif expected == "list" and not isinstance(val, list):
                mismatches.append(f"{key}: expected list, got {type(val).__name__}")
        assert not mismatches, f"Type mismatches:\n" + "\n".join(mismatches)


# ---------------------------------------------------------------------------
# New feature endpoint tests
# ---------------------------------------------------------------------------


class TestNewEndpoints:
    """Tests for session detail, logs, cron, skills, tools, raw config, analytics."""

    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch, _isolate_hermes_home):
        try:
            from starlette.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi/starlette not installed")

        import hermes_state
        from hermes_constants import get_hermes_home
        from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

        monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", get_hermes_home() / "state.db")

        self.client = TestClient(app)
        self.client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN

    def test_get_logs_default(self):
        resp = self.client.get("/api/logs")
        assert resp.status_code == 200
        data = resp.json()
        assert "file" in data
        assert "lines" in data
        assert isinstance(data["lines"], list)

    def test_get_logs_invalid_file(self):
        resp = self.client.get("/api/logs?file=nonexistent")
        assert resp.status_code == 400

    def test_cron_list(self):
        resp = self.client.get("/api/cron/jobs")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_cron_job_not_found(self):
        resp = self.client.get("/api/cron/jobs/nonexistent-id")
        assert resp.status_code == 404

    def test_sessions_can_exclude_cron_sources(self):
        from hermes_state import SessionDB

        db = SessionDB()
        try:
            db.create_session("cui-visible-session", "cli")
            db.append_message("cui-visible-session", "user", "visible")
            db.create_session("cron-hidden-session", "cron")
            db.append_message("cron-hidden-session", "user", "hidden")
        finally:
            db.close()

        resp = self.client.get("/api/sessions?limit=10&offset=0&exclude_sources=cron")

        assert resp.status_code == 200
        data = resp.json()
        ids = [session["id"] for session in data["sessions"]]
        assert "cui-visible-session" in ids
        assert "cron-hidden-session" not in ids
        assert data["total"] == 1

    # --- Profiles ---

    def test_profiles_list_includes_default(self):
        from hermes_constants import get_hermes_home
        get_hermes_home().mkdir(parents=True, exist_ok=True)

        resp = self.client.get("/api/profiles")
        assert resp.status_code == 200
        names = [p["name"] for p in resp.json()["profiles"]]
        assert "default" in names

    def test_profiles_list_falls_back_when_profile_listing_fails(self, monkeypatch):
        from hermes_constants import get_hermes_home
        import hermes_cli.profiles as profiles_mod

        hermes_home = get_hermes_home()
        hermes_home.mkdir(parents=True, exist_ok=True)
        (hermes_home / "config.yaml").write_text(
            "model:\n  provider: openrouter\n  name: anthropic/claude-sonnet-4.6\n",
            encoding="utf-8",
        )
        named = hermes_home / "profiles" / "multi-agent"
        named.mkdir(parents=True)
        (named / ".env").write_text("EXAMPLE=1\n", encoding="utf-8")
        (named / "skills" / "demo").mkdir(parents=True)
        (named / "skills" / "demo" / "SKILL.md").write_text("---\nname: demo\n---\n", encoding="utf-8")

        monkeypatch.setattr(
            profiles_mod,
            "list_profiles",
            lambda: (_ for _ in ()).throw(RuntimeError("boom")),
        )

        resp = self.client.get("/api/profiles")

        assert resp.status_code == 200
        profiles = {p["name"]: p for p in resp.json()["profiles"]}
        assert profiles["default"]["is_default"] is True
        assert profiles["default"]["provider"] == "openrouter"
        assert profiles["multi-agent"]["has_env"] is True
        assert profiles["multi-agent"]["skill_count"] == 1

    def test_profiles_create_rename_delete_round_trip(self, monkeypatch):
        # Stub gateway service teardown so the test doesn't shell out to
        # launchctl/systemctl on the host.
        import hermes_cli.profiles as profiles_mod
        monkeypatch.setattr(profiles_mod, "_cleanup_gateway_service", lambda *a, **kw: None)

        created = self.client.post("/api/profiles", json={"name": "test-prof"})
        assert created.status_code == 200

        renamed = self.client.patch(
            "/api/profiles/test-prof",
            json={"new_name": "test-prof-2"},
        )
        assert renamed.status_code == 200

        names = [p["name"] for p in self.client.get("/api/profiles").json()["profiles"]]
        assert "test-prof" not in names
        assert "test-prof-2" in names

        deleted = self.client.delete("/api/profiles/test-prof-2")
        assert deleted.status_code == 200
        names = [p["name"] for p in self.client.get("/api/profiles").json()["profiles"]]
        assert "test-prof-2" not in names

    def test_profile_setup_command_uses_named_profile_wrapper(self):
        from hermes_constants import get_hermes_home

        (get_hermes_home() / "profiles" / "coder").mkdir(parents=True)

        resp = self.client.get("/api/profiles/coder/setup-command")

        assert resp.status_code == 200
        assert resp.json()["command"] == "coder setup"

    def test_profile_setup_command_uses_hermes_for_default_profile(self):
        from hermes_constants import get_hermes_home

        get_hermes_home().mkdir(parents=True, exist_ok=True)

        resp = self.client.get("/api/profiles/default/setup-command")

        assert resp.status_code == 200
        assert resp.json()["command"] == "hermes setup"

    def test_profiles_create_creates_wrapper_alias_when_safe(self, monkeypatch, tmp_path):
        import hermes_cli.profiles as profiles_mod

        wrapper_dir = tmp_path / "bin"
        wrapper_dir.mkdir()
        monkeypatch.setattr(profiles_mod, "_get_wrapper_dir", lambda: wrapper_dir)

        resp = self.client.post(
            "/api/profiles",
            json={"name": "writer", "clone_from_default": False},
        )

        assert resp.status_code == 200
        wrapper_path = wrapper_dir / "writer"
        assert wrapper_path.exists()
        assert wrapper_path.read_text() == '#!/bin/sh\nexec hermes -p writer "$@"\n'

    def test_profiles_create_with_clone_from_default_copies_default_skills(self, monkeypatch):
        from hermes_constants import get_hermes_home
        import hermes_cli.profiles as profiles_mod

        monkeypatch.setattr(profiles_mod, "create_wrapper_script", lambda name: None)
        default_skill = get_hermes_home() / "skills" / "custom" / "new-skill"
        default_skill.mkdir(parents=True)
        (default_skill / "SKILL.md").write_text("---\nname: new-skill\n---\n", encoding="utf-8")

        resp = self.client.post(
            "/api/profiles",
            json={"name": "cloned", "clone_from_default": True},
        )

        assert resp.status_code == 200
        cloned_skill = get_hermes_home() / "profiles" / "cloned" / "skills" / "custom" / "new-skill" / "SKILL.md"
        assert cloned_skill.exists()
        profiles = {p["name"]: p for p in self.client.get("/api/profiles").json()["profiles"]}
        assert profiles["cloned"]["skill_count"] == 1

    def test_profiles_create_with_clone_from_duplicates_source(self, monkeypatch):
        from hermes_constants import get_hermes_home
        import hermes_cli.profiles as profiles_mod

        monkeypatch.setattr(profiles_mod, "create_wrapper_script", lambda name: None)

        # Create a source profile and give it a distinctive skill.
        assert self.client.post("/api/profiles", json={"name": "source-prof"}).status_code == 200
        source_skill = get_hermes_home() / "profiles" / "source-prof" / "skills" / "custom" / "src-skill"
        source_skill.mkdir(parents=True)
        (source_skill / "SKILL.md").write_text("---\nname: src-skill\n---\n", encoding="utf-8")

        # Duplicate it via an explicit clone_from source (not "default").
        resp = self.client.post(
            "/api/profiles",
            json={"name": "source-prof-copy", "clone_from": "source-prof"},
        )

        assert resp.status_code == 200
        cloned_skill = (
            get_hermes_home() / "profiles" / "source-prof-copy" / "skills" / "custom" / "src-skill" / "SKILL.md"
        )
        assert cloned_skill.exists()

    def test_profiles_create_without_clone_seeds_bundled_skills(self, monkeypatch):
        from hermes_constants import get_hermes_home
        import hermes_cli.profiles as profiles_mod

        monkeypatch.setattr(profiles_mod, "create_wrapper_script", lambda name: None)

        def fake_seed(profile_dir, quiet=False):
            skill_dir = profile_dir / "skills" / "software-development" / "plan"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("---\nname: plan\n---\n", encoding="utf-8")
            return {"copied": ["plan"]}

        monkeypatch.setattr(profiles_mod, "seed_profile_skills", fake_seed)

        resp = self.client.post(
            "/api/profiles",
            json={"name": "fresh", "clone_from_default": False},
        )

        assert resp.status_code == 200
        seeded_skill = get_hermes_home() / "profiles" / "fresh" / "skills" / "software-development" / "plan" / "SKILL.md"
        assert seeded_skill.exists()
        profiles = {p["name"]: p for p in self.client.get("/api/profiles").json()["profiles"]}
        assert profiles["fresh"]["skill_count"] == 1

    def test_profile_open_terminal_uses_macos_terminal(self, monkeypatch):
        from hermes_constants import get_hermes_home
        import hermes_cli.web_server as web_server

        (get_hermes_home() / "profiles" / "coder").mkdir(parents=True)
        calls = []
        monkeypatch.setattr(web_server.sys, "platform", "darwin")
        monkeypatch.setattr(web_server.subprocess, "Popen", lambda args, **kwargs: calls.append(args))

        resp = self.client.post("/api/profiles/coder/open-terminal")

        assert resp.status_code == 200
        assert calls
        assert calls[0][0] == "osascript"
        assert "coder setup" in " ".join(calls[0])

    def test_profile_open_terminal_uses_windows_cmd(self, monkeypatch):
        from hermes_constants import get_hermes_home
        import hermes_cli.web_server as web_server

        (get_hermes_home() / "profiles" / "coder").mkdir(parents=True)
        calls = []
        monkeypatch.setattr(web_server.sys, "platform", "win32")
        monkeypatch.setattr(web_server.subprocess, "Popen", lambda args, **kwargs: calls.append(args))

        resp = self.client.post("/api/profiles/coder/open-terminal")

        assert resp.status_code == 200
        assert calls
        assert calls[0][:4] == ["cmd.exe", "/c", "start", ""]
        assert calls[0][-1] == "coder setup"

    def test_profiles_create_rejects_invalid_name(self):
        resp = self.client.post("/api/profiles", json={"name": "Has Spaces"})
        assert resp.status_code == 400

    def test_profiles_delete_default_forbidden(self):
        resp = self.client.delete("/api/profiles/default")
        assert resp.status_code == 400

    def test_profiles_delete_not_found(self):
        resp = self.client.delete("/api/profiles/does-not-exist")
        assert resp.status_code == 404

    def test_profile_soul_round_trip(self, monkeypatch):
        import hermes_cli.profiles as profiles_mod
        monkeypatch.setattr(profiles_mod, "_cleanup_gateway_service", lambda *a, **kw: None)

        self.client.post("/api/profiles", json={"name": "soul-prof"})
        get1 = self.client.get("/api/profiles/soul-prof/soul")
        assert get1.status_code == 200
        assert get1.json()["exists"] is True

        put = self.client.put(
            "/api/profiles/soul-prof/soul",
            json={"content": "# Edited soul"},
        )
        assert put.status_code == 200

        got = self.client.get("/api/profiles/soul-prof/soul").json()
        assert got["content"] == "# Edited soul"

        self.client.delete("/api/profiles/soul-prof")

    def test_profile_soul_unknown_profile_404(self):
        resp = self.client.get("/api/profiles/nonexistent/soul")
        assert resp.status_code == 404

    # --- New profiles endpoints: active / description / model / describe-auto ---

    def test_profiles_active_defaults(self):
        from hermes_constants import get_hermes_home
        get_hermes_home().mkdir(parents=True, exist_ok=True)

        resp = self.client.get("/api/profiles/active")
        assert resp.status_code == 200
        data = resp.json()
        assert data["active"] == "default"
        assert data["current"] == "default"

    def test_profiles_set_active_round_trip(self, monkeypatch):
        import hermes_cli.profiles as profiles_mod
        monkeypatch.setattr(profiles_mod, "create_wrapper_script", lambda name: None)

        self.client.post("/api/profiles", json={"name": "router"})

        resp = self.client.post("/api/profiles/active", json={"name": "router"})
        assert resp.status_code == 200
        assert resp.json()["active"] == "router"
        assert self.client.get("/api/profiles/active").json()["active"] == "router"

    def test_profiles_set_active_unknown_404(self):
        resp = self.client.post("/api/profiles/active", json={"name": "ghost"})
        assert resp.status_code == 404

    def test_profile_description_round_trip(self, monkeypatch):
        import hermes_cli.profiles as profiles_mod
        monkeypatch.setattr(profiles_mod, "create_wrapper_script", lambda name: None)

        self.client.post("/api/profiles", json={"name": "desc-prof"})

        put = self.client.put(
            "/api/profiles/desc-prof/description",
            json={"description": "Handles code review"},
        )
        assert put.status_code == 200
        body = put.json()
        assert body["description"] == "Handles code review"
        assert body["description_auto"] is False

        profiles = {p["name"]: p for p in self.client.get("/api/profiles").json()["profiles"]}
        assert profiles["desc-prof"]["description"] == "Handles code review"
        assert profiles["desc-prof"]["description_auto"] is False

    def test_profile_description_unknown_404(self):
        resp = self.client.put(
            "/api/profiles/nope/description", json={"description": "x"}
        )
        assert resp.status_code == 404

    def test_profile_model_round_trip(self, monkeypatch):
        from hermes_constants import get_hermes_home
        import hermes_cli.profiles as profiles_mod
        monkeypatch.setattr(profiles_mod, "create_wrapper_script", lambda name: None)

        self.client.post("/api/profiles", json={"name": "model-prof"})

        resp = self.client.put(
            "/api/profiles/model-prof/model",
            json={"provider": "openrouter", "model": "anthropic/claude-sonnet-4.6"},
        )
        assert resp.status_code == 200
        assert resp.json()["provider"] == "openrouter"

        import yaml
        cfg_path = get_hermes_home() / "profiles" / "model-prof" / "config.yaml"
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        assert cfg["model"]["provider"] == "openrouter"
        assert cfg["model"]["default"] == "anthropic/claude-sonnet-4.6"

    def test_profile_model_requires_provider_and_model(self, monkeypatch):
        import hermes_cli.profiles as profiles_mod
        monkeypatch.setattr(profiles_mod, "create_wrapper_script", lambda name: None)

        self.client.post("/api/profiles", json={"name": "model-prof2"})
        resp = self.client.put(
            "/api/profiles/model-prof2/model",
            json={"provider": "", "model": ""},
        )
        assert resp.status_code == 400

    def test_profile_describe_auto_success(self, monkeypatch):
        import hermes_cli.profiles as profiles_mod
        monkeypatch.setattr(profiles_mod, "create_wrapper_script", lambda name: None)

        self.client.post("/api/profiles", json={"name": "auto-prof"})

        from hermes_cli import profile_describer
        monkeypatch.setattr(
            profile_describer,
            "describe_profile",
            lambda name, overwrite=False: profile_describer.DescribeOutcome(
                name, True, "described", description="Generated blurb"
            ),
        )

        resp = self.client.post("/api/profiles/auto-prof/describe-auto", json={})
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["description"] == "Generated blurb"
        assert body["description_auto"] is True

    def test_profile_describe_auto_failure_is_not_auto(self, monkeypatch):
        import hermes_cli.profiles as profiles_mod
        monkeypatch.setattr(profiles_mod, "create_wrapper_script", lambda name: None)

        self.client.post("/api/profiles", json={"name": "auto-fail"})

        from hermes_cli import profile_describer
        monkeypatch.setattr(
            profile_describer,
            "describe_profile",
            lambda name, overwrite=False: profile_describer.DescribeOutcome(
                name, False, "no aux client", description=None
            ),
        )

        resp = self.client.post("/api/profiles/auto-fail/describe-auto", json={})
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert body["description_auto"] is False

    def test_skills_list(self):
        resp = self.client.get("/api/skills")
        assert resp.status_code == 200
        skills = resp.json()
        assert isinstance(skills, list)
        if skills:
            assert "name" in skills[0]
            assert "enabled" in skills[0]

    def test_skills_list_includes_disabled_skills(self, monkeypatch):
        import tools.skills_tool as skills_tool
        import hermes_cli.skills_config as skills_config
        import hermes_cli.web_server as web_server

        def _fake_find_all_skills(*, skip_disabled=False):
            if skip_disabled:
                return [
                    {"name": "active-skill", "description": "active", "category": "demo"},
                    {"name": "disabled-skill", "description": "disabled", "category": "demo"},
                ]
            return [
                {"name": "active-skill", "description": "active", "category": "demo"},
            ]

        monkeypatch.setattr(skills_tool, "_find_all_skills", _fake_find_all_skills)
        monkeypatch.setattr(skills_config, "get_disabled_skills", lambda config: {"disabled-skill"})
        monkeypatch.setattr(web_server, "load_config", lambda: {"skills": {"disabled": ["disabled-skill"]}})

        resp = self.client.get("/api/skills")

        assert resp.status_code == 200
        assert resp.json() == [
            {
                "name": "active-skill",
                "description": "active",
                "category": "demo",
                "enabled": True,
            },
            {
                "name": "disabled-skill",
                "description": "disabled",
                "category": "demo",
                "enabled": False,
            },
        ]

    def test_toolsets_list(self):
        resp = self.client.get("/api/tools/toolsets")
        assert resp.status_code == 200
        toolsets = resp.json()
        assert isinstance(toolsets, list)
        if toolsets:
            assert "name" in toolsets[0]
            assert "label" in toolsets[0]
            assert "enabled" in toolsets[0]

    def test_toolsets_list_matches_cli_enabled_state(self, monkeypatch):
        import hermes_cli.tools_config as tools_config
        import toolsets as toolsets_module
        import hermes_cli.web_server as web_server

        monkeypatch.setattr(
            tools_config,
            "_get_effective_configurable_toolsets",
            lambda: [
                ("web", "🔍 Web Search & Scraping", "web_search, web_extract"),
                ("skills", "📚 Skills", "list, view, manage"),
                ("memory", "💾 Memory", "persistent memory across sessions"),
            ],
        )
        monkeypatch.setattr(
            tools_config,
            "_get_platform_tools",
            lambda config, platform, include_default_mcp_servers=False: {"web", "skills"},
        )
        monkeypatch.setattr(
            tools_config,
            "_toolset_has_keys",
            lambda ts_key, config=None: ts_key != "web",
        )
        monkeypatch.setattr(
            toolsets_module,
            "resolve_toolset",
            lambda name: {
                "web": ["web_search", "web_extract"],
                "skills": ["skills_list", "skill_view"],
                "memory": ["memory_read"],
            }[name],
        )
        monkeypatch.setattr(web_server, "load_config", lambda: {"platform_toolsets": {"cli": ["web", "skills"]}})

        resp = self.client.get("/api/tools/toolsets")

        assert resp.status_code == 200
        assert resp.json() == [
            {
                "name": "web",
                "label": "Web Search & Scraping",
                "description": "web_search, web_extract",
                "enabled": True,
                "available": True,
                "configured": False,
                "tools": ["web_extract", "web_search"],
            },
            {
                "name": "skills",
                "label": "Skills",
                "description": "list, view, manage",
                "enabled": True,
                "available": True,
                "configured": True,
                "tools": ["skill_view", "skills_list"],
            },
            {
                "name": "memory",
                "label": "Memory",
                "description": "persistent memory across sessions",
                "enabled": False,
                "available": False,
                "configured": True,
                "tools": ["memory_read"],
            },
        ]

    def test_toggle_toolset_enable_disable(self):
        """PUT /api/tools/toolsets/{name} round-trips through config and the list view."""
        # Enable a toolset that is off-by-default so the state change is observable.
        resp = self.client.put("/api/tools/toolsets/x_search", json={"enabled": True})
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["name"] == "x_search"
        assert body["enabled"] is True

        listing = {t["name"]: t for t in self.client.get("/api/tools/toolsets").json()}
        assert listing["x_search"]["enabled"] is True

        # Disable it again.
        resp = self.client.put("/api/tools/toolsets/x_search", json={"enabled": False})
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False

        listing = {t["name"]: t for t in self.client.get("/api/tools/toolsets").json()}
        assert listing["x_search"]["enabled"] is False

    def test_toggle_toolset_unknown_returns_400(self):
        resp = self.client.put(
            "/api/tools/toolsets/not_a_real_toolset", json={"enabled": True}
        )
        assert resp.status_code == 400

    def test_get_toolset_config_returns_provider_matrix(self):
        """GET .../config returns provider rows with structured env_vars."""
        resp = self.client.get("/api/tools/toolsets/tts/config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "tts"
        assert data["has_category"] is True
        assert isinstance(data["providers"], list)
        assert data["providers"], "tts always has at least the built-in providers"
        # active_provider is part of the contract so the GUI can highlight the
        # provider actually written to config (else it falls back to the first
        # keyless one). It's either None or the name of one listed provider.
        assert "active_provider" in data
        names = {p["name"] for p in data["providers"]}
        assert data["active_provider"] is None or data["active_provider"] in names
        for prov in data["providers"]:
            assert "name" in prov
            assert "is_active" in prov
            assert "env_vars" in prov
            assert isinstance(prov["env_vars"], list)
            for ev in prov["env_vars"]:
                assert "key" in ev
                assert "is_set" in ev
        # active_provider summarizes the first provider flagged is_active
        # (some catalogs list two rows backed by the same config value, e.g.
        # Firecrawl cloud + self-hosted both map to web.backend=firecrawl).
        active = [p["name"] for p in data["providers"] if p["is_active"]]
        if active:
            assert data["active_provider"] == active[0]
        else:
            assert data["active_provider"] is None

    def test_get_toolset_config_reflects_selected_provider(self):
        """Selecting a provider is reflected in the next /config read.

        Regression: the GUI's provider panel highlighted the first keyless
        provider on relaunch because /config never reported which provider was
        actually active. After selecting one, is_active / active_provider must
        point at it.
        """
        sel = self.client.put(
            "/api/tools/toolsets/web/provider",
            json={"provider": "Firecrawl Self-Hosted"},
        )
        assert sel.status_code == 200

        resp = self.client.get("/api/tools/toolsets/web/config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["active_provider"] == "Firecrawl Self-Hosted"
        active = [p["name"] for p in data["providers"] if p["is_active"]]
        # The first active row is what the GUI highlights; it must be the
        # selected provider.
        assert active, "expected at least one provider flagged active"
        assert active[0] == "Firecrawl Self-Hosted"

    def test_get_toolset_config_no_category_toolset(self):
        """A toolset without a TOOL_CATEGORIES entry returns has_category False."""
        resp = self.client.get("/api/tools/toolsets/todo/config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "todo"
        assert data["has_category"] is False
        assert data["providers"] == []

    def test_get_toolset_config_unknown_returns_400(self):
        resp = self.client.get("/api/tools/toolsets/not_a_real_toolset/config")
        assert resp.status_code == 400

    def test_select_toolset_provider_persists_backend(self):
        """PUT .../provider writes the backend selection to config."""
        resp = self.client.put(
            "/api/tools/toolsets/web/provider",
            json={"provider": "Firecrawl Self-Hosted"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["name"] == "web"
        assert body["provider"] == "Firecrawl Self-Hosted"

        from hermes_cli.config import load_config
        cfg = load_config()
        assert cfg["web"]["backend"] == "firecrawl"

    def test_select_toolset_provider_unknown_provider_returns_400(self):
        resp = self.client.put(
            "/api/tools/toolsets/web/provider",
            json={"provider": "No Such Provider"},
        )
        assert resp.status_code == 400

    def test_select_toolset_provider_unknown_toolset_returns_400(self):
        resp = self.client.put(
            "/api/tools/toolsets/not_a_real_toolset/provider",
            json={"provider": "whatever"},
        )
        assert resp.status_code == 400

    def test_config_raw_get(self):
        resp = self.client.get("/api/config/raw")
        assert resp.status_code == 200
        assert "yaml" in resp.json()

    def test_config_raw_put_valid(self):
        resp = self.client.put(
            "/api/config/raw",
            json={"yaml_text": "model: test\ntoolsets:\n  - all\n"},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_config_raw_put_invalid(self):
        resp = self.client.put(
            "/api/config/raw",
            json={"yaml_text": "- this is a list not a dict"},
        )
        assert resp.status_code == 400

    def test_analytics_usage(self):
        resp = self.client.get("/api/analytics/usage?days=7")
        assert resp.status_code == 200
        data = resp.json()
        assert "daily" in data
        assert "by_model" in data
        assert "totals" in data
        assert "skills" in data
        assert isinstance(data["daily"], list)
        assert "total_sessions" in data["totals"]
        assert "total_api_calls" in data["totals"]
        assert data["skills"] == {
            "summary": {
                "total_skill_loads": 0,
                "total_skill_edits": 0,
                "total_skill_actions": 0,
                "distinct_skills_used": 0,
            },
            "top_skills": [],
        }

    def test_analytics_usage_includes_skill_breakdown(self):
        from hermes_state import SessionDB

        db = SessionDB()
        try:
            db.create_session(
                session_id="skills-analytics-test",
                source="cli",
                model="anthropic/claude-sonnet-4",
            )
            db.update_token_counts(
                "skills-analytics-test",
                input_tokens=120,
                output_tokens=45,
            )
            db.append_message(
                "skills-analytics-test",
                role="assistant",
                content="Loading and updating skills.",
                tool_calls=[
                    {
                        "function": {
                            "name": "skill_view",
                            "arguments": '{"name":"github-pr-workflow"}',
                        }
                    },
                    {
                        "function": {
                            "name": "skill_manage",
                            "arguments": '{"name":"github-code-review"}',
                        }
                    },
                ],
            )
        finally:
            db.close()

        resp = self.client.get("/api/analytics/usage?days=7")
        assert resp.status_code == 200

        data = resp.json()
        assert data["skills"]["summary"] == {
            "total_skill_loads": 1,
            "total_skill_edits": 1,
            "total_skill_actions": 2,
            "distinct_skills_used": 2,
        }
        assert len(data["skills"]["top_skills"]) == 2

        top_skill = data["skills"]["top_skills"][0]
        assert top_skill["skill"] == "github-pr-workflow"
        assert top_skill["view_count"] == 1
        assert top_skill["manage_count"] == 0
        assert top_skill["total_count"] == 1
        assert top_skill["last_used_at"] is not None

    def test_session_token_endpoint_removed(self):
        """GET /api/auth/session-token no longer exists."""
        resp = self.client.get("/api/auth/session-token")
        # Should not return a JSON token object
        assert resp.status_code in {200, 404}
        try:
            data = resp.json()
            assert "token" not in data
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Model context length: normalize/denormalize + /api/model/info
# ---------------------------------------------------------------------------


class TestModelContextLength:
    """Tests for model_context_length in normalize/denormalize and /api/model/info."""

    def test_normalize_extracts_context_length_from_dict(self):
        """normalize should surface context_length from model dict."""
        from hermes_cli.web_server import _normalize_config_for_web

        cfg = {
            "model": {
                "default": "anthropic/claude-opus-4.6",
                "provider": "openrouter",
                "context_length": 200000,
            }
        }
        result = _normalize_config_for_web(cfg)
        assert result["model"] == "anthropic/claude-opus-4.6"
        assert result["model_context_length"] == 200000

    def test_normalize_bare_string_model_yields_zero(self):
        """normalize should set model_context_length=0 for bare string model."""
        from hermes_cli.web_server import _normalize_config_for_web

        result = _normalize_config_for_web({"model": "anthropic/claude-sonnet-4"})
        assert result["model"] == "anthropic/claude-sonnet-4"
        assert result["model_context_length"] == 0

    def test_normalize_dict_without_context_length_yields_zero(self):
        """normalize should default to 0 when model dict has no context_length."""
        from hermes_cli.web_server import _normalize_config_for_web

        cfg = {"model": {"default": "test/model", "provider": "openrouter"}}
        result = _normalize_config_for_web(cfg)
        assert result["model_context_length"] == 0

    def test_normalize_non_int_context_length_yields_zero(self):
        """normalize should coerce non-int context_length to 0."""
        from hermes_cli.web_server import _normalize_config_for_web

        cfg = {"model": {"default": "test/model", "context_length": "invalid"}}
        result = _normalize_config_for_web(cfg)
        assert result["model_context_length"] == 0

    def test_denormalize_writes_context_length_into_model_dict(self):
        """denormalize should write model_context_length back into model dict."""
        from hermes_cli.web_server import _denormalize_config_from_web
        from hermes_cli.config import save_config

        # Set up disk config with model as a dict
        save_config({
            "model": {"default": "anthropic/claude-opus-4.6", "provider": "openrouter"}
        })

        result = _denormalize_config_from_web({
            "model": "anthropic/claude-opus-4.6",
            "model_context_length": 100000,
        })
        assert isinstance(result["model"], dict)
        assert result["model"]["context_length"] == 100000
        assert "model_context_length" not in result  # virtual field removed

    def test_denormalize_zero_removes_context_length(self):
        """denormalize with model_context_length=0 should remove context_length key."""
        from hermes_cli.web_server import _denormalize_config_from_web
        from hermes_cli.config import save_config

        save_config({
            "model": {
                "default": "anthropic/claude-opus-4.6",
                "provider": "openrouter",
                "context_length": 50000,
            }
        })

        result = _denormalize_config_from_web({
            "model": "anthropic/claude-opus-4.6",
            "model_context_length": 0,
        })
        assert isinstance(result["model"], dict)
        assert "context_length" not in result["model"]

    def test_denormalize_upgrades_bare_string_to_dict(self):
        """denormalize should upgrade bare string model to dict when context_length set."""
        from hermes_cli.web_server import _denormalize_config_from_web
        from hermes_cli.config import save_config

        # Disk has model as bare string
        save_config({"model": "anthropic/claude-sonnet-4"})

        result = _denormalize_config_from_web({
            "model": "anthropic/claude-sonnet-4",
            "model_context_length": 65000,
        })
        assert isinstance(result["model"], dict)
        assert result["model"]["default"] == "anthropic/claude-sonnet-4"
        assert result["model"]["context_length"] == 65000

    def test_denormalize_bare_string_stays_string_when_zero(self):
        """denormalize should keep bare string model as string when context_length=0."""
        from hermes_cli.web_server import _denormalize_config_from_web
        from hermes_cli.config import save_config

        save_config({"model": "anthropic/claude-sonnet-4"})

        result = _denormalize_config_from_web({
            "model": "anthropic/claude-sonnet-4",
            "model_context_length": 0,
        })
        assert result["model"] == "anthropic/claude-sonnet-4"

    def test_denormalize_coerces_string_context_length(self):
        """denormalize should handle string model_context_length from frontend."""
        from hermes_cli.web_server import _denormalize_config_from_web
        from hermes_cli.config import save_config

        save_config({
            "model": {"default": "test/model", "provider": "openrouter"}
        })

        result = _denormalize_config_from_web({
            "model": "test/model",
            "model_context_length": "32000",
        })
        assert isinstance(result["model"], dict)
        assert result["model"]["context_length"] == 32000


class TestModelContextLengthSchema:
    """Tests for model_context_length placement in CONFIG_SCHEMA."""

    def test_schema_has_model_context_length(self):
        from hermes_cli.web_server import CONFIG_SCHEMA
        assert "model_context_length" in CONFIG_SCHEMA

    def test_schema_model_context_length_after_model(self):
        """model_context_length should appear immediately after model in schema."""
        from hermes_cli.web_server import CONFIG_SCHEMA
        keys = list(CONFIG_SCHEMA.keys())
        model_idx = keys.index("model")
        assert keys[model_idx + 1] == "model_context_length"

    def test_schema_model_context_length_is_number(self):
        from hermes_cli.web_server import CONFIG_SCHEMA
        entry = CONFIG_SCHEMA["model_context_length"]
        assert entry["type"] == "number"
        assert "category" in entry


class TestModelInfoEndpoint:
    """Tests for GET /api/model/info endpoint."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        try:
            from starlette.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi/starlette not installed")
        from hermes_cli.web_server import app
        self.client = TestClient(app)

    def test_model_info_returns_200(self):
        resp = self.client.get("/api/model/info")
        assert resp.status_code == 200
        data = resp.json()
        assert "model" in data
        assert "provider" in data
        assert "auto_context_length" in data
        assert "config_context_length" in data
        assert "effective_context_length" in data
        assert "capabilities" in data

    def test_model_info_with_dict_config(self, monkeypatch):
        import hermes_cli.web_server as ws

        monkeypatch.setattr(ws, "load_config", lambda: {
            "model": {
                "default": "anthropic/claude-opus-4.6",
                "provider": "openrouter",
                "context_length": 100000,
            }
        })

        with patch("agent.model_metadata.get_model_context_length", return_value=200000):
            resp = self.client.get("/api/model/info")

        data = resp.json()
        assert data["model"] == "anthropic/claude-opus-4.6"
        assert data["provider"] == "openrouter"
        assert data["auto_context_length"] == 200000
        assert data["config_context_length"] == 100000
        assert data["effective_context_length"] == 100000  # override wins

    def test_model_info_auto_detect_when_no_override(self, monkeypatch):
        import hermes_cli.web_server as ws

        monkeypatch.setattr(ws, "load_config", lambda: {
            "model": {"default": "anthropic/claude-opus-4.6", "provider": "openrouter"}
        })

        with patch("agent.model_metadata.get_model_context_length", return_value=200000):
            resp = self.client.get("/api/model/info")

        data = resp.json()
        assert data["auto_context_length"] == 200000
        assert data["config_context_length"] == 0
        assert data["effective_context_length"] == 200000  # auto wins

    def test_model_info_empty_model(self, monkeypatch):
        import hermes_cli.web_server as ws

        monkeypatch.setattr(ws, "load_config", lambda: {"model": ""})

        resp = self.client.get("/api/model/info")
        data = resp.json()
        assert data["model"] == ""
        assert data["effective_context_length"] == 0

    def test_model_info_bare_string_model(self, monkeypatch):
        import hermes_cli.web_server as ws

        monkeypatch.setattr(ws, "load_config", lambda: {
            "model": "anthropic/claude-sonnet-4"
        })

        with patch("agent.model_metadata.get_model_context_length", return_value=200000):
            resp = self.client.get("/api/model/info")

        data = resp.json()
        assert data["model"] == "anthropic/claude-sonnet-4"
        assert data["provider"] == ""
        assert data["config_context_length"] == 0
        assert data["effective_context_length"] == 200000

    def test_model_info_capabilities(self, monkeypatch):
        import hermes_cli.web_server as ws

        monkeypatch.setattr(ws, "load_config", lambda: {
            "model": {"default": "anthropic/claude-opus-4.6", "provider": "openrouter"}
        })

        mock_caps = MagicMock()
        mock_caps.supports_tools = True
        mock_caps.supports_vision = True
        mock_caps.supports_reasoning = True
        mock_caps.context_window = 200000
        mock_caps.max_output_tokens = 32000
        mock_caps.model_family = "claude-opus"

        with patch("agent.model_metadata.get_model_context_length", return_value=200000), \
             patch("agent.models_dev.get_model_capabilities", return_value=mock_caps):
            resp = self.client.get("/api/model/info")

        caps = resp.json()["capabilities"]
        assert caps["supports_tools"] is True
        assert caps["supports_vision"] is True
        assert caps["supports_reasoning"] is True
        assert caps["max_output_tokens"] == 32000
        assert caps["model_family"] == "claude-opus"

    def test_model_info_graceful_on_metadata_error(self, monkeypatch):
        """Endpoint should return zeros on import/resolution errors, not 500."""
        import hermes_cli.web_server as ws

        monkeypatch.setattr(ws, "load_config", lambda: {
            "model": "some/obscure-model"
        })

        with patch("agent.model_metadata.get_model_context_length", side_effect=Exception("boom")):
            resp = self.client.get("/api/model/info")

        assert resp.status_code == 200
        data = resp.json()
        assert data["auto_context_length"] == 0


# ---------------------------------------------------------------------------
# Gateway health probe tests
# ---------------------------------------------------------------------------


class TestProbeGatewayHealth:
    """Tests for _probe_gateway_health() — cross-container gateway detection."""

    def test_returns_false_when_no_url_configured(self, monkeypatch):
        """When GATEWAY_HEALTH_URL is unset, the probe returns (False, None)."""
        import hermes_cli.web_server as ws
        monkeypatch.setattr(ws, "_GATEWAY_HEALTH_URL", None)
        alive, body = ws._probe_gateway_health()
        assert alive is False
        assert body is None

    def test_normalizes_url_with_health_suffix(self, monkeypatch):
        """If the user sets the URL to include /health, it's stripped to base."""
        import hermes_cli.web_server as ws
        monkeypatch.setattr(ws, "_GATEWAY_HEALTH_URL", "http://gw:8642/health")
        monkeypatch.setattr(ws, "_GATEWAY_HEALTH_TIMEOUT", 1)
        # Both paths should fail (no server), but we verify they were constructed
        # correctly by checking the URLs attempted.
        calls = []
        original_urlopen = ws.urllib.request.urlopen

        def mock_urlopen(req, **kwargs):
            calls.append(req.full_url)
            raise ConnectionError("mock")

        monkeypatch.setattr(ws.urllib.request, "urlopen", mock_urlopen)
        alive, body = ws._probe_gateway_health()
        assert alive is False
        assert "http://gw:8642/health/detailed" in calls
        assert "http://gw:8642/health" in calls

    def test_normalizes_url_with_health_detailed_suffix(self, monkeypatch):
        """If the user sets the URL to include /health/detailed, it's stripped to base."""
        import hermes_cli.web_server as ws
        monkeypatch.setattr(ws, "_GATEWAY_HEALTH_URL", "http://gw:8642/health/detailed")
        monkeypatch.setattr(ws, "_GATEWAY_HEALTH_TIMEOUT", 1)
        calls = []

        def mock_urlopen(req, **kwargs):
            calls.append(req.full_url)
            raise ConnectionError("mock")

        monkeypatch.setattr(ws.urllib.request, "urlopen", mock_urlopen)
        ws._probe_gateway_health()
        assert "http://gw:8642/health/detailed" in calls
        assert "http://gw:8642/health" in calls

    def test_successful_detailed_probe(self, monkeypatch):
        """Successful /health/detailed probe returns (True, body_dict)."""
        import hermes_cli.web_server as ws
        monkeypatch.setattr(ws, "_GATEWAY_HEALTH_URL", "http://gw:8642")
        monkeypatch.setattr(ws, "_GATEWAY_HEALTH_TIMEOUT", 1)

        response_body = json.dumps({
            "status": "ok",
            "gateway_state": "running",
            "pid": 42,
        })

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = response_body.encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        monkeypatch.setattr(ws.urllib.request, "urlopen", lambda req, **kw: mock_resp)
        alive, body = ws._probe_gateway_health()
        assert alive is True
        assert body["status"] == "ok"
        assert body["pid"] == 42

    def test_detailed_fails_falls_back_to_simple_health(self, monkeypatch):
        """If /health/detailed fails, falls back to /health."""
        import hermes_cli.web_server as ws
        monkeypatch.setattr(ws, "_GATEWAY_HEALTH_URL", "http://gw:8642")
        monkeypatch.setattr(ws, "_GATEWAY_HEALTH_TIMEOUT", 1)

        call_count = [0]

        def mock_urlopen(req, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise ConnectionError("detailed failed")
            mock_resp = MagicMock()
            mock_resp.status = 200
            mock_resp.read.return_value = json.dumps({"status": "ok"}).encode()
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            return mock_resp

        monkeypatch.setattr(ws.urllib.request, "urlopen", mock_urlopen)
        alive, body = ws._probe_gateway_health()
        assert alive is True
        assert body["status"] == "ok"
        assert call_count[0] == 2


class TestStatusRemoteGateway:
    """Tests for /api/status with remote gateway health fallback."""

    @pytest.fixture(autouse=True)
    def _setup_test_client(self):
        try:
            from starlette.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi/starlette not installed")

        from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN
        self.client = TestClient(app)
        self.client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN

    def test_status_falls_back_to_remote_probe(self, monkeypatch):
        """When local PID check fails and remote probe succeeds, gateway shows running."""
        import hermes_cli.web_server as ws

        monkeypatch.setattr(ws, "get_running_pid", lambda: None)
        monkeypatch.setattr(ws, "read_runtime_status", lambda: None)
        monkeypatch.setattr(ws, "_GATEWAY_HEALTH_URL", "http://gw:8642")
        monkeypatch.setattr(ws, "_probe_gateway_health", lambda: (True, {
            "status": "ok",
            "gateway_state": "running",
            "platforms": {"telegram": {"state": "connected"}},
            "pid": 999,
        }))

        resp = self.client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["gateway_running"] is True
        assert data["gateway_pid"] == 999
        assert data["gateway_state"] == "running"
        assert data["gateway_health_url"] == "http://gw:8642"

    def test_status_remote_probe_not_attempted_when_local_pid_found(self, monkeypatch):
        """When local PID check succeeds, the remote probe is never called."""
        import hermes_cli.web_server as ws

        monkeypatch.setattr(ws, "get_running_pid", lambda: 1234)
        monkeypatch.setattr(ws, "read_runtime_status", lambda: {
            "gateway_state": "running",
            "platforms": {},
        })
        monkeypatch.setattr(ws, "_GATEWAY_HEALTH_URL", "http://gw:8642")
        probe_called = [False]
        original = ws._probe_gateway_health

        def track_probe():
            probe_called[0] = True
            return original()

        monkeypatch.setattr(ws, "_probe_gateway_health", track_probe)

        resp = self.client.get("/api/status")
        assert resp.status_code == 200
        assert not probe_called[0]

    def test_status_remote_probe_not_attempted_when_no_url(self, monkeypatch):
        """When GATEWAY_HEALTH_URL is unset, no probe is attempted."""
        import hermes_cli.web_server as ws

        monkeypatch.setattr(ws, "get_running_pid", lambda: None)
        monkeypatch.setattr(ws, "read_runtime_status", lambda: None)
        monkeypatch.setattr(ws, "_GATEWAY_HEALTH_URL", None)

        resp = self.client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["gateway_running"] is False
        assert data["gateway_health_url"] is None

    def test_status_remote_running_null_pid(self, monkeypatch):
        """Remote gateway running but PID not in response — pid should be None."""
        import hermes_cli.web_server as ws

        monkeypatch.setattr(ws, "get_running_pid", lambda: None)
        monkeypatch.setattr(ws, "read_runtime_status", lambda: None)
        monkeypatch.setattr(ws, "_GATEWAY_HEALTH_URL", "http://gw:8642")
        monkeypatch.setattr(ws, "_probe_gateway_health", lambda: (True, {
            "status": "ok",
        }))

        resp = self.client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["gateway_running"] is True
        assert data["gateway_pid"] is None
        assert data["gateway_state"] == "running"


# ---------------------------------------------------------------------------
# Dashboard theme normaliser tests
# ---------------------------------------------------------------------------


class TestNormaliseThemeDefinition:
    """Tests for _normalise_theme_definition() — parses YAML theme files."""

    def test_rejects_missing_name(self):
        from hermes_cli.web_server import _normalise_theme_definition
        assert _normalise_theme_definition({}) is None
        assert _normalise_theme_definition({"name": ""}) is None
        assert _normalise_theme_definition({"name": "   "}) is None

    def test_rejects_non_dict(self):
        from hermes_cli.web_server import _normalise_theme_definition
        assert _normalise_theme_definition("string") is None
        assert _normalise_theme_definition(None) is None
        assert _normalise_theme_definition([1, 2, 3]) is None

    def test_loose_colors_shorthand(self):
        """Bare hex strings under `colors` parse as {hex, alpha=1.0}."""
        from hermes_cli.web_server import _normalise_theme_definition
        result = _normalise_theme_definition({
            "name": "loose",
            "colors": {"background": "#000000", "midground": "#ffffff"},
        })
        assert result is not None
        assert result["palette"]["background"] == {"hex": "#000000", "alpha": 1.0}
        assert result["palette"]["midground"] == {"hex": "#ffffff", "alpha": 1.0}
        # foreground falls back to default (transparent white)
        assert result["palette"]["foreground"]["hex"] == "#ffffff"
        assert result["palette"]["foreground"]["alpha"] == 0.0

    def test_full_palette_form(self):
        from hermes_cli.web_server import _normalise_theme_definition
        result = _normalise_theme_definition({
            "name": "full",
            "palette": {
                "background": {"hex": "#0a1628", "alpha": 1.0},
                "midground": {"hex": "#a8d0ff", "alpha": 0.9},
                "warmGlow": "rgba(255, 0, 0, 0.5)",
                "noiseOpacity": 0.5,
            },
        })
        assert result["palette"]["background"]["hex"] == "#0a1628"
        assert result["palette"]["midground"]["alpha"] == 0.9
        assert result["palette"]["warmGlow"] == "rgba(255, 0, 0, 0.5)"
        assert result["palette"]["noiseOpacity"] == 0.5

    def test_default_typography_applied_when_missing(self):
        from hermes_cli.web_server import _normalise_theme_definition
        result = _normalise_theme_definition({"name": "minimal"})
        typo = result["typography"]
        assert "fontSans" in typo
        assert "fontMono" in typo
        assert typo["baseSize"] == "15px"
        assert typo["lineHeight"] == "1.55"
        assert typo["letterSpacing"] == "0"

    def test_partial_typography_merges_with_defaults(self):
        from hermes_cli.web_server import _normalise_theme_definition
        result = _normalise_theme_definition({
            "name": "partial",
            "typography": {
                "fontSans": "MyFont, sans-serif",
                "baseSize": "12px",
            },
        })
        assert result["typography"]["fontSans"] == "MyFont, sans-serif"
        assert result["typography"]["baseSize"] == "12px"
        # fontMono defaulted
        assert "monospace" in result["typography"]["fontMono"]

    def test_layout_defaults(self):
        from hermes_cli.web_server import _normalise_theme_definition
        result = _normalise_theme_definition({"name": "minimal"})
        assert result["layout"]["radius"] == "0.5rem"
        assert result["layout"]["density"] == "comfortable"

    def test_invalid_density_falls_back(self):
        from hermes_cli.web_server import _normalise_theme_definition
        result = _normalise_theme_definition({
            "name": "bad",
            "layout": {"density": "ultra-spacious"},
        })
        assert result["layout"]["density"] == "comfortable"

    def test_valid_densities_accepted(self):
        from hermes_cli.web_server import _normalise_theme_definition
        for d in ("compact", "comfortable", "spacious"):
            r = _normalise_theme_definition({"name": "x", "layout": {"density": d}})
            assert r["layout"]["density"] == d

    def test_color_overrides_filter_unknown_keys(self):
        from hermes_cli.web_server import _normalise_theme_definition
        result = _normalise_theme_definition({
            "name": "o",
            "colorOverrides": {
                "card": "#123456",
                "fakeToken": "#abcdef",
                "primary": 42,  # non-string rejected
                "destructive": "#ff0000",
            },
        })
        assert result["colorOverrides"] == {
            "card": "#123456",
            "destructive": "#ff0000",
        }

    def test_color_overrides_omitted_when_empty(self):
        from hermes_cli.web_server import _normalise_theme_definition
        result = _normalise_theme_definition({"name": "x"})
        assert "colorOverrides" not in result

    def test_alpha_clamped_to_unit_range(self):
        from hermes_cli.web_server import _normalise_theme_definition
        r = _normalise_theme_definition({
            "name": "c",
            "palette": {"background": {"hex": "#000", "alpha": 99.5}},
        })
        assert r["palette"]["background"]["alpha"] == 1.0
        r2 = _normalise_theme_definition({
            "name": "c",
            "palette": {"background": {"hex": "#000", "alpha": -5}},
        })
        assert r2["palette"]["background"]["alpha"] == 0.0

    def test_invalid_alpha_uses_default(self):
        from hermes_cli.web_server import _normalise_theme_definition
        r = _normalise_theme_definition({
            "name": "c",
            "palette": {"background": {"hex": "#000", "alpha": "not a number"}},
        })
        assert r["palette"]["background"]["alpha"] == 1.0


class TestDiscoverUserThemes:
    """Tests for _discover_user_themes() — scans ~/.hermes/dashboard-themes/."""

    def test_returns_empty_when_dir_missing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        from hermes_cli import web_server
        assert web_server._discover_user_themes() == []

    def test_loads_and_normalises_yaml(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        themes_dir = tmp_path / "dashboard-themes"
        themes_dir.mkdir()
        (themes_dir / "ocean.yaml").write_text(
            "name: ocean\n"
            "label: Ocean\n"
            "palette:\n"
            "  background:\n"
            "    hex: \"#0a1628\"\n"
            "    alpha: 1.0\n"
            "layout:\n"
            "  density: spacious\n"
        )
        from hermes_cli import web_server
        results = web_server._discover_user_themes()
        assert len(results) == 1
        assert results[0]["name"] == "ocean"
        assert results[0]["label"] == "Ocean"
        assert results[0]["palette"]["background"]["hex"] == "#0a1628"
        assert results[0]["layout"]["density"] == "spacious"
        # defaults filled in
        assert "fontSans" in results[0]["typography"]

    def test_malformed_yaml_skipped(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        themes_dir = tmp_path / "dashboard-themes"
        themes_dir.mkdir()
        (themes_dir / "bad.yaml").write_text("::: not valid yaml :::\n\tindent wrong")
        (themes_dir / "nameless.yaml").write_text("label: No Name Here\n")
        (themes_dir / "ok.yaml").write_text("name: ok\n")
        from hermes_cli import web_server
        results = web_server._discover_user_themes()
        names = [r["name"] for r in results]
        assert "ok" in names
        assert "bad" not in names  # malformed YAML
        assert len(results) == 1  # only the valid one


class TestNormaliseThemeExtensions:
    """Tests for the extended normaliser fields (assets, customCSS,
    componentStyles, layoutVariant) — the surfaces themes use to reskin
    the dashboard without shipping code."""

    def test_layout_variant_defaults_to_standard(self):
        from hermes_cli.web_server import _normalise_theme_definition
        result = _normalise_theme_definition({"name": "t"})
        assert result["layoutVariant"] == "standard"

    def test_layout_variant_accepts_known_values(self):
        from hermes_cli.web_server import _normalise_theme_definition
        for variant in ("standard", "cockpit", "tiled"):
            r = _normalise_theme_definition({"name": "t", "layoutVariant": variant})
            assert r["layoutVariant"] == variant

    def test_layout_variant_rejects_unknown(self):
        from hermes_cli.web_server import _normalise_theme_definition
        r = _normalise_theme_definition({"name": "t", "layoutVariant": "warship"})
        assert r["layoutVariant"] == "standard"
        r2 = _normalise_theme_definition({"name": "t", "layoutVariant": 12})
        assert r2["layoutVariant"] == "standard"

    def test_assets_named_slots_passthrough(self):
        from hermes_cli.web_server import _normalise_theme_definition
        r = _normalise_theme_definition({
            "name": "t",
            "assets": {
                "bg": "https://example.com/bg.jpg",
                "hero": "linear-gradient(180deg, red, blue)",
                "crest": "/ds-assets/crest.svg",
                "logo": "  ",  # whitespace-only — dropped
                "notAKnownKey": "ignored",
            },
        })
        assert r["assets"]["bg"] == "https://example.com/bg.jpg"
        assert r["assets"]["hero"].startswith("linear-gradient")
        assert r["assets"]["crest"] == "/ds-assets/crest.svg"
        assert "logo" not in r["assets"]  # whitespace-only rejected
        assert "notAKnownKey" not in r["assets"]  # unknown slot ignored

    def test_assets_custom_block(self):
        from hermes_cli.web_server import _normalise_theme_definition
        r = _normalise_theme_definition({
            "name": "t",
            "assets": {
                "custom": {
                    "scan-lines": "/img/scan.png",
                    "my_overlay": "/img/ov.png",
                    "bad key!": "x",  # non-alnum key — rejected
                    "empty": "",        # empty value — rejected
                },
            },
        })
        assert r["assets"]["custom"] == {
            "scan-lines": "/img/scan.png",
            "my_overlay": "/img/ov.png",
        }

    def test_assets_absent_means_no_field(self):
        from hermes_cli.web_server import _normalise_theme_definition
        r = _normalise_theme_definition({"name": "t"})
        assert "assets" not in r

    def test_custom_css_passthrough_and_capped(self):
        from hermes_cli.web_server import _normalise_theme_definition
        # Small CSS passes through verbatim.
        r = _normalise_theme_definition({
            "name": "t",
            "customCSS": "body { color: red; }",
        })
        assert r["customCSS"] == "body { color: red; }"

        # 40 KiB of CSS gets clipped to the 32 KiB cap.
        huge = "/* x */ " * (40 * 1024 // 8 + 10)
        r2 = _normalise_theme_definition({"name": "t", "customCSS": huge})
        assert len(r2["customCSS"]) <= 32 * 1024

    def test_custom_css_empty_dropped(self):
        from hermes_cli.web_server import _normalise_theme_definition
        for val in ("", "   \n\t", None):
            r = _normalise_theme_definition({"name": "t", "customCSS": val})
            assert "customCSS" not in r

    def test_component_styles_per_bucket(self):
        from hermes_cli.web_server import _normalise_theme_definition
        r = _normalise_theme_definition({
            "name": "t",
            "componentStyles": {
                "card": {
                    "clipPath": "polygon(0 0, 100% 0, 100% 100%, 0 100%)",
                    "boxShadow": "inset 0 0 0 1px red",
                    "bad prop!": "ignored",  # non-alnum prop rejected
                },
                "header": {"background": "linear-gradient(red, blue)"},
                "rogueBucket": {"foo": "bar"},  # not a known bucket — rejected
            },
        })
        assert r["componentStyles"]["card"] == {
            "clipPath": "polygon(0 0, 100% 0, 100% 100%, 0 100%)",
            "boxShadow": "inset 0 0 0 1px red",
        }
        assert r["componentStyles"]["header"]["background"].startswith("linear-gradient")
        assert "rogueBucket" not in r["componentStyles"]

    def test_component_styles_empty_buckets_dropped(self):
        from hermes_cli.web_server import _normalise_theme_definition
        r = _normalise_theme_definition({
            "name": "t",
            "componentStyles": {
                "card": {},        # empty — dropped entirely
                "header": {"bad prop!": "ignored"},  # all props rejected — bucket dropped
                "footer": {"background": "black"},
            },
        })
        assert "card" not in r.get("componentStyles", {})
        assert "header" not in r.get("componentStyles", {})
        assert r["componentStyles"]["footer"]["background"] == "black"

    def test_component_styles_accepts_numeric_values(self):
        """Numeric values (e.g. opacity: 0.8) are coerced to strings."""
        from hermes_cli.web_server import _normalise_theme_definition
        r = _normalise_theme_definition({
            "name": "t",
            "componentStyles": {"card": {"opacity": 0.8, "zIndex": 5}},
        })
        assert r["componentStyles"]["card"] == {"opacity": "0.8", "zIndex": "5"}


class TestBulkDeleteSessionsEndpoint:
    """Tests for ``POST /api/sessions/bulk-delete`` — backs the
    dashboard's "Delete N selected" flow on the sessions page.

    Locks in four things:

    1. Route-ordering: ``/api/sessions/bulk-delete`` must shadow the
       templated ``/api/sessions/{session_id}`` route below it (see
       the block comment in ``hermes_cli/web_server.py``).
    2. Behaviour parity with :meth:`SessionDB.delete_sessions` — real
       deleted count, archive/active sessions deleted on explicit
       selection.
    3. The 500-ID payload cap is enforced.
    4. Auth gating (issue #19533 contract).
    """

    @pytest.fixture(autouse=True)
    def _setup_test_client(self, monkeypatch, _isolate_hermes_home):
        try:
            from starlette.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi/starlette not installed")

        import hermes_state
        from hermes_constants import get_hermes_home
        from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

        monkeypatch.setattr(
            hermes_state, "DEFAULT_DB_PATH", get_hermes_home() / "state.db"
        )

        self.client = TestClient(app)
        self.auth_client = TestClient(app)
        self.auth_client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN

    def _seed(self, ids):
        from hermes_state import SessionDB

        db = SessionDB()
        try:
            for sid in ids:
                db.create_session(session_id=sid, source="cli")
        finally:
            db.close()

    def test_requires_auth(self):
        resp = self.client.post("/api/sessions/bulk-delete", json={"ids": ["x"]})
        assert resp.status_code == 401

    def test_deletes_listed_sessions_only(self):
        from hermes_state import SessionDB

        self._seed(["a", "b", "c"])
        resp = self.auth_client.post(
            "/api/sessions/bulk-delete", json={"ids": ["a", "b"]}
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "deleted": 2}

        db = SessionDB()
        try:
            assert db.get_session("a") is None
            assert db.get_session("b") is None
            assert db.get_session("c") is not None
        finally:
            db.close()

    def test_unknown_ids_silently_skipped(self):
        """The endpoint never 404s on a missing ID — it returns the
        real deleted count so a UI selection that raced against
        another tab still resolves cleanly."""
        self._seed(["real"])
        resp = self.auth_client.post(
            "/api/sessions/bulk-delete",
            json={"ids": ["real", "ghost1", "ghost2"]},
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "deleted": 1}

    def test_empty_list_is_noop(self):
        """``ids: []`` returns ``deleted: 0`` (200, not 400) — the UI
        treats an empty selection as a no-op rather than an error."""
        resp = self.auth_client.post(
            "/api/sessions/bulk-delete", json={"ids": []}
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "deleted": 0}

    def test_payload_cap_enforced(self):
        """501 IDs returns 400 — a hard cap stops a runaway selection
        from holding the SQLite writer for an extended window."""
        resp = self.auth_client.post(
            "/api/sessions/bulk-delete",
            json={"ids": [f"s{i}" for i in range(501)]},
        )
        assert resp.status_code == 400
        # 500 exactly still succeeds (no rows actually present, so
        # deleted=0 — but it's not the cap path).
        resp = self.auth_client.post(
            "/api/sessions/bulk-delete",
            json={"ids": [f"s{i}" for i in range(500)]},
        )
        assert resp.status_code == 200

    def test_route_order_not_shadowed_by_session_id(self):
        """Pin the route-ordering contract: ``POST /api/sessions/bulk-delete``
        must hit the bulk handler, not be re-interpreted via the
        templated ``/api/sessions/{session_id}`` family. Concretely the
        response carries our ``ok`` + ``deleted`` keys."""
        resp = self.auth_client.post(
            "/api/sessions/bulk-delete", json={"ids": []}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body.get("ok") is True
        assert "deleted" in body, (
            "If this assertion fails, /api/sessions/bulk-delete is "
            "being shadowed by /api/sessions/{session_id} — check "
            "registration order in hermes_cli/web_server.py."
        )


class TestDeleteEmptySessionsEndpoint:
    """Tests for ``GET /api/sessions/empty/count`` and
    ``DELETE /api/sessions/empty`` — the bulk-delete endpoints backing
    the dashboard's "Delete empty" button.

    Locks in three things the implementation has to get right:

    1. Route-ordering: the literal ``/api/sessions/empty[/count]`` paths
       must shadow the templated ``/api/sessions/{session_id}`` route
       above them. A regression here would route ``DELETE /api/sessions/
       empty`` to the single-session handler with ``session_id="empty"``
       (which 404s instead of bulk-deleting).
    2. Behaviour parity with :meth:`SessionDB.delete_empty_sessions`:
       active sessions and archived sessions are both preserved.
    3. Auth gating: both routes require the session token like every
       other ``/api/*`` endpoint (issue #19533 contract).
    """

    @pytest.fixture(autouse=True)
    def _setup_test_client(self, monkeypatch, _isolate_hermes_home):
        try:
            from starlette.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi/starlette not installed")

        import hermes_state
        from hermes_constants import get_hermes_home
        from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

        # Pin the SessionDB to the isolated HERMES_HOME so each test
        # starts with a clean state.db.
        monkeypatch.setattr(
            hermes_state, "DEFAULT_DB_PATH", get_hermes_home() / "state.db"
        )

        self.client = TestClient(app)
        self.auth_client = TestClient(app)
        self.auth_client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN

    def _seed(self):
        """Build the standard test corpus:

        * ``empty1`` / ``empty2`` — ended, no messages → should delete
        * ``hasmsg``  — ended, has one message → must survive
        * ``live``    — un-ended, empty → must survive (active)
        * ``archived``— ended, empty, archived → must survive
        """
        from hermes_state import SessionDB

        db = SessionDB()
        try:
            db.create_session(session_id="empty1", source="cli")
            db.end_session("empty1", end_reason="done")
            db.create_session(session_id="empty2", source="cli")
            db.end_session("empty2", end_reason="done")

            db.create_session(session_id="hasmsg", source="cli")
            db.append_message("hasmsg", role="user", content="hello")
            db.end_session("hasmsg", end_reason="done")

            db.create_session(session_id="live", source="cli")

            db.create_session(session_id="archived", source="cli")
            db.end_session("archived", end_reason="done")
            db.set_session_archived("archived", True)
        finally:
            db.close()

    def test_count_endpoint_requires_auth(self):
        """GET /api/sessions/empty/count must 401 without the session token."""
        resp = self.client.get("/api/sessions/empty/count")
        assert resp.status_code == 401

    def test_delete_endpoint_requires_auth(self):
        """DELETE /api/sessions/empty must 401 without the session token.

        Regression guard for issue #19533 — the bulk-delete is a strictly
        destructive primitive, the middleware must gate it even if a
        future refactor introduces a non-auth path."""
        resp = self.client.delete("/api/sessions/empty")
        assert resp.status_code == 401

    def test_count_returns_only_empty_ended_unarchived(self):
        """With the standard corpus, the count is exactly 2 — only
        ``empty1`` and ``empty2`` qualify (``hasmsg`` has a message,
        ``live`` is active, ``archived`` is archived)."""
        self._seed()
        resp = self.auth_client.get("/api/sessions/empty/count")
        assert resp.status_code == 200
        assert resp.json() == {"count": 2}

    def test_delete_returns_count_and_removes_only_empties(self):
        """DELETE returns the deleted count and removes only the
        empty-ended-unarchived rows — same shape contract as the
        DB-level method's unit tests."""
        from hermes_state import SessionDB

        self._seed()
        resp = self.auth_client.delete("/api/sessions/empty")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "deleted": 2}

        db = SessionDB()
        try:
            assert db.get_session("empty1") is None
            assert db.get_session("empty2") is None
            # Survivors: hasmsg has a message, live is active, archived
            # is archived. All three must still be there.
            assert db.get_session("hasmsg") is not None
            assert db.get_session("live") is not None
            assert db.get_session("archived") is not None
            # And the count endpoint now reports 0.
            assert db.count_empty_sessions() == 0
        finally:
            db.close()

    def test_delete_with_no_empties_returns_zero(self):
        """No empty sessions → endpoint returns ``deleted: 0`` (200,
        not 404). The dashboard relies on this no-op path to surface
        a "Nothing to clean up" toast instead of an error."""
        resp = self.auth_client.delete("/api/sessions/empty")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "deleted": 0}

    def test_route_order_empty_not_shadowed_by_session_id(self):
        """Pin the route-ordering contract: ``DELETE /api/sessions/empty``
        must hit the bulk handler, not the templated single-session
        handler (which would 404 because no session has id 'empty').

        Concretely: a request against the bulk path on an EMPTY corpus
        returns ``{ok: True, deleted: 0}``. If the templated route were
        winning, we'd see 404 ("Session not found") instead.
        """
        resp = self.auth_client.delete("/api/sessions/empty")
        assert resp.status_code == 200
        body = resp.json()
        assert "deleted" in body, (
            "If this assertion fails, the literal /api/sessions/empty "
            "route is being shadowed by the templated /api/sessions/"
            "{session_id} route — check registration order in "
            "hermes_cli/web_server.py."
        )


class TestPluginAPIAuth:
    """Tests that plugin API routes require the session token (issue #19533)."""

    @pytest.fixture(autouse=True)
    def _setup_test_client(self, monkeypatch, _isolate_hermes_home, _install_example_plugin):
        """Create a TestClient without the session token header.

        Pulls in ``_install_example_plugin`` so ``test_plugin_route_allows_auth``
        has the ``/api/plugins/example/hello`` endpoint available — the
        example plugin is no longer a bundled plugin, so the fixture
        installs it into the per-test ``HERMES_HOME``.
        """
        try:
            from starlette.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi/starlette not installed")

        import hermes_state
        from hermes_constants import get_hermes_home
        from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

        monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", get_hermes_home() / "state.db")

        self.client = TestClient(app)
        self.auth_client = TestClient(app)
        self.auth_client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN

    def test_plugin_route_requires_auth(self):
        """Plugin API routes should return 401 without a valid session token."""
        # Use a known plugin route (kanban board)
        resp = self.client.get("/api/plugins/kanban/board")
        assert resp.status_code == 401

    def test_plugin_route_allows_auth(self):
        """Plugin API routes should work with a valid session token.

        Uses ``/api/plugins/example/hello`` from the example-dashboard
        test fixture (installed into HERMES_HOME by the class-level
        ``_install_example_plugin`` fixture) — a stable, side-effect-free
        GET that's only loaded for tests. With a valid token the handler
        should run (200); without one the middleware should 401 before
        the handler is reached.
        """
        # Without auth: middleware blocks before reaching the handler.
        resp = self.client.get("/api/plugins/example/hello")
        assert resp.status_code == 401

        # With auth: handler runs.
        resp = self.auth_client.get("/api/plugins/example/hello")
        assert resp.status_code == 200

    def test_plugin_post_requires_auth(self):
        """Plugin POST routes should return 401 without a valid session token."""
        resp = self.client.post("/api/plugins/kanban/tasks", json={"title": "test"})
        assert resp.status_code == 401

    def test_plugin_patch_requires_auth(self):
        """Plugin PATCH routes should return 401 without a valid session token.

        PATCH is the mutation method most commonly used by the dashboard for
        kanban task edits — explicitly cover it so a future middleware
        regression that whitelists non-GET methods can't sneak through.
        """
        resp = self.client.patch(
            "/api/plugins/kanban/tasks/t_fake",
            json={"title": "renamed"},
        )
        assert resp.status_code == 401

    def test_plugin_delete_requires_auth(self):
        """Plugin DELETE routes should return 401 without a valid session token."""
        resp = self.client.delete("/api/plugins/kanban/tasks/t_fake")
        assert resp.status_code == 401

    def test_non_kanban_plugin_route_requires_auth(self):
        """Auth must be plugin-agnostic, not kanban-specific.

        The middleware fix is at the gate level (no per-plugin allowlist),
        so any plugin's API surface — kanban, hermes-achievements, future
        plugins — must require the session token. Hit a non-kanban plugin
        path to lock that in.
        """
        # Real plugin path (hermes-achievements is loaded by default).
        resp = self.client.get("/api/plugins/hermes-achievements/overview")
        assert resp.status_code == 401
        # Same for an arbitrary plugin namespace that doesn't even exist —
        # the middleware should 401 before routing decides 404, so an
        # attacker can't fingerprint plugin names by status codes.
        resp = self.client.get("/api/plugins/_definitely_not_a_plugin_/anything")
        assert resp.status_code == 401

    def test_plugin_websocket_unaffected_by_http_middleware(self):
        """The kanban /events WebSocket has its own ``?token=`` check;
        the HTTP middleware change must not start gating WS upgrades.

        Starlette doesn't run HTTP middleware on WebSocket upgrades anyway,
        but pin the behavior so a future refactor that moves auth into a
        shared layer can't silently break the WS auth contract.
        """
        from starlette.websockets import WebSocketDisconnect

        # Without a token the WS endpoint must close the upgrade itself
        # (its own _check_ws_token), NOT 401 from the HTTP middleware.
        try:
            with self.client.websocket_connect(
                "/api/plugins/kanban/events"
            ):
                pass  # if we got here without disconnect, the WS accepted us
        except WebSocketDisconnect:
            pass  # expected — WS endpoint rejected via its own check
        except Exception:
            # The kanban plugin may not be mounted in this test environment,
            # in which case the route doesn't exist at all (3xx/4xx during
            # upgrade). That's fine for this regression — it only matters
            # that the HTTP middleware didn't start intercepting WS upgrades.
            pass


class TestDashboardPluginManifestExtensions:
    """Tests for the extended plugin manifest fields (tab.override,
    tab.hidden, slots) read by _discover_dashboard_plugins()."""

    def _write_plugin(self, tmp_path, name, manifest):
        import json
        plug_dir = tmp_path / "plugins" / name / "dashboard"
        plug_dir.mkdir(parents=True)
        (plug_dir / "manifest.json").write_text(json.dumps(manifest))
        return plug_dir

    def test_override_and_hidden_carried_through(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        self._write_plugin(tmp_path, "skin-home", {
            "name": "skin-home",
            "label": "Skin Home",
            "tab": {"path": "/skin-home", "override": "/", "hidden": True},
            "slots": ["sidebar", "header-left"],
            "entry": "dist/index.js",
        })
        from hermes_cli import web_server
        # Bust the process-level cache so the test plugin is picked up.
        web_server._dashboard_plugins_cache = None
        plugins = web_server._get_dashboard_plugins(force_rescan=True)
        entry = next(p for p in plugins if p["name"] == "skin-home")
        assert entry["tab"]["override"] == "/"
        assert entry["tab"]["hidden"] is True
        assert entry["slots"] == ["sidebar", "header-left"]

    def test_override_requires_leading_slash(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        self._write_plugin(tmp_path, "bad-override", {
            "name": "bad-override",
            "label": "Bad",
            "tab": {"path": "/bad", "override": "no-leading-slash"},
            "entry": "dist/index.js",
        })
        from hermes_cli import web_server
        web_server._dashboard_plugins_cache = None
        plugins = web_server._get_dashboard_plugins(force_rescan=True)
        entry = next(p for p in plugins if p["name"] == "bad-override")
        assert "override" not in entry["tab"]

    def test_slots_default_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        self._write_plugin(tmp_path, "no-slots", {
            "name": "no-slots",
            "label": "No Slots",
            "tab": {"path": "/no-slots"},
            "entry": "dist/index.js",
        })
        from hermes_cli import web_server
        web_server._dashboard_plugins_cache = None
        plugins = web_server._get_dashboard_plugins(force_rescan=True)
        entry = next(p for p in plugins if p["name"] == "no-slots")
        assert entry["slots"] == []
        assert "hidden" not in entry["tab"]
        assert "override" not in entry["tab"]

    def test_slots_filters_non_string_entries(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        self._write_plugin(tmp_path, "mixed-slots", {
            "name": "mixed-slots",
            "label": "Mixed",
            "tab": {"path": "/mixed-slots"},
            "slots": ["sidebar", "", 42, None, "header-right"],
            "entry": "dist/index.js",
        })
        from hermes_cli import web_server
        web_server._dashboard_plugins_cache = None
        plugins = web_server._get_dashboard_plugins(force_rescan=True)
        entry = next(p for p in plugins if p["name"] == "mixed-slots")
        assert entry["slots"] == ["sidebar", "header-right"]

    def test_page_scoped_slots_preserved(self, tmp_path, monkeypatch):
        """Page-scoped slot names (e.g. ``sessions:top``) round-trip through
        the manifest loader untouched.  The backend has no allowlist — the
        frontend ``<PluginSlot name="...">`` placements decide what actually
        renders — but the loader must not mangle colons in slot names."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        self._write_plugin(tmp_path, "page-slots", {
            "name": "page-slots",
            "label": "Page Slots",
            "tab": {"path": "/page-slots", "hidden": True},
            "slots": [
                "sessions:top",
                "analytics:bottom",
                "logs:top",
                "skills:bottom",
                "config:top",
                "env:bottom",
                "docs:top",
                "cron:bottom",
                "chat:top",
            ],
            "entry": "dist/index.js",
        })
        from hermes_cli import web_server
        web_server._dashboard_plugins_cache = None
        plugins = web_server._get_dashboard_plugins(force_rescan=True)
        entry = next(p for p in plugins if p["name"] == "page-slots")
        assert entry["slots"] == [
            "sessions:top",
            "analytics:bottom",
            "logs:top",
            "skills:bottom",
            "config:top",
            "env:bottom",
            "docs:top",
            "cron:bottom",
            "chat:top",
        ]


# ---------------------------------------------------------------------------
# /api/pty WebSocket — terminal bridge for the dashboard "Chat" tab.
#
# These tests drive the endpoint with a tiny fake command (typically ``cat``
# or ``sh -c 'printf …'``) instead of the real ``hermes --tui`` binary.  The
# endpoint resolves its argv through ``_resolve_chat_argv``, so tests
# monkeypatch that hook.
# ---------------------------------------------------------------------------

import sys


skip_on_windows = pytest.mark.skipif(
    sys.platform.startswith("win"), reason="PTY bridge is POSIX-only"
)


@skip_on_windows
class TestPtyWebSocket:
    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch, _isolate_hermes_home):
        from starlette.testclient import TestClient

        import hermes_cli.web_server as ws

        # Avoid exec'ing the actual TUI in tests: every test below installs
        # its own fake argv via ``ws._resolve_chat_argv``.
        self.ws_module = ws
        monkeypatch.setattr(ws, "_DASHBOARD_EMBEDDED_CHAT_ENABLED", True)
        self.token = ws._SESSION_TOKEN
        self.client = TestClient(ws.app)

    def _url(self, token: str | None = None, **params: str) -> str:
        tok = token if token is not None else self.token
        # TestClient.websocket_connect takes the path; it reconstructs the
        # query string, so we pass it inline.
        from urllib.parse import urlencode

        q = {"token": tok, **params}
        return f"/api/pty?{urlencode(q)}"

    def test_resolve_chat_argv_uses_dashboard_scroll_env(self, monkeypatch):
        """Dashboard chat runs the TUI in browser-scrollback mode."""
        import hermes_cli.main as main_mod

        monkeypatch.setattr(
            main_mod,
            "_make_tui_argv",
            lambda project_root, tui_dev=False: (["node", "dist/entry.js"], "/tmp/ui-tui"),
        )

        _argv, _cwd, env = self.ws_module._resolve_chat_argv()

        assert env["HERMES_TUI_INLINE"] == "1"
        assert env["HERMES_TUI_DISABLE_MOUSE"] == "1"

    def test_rejects_when_embedded_chat_disabled(self, monkeypatch):
        monkeypatch.setattr(self.ws_module, "_DASHBOARD_EMBEDDED_CHAT_ENABLED", False)
        from starlette.websockets import WebSocketDisconnect

        with pytest.raises(WebSocketDisconnect) as exc:
            with self.client.websocket_connect(self._url()):
                pass
        assert exc.value.code == 4404

    def test_rejects_raw_pty_in_assistant_mode(self, monkeypatch):
        # The raw terminal must not be reachable on the customer surface, even
        # with a valid session token and embedded chat enabled.
        monkeypatch.setattr(self.ws_module, "_DASHBOARD_MODE", "assistant")
        from starlette.websockets import WebSocketDisconnect

        with pytest.raises(WebSocketDisconnect) as exc:
            with self.client.websocket_connect(self._url()):
                pass
        assert exc.value.code == 4403

    def test_rejects_missing_token(self, monkeypatch):
        monkeypatch.setattr(
            self.ws_module,
            "_resolve_chat_argv",
            lambda resume=None, sidecar_url=None: (["/bin/cat"], None, None),
        )
        from starlette.websockets import WebSocketDisconnect

        with pytest.raises(WebSocketDisconnect) as exc:
            with self.client.websocket_connect("/api/pty"):
                pass
        assert exc.value.code == 4401

    def test_rejects_bad_token(self, monkeypatch):
        monkeypatch.setattr(
            self.ws_module,
            "_resolve_chat_argv",
            lambda resume=None, sidecar_url=None: (["/bin/cat"], None, None),
        )
        from starlette.websockets import WebSocketDisconnect

        with pytest.raises(WebSocketDisconnect) as exc:
            with self.client.websocket_connect(self._url(token="wrong")):
                pass
        assert exc.value.code == 4401

    def test_streams_child_stdout_to_client(self, monkeypatch):
        monkeypatch.setattr(
            self.ws_module,
            "_resolve_chat_argv",
            lambda resume=None, sidecar_url=None: (
                ["/bin/sh", "-c", "printf hermes-ws-ok"],
                None,
                None,
            ),
        )
        with self.client.websocket_connect(self._url()) as conn:
            # Drain frames until we see the needle or time out.  TestClient's
            # recv_bytes blocks; loop until we have the signal byte string.
            buf = b""
            import time

            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                try:
                    frame = conn.receive_bytes()
                except Exception:
                    break
                if frame:
                    buf += frame
                if b"hermes-ws-ok" in buf:
                    break
            assert b"hermes-ws-ok" in buf

    def test_client_input_reaches_child_stdin(self, monkeypatch):
        # ``cat`` echoes stdin back, so a write → read round-trip proves
        # the full duplex path.
        monkeypatch.setattr(
            self.ws_module,
            "_resolve_chat_argv",
            lambda resume=None, sidecar_url=None: (["/bin/cat"], None, None),
        )
        with self.client.websocket_connect(self._url()) as conn:
            conn.send_bytes(b"round-trip-payload\n")
            buf = b""
            import time

            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                frame = conn.receive_bytes()
                if frame:
                    buf += frame
                if b"round-trip-payload" in buf:
                    break
            assert b"round-trip-payload" in buf

    def test_resize_escape_is_forwarded(self, monkeypatch):
        # Resize escape gets intercepted and applied via TIOCSWINSZ, then the
        # child reads the TTY ioctl directly. Avoid tput because CI may not set
        # TERM for non-interactive shells.
        import sys

        winsize_script = (
            "import fcntl, struct, termios, time; "
            "time.sleep(0.5); "
            "rows, cols, *_ = struct.unpack('HHHH', "
            "fcntl.ioctl(0, termios.TIOCGWINSZ, b'\\0' * 8)); "
            "print(cols); print(rows)"
        )
        monkeypatch.setattr(
            self.ws_module,
            "_resolve_chat_argv",
            # sleep gives the test time to push the resize before the child reads the ioctl.
            lambda resume=None, sidecar_url=None: (
                [sys.executable, "-c", winsize_script],
                None,
                None,
            ),
        )
        with self.client.websocket_connect(self._url()) as conn:
            conn.send_text("\x1b[RESIZE:99;41]")
            buf = b""
            import time

            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                # receive_bytes() blocks; once the child prints its winsize and
                # exits, the PTY closes and further reads raise. Without this
                # guard a missed-marker run blocks until the 30s pytest-timeout
                # (flaky failure) instead of failing fast on the assert below.
                try:
                    frame = conn.receive_bytes()
                except Exception:
                    break
                if frame:
                    buf += frame
                if b"99" in buf and b"41" in buf:
                    break
            assert b"99" in buf and b"41" in buf

    def test_unavailable_platform_closes_with_message(self, monkeypatch):
        from hermes_cli.pty_bridge import PtyUnavailableError

        def _raise(argv, **kwargs):
            raise PtyUnavailableError("pty missing for tests")

        monkeypatch.setattr(
            self.ws_module,
            "_resolve_chat_argv",
            lambda resume=None, sidecar_url=None: (["/bin/cat"], None, None),
        )
        # Patch PtyBridge.spawn at the web_server module's binding.
        import hermes_cli.web_server as ws_mod

        monkeypatch.setattr(ws_mod.PtyBridge, "spawn", classmethod(lambda cls, *a, **k: _raise(*a, **k)))

        with self.client.websocket_connect(self._url()) as conn:
            # Expect a final text frame with the error message, then close.
            msg = conn.receive_text()
            assert "pty missing" in msg or "unavailable" in msg.lower() or "pty" in msg.lower()

    def test_resume_parameter_is_forwarded_to_argv(self, monkeypatch):
        captured: dict = {}

        def fake_resolve(resume=None, sidecar_url=None):
            captured["resume"] = resume
            return (["/bin/sh", "-c", "printf resume-arg-ok"], None, None)

        monkeypatch.setattr(self.ws_module, "_resolve_chat_argv", fake_resolve)

        with self.client.websocket_connect(self._url(resume="sess-42")) as conn:
            # Drain briefly so the handler actually invokes the resolver.
            try:
                conn.receive_bytes()
            except Exception:
                pass
        assert captured.get("resume") == "sess-42"

    def test_channel_param_propagates_sidecar_url(self, monkeypatch):
        """When /api/pty is opened with ?channel=, the PTY child gets a
        HERMES_TUI_SIDECAR_URL env var pointing back at /api/pub on the
        same channel — which is how tool events reach the dashboard sidebar."""
        captured: dict = {}

        def fake_resolve(resume=None, sidecar_url=None):
            captured["sidecar_url"] = sidecar_url
            return (["/bin/sh", "-c", "printf sidecar-ok"], None, None)

        monkeypatch.setattr(self.ws_module, "_resolve_chat_argv", fake_resolve)
        monkeypatch.setattr(
            self.ws_module.app.state, "bound_host", "127.0.0.1", raising=False
        )
        monkeypatch.setattr(
            self.ws_module.app.state, "bound_port", 9119, raising=False
        )

        headers = {"host": "127.0.0.1:9119", "origin": "http://127.0.0.1:9119"}
        with self.client.websocket_connect(
            self._url(channel="abc-123"), headers=headers
        ) as conn:
            try:
                conn.receive_bytes()
            except Exception:
                pass

        url = captured.get("sidecar_url") or ""
        assert url.startswith("ws://127.0.0.1:9119/api/pub?")
        assert "channel=abc-123" in url
        assert "token=" in url

    def test_pub_broadcasts_to_events_subscribers(self):
        """A frame handed to _broadcast_event is sent verbatim to every
        subscriber registered on that channel — and not to subscribers on
        other channels.

        This drives the broadcast unit directly under asyncio rather than
        round-tripping through Starlette's TestClient WebSocket portal. The
        portal version was flaky under heavy parallel CI load: the broadcast
        had to traverse two nested threaded portals within a 10s wall-clock
        budget, and a starved ASGI thread occasionally blew that budget even
        though the server logic was correct. Testing _broadcast_event with
        fake subscribers removes the scheduling surface entirely while
        asserting the exact fan-out contract.
        """
        import asyncio
        from hermes_cli import web_server as ws_mod

        class _FakeSub:
            def __init__(self):
                self.sent: list[str] = []

            async def send_text(self, payload: str) -> None:
                self.sent.append(payload)

        app = ws_mod.app

        async def _run():
            sub_a1 = _FakeSub()
            sub_a2 = _FakeSub()
            sub_other = _FakeSub()
            frame = '{"type":"tool.start","payload":{"tool_id":"t1"}}'

            event_channels, event_lock = ws_mod._get_event_state(app)
            # Register two subscribers on the target channel and one on a
            # different channel, exactly as the /api/events handler does.
            async with event_lock:
                event_channels.setdefault("broadcast-test", set()).update(
                    {sub_a1, sub_a2}
                )
                event_channels.setdefault("other-channel", set()).add(sub_other)
            try:
                await ws_mod._broadcast_event(app, "broadcast-test", frame)
            finally:
                async with event_lock:
                    event_channels.pop("broadcast-test", None)
                    event_channels.pop("other-channel", None)

            return sub_a1, sub_a2, sub_other, frame

        sub_a1, sub_a2, sub_other, frame = asyncio.run(_run())

        # Every subscriber on the channel got the frame verbatim, exactly once.
        assert sub_a1.sent == [frame]
        assert sub_a2.sent == [frame]
        # A subscriber on a different channel got nothing.
        assert sub_other.sent == []

    def test_events_rejects_missing_channel(self):
        from starlette.websockets import WebSocketDisconnect

        with pytest.raises(WebSocketDisconnect) as exc:
            with self.client.websocket_connect(
                f"/api/events?token={self.token}"
            ):
                pass
        assert exc.value.code == 4400


def test_resolve_chat_argv_injects_gateway_ws_url(monkeypatch):
    import hermes_cli.main as cli_main
    import hermes_cli.web_server as ws

    monkeypatch.setattr(
        cli_main,
        "_make_tui_argv",
        lambda *_args, **_kwargs: (["node", "fake-tui.js"], Path("/tmp")),
    )
    monkeypatch.setattr(ws.app.state, "bound_host", "127.0.0.1", raising=False)
    monkeypatch.setattr(ws.app.state, "bound_port", 9119, raising=False)

    _argv, _cwd, env = ws._resolve_chat_argv()

    assert env is not None
    gateway_url = env.get("HERMES_TUI_GATEWAY_URL", "")
    assert gateway_url.startswith("ws://127.0.0.1:9119/api/ws?")
    assert "token=" in gateway_url


class TestDashboardPluginStaticAssetAllowlist:
    """``/dashboard-plugins/<name>/<path>`` is unauthenticated by design —
    the SPA loads plugin JS via ``<script src>`` and CSS via
    ``<link href>``, neither of which can attach a custom auth header.
    Instead the route restricts file types to the browser-asset
    allowlist (JS/CSS/JSON/images/fonts) so that user-installed
    plugins shipping a ``plugin_api.py`` backend module don't leak
    their Python source to anyone reachable on the loopback port.

    Regression test for the dashboard pentest finding filed alongside
    the ``web-pentest`` skill (PR #32265 / issue #32267).
    """

    @pytest.fixture(autouse=True)
    def _setup_test_client(self, monkeypatch, _isolate_hermes_home, _install_example_plugin):
        """Create a TestClient and install the example-dashboard fixture.

        The static-asset allowlist tests need a plugin to point at —
        they verify that ``/dashboard-plugins/example/manifest.json``
        is served while ``plugin_api.py`` and ``__pycache__/*.pyc``
        from the same directory are not. Since the example plugin is
        no longer bundled, ``_install_example_plugin`` lays it down in
        the per-test ``HERMES_HOME`` user-plugins dir.
        """
        try:
            from starlette.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi/starlette not installed")

        from hermes_cli.web_server import app

        self.client = TestClient(app)

    def test_python_source_is_404(self):
        """The example plugin's ``plugin_api.py`` must NOT be served as
        a static asset, even though the file exists under the plugin's
        dashboard directory. Suffix not in the allowlist → 404."""
        resp = self.client.get("/dashboard-plugins/example/plugin_api.py")
        assert resp.status_code == 404

    def test_pycache_is_404(self):
        """Same protection for compiled Python (``.pyc``) inside the
        plugin's ``__pycache__/``. Real plugins ship these as a
        side-effect of running tests / dashboard once."""
        # __pycache__ files are only generated after the api file has
        # been imported once. Use the path the example plugin actually
        # generates during the dashboard test boot.
        resp = self.client.get(
            "/dashboard-plugins/example/__pycache__/plugin_api.cpython-311.pyc"
        )
        # 404 either way (file may not exist on this CI Python version);
        # what matters is we never get a 200 with the bytes.
        assert resp.status_code == 404

    def test_manifest_json_still_served(self):
        """JSON files remain browser-fetchable — manifests, localized
        data, source maps, etc. all sit in this bucket."""
        resp = self.client.get("/dashboard-plugins/example/manifest.json")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/json")
        # And the body is actually the manifest, not the SPA fallback.
        body = resp.json()
        assert body.get("name") == "example"

    def test_unknown_plugin_is_404(self):
        """Existing behaviour preserved: nonexistent plugin name → 404."""
        resp = self.client.get(
            "/dashboard-plugins/_definitely_not_a_plugin_/manifest.json"
        )
        assert resp.status_code == 404

    def test_path_traversal_still_blocked(self):
        """The allowlist is on top of the existing ``.resolve()`` /
        ``is_relative_to()`` check — a ``.js`` named file at an
        out-of-base path is still rejected as traversal, not served."""
        resp = self.client.get(
            "/dashboard-plugins/example/..%2Fplugin_api.py"
        )
        # 403 traversal-blocked OR 404 (depending on URL decode order)
        # — never 200.
        assert resp.status_code in (403, 404)


def _fake_httpx_client(*, status: int | None = None, raise_exc: bool = False):
    """Build a drop-in for httpx.Client whose .get() returns a canned status
    (or raises a transport error). Patched in for the credential-validate probe
    so tests never touch the network."""
    class _Resp:
        def __init__(self, code):
            self.status_code = code

        @property
        def is_success(self):
            return 200 <= self.status_code < 300

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, *a, **k):
            if raise_exc:
                raise RuntimeError("connection refused")
            return _Resp(status)

    return _Client


class TestValidateProviderCredential:
    """Live-probe credential validation (/api/providers/validate)."""

    @pytest.fixture(autouse=True)
    def _setup_test_client(self, monkeypatch, _isolate_hermes_home):
        try:
            from starlette.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi/starlette not installed")

        from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

        self.client = TestClient(app)
        self.client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN

    def _post(self, key, value):
        return self.client.post("/api/providers/validate", json={"key": key, "value": value})

    def test_rejected_key_blocks(self, monkeypatch):
        monkeypatch.setattr("httpx.Client", _fake_httpx_client(status=401))
        data = self._post("OPENROUTER_API_KEY", "sk-bogus").json()
        assert data["ok"] is False and data["reachable"] is True

    def test_valid_key_passes(self, monkeypatch):
        monkeypatch.setattr("httpx.Client", _fake_httpx_client(status=200))
        data = self._post("OPENAI_API_KEY", "sk-real").json()
        assert data["ok"] is True and data["reachable"] is True

    def test_rate_limited_counts_as_valid(self, monkeypatch):
        monkeypatch.setattr("httpx.Client", _fake_httpx_client(status=429))
        data = self._post("XAI_API_KEY", "xai-real").json()
        assert data["ok"] is True

    def test_network_error_is_unreachable_not_blocking(self, monkeypatch):
        monkeypatch.setattr("httpx.Client", _fake_httpx_client(raise_exc=True))
        data = self._post("OPENROUTER_API_KEY", "sk-real").json()
        assert data["ok"] is False and data["reachable"] is False

    def test_unknown_provider_is_not_validated(self):
        # No probe for this key → don't block (ok True, reachable False).
        data = self._post("SOME_OTHER_API_KEY", "whatever-value").json()
        assert data["ok"] is True and data["reachable"] is False

    def test_empty_value_rejected(self):
        data = self._post("OPENAI_API_KEY", "   ").json()
        assert data["ok"] is False


class TestDocxExtractionHardening:
    """_extract_uploaded_text must not be a zip-bomb / XML-entity DoS vector."""

    @staticmethod
    def _make_docx(path, document_xml: bytes) -> None:
        import zipfile

        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("word/document.xml", document_xml)

    def test_normal_docx_extracts_text(self, tmp_path):
        from hermes_cli.web_server import _extract_uploaded_text

        path = tmp_path / "ok.docx"
        self._make_docx(path, b"<document><t>Hello world</t></document>")
        text, note = _extract_uploaded_text(path)
        assert note == "docx"
        assert "Hello world" in text

    def test_docx_with_dtd_entities_is_rejected(self, tmp_path):
        from hermes_cli.web_server import _extract_uploaded_text

        # Classic billion-laughs shape: must be refused, not expanded.
        bomb = (
            b"<?xml version='1.0'?>"
            b"<!DOCTYPE lolz [<!ENTITY lol 'lol'>"
            b"<!ENTITY lol2 '&lol;&lol;&lol;&lol;&lol;'>]>"
            b"<document><t>&lol2;</t></document>"
        )
        path = tmp_path / "bomb.docx"
        self._make_docx(path, bomb)
        text, note = _extract_uploaded_text(path)
        assert note == "docx-extraction-failed"
        assert text == ""

    def test_docx_dtd_after_large_comment_is_rejected(self, tmp_path):
        from hermes_cli.web_server import _extract_uploaded_text

        # Bypass attempt: a >64KB leading comment pushes the DOCTYPE/ENTITY
        # declarations past the old 64KB substring window. The parser-level
        # guard must still refuse it, with the entity never expanded.
        padding = b"<!-- " + b"A" * (70 * 1024) + b" -->"
        bomb = (
            b"<?xml version='1.0'?>"
            + padding
            + b"<!DOCTYPE lolz [<!ENTITY lol 'PWNED'>]>"
            + b"<document><t>&lol;</t></document>"
        )
        path = tmp_path / "padded-bomb.docx"
        self._make_docx(path, bomb)
        text, note = _extract_uploaded_text(path)
        assert note == "docx-extraction-failed"
        assert text == ""
        assert "PWNED" not in text

    def test_docx_oversized_xml_is_rejected(self, tmp_path, monkeypatch):
        import hermes_cli.web_server as web_server

        monkeypatch.setattr(web_server, "_ASSISTANT_DOCX_MAX_XML_BYTES", 100)
        path = tmp_path / "big.docx"
        self._make_docx(path, b"<document><t>" + b"A" * 500 + b"</t></document>")
        text, note = web_server._extract_uploaded_text(path)
        assert note == "docx-too-large"
        assert text == ""
def test_clean_todo_text_strips_agent_metadata_comment():
    from hermes_cli.web_server import _clean_todo_text

    assert _clean_todo_text("Angebot prüfen <!-- hermes:id=a status=pending -->") == "Angebot prüfen"


def test_todo_summary_does_not_leak_agent_metadata_into_cui(tmp_path, monkeypatch):
    # The agent writes TODO.md with hidden round-trip metadata; the customer
    # Aufgaben panel (_todo_summary) must not render it.
    import hermes_cli.web_server as web_server
    from tools.todo_tool import TodoStore

    todo_file = tmp_path / "TODO.md"
    store = TodoStore(markdown_path=todo_file)
    store.write([
        {"id": "a", "content": "Angebot prüfen", "status": "pending"},
        {"id": "b", "content": "Rechnung senden", "status": "in_progress"},
    ])
    raw = todo_file.read_text(encoding="utf-8")
    assert "hermes:id=a" in raw  # metadata really is in the file...

    monkeypatch.setenv("AIWERK_CUI_TODO_PATH", str(todo_file))
    summary = web_server._todo_summary({})
    texts = [item["text"] for item in summary["items"]]

    assert texts == ["Angebot prüfen", "Rechnung senden"]
    assert all("<!--" not in t and "hermes:id" not in t and "status=" not in t for t in texts)


class TestRequestLooksLocalSpoofing:
    """_request_looks_local must not trust client-supplied forwarding headers."""

    class _Client:
        host = "127.0.0.1"

    class _Req:
        def __init__(self, headers):
            self.headers = headers
            self.client = TestRequestLooksLocalSpoofing._Client()

    def test_direct_loopback_is_local(self):
        import hermes_cli.web_server as web_server

        assert web_server._request_looks_local(self._Req({"host": "127.0.0.1"})) is True

    def test_spoofed_x_forwarded_for_is_not_local(self):
        import hermes_cli.web_server as web_server

        req = self._Req({"host": "127.0.0.1", "x-forwarded-for": "127.0.0.1"})
        assert web_server._request_looks_local(req) is False

    def test_spoofed_x_real_ip_is_not_local(self):
        import hermes_cli.web_server as web_server

        req = self._Req({"host": "127.0.0.1", "x-real-ip": "127.0.0.1"})
        assert web_server._request_looks_local(req) is False

    def test_remote_peer_is_not_local(self):
        import hermes_cli.web_server as web_server

        req = self._Req({"host": "127.0.0.1"})
        req.client = type("C", (), {"host": "203.0.113.7"})()
        assert web_server._request_looks_local(req) is False


class TestAssistantWsGate:
    """Regression tests for the /api/ws assistant-mode confinement
    (_assistant_ws_request_gate). The WebSocket gateway bypasses the HTTP
    auth_middleware, so this predicate is the only thing stopping a confined
    customer from calling admin RPCs — shell.exec / cli.exec / config.set
    model=... / config.get full / slash.exec /config — straight through
    tui_gateway.server.dispatch.
    """

    def _gate(self):
        import hermes_cli.web_server as web_server

        return web_server._assistant_ws_request_gate

    def test_allows_chat_methods(self):
        gate = self._gate()
        for method in [
            "session.create", "session.resume", "session.title", "session.notes",
            "session.usage", "session.steer", "session.side.start",
            "session.side.back", "prompt.submit", "approval.respond",
        ]:
            assert gate({"id": 1, "method": method, "params": {}}) is None, method

    def test_blocks_admin_methods(self):
        gate = self._gate()
        for method in [
            "shell.exec", "cli.exec", "reload.env", "reload.mcp", "skills.manage",
            "skills.reload", "cron.manage", "browser.manage", "model.save_key",
            "config.show", "process.stop", "rollback.restore", "command.dispatch",
            "session.delete", "tools.configure",
        ]:
            reason = gate({"id": 1, "method": method, "params": {}})
            assert reason is not None and "assistant mode" in reason, method

    def test_shell_exec_is_refused(self):
        gate = self._gate()
        reason = gate(
            {"method": "shell.exec", "params": {"command": "id; cat ~/.hermes/.env"}}
        )
        assert reason is not None

    def test_config_key_allowlist(self):
        gate = self._gate()
        for key in ["busy", "reasoning", "fast", "yolo"]:
            assert gate({"method": "config.get", "params": {"key": key}}) is None
            assert gate(
                {"method": "config.set", "params": {"key": key, "value": "on"}}
            ) is None
        # Powerful keys the SPA never touches must be refused on both verbs.
        for key in ["model", "full", "prompt", "provider", "profile", "project", ""]:
            assert gate({"method": "config.get", "params": {"key": key}}) is not None, key
            assert gate(
                {"method": "config.set", "params": {"key": key, "value": "x"}}
            ) is not None, key

    def test_config_get_full_dump_is_refused(self):
        # config.get key="full" returns the entire config.yaml incl. API keys.
        gate = self._gate()
        assert gate({"method": "config.get", "params": {"key": "full"}}) is not None

    def test_slash_exec_command_allowlist(self):
        gate = self._gate()
        for cmd in ["compress", "reload-mcp", "stop", "/compress", "/reload-mcp", "/stop"]:
            assert gate({"method": "slash.exec", "params": {"command": cmd}}) is None, cmd
        for cmd in ["config", "/config set model x", "model", "shell", "snapshot restore", ""]:
            assert gate(
                {"method": "slash.exec", "params": {"command": cmd}}
            ) is not None, cmd

    def test_unknown_and_malformed_requests(self):
        gate = self._gate()
        assert gate({"method": "totally.bogus", "params": {}}) is not None
        # Malformed frames fall through to dispatch's own JSON-RPC validation.
        assert gate("not-a-dict") is None
        assert gate({"params": {}}) is None
        assert gate({"method": "", "params": {}}) is None


class TestAssistantWsGateWiring:
    """Prove handle_ws actually applies the injected gate *before* dispatch,
    and that admin mode (gate=None) leaves the full method table reachable.
    """

    class _FakeWS:
        def __init__(self, frames):
            self._frames = list(frames)
            self.sent = []
            self.client = type("C", (), {"host": "127.0.0.1", "port": 5})()

        async def accept(self):
            return None

        async def receive_text(self):
            if self._frames:
                return self._frames.pop(0)
            import tui_gateway.ws as wsmod

            raise wsmod._WebSocketDisconnect(1000)

        async def send_text(self, line):
            self.sent.append(line)

        def close(self):
            return None

    def _run(self, frames, gate, monkeypatch):
        import asyncio
        import tui_gateway.ws as wsmod

        dispatched = []

        def fake_dispatch(req, transport=None):
            dispatched.append(req.get("method"))
            return {"jsonrpc": "2.0", "id": req.get("id"), "result": {"ok": True}}

        monkeypatch.setattr(wsmod.server, "dispatch", fake_dispatch)
        ws = self._FakeWS(frames)
        asyncio.run(wsmod.handle_ws(ws, request_gate=gate))
        return ws, dispatched

    def test_refused_request_never_reaches_dispatch(self, monkeypatch):
        import hermes_cli.web_server as web_server

        frames = [json.dumps({"jsonrpc": "2.0", "id": 9, "method": "shell.exec",
                              "params": {"command": "id"}})]
        ws, dispatched = self._run(
            frames, web_server._assistant_ws_request_gate, monkeypatch
        )
        assert dispatched == []  # shell.exec is refused before dispatch
        errors = [json.loads(s) for s in ws.sent if '"error"' in s]
        assert any(
            e.get("error", {}).get("code") == -32601 and e.get("id") == 9
            for e in errors
        )

    def test_allowed_request_reaches_dispatch(self, monkeypatch):
        import hermes_cli.web_server as web_server

        frames = [json.dumps({"jsonrpc": "2.0", "id": 3, "method": "session.usage",
                              "params": {"session_id": "s1"}})]
        _ws, dispatched = self._run(
            frames, web_server._assistant_ws_request_gate, monkeypatch
        )
        assert dispatched == ["session.usage"]

    def test_no_gate_admin_mode_allows_shell_exec(self, monkeypatch):
        # gateway_ws passes request_gate=None in admin mode — nothing filtered.
        frames = [json.dumps({"jsonrpc": "2.0", "id": 7, "method": "shell.exec",
                              "params": {"command": "id"}})]
        _ws, dispatched = self._run(frames, None, monkeypatch)
        assert dispatched == ["shell.exec"]

