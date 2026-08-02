"""Tests for hermes_cli.web_server and related config utilities."""

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

    # The dashboard now gates user-plugin asset serving + backend import
    # behind the ``plugins.enabled`` allow-list (GHSA-mcfc-hp25-cjv7).
    # An installed-but-not-enabled user plugin has its API mount skipped
    # and its assets 404'd — which is the whole point of the gate. These
    # fixtures exist to exercise the *serving* paths, so opt the example
    # plugin in exactly as a real operator would with `hermes plugins
    # enable example`.
    from hermes_cli.config import load_config, save_config
    _cfg = load_config()
    _plugins_cfg = _cfg.setdefault("plugins", {})
    _enabled = _plugins_cfg.get("enabled")
    if not isinstance(_enabled, list):
        _enabled = []
    if "example" not in _enabled:
        _enabled.append("example")
    _plugins_cfg["enabled"] = _enabled
    save_config(_cfg)

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
        import hermes_cli.web_server as ws

        original_app = ws.app
        original_token = ws._SESSION_TOKEN
        monkeypatch.setenv("HERMES_DASHBOARD_SESSION_TOKEN", "desktop-seeded-token")
        assert ws._resolve_session_token() == "desktop-seeded-token"
        # No module reload: the loaded app and its adopted token are untouched.
        assert ws.app is original_app
        assert ws._SESSION_TOKEN == original_token

    def test_falls_back_to_random_token(self, monkeypatch):
        import hermes_cli.web_server as ws

        monkeypatch.delenv("HERMES_DASHBOARD_SESSION_TOKEN", raising=False)
        with patch.object(
            ws.secrets, "token_urlsafe", return_value="generated-token"
        ) as token_urlsafe:
            assert ws._resolve_session_token() == "generated-token"
        token_urlsafe.assert_called_once_with(32)

    def test_session_token_resolution_preserves_loaded_app_auth(self, monkeypatch):
        import hermes_cli.web_server as ws
        from starlette.testclient import TestClient

        original_app = ws.app
        original_header_name = ws._SESSION_HEADER_NAME
        original_token = ws._SESSION_TOKEN
        monkeypatch.setenv("HERMES_DASHBOARD_SESSION_TOKEN", "desktop-seeded-token")
        assert ws._resolve_session_token() == "desktop-seeded-token"
        monkeypatch.delenv("HERMES_DASHBOARD_SESSION_TOKEN", raising=False)
        with patch.object(ws.secrets, "token_urlsafe", return_value="generated-token"):
            assert ws._resolve_session_token() == "generated-token"

        client = TestClient(original_app)
        client.headers[original_header_name] = original_token
        assert client.get("/api/__session_token_probe").status_code == 404
        assert ws.app is original_app
        assert ws._SESSION_HEADER_NAME == original_header_name
        assert ws._SESSION_TOKEN == original_token


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
            "User's name is Owner.\n"
            "golem is Owner's AIWerk test base agent.\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(ws, "get_hermes_home", lambda: home)

        assert ws._assistant_user_display_name() == "Owner"

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
        import hermes_cli.web_server as web_server

        resp = self.client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "version" in data
        assert "hermes_home" in data
        assert "active_sessions" in data
        assert data["can_update_hermes"] is (not web_server._dashboard_local_update_managed_externally())




    def test_messaging_platforms_profile_scopes_gateway_reads(self, monkeypatch):
        """?profile=<name> must resolve liveness from the profile's own home.

        The gateway status readers resolve process-level paths and ignore the
        HERMES_HOME contextvar override (#56986), so /api/messaging/platforms
        has to pass the profile directory explicitly — otherwise it reports a
        DIFFERENT profile's gateway as this profile's, which hides a real
        outage behind a false "connected" (issue #71211).
        """
        import hermes_cli.web_server as web_server
        from hermes_cli import profiles as profiles_mod

        worker_home = profiles_mod.get_profile_dir("worker")
        worker_home.mkdir(parents=True)

        seen = {}

        def _pid(pid_path=None, **kw):
            seen["pid_path"] = pid_path
            return None

        def _runtime(path=None):
            seen["status_path"] = path
            return None

        def _runtime_pid(runtime=None, *, expected_home=None):
            seen["expected_home"] = expected_home
            return None

        monkeypatch.setattr(web_server, "get_running_pid_cached", _pid)
        monkeypatch.setattr(web_server, "get_running_pid", _pid)
        monkeypatch.setattr(web_server, "read_runtime_status", _runtime)
        monkeypatch.setattr(web_server, "get_runtime_status_running_pid", _runtime_pid)
        monkeypatch.setattr(web_server, "_GATEWAY_HEALTH_URL", None)

        resp = self.client.get("/api/messaging/platforms?profile=worker")

        assert resp.status_code == 200
        assert seen["pid_path"] == worker_home / "gateway.pid"
        assert seen["status_path"] == worker_home / "gateway_state.json"
        assert seen["expected_home"] == worker_home




    def test_gateway_drain_bad_action_400(self):
        resp = self.client.post("/api/gateway/drain", json={"action": "explode"})
        assert resp.status_code == 400




    @staticmethod
    def _provider_field_map(payload):
        return {field["key"]: field for field in payload["fields"]}






    def test_declared_surface_put_writes_config_and_secret(self):
        from hermes_constants import get_hermes_home
        from hermes_cli.config import load_env

        resp = self.client.put(
            "/api/memory/providers/hindsight/config?surface=declared",
            json={
                "values": {
                    "mode": "local_external",
                    "api_url": "http://localhost:8888",
                    "api_key": "hs-declared-key",
                }
            },
        )

        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        assert load_env()["HINDSIGHT_API_KEY"] == "hs-declared-key"

        config_path = get_hermes_home() / "hindsight" / "config.json"
        provider_config = json.loads(config_path.read_text(encoding="utf-8"))
        assert provider_config["mode"] == "local_external"
        assert provider_config["api_url"] == "http://localhost:8888"
        assert "api_key" not in provider_config






    def test_post_memory_provider_setup_routes_pip_through_lazy_deps(self, monkeypatch):
        """NS-605: dashboard pip installs must use the environment-aware
        lazy_deps pipeline (durable-target redirect on immutable hosted
        images), never a direct `pip install --python sys.executable`."""
        import subprocess as _subprocess

        import hermes_cli.web_server as web_server
        from tools import lazy_deps as ld

        # honcho declares pip_dependencies: [honcho-ai]; force it missing.
        monkeypatch.setattr(web_server, "_dependency_importable", lambda dep: False)

        installed = []

        def fake_install_specs(specs, *, timeout=300):
            installed.append(tuple(specs))
            return ld.InstallSpecsResult(
                ok=True, command="uv pip install --target /opt/data/lazy-packages honcho-ai",
                stdout="ok", stderr="",
            )

        monkeypatch.setattr(ld, "install_specs", fake_install_specs)

        # Any direct pip/uv subprocess from the memory-provider pip path is
        # a regression; external-dep checks may still run subprocess, so only
        # trip on pip-flavored commands.
        real_run = _subprocess.run

        def guarded_run(command, **kwargs):
            flat = command if isinstance(command, str) else " ".join(map(str, command))
            assert "pip install" not in flat, f"direct pip call leaked: {flat}"
            return real_run(command, **kwargs)

        monkeypatch.setattr(web_server.subprocess, "run", guarded_run)

        resp = self.client.post("/api/memory/providers/honcho/setup", json={"values": {}})

        assert resp.status_code == 200
        data = resp.json()
        pip_rows = [row for row in data["results"] if row["kind"] == "pip"]
        assert pip_rows and pip_rows[0]["status"] == "installed"
        assert "--target /opt/data/lazy-packages" in pip_rows[0]["command"]
        assert installed == [("honcho-ai",)]





    def test_put_memory_provider_config_writes_config_and_secret(self):
        from hermes_constants import get_hermes_home
        from hermes_cli.config import load_config, load_env

        resp = self.client.put(
            "/api/memory/providers/hindsight/config",
            json={
                "values": {
                    "mode": "local_external",
                    "api_url": "http://localhost:8888",
                    "api_key": "hs-test-key",
                    "bank_id": "ben-bank",
                    "recall_budget": "high",
                }
            },
        )

        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "active": "hindsight"}
        assert load_config()["memory"]["provider"] == "hindsight"
        assert load_env()["HINDSIGHT_API_KEY"] == "hs-test-key"

        config_path = get_hermes_home() / "hindsight" / "config.json"
        provider_config = json.loads(config_path.read_text(encoding="utf-8"))
        assert provider_config["mode"] == "local_external"
        assert provider_config["api_url"] == "http://localhost:8888"
        assert provider_config["bank_id"] == "ben-bank"
        assert provider_config["recall_budget"] == "high"
        assert "api_key" not in provider_config


    def test_get_memory_provider_config_does_not_return_secret(self):
        self.client.put(
            "/api/memory/providers/hindsight/config",
            json={
                "values": {
                    "mode": "cloud",
                    "api_url": "https://api.hindsight.vectorize.io",
                    "api_key": "secret-value",
                    "bank_id": "hermes",
                    "recall_budget": "mid",
                }
            },
        )

        resp = self.client.get("/api/memory/providers/hindsight/config")

        assert resp.status_code == 200
        data = resp.json()
        fields = self._provider_field_map(data)
        assert fields["api_key"]["is_set"] is True
        assert fields["api_key"]["value"] == ""
        assert "secret-value" not in json.dumps(data)




    # ── Memory provider config (Honcho host-block backend) ──────────────

    @pytest.fixture(autouse=True)
    def _isolate_honcho_config(self):
        # Honcho tests write the suite-wide HERMES_HOME honcho.json; snapshot and
        # restore it so provider status/config state never leaks across tests.
        from hermes_constants import get_hermes_home

        path = get_hermes_home() / "honcho.json"
        before = path.read_bytes() if path.exists() else None
        yield
        if before is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(before)

    @staticmethod
    def _seed_local_honcho(cfg=None):
        from hermes_constants import get_hermes_home

        path = get_hermes_home() / "honcho.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cfg if cfg is not None else {}), encoding="utf-8")
        return path


    def test_put_honcho_writes_host_block_root_and_secret(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("HONCHO_API_KEY", "guard")
        monkeypatch.delenv("HONCHO_API_KEY")
        self._seed_local_honcho()
        from hermes_constants import get_hermes_home
        from hermes_cli.config import load_config, load_env

        resp = self.client.put(
            "/api/memory/providers/honcho/config?surface=declared",
            json={
                "values": {
                    "apiKey": "hch-test-key",
                    "baseUrl": "https://honcho.example.dev",
                    "environment": "local",
                    "workspace": "myws",
                    "peerName": "eri",
                    "aiPeer": "hermes",
                    "sessionStrategy": "per-repo",
                }
            },
        )

        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        assert load_config()["memory"]["provider"] == "honcho"
        assert load_env()["HONCHO_API_KEY"] == "hch-test-key"

        cfg = json.loads((get_hermes_home() / "honcho.json").read_text(encoding="utf-8"))
        # baseUrl is root-scoped; the rest live in the active host block.
        assert cfg["baseUrl"] == "https://honcho.example.dev"
        assert cfg["hosts"]["hermes"]["workspace"] == "myws"
        assert cfg["hosts"]["hermes"]["peerName"] == "eri"
        assert cfg["hosts"]["hermes"]["environment"] == "local"
        assert cfg["hosts"]["hermes"]["sessionStrategy"] == "per-repo"
        # The key lands where the client reads first; GET keeps it write-only.
        assert cfg["hosts"]["hermes"]["apiKey"] == "hch-test-key"


    def test_get_honcho_config_does_not_return_secret(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("HONCHO_API_KEY", "guard")
        monkeypatch.delenv("HONCHO_API_KEY")
        self._seed_local_honcho()

        self.client.put(
            "/api/memory/providers/honcho/config?surface=declared",
            json={"values": {"apiKey": "secret-value"}},
        )

        resp = self.client.get("/api/memory/providers/honcho/config?surface=declared")

        assert resp.status_code == 200
        data = resp.json()
        fields = self._provider_field_map(data)
        assert fields["apiKey"]["is_set"] is True
        assert fields["apiKey"]["value"] == ""
        assert "secret-value" not in json.dumps(data)





    # ── GET /api/media (remote image display) ───────────────────────────





    def test_get_media_requires_auth(self):
        from hermes_cli.web_server import _SESSION_HEADER_NAME

        resp = self.client.get(
            "/api/media",
            params={"path": "/tmp/x.png"},
            headers={_SESSION_HEADER_NAME: "wrong-token"},
        )
        assert resp.status_code == 401

    # ── POST /api/chat/image-upload (browser clipboard/drop images) ─────





    # ── Dashboard font override ─────────────────────────────────────────




    def test_cui_recents_hide_health_check_probe_sessions(self):
        """Assistant left-rail recents must not show automated health probes."""
        from hermes_state import SessionDB

        db = SessionDB()
        try:
            db.create_session(session_id="real-chat", source="cli")
            db.append_message("real-chat", role="user", content="Please help with the offer")
            db.append_message("real-chat", role="assistant", content="Sure")
            db.create_session(session_id="health-probe", source="cli")
            db.append_message("health-probe", role="user", content="Health check: reply exactly OK")
            db.append_message("health-probe", role="assistant", content="OK")
        finally:
            db.close()

        resp = self.client.get("/api/sessions?limit=20&offset=0&hide_automated=1")

        assert resp.status_code == 200
        ids = [s["id"] for s in resp.json()["sessions"]]
        assert "real-chat" in ids
        assert "health-probe" not in ids

    def test_cui_actor_sessions_are_scoped_to_same_actor_for_user_and_admin(self, monkeypatch):
        """CUI recents are per authenticated actor, not shared per tenant/role."""
        import hermes_cli.web_server as ws
        from hermes_state import SessionDB

        tenant = "tenant-1"
        user_actor = {"tenant_id": tenant, "actor_id": "user-1", "role": "user"}
        admin_actor = {"tenant_id": tenant, "actor_id": "admin-1", "role": "admin"}

        def cfg(actor):
            return {
                "_cui_visibility_scope": "customer" if actor["role"] == "user" else "admin",
                "_cui_actor_role": actor["role"],
                "_cui_actor_id": actor["actor_id"],
                "_cui_tenant_id": actor["tenant_id"],
            }

        db = SessionDB()
        try:
            db.create_session(session_id="user-chat", source="cli", model_config=cfg(user_actor))
            db.append_message("user-chat", role="user", content="customer question")
            db.create_session(session_id="admin-chat", source="cli", model_config=cfg(admin_actor))
            db.append_message("admin-chat", role="user", content="admin operation")
        finally:
            db.close()

        monkeypatch.setattr(ws, "_cui_actor_context_from_request", lambda request: user_actor)
        user_resp = self.client.get("/api/sessions?limit=20&offset=0&hide_automated=1")
        assert user_resp.status_code == 200
        user_ids = [s["id"] for s in user_resp.json()["sessions"]]
        assert "user-chat" in user_ids
        assert "admin-chat" not in user_ids

        monkeypatch.setattr(ws, "_cui_actor_context_from_request", lambda request: admin_actor)
        admin_resp = self.client.get("/api/sessions?limit=20&offset=0&hide_automated=1")
        assert admin_resp.status_code == 200
        admin_ids = [s["id"] for s in admin_resp.json()["sessions"]]
        assert "admin-chat" in admin_ids
        assert "user-chat" not in admin_ids

    def _create_session_with_heavy_fields(self, session_id: str) -> None:
        from hermes_state import SessionDB

        db = SessionDB()
        try:
            db.create_session(
                session_id=session_id,
                source="cli",
                system_prompt="# SOUL.md\n" + ("prompt body " * 500),
                model_config={"temperature": 0.7, "notes": "x" * 200},
            )
        finally:
            db.close()

    def test_get_sessions_strips_heavy_fields_by_default(self):
        """List rows must omit system_prompt/model_config by default."""
        self._create_session_with_heavy_fields("lean-list-row")

        resp = self.client.get("/api/sessions?limit=20&offset=0")
        assert resp.status_code == 200
        rows = [s for s in resp.json()["sessions"] if s["id"] == "lean-list-row"]
        assert rows, "created session missing from list"
        row = rows[0]
        assert "system_prompt" not in row
        assert "model_config" not in row
        for key in ("id", "source", "started_at", "message_count", "is_active"):
            assert key in row

    def test_get_sessions_full_param_keeps_heavy_fields(self):
        """?full=1 is the escape hatch for complete rows."""
        self._create_session_with_heavy_fields("full-list-row")

        resp = self.client.get("/api/sessions?limit=20&offset=0&full=1")
        assert resp.status_code == 200
        rows = [s for s in resp.json()["sessions"] if s["id"] == "full-list-row"]
        assert rows, "created session missing from list"
        row = rows[0]
        assert row["system_prompt"].startswith("# SOUL.md")
        assert "temperature" in (row["model_config"] or "")

    def test_profiles_sessions_strips_heavy_fields_by_default(self):
        """The cross-profile aggregate applies the same list projection."""
        self._create_session_with_heavy_fields("lean-profiles-row")

        resp = self.client.get("/api/profiles/sessions?limit=20&offset=0")
        assert resp.status_code == 200
        rows = [s for s in resp.json()["sessions"] if s["id"] == "lean-profiles-row"]
        assert rows, "created session missing from profiles list"
        row = rows[0]
        assert "system_prompt" not in row
        assert "model_config" not in row
        assert row["profile"] == "default"

        full = self.client.get("/api/profiles/sessions?limit=20&offset=0&full=1")
        assert full.status_code == 200
        full_rows = [s for s in full.json()["sessions"] if s["id"] == "lean-profiles-row"]
        assert full_rows and full_rows[0]["system_prompt"].startswith("# SOUL.md")

    def test_profiles_session_lists_filter_before_count_pagination_and_full(self, monkeypatch):
        """Cross-profile list projections must use the canonical actor predicate."""
        import hermes_cli.web_server as ws
        from hermes_state import SessionDB

        actor = {"tenant_id": "tenant-1", "actor_id": "user-1", "role": "user"}

        def actor_cfg(actor_id):
            return {
                "_cui_visibility_scope": "customer",
                "_cui_actor_role": "user",
                "_cui_actor_id": actor_id,
                "_cui_tenant_id": "tenant-1",
            }

        db = SessionDB()
        try:
            db.create_session(session_id="profiles-owned", source="cli", model_config=actor_cfg("user-1"))
            db.append_message("profiles-owned", role="user", content="owned")
            db.create_session(session_id="profiles-foreign", source="cli", model_config=actor_cfg("user-2"))
            db.append_message("profiles-foreign", role="user", content="foreign")
        finally:
            db.close()

        monkeypatch.setattr(ws, "_cui_actor_context_from_request", lambda request: actor)
        for suffix in ("", "&full=1"):
            resp = self.client.get(f"/api/profiles/sessions?limit=1&offset=0{suffix}")
            assert resp.status_code == 200
            data = resp.json()
            assert data["total"] == 1
            assert data["profile_totals"]["default"] == 1
            assert [row["id"] for row in data["sessions"]] == ["profiles-owned"]

        db = SessionDB()
        try:
            db.create_session(
                session_id="profiles-owned-cron",
                source="cron",
                model_config=actor_cfg("user-1"),
            )
            db.append_message("profiles-owned-cron", role="user", content="owned cron")
            db.create_session(
                session_id="profiles-foreign-cron",
                source="cron",
                model_config=actor_cfg("user-2"),
            )
            db.append_message("profiles-foreign-cron", role="user", content="foreign cron")
        finally:
            db.close()

        filtered = self.client.get(
            "/api/profiles/sessions?limit=20&offset=0&sources=cron"
        ).json()
        assert filtered["total"] == 1
        assert filtered["profile_totals"]["default"] == 1
        assert [row["id"] for row in filtered["sessions"]] == ["profiles-owned-cron"]

        actor.update({"actor_id": "admin-1", "role": "admin"})
        admin = self.client.get("/api/profiles/sessions?limit=20&offset=0&full=1").json()
        assert "profiles-foreign" not in {row["id"] for row in admin["sessions"]}

        monkeypatch.setattr(ws, "_cui_actor_context_from_request", lambda request: {"role": "user", "_restricted": "1"})
        restricted = self.client.get("/api/profiles/sessions?limit=20&offset=0&full=1").json()
        assert restricted["sessions"] == []
        assert restricted["total"] == 0

    def test_profiles_sidebar_filters_every_slice_and_fails_closed(self, monkeypatch):
        import hermes_cli.web_server as ws
        from hermes_state import SessionDB

        def cfg(actor_id):
            return {
                "_cui_visibility_scope": "customer",
                "_cui_actor_role": "user",
                "_cui_actor_id": actor_id,
                "_cui_tenant_id": "tenant-1",
            }

        db = SessionDB()
        try:
            for sid, source, owner in (
                ("sidebar-owned", "cli", "user-1"),
                ("sidebar-foreign", "cli", "user-2"),
                ("cron-owned", "cron", "user-1"),
                ("cron-foreign", "cron", "user-2"),
            ):
                db.create_session(session_id=sid, source=source, model_config=cfg(owner))
                db.append_message(sid, role="user", content=sid)
        finally:
            db.close()

        actor = {"tenant_id": "tenant-1", "actor_id": "user-1", "role": "user"}
        monkeypatch.setattr(ws, "_cui_actor_context_from_request", lambda request: actor)
        data = self.client.get("/api/profiles/sessions/sidebar").json()
        all_ids = {
            row["id"]
            for section in ("recents", "cron", "messaging")
            for row in data[section]["sessions"]
        }
        assert {"sidebar-owned", "cron-owned"} <= all_ids
        assert "sidebar-foreign" not in all_ids
        assert "cron-foreign" not in all_ids
        assert data["recents"]["total"] == 2

        monkeypatch.setattr(ws, "_cui_actor_context_from_request", lambda request: {"role": "user", "_restricted": "1"})
        restricted = self.client.get("/api/profiles/sessions/sidebar").json()
        assert all(not restricted[name]["sessions"] for name in ("recents", "cron", "messaging"))
        assert restricted["recents"]["total"] == 0

    def test_session_mutations_hide_foreign_targets_and_bulk_delete_is_atomic(self, monkeypatch):
        import hermes_cli.web_server as ws
        from hermes_state import SessionDB

        actor = {"tenant_id": "tenant-1", "actor_id": "user-1", "role": "user"}

        def cfg(actor_id):
            return {
                "_cui_visibility_scope": "customer",
                "_cui_actor_role": "user",
                "_cui_actor_id": actor_id,
                "_cui_tenant_id": "tenant-1",
            }

        db = SessionDB()
        try:
            db.create_session(session_id="mutate-owned", source="cli", model_config=cfg("user-1"))
            db.create_session(session_id="mutate-foreign", source="cli", model_config=cfg("user-2"))
        finally:
            db.close()
        monkeypatch.setattr(ws, "_cui_actor_context_from_request", lambda request: actor)

        assert self.client.delete("/api/sessions/mutate-foreign").status_code == 404
        for payload in ({"title": "stolen"}, {"archived": True}, {"pinned": True}):
            assert self.client.patch("/api/sessions/mutate-foreign", json=payload).status_code == 404

        bulk = self.client.post(
            "/api/sessions/bulk-delete",
            json={"ids": ["mutate-owned", "mutate-foreign"]},
        )
        assert bulk.status_code == 404
        db = SessionDB()
        try:
            assert db.get_session("mutate-owned") is not None
            foreign = db.get_session("mutate-foreign")
            assert foreign is not None
            assert not foreign.get("title") and not foreign.get("archived") and not foreign.get("pinned")
        finally:
            db.close()

    def test_session_sibling_routes_confine_reads_and_authorize_sets_before_delete(self, monkeypatch):
        import hermes_cli.web_server as ws
        from hermes_state import SessionDB

        actor = {"tenant_id": "tenant-1", "actor_id": "user-1", "role": "user"}

        def cfg(actor_id):
            return {
                "_cui_visibility_scope": "customer",
                "_cui_actor_role": "user",
                "_cui_actor_id": actor_id,
                "_cui_tenant_id": "tenant-1",
            }

        db = SessionDB()
        try:
            for sid, owner in (("route-owned", "user-1"), ("route-foreign", "user-2")):
                db.create_session(session_id=sid, source="cli", model_config=cfg(owner))
                db.append_message(sid, role="user", content=sid)
                db.end_session(sid, end_reason="done")
            for sid, owner in (("empty-owned", "user-1"), ("empty-foreign", "user-2")):
                db.create_session(session_id=sid, source="cli", model_config=cfg(owner))
                db.end_session(sid, end_reason="done")
        finally:
            db.close()

        monkeypatch.setattr(ws, "_cui_actor_context_from_request", lambda request: actor)

        assert self.client.get("/api/sessions/route-foreign/export").status_code == 404
        stats = self.client.get("/api/sessions/stats").json()
        assert stats["total"] == 2
        assert stats["messages"] == 1
        assert stats["by_source"] == {"cli": 2}
        assert self.client.get("/api/sessions/empty/count").json() == {"count": 1}

        dry_run = self.client.post(
            "/api/sessions/prune", json={"started_before": 9999999999, "dry_run": True}
        )
        assert dry_run.status_code == 200
        assert {row["id"] for row in dry_run.json()["sessions"]} == {
            "route-owned", "empty-owned"
        }

        assert self.client.delete("/api/sessions/empty").status_code == 404
        assert self.client.post(
            "/api/sessions/prune", json={"started_before": 9999999999}
        ).status_code == 404
        db = SessionDB()
        try:
            for sid in ("route-owned", "route-foreign", "empty-owned", "empty-foreign"):
                assert db.get_session(sid) is not None
        finally:
            db.close()

    def test_session_import_is_actor_scoped_and_cross_profile_is_admin_only(self, monkeypatch):
        import hermes_cli.web_server as ws
        from hermes_state import SessionDB

        actor = {"tenant_id": "tenant-1", "actor_id": "user-1", "role": "user"}
        monkeypatch.setattr(ws, "_cui_actor_context_from_request", lambda request: actor)

        def payload(sid, owner):
            return {
                "id": sid,
                "source": "cli",
                "model_config": {
                    "_cui_visibility_scope": "customer",
                    "_cui_actor_role": "user",
                    "_cui_actor_id": owner,
                    "_cui_tenant_id": "tenant-1",
                },
                "messages": [],
            }

        foreign = self.client.post(
            "/api/sessions/import",
            json={"sessions": [payload("import-owned", "user-1"), payload("import-foreign", "user-2")]},
        )
        assert foreign.status_code == 404
        db = SessionDB()
        try:
            assert db.get_session("import-owned") is None
            assert db.get_session("import-foreign") is None
        finally:
            db.close()

        cross_profile = self.client.post(
            "/api/sessions/import",
            json={"profile": "worker", "sessions": [payload("worker-owned", "user-1")]},
        )
        assert cross_profile.status_code == 403

    def test_actor_filtered_session_scan_pages_until_exhaustion(self):
        import hermes_cli.web_server as ws

        class FakeDB:
            def __init__(self):
                self.offsets = []

            def list_sessions_rich(self, *, limit, offset, **kwargs):
                self.offsets.append(offset)
                if offset >= 10001:
                    return []
                stop = min(offset + limit, 10001)
                return [{"id": f"s{i}"} for i in range(offset, stop)]

        db = FakeDB()
        rows = ws._list_sessions_rich_all(db, compact_rows=False)

        assert len(rows) == 10001
        assert db.offsets[-1] == 10000
        assert len(db.offsets) > 1


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

    def test_import_sessions_endpoint_imports_exported_json(self):
        from hermes_state import SessionDB

        payload = {
            "id": "imported-web-session",
            "source": "cli",
            "title": "Imported from dashboard",
            "started_at": 100.0,
            "ended_at": 110.0,
            "end_reason": "complete",
            "messages": [
                {"role": "user", "content": "hello", "timestamp": 101.0},
                {"role": "assistant", "content": "hi", "timestamp": 102.0},
            ],
        }

        resp = self.client.post("/api/sessions/import", json={"sessions": [payload]})
        assert resp.status_code == 200
        data = resp.json()
        assert data["imported"] == 1
        assert data["skipped"] == 0

        db = SessionDB()
        try:
            session = db.get_session("imported-web-session")
            assert session["title"] == "Imported from dashboard"
            assert session["message_count"] == 2
            assert [m["content"] for m in db.get_messages("imported-web-session")] == [
                "hello",
                "hi",
            ]
        finally:
            db.close()

        duplicate = self.client.post("/api/sessions/import", json={"sessions": [payload]})
        assert duplicate.status_code == 200
        assert duplicate.json()["skipped_ids"] == ["imported-web-session"]

        invalid = self.client.post(
            "/api/sessions/import",
            json={"sessions": [{"source": "cli", "messages": []}]},
        )
        assert invalid.status_code == 400
        assert invalid.json()["detail"]["errors"] == [
            {"index": 0, "error": "session id is required"}
        ]

    def test_archive_session_via_patch(self):
        """PATCH archived=true soft-hides a session; false restores it."""
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

    def test_patch_session_pins_and_exempts_from_auto_archive(self):
        """PATCH pinned=true sets the keep flag; a pinned stale session is
        spared by the auto-archive sweep while an unpinned one is hidden."""
        import time as _time

        from hermes_state import SessionDB

        old = _time.time() - 30 * 86400
        db = SessionDB()
        try:
            for sid in ("keep-me", "drop-me"):
                db.create_session(session_id=sid, source="cli")
                db.append_message(session_id=sid, role="user", content="hi")
                db._conn.execute(
                    "UPDATE sessions SET started_at = ? WHERE id = ?", (old, sid)
                )
                db._conn.execute(
                    "UPDATE messages SET timestamp = ? WHERE session_id = ?", (old, sid)
                )
            db._conn.commit()
        finally:
            db.close()

        resp = self.client.patch("/api/sessions/keep-me", json={"pinned": True})
        assert resp.status_code == 200
        assert resp.json()["pinned"] is True

        db = SessionDB()
        try:
            archived = db.archive_stale_sessions(3)
        finally:
            db.close()

        assert archived == 1
        listed = self.client.get("/api/sessions").json()["sessions"]
        ids = {s["id"] for s in listed}
        assert "keep-me" in ids  # pinned -> spared
        assert "drop-me" not in ids  # unpinned + stale -> archived

    def test_list_triggers_config_gated_auto_archive(self):
        """With sessions.auto_archive on, listing sessions opportunistically
        sweeps stale ones (the Desktop `hermes serve` code path)."""
        import time as _time

        import hermes_cli.web_server as ws
        from hermes_state import SessionDB

        old = _time.time() - 30 * 86400
        db = SessionDB()
        try:
            db.create_session(session_id="stale-serve", source="cli")
            db.append_message(session_id="stale-serve", role="user", content="hi")
            db._conn.execute(
                "UPDATE sessions SET started_at = ? WHERE id = ?", (old, "stale-serve")
            )
            db._conn.execute(
                "UPDATE messages SET timestamp = ? WHERE session_id = ?",
                (old, "stale-serve"),
            )
            db._conn.commit()
        finally:
            db.close()

        # Reset the in-process throttle so the trigger actually evaluates config.
        ws._last_auto_archive_check.clear()

        # The helper imports load_config lazily from hermes_cli.config; patch there.
        cfg = {"sessions": {"auto_archive": True, "auto_archive_days": 3, "min_interval_hours": 0}}
        try:
            with patch("hermes_cli.config.load_config", return_value=cfg):
                listed = self.client.get("/api/sessions").json()["sessions"]
        finally:
            ws._last_auto_archive_check.clear()

        assert all(s["id"] != "stale-serve" for s in listed)

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

    def test_profiles_sessions_trusted_deep_page_is_not_capped_at_500(self):
        from hermes_state import SessionDB

        db = SessionDB()
        try:
            for i in range(520):
                db.create_session(session_id=f"deep-page-{i:03d}", source="deep-page")
        finally:
            db.close()

        resp = self.client.get(
            "/api/profiles/sessions?source=deep-page&min_messages=0"
            "&order=created&offset=510&limit=5"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 520
        assert len(data["sessions"]) == 5
        assert all(row["id"].startswith("deep-page-") for row in data["sessions"])

    def test_profiles_sessions_sidebar_batches_three_slices(self):
        """The batched sidebar endpoint returns recents/cron/messaging in one
        pass, each source-scoped by the caller-supplied excludes, so the desktop
        stops reopening every profile DB three times per refresh."""
        from hermes_state import SessionDB

        db = SessionDB()
        try:
            for sid, src in (
                ("sb-desktop", "desktop"),
                ("sb-cron", "cron"),
                ("sb-telegram", "telegram"),
            ):
                db.create_session(session_id=sid, source=src)
                db.append_message(session_id=sid, role="user", content="hi")
        finally:
            db.close()

        resp = self.client.get(
            "/api/profiles/sessions/sidebar"
            "?recents_profile=all&recents_limit=20&recents_exclude=cron,telegram"
            "&cron_limit=50&messaging_limit=100"
            "&messaging_exclude=cron,cli,codex,desktop,gateway,local,tui"
        )
        assert resp.status_code == 200
        data = resp.json()

        recents_ids = {s["id"] for s in data["recents"]["sessions"]}
        cron_ids = {s["id"] for s in data["cron"]["sessions"]}
        messaging_ids = {s["id"] for s in data["messaging"]["sessions"]}

        # Each session lands only in its own slice.
        assert "sb-desktop" in recents_ids
        assert "sb-desktop" not in cron_ids and "sb-desktop" not in messaging_ids
        assert "sb-cron" in cron_ids
        assert "sb-cron" not in recents_ids and "sb-cron" not in messaging_ids
        assert "sb-telegram" in messaging_ids
        assert "sb-telegram" not in recents_ids and "sb-telegram" not in cron_ids

        # Rows carry profile tagging like /api/profiles/sessions.
        row = next(s for s in data["recents"]["sessions"] if s["id"] == "sb-desktop")
        assert row["profile"] == "default"
        assert row["is_default_profile"] is True
        assert isinstance(data.get("errors"), list)
        # Pagination reports "was this window capped?" per profile, not an exact
        # COUNT(*) — one row against a 20-row cap means nothing more to load.
        assert data["recents"]["profiles_truncated"]["default"] is False

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

    def test_sessions_endpoint_reads_requested_profile(self):
        """The machine dashboard's global profile switcher must retarget
        the Sessions page, not just config/skills/model pages."""
        from hermes_state import SessionDB
        from hermes_cli import profiles as profiles_mod

        worker_home = profiles_mod.get_profile_dir("worker")
        worker_home.mkdir(parents=True)

        default_db = SessionDB()
        try:
            default_db.create_session(session_id="default-only", source="cli")
            default_db.append_message("default-only", role="user", content="default")
        finally:
            default_db.close()

        worker_db = SessionDB(db_path=worker_home / "state.db")
        try:
            worker_db.create_session(session_id="worker-only", source="cli")
            worker_db.append_message("worker-only", role="user", content="worker")
        finally:
            worker_db.close()

        resp = self.client.get("/api/sessions?profile=worker&limit=20&min_messages=0")
        assert resp.status_code == 200
        data = resp.json()
        ids = {s["id"] for s in data["sessions"]}
        assert "worker-only" in ids
        assert "default-only" not in ids
        row = next(s for s in data["sessions"] if s["id"] == "worker-only")
        assert row["profile"] == "worker"
        assert row["is_default_profile"] is False

        stats = self.client.get("/api/sessions/stats?profile=worker").json()
        assert stats["total"] == 1
        assert stats["messages"] == 1

        messages = self.client.get("/api/sessions/worker-only/messages?profile=worker").json()
        assert [m["content"] for m in messages["messages"]] == ["worker"]

    def test_latest_descendant_reads_requested_profile(self):
        """Chat resume must resolve compression tips in the chat profile DB."""
        from hermes_state import SessionDB
        from hermes_cli import profiles as profiles_mod

        worker_home = profiles_mod.get_profile_dir("worker")
        worker_home.mkdir(parents=True)

        default_db = SessionDB()
        try:
            default_db.create_session(session_id="shared-root", source="cli")
        finally:
            default_db.close()

        worker_db = SessionDB(db_path=worker_home / "state.db")
        try:
            worker_db.create_session(session_id="shared-root", source="cli")
            worker_db.create_session(
                session_id="worker-tip",
                source="cli",
                parent_session_id="shared-root",
            )
        finally:
            worker_db.close()

        default_resp = self.client.get("/api/sessions/shared-root/latest-descendant")
        assert default_resp.status_code == 200
        assert default_resp.json()["session_id"] == "shared-root"

        worker_resp = self.client.get(
            "/api/sessions/shared-root/latest-descendant?profile=worker"
        )
        assert worker_resp.status_code == 200
        assert worker_resp.json()["session_id"] == "worker-tip"

    def test_latest_descendant_survives_parent_cycle(self):
        """Regression for the #39140 CTE salvage: a corrupted parent chain
        that loops (a -> b -> a) must terminate (UNION dedup) instead of
        recursing forever like UNION ALL would."""
        from hermes_state import SessionDB

        db = SessionDB()
        try:
            db.create_session(session_id="cyc-a", source="cli")
            db.create_session(
                session_id="cyc-b", source="cli", parent_session_id="cyc-a"
            )
            db._conn.execute(
                "UPDATE sessions SET parent_session_id='cyc-b' WHERE id='cyc-a'"
            )
            db._conn.commit()
        finally:
            db.close()

        resp = self.client.get("/api/sessions/cyc-a/latest-descendant")
        assert resp.status_code == 200
        assert resp.json()["session_id"] == "cyc-b"














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

    def test_search_sessions_respects_source_filters_for_messages_and_ids(self):
        from hermes_state import SessionDB

        db = SessionDB()
        try:
            db.create_session(session_id="filter-chat-id", source="cli")
            db.append_message(
                session_id="filter-chat-id",
                role="user",
                content="filterneedle human question",
            )
            db.create_session(session_id="filter-cron-id", source="cron")
            db.append_message(
                session_id="filter-cron-id",
                role="user",
                content="filterneedle scheduled run",
            )
            db.create_session(session_id="idmatch-chat", source="cli")
            db.append_message(session_id="idmatch-chat", role="user", content="ordinary")
            db.create_session(session_id="idmatch-cron", source="cron")
            db.append_message(session_id="idmatch-cron", role="user", content="ordinary")
        finally:
            db.close()

        message_resp = self.client.get(
            "/api/sessions/search?q=filterneedle&exclude_sources=cron"
        )
        assert message_resp.status_code == 200
        message_results = message_resp.json()["results"]
        assert {r["session_id"] for r in message_results} == {"filter-chat-id"}
        assert message_results[0]["id"] == "filter-chat-id"
        assert "message_count" in message_results[0]

        id_resp = self.client.get(
            "/api/sessions/search?q=idmatch&exclude_sources=cron"
        )
        assert id_resp.status_code == 200
        assert {r["session_id"] for r in id_resp.json()["results"]} == {
            "idmatch-chat"
        }

        automation_resp = self.client.get(
            "/api/sessions/search?q=idmatch&sources=cron"
        )
        assert automation_resp.status_code == 200
        assert {r["session_id"] for r in automation_resp.json()["results"]} == {
            "idmatch-cron"
        }

    def test_get_session_messages_follows_compression_tip(self):
        """Reading a compressed session by its old id should hydrate from the
        live continuation, matching /resume behavior."""
        import time as _time

        from hermes_state import SessionDB

        db = SessionDB()
        try:
            db.create_session(session_id="desktop-root", source="cli")
            db.append_message(session_id="desktop-root", role="user", content="before compression")
            # Empty before closing: the closed-parent write guard refuses
            # durable writes to compression-ended sessions.
            db.replace_messages("desktop-root", [])
            db.end_session("desktop-root", "compression")
            now = _time.time()
            db._conn.execute(
                "UPDATE sessions SET started_at = ?, ended_at = ? WHERE id = ?",
                (now - 10, now - 5, "desktop-root"),
            )
            db.create_session(session_id="desktop-tip", source="cli", parent_session_id="desktop-root")
            db._conn.execute("UPDATE sessions SET started_at = ? WHERE id = ?", (now - 4, "desktop-tip"))
            db.append_message(session_id="desktop-tip", role="user", content="after compression")
            db._conn.commit()
        finally:
            db.close()

        resp = self.client.get("/api/sessions/desktop-root/messages")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["session_id"] == "desktop-tip"
        assert [m["content"] for m in payload["messages"]] == ["after compression"]

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

        def fake_transcribe_audio(path, model=None):
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

    def test_audio_transcription_no_speech_is_not_an_error(self, monkeypatch):
        """A provider hearing silence (empty transcript) must return 200/"" —
        the live voice loop treats it as a quiet turn and re-listens, instead
        of surfacing a 400 toast on every pause (the ElevenLabs empty-
        transcript spam)."""
        import tools.transcription_tools as transcription_tools

        monkeypatch.setattr(
            transcription_tools,
            "transcribe_audio",
            lambda path, model=None: {
                "success": False,
                "transcript": "",
                "error": "ElevenLabs STT returned empty transcript",
                "no_speech": True,
            },
        )

        resp = self.client.post(
            "/api/audio/transcribe",
            json={
                "data_url": "data:audio/webm;base64,aGVsbG8=",
                "mime_type": "audio/webm",
            },
        )

        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert resp.json()["transcript"] == ""

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

        # Bypass the managed-externally gate so we reach the docker install check.
        monkeypatch.setattr(web_server, "_dashboard_local_update_managed_externally", lambda: False)
        monkeypatch.setattr(web_server, "detect_install_method", lambda _root: "docker")
        monkeypatch.setattr(web_server, "_dashboard_local_update_managed_externally", lambda: False)
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

    def test_update_hermes_returns_nix_guidance_without_spawning(self, monkeypatch):
        import hermes_cli.web_server as web_server

        def fail_spawn(*_args, **_kwargs):
            raise AssertionError("Nix update guard should not spawn hermes update")

        monkeypatch.setattr(web_server, "_dashboard_local_update_managed_externally", lambda: False)
        monkeypatch.setattr(web_server, "detect_install_method", lambda _root: "nix")
        monkeypatch.setattr(web_server, "_spawn_hermes_action", fail_spawn)
        web_server._ACTION_PROCS.pop("hermes-update", None)
        web_server._ACTION_RESULTS.pop("hermes-update", None)

        resp = self.client.post("/api/hermes/update")

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert data["pid"] is None
        assert data["error"] == "nix_update_unsupported"
        assert "Nix" in data["message"]

    def test_update_hermes_returns_managed_runtime_guidance_without_spawning(self, monkeypatch):
        import hermes_cli.web_server as web_server

        spawned = False
        detected = False

        def fail_spawn(*_args, **_kwargs):
            nonlocal spawned
            spawned = True
            raise AssertionError("managed runtime update guard should not spawn hermes update")

        def fail_detect(*_args, **_kwargs):
            nonlocal detected
            detected = True
            raise AssertionError("managed runtime update guard should not detect install method")

        monkeypatch.setattr(web_server, "_dashboard_local_update_managed_externally", lambda: True)
        monkeypatch.setattr(web_server, "detect_install_method", fail_detect)
        monkeypatch.setattr(web_server, "_spawn_hermes_action", fail_spawn)
        web_server._ACTION_PROCS.pop("hermes-update", None)
        web_server._ACTION_RESULTS.pop("hermes-update", None)

        resp = self.client.post("/api/hermes/update")

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert data["name"] == "hermes-update"
        assert data["pid"] is None
        assert data["error"] == "dashboard_update_managed_externally"
        assert "managed outside this dashboard" in data["message"]
        assert spawned is False
        assert detected is False

        status = self.client.get("/api/actions/hermes-update/status")
        assert status.status_code == 200
        status_data = status.json()
        assert status_data["running"] is False
        assert status_data["exit_code"] == 1
        assert status_data["pid"] is None
        assert any("managed outside this dashboard" in line for line in status_data["lines"])

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
        monkeypatch.setattr(web_server, "_dashboard_local_update_managed_externally", lambda: False)
        monkeypatch.setattr(web_server, "_spawn_hermes_action", fake_spawn)
        web_server._ACTION_PROCS.pop("hermes-update", None)
        web_server._ACTION_RESULTS.pop("hermes-update", None)

        resp = self.client.post("/api/hermes/update")

        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "pid": 12345, "name": "hermes-update"}
        assert calls == [(["update"], "hermes-update")]

    def test_action_status_reaps_completed_process(self, monkeypatch):
        import hermes_cli.web_server as web_server

        waited = {"done": False}

        class _Proc:
            pid = 42424

            def poll(self):
                return 0

            def wait(self, timeout=None):
                waited["done"] = True

        proc = _Proc()
        web_server._ACTION_PROCS.pop("hermes-update", None)
        web_server._ACTION_RESULTS.pop("hermes-update", None)
        web_server._ACTION_PROCS["hermes-update"] = proc

        resp = self.client.get("/api/actions/hermes-update/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["running"] is False
        assert data["exit_code"] == 0
        assert data["pid"] == 42424

        # Process should have been reaped and moved to results.
        assert waited["done"] is True
        assert "hermes-update" not in web_server._ACTION_PROCS
        assert web_server._ACTION_RESULTS["hermes-update"] == {
            "exit_code": 0,
            "pid": 42424,
        }

    def test_action_status_ignores_wait_failure(self, monkeypatch):
        import hermes_cli.web_server as web_server

        class _Proc:
            pid = 99

            def poll(self):
                return 1

            def wait(self, timeout=None):
                raise OSError("already reaped")

        proc = _Proc()
        web_server._ACTION_PROCS.pop("hermes-update", None)
        web_server._ACTION_RESULTS.pop("hermes-update", None)
        web_server._ACTION_PROCS["hermes-update"] = proc

        resp = self.client.get("/api/actions/hermes-update/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["exit_code"] == 1
        # Still reaped despite wait() raising.
        assert "hermes-update" not in web_server._ACTION_PROCS
        assert web_server._ACTION_RESULTS["hermes-update"] == {
            "exit_code": 1,
            "pid": 99,
        }

    def test_action_status_tails_large_log_without_read_text(self, tmp_path, monkeypatch):
        import hermes_cli.web_server as web_server

        monkeypatch.setattr(web_server, "_ACTION_LOG_DIR", tmp_path)
        web_server._ACTION_PROCS.pop("hermes-update", None)
        web_server._ACTION_RESULTS.pop("hermes-update", None)

        log_path = tmp_path / web_server._ACTION_LOG_FILES["hermes-update"]
        log_path.write_text(
            "stale-start\n"
            + ("x" * (web_server._ACTION_LOG_TAIL_MAX_BYTES + 1024))
            + "\ntail-one\ntail-two\n",
            encoding="utf-8",
        )
        assert log_path.stat().st_size > web_server._ACTION_LOG_TAIL_MAX_BYTES

        original_read_text = Path.read_text

        def fail_if_status_reads_whole_log(path, *args, **kwargs):
            if path == log_path:
                raise AssertionError("action status must not read the entire log")
            return original_read_text(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", fail_if_status_reads_whole_log)

        resp = self.client.get("/api/actions/hermes-update/status?lines=3")

        assert resp.status_code == 200
        assert resp.json()["lines"] == ["tail-one", "tail-two"]

    def test_get_status_filters_unconfigured_gateway_platforms(self, monkeypatch):
        import gateway.config as gateway_config
        import hermes_cli.web_server as web_server

        class _Platform:
            def __init__(self, value):
                self.value = value

        class _GatewayConfig:
            def get_connected_platforms(self):
                return [_Platform("telegram")]

        monkeypatch.setattr(web_server, "get_running_pid_cached", lambda: 1234)
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

        monkeypatch.setattr(web_server, "get_running_pid_cached", lambda: None)
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

    def _schema_provider_options(self, key):
        resp = self.client.get("/api/config/schema")
        assert resp.status_code == 200
        return resp.json()["fields"][key]["options"]






        cfg = load_config()
        cfg.setdefault("stt", {}).setdefault("providers", {})["mywhisper"] = {
            "command": "whisper-cli {input}",  # type: omitted → command implied
        }
        save_config(cfg)

        options = self._schema_provider_options("stt.provider")
        assert "mywhisper" in options

    def test_config_schema_excludes_builtin_name_collisions(self):
        """A providers.EDGE command block must NOT be offered — the runtime
        rejects built-in names as command providers (case-insensitively)."""
        from hermes_cli.config import load_config, save_config

        cfg = load_config()
        cfg.setdefault("tts", {}).setdefault("providers", {})["EDGE"] = {
            "type": "command",
            "command": "fake-edge {text}",
        }
        save_config(cfg)

        options = self._schema_provider_options("tts.provider")
        lowered = [o.lower() for o in options]
        assert lowered.count("edge") == 1  # only the built-in entry

    def test_config_schema_excludes_non_command_blocks(self):
        """Built-in-shaped blocks (voice/model, no command) and non-dicts are
        not offered as providers."""
        from hermes_cli.config import load_config, save_config

        cfg = load_config()
        tts = cfg.setdefault("tts", {})
        tts.setdefault("providers", {})["notacommand"] = {"voice": "en-US-Foo"}
        tts["stringy"] = "oops"
        save_config(cfg)

        options = self._schema_provider_options("tts.provider")
        assert "notacommand" not in options
        assert "stringy" not in options

    def test_config_schema_preserves_current_custom_provider_value(self):
        """A custom active tts.provider without a providers.<name> block stays
        selectable (current-value preservation, matching desktop behavior)."""
        from hermes_cli.config import load_config, save_config

        cfg = load_config()
        cfg.setdefault("tts", {})["provider"] = "orphancustom"
        save_config(cfg)

        options = self._schema_provider_options("tts.provider")
        assert "orphancustom" in options

    def test_config_schema_reflects_config_changes_without_restart(self):
        """Options are computed per-request — adding a provider after the
        first schema fetch shows up on the next fetch."""
        from hermes_cli.config import load_config, save_config

        before = self._schema_provider_options("tts.provider")
        assert "latecomer" not in before

        cfg = load_config()
        cfg.setdefault("tts", {}).setdefault("providers", {})["latecomer"] = {
            "type": "command",
            "command": "late {text}",
        }
        save_config(cfg)

        after = self._schema_provider_options("tts.provider")
        assert "latecomer" in after

    def test_config_schema_legacy_toplevel_command_provider(self):
        """The legacy top-level ``tts.<name>`` command block (runtime
        back-compat fallback) is also offered."""
        from hermes_cli.config import load_config, save_config

        cfg = load_config()
        cfg.setdefault("tts", {})["legacytts"] = {
            "type": "command",
            "command": "legacy {text}",
        }
        save_config(cfg)

        options = self._schema_provider_options("tts.provider")
        assert "legacytts" in options

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
        assert email_forced.json()["email"]["unread_count"] == 3
        assert email_forced.json()["cache"]["resources"]["email"]["cached"] is False
        assert email_forced.json()["cache"]["resources"]["calendar"]["cached"] is True

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
        assert forced_contacts.json()["contacts"]["total_count"] == 2
        assert forced_contacts.json()["cache"]["resources"]["contacts"]["cached"] is False

    def test_cui_contacts_deduplicates_source_badges(self):
        import hermes_cli.web_server as web_server

        contact = getattr(web_server, "_normalize_contact")({
            "display_name": "Casey Example",
            "email": "casey@example.test",
            "source_badges": ["Google Contacts", "owner@example.test", "owner@example.test", "Aus E-Mail"],
            "source": "google contacts",
        })
        assert contact is not None
        assert contact["source_badges"] == ["Google Contacts", "owner@example.test", "Aus E-Mail"]

        decoded = getattr(web_server, "_contact_from_address")(
            "=?utf-8?q?Synthetic_Notification_System?= <noreply@system.example.test>",
            source="Aus E-Mail",
            relevance="relevant",
        )
        assert decoded is not None
        assert decoded["display_name"] == "Synthetic Notification System"
        assert getattr(web_server, "_is_probably_system_contact")(decoded, own_emails=set()) is True

        merged = getattr(web_server, "_dedupe_contacts")([
            {"display_name": "Casey Example", "email": "casey@example.test", "source_badges": ["Google Contacts", "owner@example.test"]},
            {"display_name": "Casey Example", "email": "casey@example.test", "source_badges": ["owner@example.test", "Aus E-Mail"]},
        ])
        assert merged[0]["source_badges"] == ["Google Contacts", "owner@example.test", "Aus E-Mail"]

    def test_cui_contacts_create_search_and_resource_summary(self, tmp_path, monkeypatch):
        import hermes_cli.web_server as web_server

        with web_server._ASSISTANT_RESOURCE_CACHE_LOCK:
            web_server._ASSISTANT_RESOURCE_CACHE.clear()
        monkeypatch.delenv("AIWERK_CUI_CONTACTS_JSON", raising=False)
        monkeypatch.setattr(web_server, "load_config", lambda: {})
        monkeypatch.setattr(web_server, "_assistant_contacts_store_path", lambda: tmp_path / "cui_contacts.json")

        created = self.client.post("/api/cui/contacts", json={
            "name": "Avery Example",
            "organization": "Fixture Test Organization",
            "role": "Test Manager",
            "email": "Avery@Example.Test",
            "phone": "+1 202 555 0100",
            "note": "Nur kurze Notiz",
        })
        assert created.status_code == 200
        assert created.json()["contact"]["email"] == "avery@example.test"
        assert "Manuell" in created.json()["contact"]["source_badges"]

        resources = self.client.get("/api/assistant/resources?refresh=1&resource=contacts")
        assert resources.status_code == 200
        contacts = resources.json()["contacts"]
        assert contacts["status"] == "connected"
        assert contacts["total_count"] == 1
        assert contacts["frequent"][0]["display_name"] == "Avery Example"

        search = self.client.get("/api/cui/contacts/search?q=fixture")
        assert search.status_code == 200
        assert search.json()["total_count"] == 1
        assert search.json()["items"][0]["organization"] == "Fixture Test Organization"

        hidden = self.client.post("/api/cui/contacts/hide", json={"id": created.json()["contact"]["id"], "email": created.json()["contact"]["email"]})
        assert hidden.status_code == 200
        assert "email:avery@example.test" in hidden.json()["hidden"]

        resources = self.client.get("/api/assistant/resources?refresh=1&resource=contacts")
        assert resources.status_code == 200
        assert resources.json()["contacts"]["total_count"] == 0
        search = self.client.get("/api/cui/contacts/search?q=fixture")
        assert search.status_code == 200
        assert search.json()["total_count"] == 0

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
                    return {"result": {"structuredContent": {"result": "Retrieved 1 messages:\n\nMessage ID: sent-1\nSubject: Angebot\nFrom: Kontakt <contact@example.test>\nDate: Tue, 2 Jun 2026 10:00:00 +0000\nTo: Anna Example <anna@example.test>\n"}}}
                return {"result": {"structuredContent": {"result": "Retrieved 2 messages:\n\nMessage ID: sent-1\nSubject: Hallo\nFrom: Owner <owner@example.test>\nDate: Tue, 2 Jun 2026 11:00:00 +0000\nTo: Bela Privat <bela@example.ch>\n\nMessage ID: inbox-1\nSubject: Analytics\nFrom: Google Analytics <analytics-noreply@google.com>\nDate: Tue, 2 Jun 2026 12:00:00 +0000\nTo: owner@example.test\nPrecedence: bulk\nList-Unsubscribe: <https://example.com/unsub>\n"}}}
            if tool == "list_contacts" and server == "google-workspace-aiwerk":
                return {"result": {"structuredContent": {"result": "Contacts for contact@example.test (1 of 1):\n\nContact ID: c_aiwerk\nName: Anna Example\nEmail: anna@example.test (Work)\nPhone: +41 31 555 12 12 (Work)\nOrganization: CEO at Example AG\n\n"}}}
            if tool == "list_contacts":
                return {"result": {"structuredContent": {"result": "Contacts for owner@example.test (1 of 1):\n\nContact ID: c_private\nName: Bela Privat\nEmail: bela@example.ch (Other)\n\n"}}}
            return {}

        monkeypatch.setattr(web_server, "_call_aiwerk_bridge_tool", fake_bridge_call)
        config = {
            "assistant": {
                "email": {
                    "accounts": [
                        {"address": "contact@example.test", "backend": "google_workspace", "mcp_server": "google-workspace-aiwerk", "user_google_email": "contact@example.test"},
                        {"address": "owner@example.test", "backend": "google_workspace", "mcp_server": "google-workspace-owner", "user_google_email": "owner@example.test"},
                    ]
                }
            }
        }
        email = {"accounts": [{"address": "contact@example.test", "items": [{"sender": "Anna Example <anna@example.test>"}]}]}

        summary = getattr(web_server, "_contacts_summary")(config, email, {})

        assert {call["server"] for call in calls if call["tool"] == "search_gmail_messages"} == {"google-workspace-aiwerk", "google-workspace-owner"}
        assert any(call["params"].get("query") == "in:sent newer_than:10d" for call in calls if call["tool"] == "search_gmail_messages")
        assert any(call["params"].get("query") == "newer_than:10d -in:sent" for call in calls if call["tool"] == "search_gmail_messages")
        emails = {item["email"] for item in summary["items"]}
        assert {"anna@example.test", "bela@example.ch"}.issubset(emails)
        assert "analytics-noreply@google.com" not in emails
        assert "contact@example.test" not in emails
        assert "owner@example.test" not in emails
        assert summary["google_count"] == 2
        assert summary["summary"] == "2 relevante Kontakte"
        assert summary["source_label"] == "Relevante Kontakte"
        assert summary["relevance_window_days"] == 10
        assert summary["interaction_count"] == 2
        anna = next(item for item in summary["items"] if item["email"] == "anna@example.test")
        assert anna["organization"] == "Example AG"
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
                        "from": {"name": "Owner", "addr": "user@example.ch"},
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

    def test_cui_contacts_payload_final_guard_filters_cached_own_contacts(self):
        import hermes_cli.web_server as web_server

        payload = {
            "status": "connected",
            "summary": "3 Kontakte verfügbar",
            "items": [
                {"display_name": "Contact Example", "email": "contact@example.test", "source_badges": ["Google Contacts", "contact@example.test"]},
                {"display_name": "Owner", "email": "owner@example.test", "source_badges": ["Google Contacts", "owner@example.test"]},
                {"display_name": "Human", "email": "human@example.ch", "source_badges": ["Google Contacts", "contact@example.test", "owner@example.test"]},
            ],
            "frequent": [
                {"display_name": "Contact Example", "email": "contact@example.test", "source_badges": ["contact@example.test"]},
                {"display_name": "Human", "email": "human@example.ch", "source_badges": ["Google Contacts", "contact@example.test"]},
            ],
            "relevant": [{"display_name": "Owner", "email": "owner@example.test", "source_badges": ["owner@example.test"]}],
            "total_count": 3,
        }

        filtered = getattr(web_server, "_filter_contacts_payload")(
            payload,
            own_emails={"contact@example.test", "owner@example.test"},
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

    def test_assistant_ui_locale_resolves_hidden_customer_setting(self, monkeypatch):
        import hermes_cli.web_server as web_server

        resolve_locale = getattr(web_server, "_assistant_ui_locale_from_config")
        monkeypatch.setenv("AIWERK_CUI_LOCALE", "hu")
        assert resolve_locale({}) == "hu"
        monkeypatch.setenv("AIWERK_CUI_LOCALE", "")
        assert resolve_locale({"dashboard": {"locale": "de_CH"}}) == "de"
        assert resolve_locale({"assistant": {"language": "Magyar"}}) == "hu"
        assert resolve_locale({"assistant": {"language": "invalid"}}) == "de"

    def test_assistant_mode_allows_read_only_commands_catalog_rpc(self):
        import hermes_cli.web_server as web_server

        gate = web_server._assistant_ws_request_gate
        assert gate({"method": "commands.catalog", "params": {}}) is None
        assert gate({"method": "cli.exec", "params": {"argv": ["status"]}}) == "method not available in assistant mode: cli.exec"

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

    def test_create_shared_file_public_link_reuses_existing_sftpgo_share(self, monkeypatch):
        import hermes_cli.web_server as web_server

        monkeypatch.setattr(web_server, "_sftpgo_user_api_token", lambda cloud: "user-token")

        def fake_api_json(base_url, token, path, *, method="GET", payload=None):
            assert base_url == "https://cloud.aiwerk.ch"
            assert token == "user-token"
            assert method == "GET"
            assert payload is None
            return 200, [{"id": "share123", "scope": 1, "paths": ["/Agent-Downloads/report.pptx"]}], {}

        monkeypatch.setattr(web_server, "_sftpgo_user_api_json", fake_api_json)
        config = {"dashboard": {"shared_cloud": {"base_url": "https://dav.aiwerk.ch", "username": "example.user", "password_pass_entry": "cloud/example-user"}}}

        link = web_server._create_shared_file_public_link(config, "Agent-Downloads/report.pptx")

        assert link == {
            "url": "https://cloud.aiwerk.ch/web/client/pubshares/share123?compress=false",
            "download_url": "https://cloud.aiwerk.ch/web/client/pubshares/share123/download",
        }

    def test_create_shared_file_public_link_creates_sftpgo_share(self, monkeypatch):
        import hermes_cli.web_server as web_server

        calls = []
        monkeypatch.setattr(web_server, "_sftpgo_user_api_token", lambda cloud: "user-token")

        def fake_api_json(base_url, token, path, *, method="GET", payload=None):
            calls.append((base_url, token, path, method, payload))
            if method == "GET":
                return 200, [], {}
            return 201, None, {"X-Object-ID": "newshare"}

        monkeypatch.setattr(web_server, "_sftpgo_user_api_json", fake_api_json)
        config = {"dashboard": {"shared_cloud": {"base_url": "https://dav.aiwerk.ch", "username": "example.user", "password_pass_entry": "cloud/example-user"}}}

        link = web_server._create_shared_file_public_link(config, "Agent-Downloads/report.pptx", name="Report")

        assert calls[1] == (
            "https://cloud.aiwerk.ch",
            "user-token",
            "/api/v2/user/shares",
            "POST",
            {"name": "Report", "scope": 1, "paths": ["/Agent-Downloads/report.pptx"], "expires_at": 0, "max_tokens": 0},
        )
        assert link is not None
        assert link["url"] == "https://cloud.aiwerk.ch/web/client/pubshares/newshare?compress=false"
        assert link["download_url"] == "https://cloud.aiwerk.ch/web/client/pubshares/newshare/download"

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

    def test_aiwerk_bridge_connectors_do_not_fall_back_to_demo_catalog(self, monkeypatch):
        import hermes_cli.web_server as web_server

        def bridge_inventory_fails(config):
            raise RuntimeError("bridge status unavailable")

        monkeypatch.setattr(web_server, "_aiwerk_bridge_live_subservers", bridge_inventory_fails)
        connectors = web_server._connector_summary(
            {"mcp_servers": {"aiwerk_bridge": {"url": "http://127.0.0.1:8000/mcp", "enabled": True}}},
            {},
            {},
            {},
        )

        assert [connector["label"] for connector in connectors] == ["AIWerk Bridge"]
        assert "children" not in connectors[0]

    def test_aiwerk_bridge_connectors_use_explicit_config_instead_of_demo_catalog(self, monkeypatch):
        import hermes_cli.web_server as web_server

        def bridge_inventory_should_not_be_called(config):
            raise AssertionError("explicit dashboard subservers should be used without live fallback")

        monkeypatch.setattr(web_server, "_aiwerk_bridge_live_subservers", bridge_inventory_should_not_be_called)
        connectors = web_server._connector_summary(
            {
                "dashboard": {"aiwerk_bridge": {"subservers": ["microsoft-calendar", "vault"]}},
                "mcp_servers": {"aiwerk_bridge": {"url": "http://127.0.0.1:8000/mcp", "enabled": True}},
            },
            {},
            {},
            {},
        )

        bridge = connectors[0]
        assert [child["label"] for child in bridge["children"]] == ["Microsoft Calendar", "Vault"]
        assert "CoinMarketCap" not in [child["label"] for child in bridge["children"]]

    def test_aiwerk_bridge_connector_status_comes_from_live_inventory(self):
        import hermes_cli.web_server as web_server

        item = web_server._aiwerk_bridge_subserver_item(
            "microsoft-calendar",
            {"status": "disconnected", "description": "Starts on demand"},
        )

        assert item["label"] == "Microsoft Calendar"
        assert item["status"] == "limited"
        assert item["status_label"] == "Eingeschränkt"
        assert item["description"] == "Starts on demand"

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
        assert file_item["reference_uri"] == "shared://Bedienungsanleitungen/B03900_IM_Kaffeevollautomat_Finessa_0322_WEB.pdf"

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

    def test_webdav_download_allows_large_price_list_under_100mb(self, monkeypatch):
        import hermes_cli.web_server as web_server

        payload = b"x" * (70 * 1024 * 1024)

        class FakeResponse:
            status = 200
            headers = {"content-type": "application/pdf"}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, size=-1):
                assert size == web_server._ASSISTANT_SHARED_FILE_OPEN_MAX_BYTES + 1
                return payload

        monkeypatch.setattr(web_server, "_pass_first_line", lambda entry: "secret")
        monkeypatch.setattr(web_server.urllib.request, "urlopen", lambda request, timeout=None: FakeResponse())

        downloaded = web_server._download_webdav_cloud_file({
            "type": "sftpgo_webdav",
            "base_url": "https://dav.example.test",
            "username": "customer.example",
            "password_pass_entry": "pass/entry",
            "path": "/",
        }, "Ligne Roset Preislisten/TARIF_A_JOUR.pdf")

        assert downloaded is not None
        assert len(downloaded[0]) == len(payload)
        assert downloaded[1] == "application/pdf"
        assert downloaded[2] == "TARIF_A_JOUR.pdf"

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
                "display_name": "Avery Example",
                "organization": "Fixture Test Organization",
                "role": "Test Manager",
                "email": "avery@example.test",
                "phone": "+1 202 555 0100",
                "source_badges": ["Gmail", "Calendar"],
                "raw_connector_metadata": "must-not-leak",
            },
        })

        assert resp.status_code == 200
        attachment = resp.json()["attachments"][0]
        assert attachment["name"].startswith("contact-Avery-Example")
        text = Path(attachment["path"]).read_text(encoding="utf-8")
        assert "Attached contact context" in text
        assert "Avery Example" in text
        assert "Fixture Test Organization" in text
        assert "avery@example.test" in text
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
                            "address": "contact@example.test",
                            "mcp_server": "google-workspace-aiwerk",
                            "user_google_email": "contact@example.test",
                        }
                    ]
                }
            },
            "mcp_servers": {"aiwerk_bridge": {"url": "https://bridge.aiwerk.ch/u/demo/mcp", "enabled": True}},
        })
        monkeypatch.setattr(web_server, "_assistant_resources_payload", lambda request, force_refresh=False, **kwargs: {
            "email": {
                "accounts": [{
                    "address": "contact@example.test",
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
To: contact@example.test
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

        resp = self.client.get("/api/assistant/email/view?account=contact%40example.test&id=gmail-1")

        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert resp.headers["cache-control"] == "no-store"
        assert "default-src 'none'" in resp.headers["content-security-policy"]
        assert seen == {"message_id": "gmail-1", "account": "contact@example.test"}
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
From: Example Website <contact@example.test>
Date: Sat, 30 May 2026 19:10:00 +0000
To: <contact@example.test>
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
        assert email["items"][0]["sender"] == "Example Website <contact@example.test>"
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
            assert arguments["params"]["user_google_email"] == "contact@example.test"
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
                        {"backend": "google_workspace", "address": "contact@example.test", "mcp_server": "google-workspace-aiwerk", "user_google_email": "contact@example.test"},
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
        assert {account["label"] for account in email["accounts"]} == {"contact@example.test", "info@example.ch"}
        assert {account["address"] for account in email["accounts"]} == {"contact@example.test", "info@example.ch"}
        assert {item["account_label"] for item in email["items"]} == {"contact@example.test", "info@example.ch"}
        assert {item["account_address"] for item in email["items"]} == {"contact@example.test", "info@example.ch"}
        account_items = {account["address"]: account["items"] for account in email["accounts"]}
        assert account_items["contact@example.test"][0]["subject"] == "Gmail Anfrage"
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

    def test_get_env_vars_surfaces_catalog_providers(self):
        """Every keys-tab provider in the unified catalog must appear in /api/env
        as a provider card, even when it has no hand entry in OPTIONAL_ENV_VARS.

        Regression for the GUI⇄CLI drift: openai-api, kilocode, novita,
        tencent-tokenhub, copilot were configurable via `hermes model` but
        invisible in the desktop Providers → API keys tab.
        """
        from hermes_cli.provider_catalog import provider_catalog

        data = self.client.get("/api/env").json()
        for d in provider_catalog():
            if d.tab != "keys" or not d.api_key_env_vars:
                continue
            # The PRIMARY credential var must surface as this provider's card.
            # (Shared aliases like GITHUB_TOKEN are intentionally left on their
            # existing tool category and not hijacked — see the copilot test.)
            primary = d.api_key_env_vars[0]
            assert primary in data, f"{primary} ({d.slug}) missing from /api/env"
            info = data[primary]
            assert info["category"] == "provider"
            assert info["provider"] == d.slug
            assert info["provider_label"] == d.label

    def test_get_env_vars_provider_rows_carry_grouping_hints(self):
        """Provider env rows expose the backend `provider`/`provider_label` the
        desktop Keys tab groups by (so it no longer relies on prefix guesses)."""
        data = self.client.get("/api/env").json()
        # OPENAI_API_KEY is a hand-listed protected var AND a catalog provider;
        # it must come back tagged to the openai-api provider.
        assert data["OPENAI_API_KEY"]["provider"] == "openai-api"
        assert data["OPENAI_API_KEY"]["category"] == "provider"

    def test_get_env_vars_copilot_uses_provider_token_not_shared_github_token(self):
        """Copilot surfaces as its own provider card via COPILOT_GITHUB_TOKEN;
        the shared GITHUB_TOKEN keeps its existing (tool) category."""
        data = self.client.get("/api/env").json()
        assert data["COPILOT_GITHUB_TOKEN"]["provider"] == "copilot"
        assert data["COPILOT_GITHUB_TOKEN"]["category"] == "provider"
        # Shared GITHUB_TOKEN must NOT be hijacked into the copilot provider card.
        assert data.get("GITHUB_TOKEN", {}).get("provider", "") != "copilot"

    def test_get_env_vars_bedrock_aws_vars_tagged_to_provider(self):
        """Bedrock (aws_sdk, no api-key) must still appear on the Keys tab: its
        AWS_REGION/AWS_PROFILE settings are tagged to the bedrock provider card.
        """
        data = self.client.get("/api/env").json()
        assert data["AWS_REGION"]["provider"] == "bedrock"
        assert data["AWS_REGION"]["category"] == "provider"
        assert data["AWS_PROFILE"]["provider"] == "bedrock"

    def test_platform_scoped_messaging_env_vars_are_channel_managed(self):
        """Platform-scoped vars belong to a Channels card; cross-cutting
        gateway vars belong to the Keys page.

        Uses credentials as the example: the self-configuring knobs
        (*_HOME_CHANNEL, *_ALLOW_ALL_USERS, …) were deliberately dropped from
        the setup cards and handed back to Keys — see
        tests/hermes_cli/test_setup_hidden_env.py.
        """
        from hermes_cli.web_server import (
            _MESSAGING_KEYS_PAGE_KEYS,
            _build_catalog_entry,
            _channel_managed_env_keys,
        )

        discord = _build_catalog_entry("discord")
        assert "DISCORD_BOT_TOKEN" in discord["env_vars"]
        assert "DISCORD_ALLOWED_USERS" in discord["env_vars"]

        managed = _channel_managed_env_keys()
        assert "DISCORD_BOT_TOKEN" in managed
        assert "MATTERMOST_TOKEN" in managed
        assert "GATEWAY_PROXY_URL" not in managed
        assert "GATEWAY_PROXY_URL" in _MESSAGING_KEYS_PAGE_KEYS



    def test_model_set_maps_unknown_vendor_to_aggregator(self, monkeypatch):
        """A bare vendor name from analytics rows (no billing_provider) is not
        a Hermes provider — keep the user's aggregator instead of writing a
        provider that can never resolve credentials."""
        monkeypatch.setattr(
            "hermes_cli.model_cost_guard.expensive_model_warning",
            lambda *_args, **_kwargs: None,
        )
        from hermes_cli.config import load_config, save_config
        cfg = load_config()
        cfg["model"] = {"provider": "openrouter", "default": "openai/gpt-5.5"}
        save_config(cfg)

        resp = self.client.post(
            "/api/model/set",
            json={
                "scope": "main",
                "provider": "moonshotai",  # vendor prefix, not a provider
                "model": "moonshotai/kimi-k2.6",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["provider"] == "openrouter"
        assert data["model"] == "moonshotai/kimi-k2.6"








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






    def test_messaging_catalog_prefers_plugin_label_over_enum_pseudo_member(self):
        """A plugin platform that leaked into Platform.__members__ as a pseudo-
        member must still render with its plugin label, not a title-cased id.

        Regression: Platform("<plugin id>") caches a pseudo-member in the enum;
        the catalog iterated the enum FIRST and claimed the id with no plugin
        metadata, so bundled plugin platforms (irc, ntfy, photon, …) rendered
        as nameless "Irc"/"Ntfy" cards with empty descriptions.
        """
        from gateway.config import Platform
        from gateway.platform_registry import PlatformEntry, platform_registry

        entry = PlatformEntry(
            name="pseudofake",
            label="Pseudo Fake (plugin label)",
            adapter_factory=lambda cfg: None,
            check_fn=lambda: True,
            source="plugin",
        )
        platform_registry.register(entry)
        try:
            # Materialize the enum pseudo-member the way any earlier config
            # read would (Platform(value) on a registered plugin platform).
            member = Platform("pseudofake")
            assert member.value == "pseudofake"
            assert "PSEUDOFAKE" in Platform.__members__

            resp = self.client.get("/api/messaging/platforms")
            ids = {row["id"]: row for row in resp.json()["platforms"]}
            assert "pseudofake" in ids
            assert ids["pseudofake"]["name"] == "Pseudo Fake (plugin label)"
        finally:
            platform_registry.unregister("pseudofake")
            Platform._value2member_map_.pop("pseudofake", None)
            Platform._member_map_.pop("PSEUDOFAKE", None)










    def test_telegram_onboarding_apply_reports_restart_failure_after_save(
        self, monkeypatch
    ):
        import hermes_cli.web_server as ws
        from hermes_cli.config import load_config, load_env

        with ws._telegram_onboarding_lock:
            ws._telegram_onboarding_pairings.clear()

        def fake_request(method, path, *, body=None, bearer_token=None):
            if method == "POST":
                return {
                    "pairing_id": "pair-restart-fails",
                    "poll_token": "poll-secret",
                    "suggested_username": "hermes_pair_restart_fails_bot",
                    "deep_link": "https://t.me/newbot/HermesSetupBot/hermes_pair_restart_fails_bot",
                    "qr_payload": "https://t.me/newbot/HermesSetupBot/hermes_pair_restart_fails_bot",
                    "expires_at": "2027-05-18T00:00:00.000Z",
                }
            assert method == "GET"
            assert path == "/v1/telegram/pairings/pair-restart-fails"
            assert bearer_token == "poll-secret"
            return {
                "status": "ready",
                "bot_username": "hermes_pair_restart_fails_bot",
                "owner_user_id": 123456789,
                "token": "123456:SECRET",
            }

        monkeypatch.setattr(ws, "_telegram_onboarding_request_sync", fake_request)
        ws._ACTION_PROCS.pop("gateway-restart", None)

        def fail_spawn_action(subcommand, name):
            assert subcommand == ["gateway", "restart"]
            assert name == "gateway-restart"
            raise RuntimeError("supervisor unavailable")

        monkeypatch.setattr(ws, "_spawn_hermes_action", fail_spawn_action)

        start = self.client.post("/api/messaging/telegram/onboarding/start", json={})
        assert start.status_code == 200
        ready = self.client.get("/api/messaging/telegram/onboarding/pair-restart-fails")
        assert ready.status_code == 200
        assert ready.json()["status"] == "ready"

        applied = self.client.post(
            "/api/messaging/telegram/onboarding/pair-restart-fails/apply",
            json={"allowed_user_ids": ["123456789"]},
        )

        assert applied.status_code == 200
        applied_data = applied.json()
        assert applied_data["ok"] is True
        assert applied_data["needs_restart"] is True
        assert applied_data["restart_started"] is False
        assert "supervisor unavailable" in applied_data["restart_error"]
        assert "token" not in applied_data
        env = load_env()
        assert env["TELEGRAM_BOT_TOKEN"] == "123456:SECRET"
        assert env["TELEGRAM_ALLOWED_USERS"] == "123456789"
        assert load_config()["platforms"]["telegram"]["enabled"] is True





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


    def test_set_model_main_custom_persists_api_key_and_registers_provider(self):
        """A custom endpoint that requires auth must persist model.api_key (where
        the runtime reads it) AND register a named custom_providers entry so the
        endpoint reappears as a ready row in the picker — matching the
        ``hermes model`` custom flow. Regression for the desktop loop where a
        keyed custom endpoint could never be configured from the GUI."""
        from hermes_cli.config import load_config

        resp = self.client.post(
            "/api/model/set",
            json={
                "scope": "main",
                "provider": "custom",
                "model": "gpt-oss-120b",
                "base_url": "https://text.example.com/v1",
                "api_key": "sk-secret",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        cfg = load_config()
        model_cfg = cfg.get("model")
        assert isinstance(model_cfg, dict)
        assert model_cfg["provider"] == "custom"
        assert model_cfg["base_url"] == "https://text.example.com/v1"
        assert model_cfg["api_key"] == "sk-secret"

        # Registered in custom_providers (dedup by base_url) so the picker shows
        # a proper ready row instead of the "needs setup" dead-end.
        custom = cfg.get("custom_providers") or []
        assert any(
            isinstance(e, dict)
            and e.get("base_url") == "https://text.example.com/v1"
            and e.get("api_key") == "sk-secret"
            and e.get("model") == "gpt-oss-120b"
            for e in custom
        )






    def test_custom_endpoint_save_refusal_does_not_persist_config(self, monkeypatch):
        import hermes_cli.web_server as ws

        saved = []
        monkeypatch.setattr(ws, "save_env_value", lambda key, value: False)
        monkeypatch.setattr(ws, "save_config", lambda cfg: saved.append(cfg))

        resp = self.client.post(
            "/api/providers/custom-endpoints",
            json={
                "id": "managed-proxy",
                "name": "Managed Proxy",
                "base_url": "https://proxy.example/v1",
                "model": "proxy/model",
                "api_key": "new-key",
            },
        )

        assert resp.status_code == 500
        assert saved == []

    def test_custom_endpoint_rollback_prefers_actual_dotenv_over_inherited_value(self, monkeypatch):
        import hermes_cli.web_server as ws
        from hermes_cli.config import custom_endpoint_key_env, load_env

        env_var = custom_endpoint_key_env("conflict-proxy")
        assert ws.save_env_value(env_var, "dotenv-old") is True
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

    def test_custom_endpoint_delete_refusal_does_not_persist_config(self, monkeypatch):
        import hermes_cli.web_server as ws
        from hermes_cli.config import load_config

        created = self.client.post(
            "/api/providers/custom-endpoints",
            json={
                "id": "managed-delete",
                "name": "Managed Delete",
                "base_url": "https://proxy.example/v1",
                "model": "proxy/model",
                "api_key": "keep-key",
            },
        )
        assert created.status_code == 200
        saved = []
        monkeypatch.setattr(ws, "remove_env_value", lambda key: False)
        monkeypatch.setattr(ws, "save_config", lambda cfg: saved.append(cfg))

        resp = self.client.delete("/api/providers/custom-endpoints/managed-delete")

        assert resp.status_code == 500
        assert saved == []
        assert "managed-delete" in load_config()["providers"]

    def test_custom_endpoint_upsert_restores_env_credential_when_config_save_fails(self, monkeypatch):
        import hermes_cli.web_server as ws
        from hermes_cli.config import custom_endpoint_key_env, get_env_value

        env_var = custom_endpoint_key_env("rollback-proxy")
        ws.save_env_value(env_var, "old-key")
        monkeypatch.setattr(ws, "save_config", lambda cfg: (_ for _ in ()).throw(OSError("disk full")))

        resp = self.client.post(
            "/api/providers/custom-endpoints",
            json={
                "id": "rollback-proxy",
                "name": "Rollback Proxy",
                "base_url": "https://proxy.example/v1",
                "model": "proxy/model",
                "api_key": "new-key",
            },
        )

        assert resp.status_code == 500
        assert get_env_value(env_var) == "old-key"

    def test_custom_endpoint_delete_restores_env_credential_when_config_save_fails(self, monkeypatch):
        import hermes_cli.web_server as ws
        from hermes_cli.config import custom_endpoint_key_env, get_env_value

        created = self.client.post(
            "/api/providers/custom-endpoints",
            json={
                "id": "delete-rollback",
                "name": "Delete Rollback",
                "base_url": "https://proxy.example/v1",
                "model": "proxy/model",
                "api_key": "keep-key",
            },
        )
        assert created.status_code == 200
        env_var = custom_endpoint_key_env("delete-rollback")
        monkeypatch.setattr(ws, "save_config", lambda cfg: (_ for _ in ()).throw(OSError("disk full")))

        resp = self.client.delete("/api/providers/custom-endpoints/delete-rollback")

        assert resp.status_code == 500
        assert get_env_value(env_var) == "keep-key"

    def _seed_custom_provider_with_key(self):
        from hermes_cli.config import load_config, save_config

        cfg = load_config()
        cfg["providers"] = {
            "acme": {
                "name": "Acme",
                "base_url": "https://llm.acme.corp/v1",
                "model": "acme/m1",
                "api_key": "sk-stored-old",
                "models": {"acme/m1": {}},
            }
        }
        save_config(cfg)


    def test_deleting_the_active_custom_endpoint_clears_its_model_mirror(self):
        """Deleting an endpoint must not leave its credential running the agent.

        ``activate`` mirrors the endpoint's base_url + credential reference
        onto ``model``, and that mirror outranks the environment at client
        construction (#62269). Without clearing it the agent keeps
        authenticating to the deleted host, and the credential the operator
        just removed through the dashboard survives the delete.
        """
        from hermes_cli.config import custom_endpoint_key_env, get_env_value, load_config

        self.client.post(
            "/api/providers/custom-endpoints",
            json={
                "id": "acme",
                "name": "Acme",
                "base_url": "https://llm.acme.corp/v1",
                "model": "acme/model-1",
                "api_key": "sk-acme-secret",
            },
        )
        assert self.client.post(
            "/api/providers/custom-endpoints/acme/activate", json={}
        ).status_code == 200

        env_var = custom_endpoint_key_env("acme")
        cfg = load_config()
        assert cfg["model"]["key_env"] == env_var
        assert get_env_value(env_var) == "sk-acme-secret"

        assert self.client.request(
            "DELETE", "/api/providers/custom-endpoints/acme"
        ).status_code == 200

        cfg = load_config()
        assert "acme" not in (cfg.get("providers") or {})
        model_cfg = cfg.get("model") or {}
        assert not model_cfg.get("api_key"), "deleted endpoint's key still in config.yaml"
        assert not model_cfg.get("key_env"), "deleted endpoint's key ref still in config.yaml"
        assert not model_cfg.get("base_url"), "deleted endpoint's host still routed to"
        assert not model_cfg.get("provider")
        assert not get_env_value(env_var), "deleted endpoint's key still in .env"



    def test_custom_endpoint_save_keeps_the_api_key_out_of_config(self):
        """The key belongs in .env behind key_env, never in config.yaml (#69449)."""
        from hermes_cli.config import custom_endpoint_key_env, get_env_value, load_config

        self.client.post(
            "/api/providers/custom-endpoints",
            json={
                "id": "proxy",
                "name": "Proxy",
                "base_url": "https://llm.example.com/v1",
                "model": "m",
                "api_key": "sk-super-secret",
                "make_default": True,
            },
        )

        cfg = load_config()
        entry = cfg["providers"]["proxy"]
        env_var = custom_endpoint_key_env("proxy")
        assert entry["key_env"] == env_var
        assert "api_key" not in entry
        assert "api_key" not in cfg["model"]
        assert get_env_value(env_var) == "sk-super-secret"
        assert "sk-super-secret" not in yaml.safe_dump(cfg)


    def test_custom_endpoint_save_leaves_a_hand_written_env_ref_alone(self, monkeypatch):
        """``api_key: ${MY_KEY}`` is already safe — don't copy it elsewhere.

        load_config() expands env refs, so such an entry looks like a literal
        secret by the time Save sees it. Migrating it would duplicate the
        user's secret into a second env var they never asked for.
        """
        import yaml

        from hermes_cli.config import custom_endpoint_key_env, get_config_path, get_env_value

        monkeypatch.setenv("MY_PROXY_KEY", "sk-user-managed")
        get_config_path().write_text(
            yaml.safe_dump({
                "providers": {
                    "proxy": {
                        "name": "Proxy",
                        "base_url": "https://llm.example.com/v1",
                        "model": "m",
                        "api_key": "${MY_PROXY_KEY}",
                    }
                },
            }),
            encoding="utf-8",
        )

        self.client.post(
            "/api/providers/custom-endpoints",
            json={
                "id": "proxy",
                "name": "Proxy",
                "base_url": "https://llm.example.com/v1",
                "model": "m",
            },
        )

        raw = yaml.safe_load(get_config_path().read_text(encoding="utf-8"))
        assert raw["providers"]["proxy"]["api_key"] == "${MY_PROXY_KEY}"
        assert not get_env_value(custom_endpoint_key_env("proxy"))


    def test_two_endpoints_on_one_host_keep_separate_credentials(self):
        """Two local servers must not share an .env slot.

        Deriving the env var from the hostname collapses ``127.0.0.1:8000``
        and ``:8001`` onto one name, so saving the second silently overwrites
        the first's key.
        """
        from hermes_cli.config import custom_endpoint_key_env, get_env_value

        for port, key in ((8000, "sk-first"), (8001, "sk-second")):
            self.client.post(
                "/api/providers/custom-endpoints",
                json={
                    "id": f"local-{port}",
                    "name": f"Local {port}",
                    "base_url": f"http://127.0.0.1:{port}/v1",
                    "model": "m",
                    "api_key": key,
                },
            )

        assert get_env_value(custom_endpoint_key_env("local-8000")) == "sk-first"
        assert get_env_value(custom_endpoint_key_env("local-8001")) == "sk-second"

    def test_custom_endpoint_response_reports_a_key_held_in_env(self):
        """has_api_key must follow key_env, not just a plaintext api_key.

        Reading only ``api_key`` made the panel report "no API key" for every
        endpoint whose credential had been moved to .env.
        """
        resp = self.client.post(
            "/api/providers/custom-endpoints",
            json={
                "id": "proxy",
                "name": "Proxy",
                "base_url": "https://llm.example.com/v1",
                "model": "m",
                "api_key": "sk-in-env",
            },
        )

        endpoint = next(e for e in resp.json()["endpoints"] if e["id"] == "proxy")
        assert endpoint["has_api_key"] is True
        assert "sk-in-env" not in (endpoint["api_key_preview"] or "")

    def test_activating_an_endpoint_carries_its_credential_either_way(self):
        """Activate must work for both key_env and pre-#69449 plaintext entries."""
        from hermes_cli.config import load_config, save_config

        cfg = load_config()
        cfg["providers"] = {
            "legacy": {
                "name": "Legacy",
                "base_url": "https://llm.legacy.com/v1",
                "model": "m",
                "api_key": "sk-legacy",
                "models": {"m": {}},
            },
            "modern": {
                "name": "Modern",
                "base_url": "https://llm.modern.com/v1",
                "model": "m",
                "key_env": "MODERN_API_KEY",
                "models": {"m": {}},
            },
        }
        save_config(cfg)

        self.client.post("/api/providers/custom-endpoints/modern/activate", json={})
        model_cfg = load_config()["model"]
        assert model_cfg["key_env"] == "MODERN_API_KEY"
        assert "api_key" not in model_cfg

        self.client.post("/api/providers/custom-endpoints/legacy/activate", json={})
        model_cfg = load_config()["model"]
        assert model_cfg["api_key"] == "sk-legacy"

    def test_get_sessions_rejects_negative_limit(self):
        """limit=-1 must be rejected (422), not passed through to SQLite as
        LIMIT -1 (unbounded) — issue #74316."""
        resp = self.client.get("/api/sessions?limit=-1")
        assert resp.status_code == 422

    def test_get_sessions_rejects_negative_offset(self):
        resp = self.client.get("/api/sessions?offset=-1")
        assert resp.status_code == 422

    def test_get_sessions_positive_limit_still_works(self):
        from hermes_state import SessionDB

        db = SessionDB()
        try:
            for i in range(5):
                db.create_session(session_id=f"pos-limit-{i}", source="cli")
                db.append_message(session_id=f"pos-limit-{i}", role="user", content="hi")
        finally:
            db.close()

        resp = self.client.get("/api/sessions?limit=3&offset=0")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["limit"] == 3
        assert len(payload["sessions"]) == 3

    def test_profiles_sessions_rejects_negative_limit(self):
        """Same guard on the cross-profile aggregate route — negative limit
        previously bypassed the per-profile 500-row clamp entirely."""
        resp = self.client.get("/api/profiles/sessions?limit=-1")
        assert resp.status_code == 422

    def test_profiles_sessions_rejects_negative_offset(self):
        resp = self.client.get("/api/profiles/sessions?offset=-1")
        assert resp.status_code == 422

    def test_profiles_sessions_positive_limit_still_works(self):
        from hermes_state import SessionDB

        db = SessionDB()
        try:
            for i in range(5):
                db.create_session(session_id=f"pos-plimit-{i}", source="cli")
                db.append_message(session_id=f"pos-plimit-{i}", role="user", content="hi")
        finally:
            db.close()

        resp = self.client.get("/api/profiles/sessions?limit=3&offset=0")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["limit"] == 3
        assert len(payload["sessions"]) == 3

    def test_get_session_messages_rejects_negative_limit(self):
        """limit=-1 previously bypassed the documented 500-row clamp because
        min(-1, 500) == -1, which SQLite treats as 'no limit'."""
        from hermes_state import SessionDB

        db = SessionDB()
        try:
            db.create_session(session_id="neg-limit-messages", source="cli")
            for i in range(60):
                db.append_message(
                    session_id="neg-limit-messages", role="user", content=f"msg {i}"
                )
        finally:
            db.close()

        resp = self.client.get("/api/sessions/neg-limit-messages/messages?limit=-1")
        assert resp.status_code == 422

    def test_get_session_messages_rejects_negative_offset(self):
        from hermes_state import SessionDB

        db = SessionDB()
        try:
            db.create_session(session_id="neg-offset-messages", source="cli")
            db.append_message(session_id="neg-offset-messages", role="user", content="hi")
        finally:
            db.close()

        resp = self.client.get("/api/sessions/neg-offset-messages/messages?offset=-1")
        assert resp.status_code == 422

    def test_get_session_messages_limit_above_500_is_capped_not_rejected(self):
        """A limit above the documented 500-row cap is silently clamped
        (existing ``min(limit, 500)`` behaviour), not rejected — the request
        still succeeds."""
        from hermes_state import SessionDB

        db = SessionDB()
        try:
            db.create_session(session_id="many-messages", source="cli")
            for i in range(60):
                db.append_message(session_id="many-messages", role="user", content=f"msg {i}")
        finally:
            db.close()

        resp = self.client.get("/api/sessions/many-messages/messages?limit=1000")
        assert resp.status_code == 200
        assert resp.json()["pagination"]["limit"] == 500







# ---------------------------------------------------------------------------
# _build_schema_from_config tests
# ---------------------------------------------------------------------------


class TestBuildSchemaFromConfig:


    def test_overrides_applied(self):
        from hermes_cli.web_server import CONFIG_SCHEMA
        # terminal.backend should be a select with options
        if "terminal.backend" in CONFIG_SCHEMA:
            entry = CONFIG_SCHEMA["terminal.backend"]
            assert entry["type"] == "select"
            assert "options" in entry
            assert "local" in entry["options"]
            assert "vercel_sandbox" in entry["options"]
        runtime_entry = CONFIG_SCHEMA["terminal.vercel_runtime"]
        assert runtime_entry["type"] == "select"
        assert "node24" in runtime_entry["options"]
        assert "python3.13" in runtime_entry["options"]
        assert len(runtime_entry["options"]) >= 3



    def test_timezone_field_is_searchable_select(self):
        """timezone must ship as a searchable, clearable select of IANA ids.

        Desktop renders this via SearchableSelect (Popover + cmdk); the old
        free-text input let users type invalid timezone strings (#68970).
        Invariants, not snapshots: valid IANA entries present, sorted, no
        blank entry server-side (the clear item is client-side via
        ``clearable``), and never empty even without tzdata (UTC fallback).
        """
        from hermes_cli.web_server import CONFIG_SCHEMA, _timezone_options

        entry = CONFIG_SCHEMA["timezone"]
        assert entry["type"] == "select"
        assert entry.get("searchable") is True
        assert entry.get("clearable") is True
        options = entry["options"]
        assert len(options) >= 1
        assert options == sorted(options)
        assert "" not in options
        assert "UTC" in options
        # Fallback path: never returns an empty list.
        assert len(_timezone_options()) >= 1

    def test_dynamic_merge_recomputes_memory_provider_options(self, monkeypatch):
        """The per-request schema merge re-discovers memory providers.

        The import-time _SCHEMA_OVERRIDES freezes the list at server start;
        _schema_with_dynamic_provider_options must recompute it so a provider
        installed mid-session is selectable without a restart.
        """
        from hermes_cli import web_server

        monkeypatch.setattr(web_server, "load_config", lambda: {"memory": {"provider": "honcho"}})
        monkeypatch.setattr(
            web_server,
            "_memory_provider_options",
            lambda: ["", "honcho", "hindsight", "freshly_installed"],
        )

        fields = web_server._schema_with_dynamic_provider_options()

        assert "freshly_installed" in fields["memory.provider"]["options"]
        # The entry is copied, not mutated in place, and keeps its select type.
        assert fields["memory.provider"]["type"] == "select"
        assert web_server.CONFIG_SCHEMA["memory.provider"] is not fields["memory.provider"]







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






    def test_round_trip_preserves_schema_invisible_nested_keys(self):
        """Nested keys that aren't in CONFIG_SCHEMA must also survive a
        round-trip. Deep-merge is required — a shallow merge would drop
        ``agent.<custom_key>`` when the frontend sends a partial ``agent``
        dict containing only schema-known sub-fields."""
        from hermes_cli.config import load_config, read_raw_config, save_config

        # Seed config with a key under `agent` that isn't in the schema.
        # Use a sentinel name to avoid colliding with future schema fields.
        save_config({
            "agent": {
                "max_turns": 50,
                "x_dashboard_invisible_test_key": {"nested": "value"},
            },
        })

        # PUT only schema-known agent fields, exactly like the dashboard.
        web_config = self.client.get("/api/config").json()
        web_config.setdefault("agent", {})
        web_config["agent"]["max_turns"] = 75
        # Strip our sentinel so we're sending what the schema-driven form
        # would send.
        web_config["agent"].pop("x_dashboard_invisible_test_key", None)

        resp = self.client.put("/api/config", json={"config": web_config})
        assert resp.status_code == 200

        on_disk = read_raw_config()
        assert on_disk.get("agent", {}).get("max_turns") == 75
        assert on_disk.get("agent", {}).get("x_dashboard_invisible_test_key") \
            == {"nested": "value"}, \
            "Shallow-merge regression: agent.x_dashboard_invisible_test_key " \
            "was wiped when the frontend sent a partial agent dict."

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
        assert not mismatches, "Type mismatches:\n" + "\n".join(mismatches)

    def test_desktop_terminal_font_round_trip_preserves_terminal_config(self):
        """The Appearance picker persists a font without replacing sibling settings."""
        from hermes_cli.config import load_config

        web_config = self.client.get("/api/config").json()
        terminal_before = dict(web_config.get("terminal", {}))
        web_config.setdefault("terminal", {})["font_family"] = "MesloLGS NF"

        response = self.client.put("/api/config", json={"config": web_config})

        assert response.status_code == 200
        persisted = load_config()["terminal"]
        assert persisted["font_family"] == "MesloLGS NF"
        for key, value in terminal_before.items():
            if key != "font_family":
                assert persisted[key] == value

        reloaded = self.client.get("/api/config").json()
        assert reloaded["terminal"]["font_family"] == "MesloLGS NF"


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

    # --- Automation Blueprints ---




    # --- Profiles ---



    def test_profiles_create_builder_mcp_auth_is_profile_scoped(
        self, monkeypatch
    ):
        from hermes_constants import get_hermes_home
        import hermes_cli.profiles as profiles_mod

        monkeypatch.setattr(profiles_mod, "create_wrapper_script", lambda name: None)

        secret = "profile-builder-secret"
        resp = self.client.post(
            "/api/profiles",
            json={
                "name": "builder-auth",
                "mcp_servers": [
                    {
                        "name": "Bearer Server",
                        "url": "https://example.com/mcp",
                        "auth": "header",
                        "bearer_token": f"Bearer {secret}",
                    },
                    {
                        "name": "oauth-server",
                        "url": "https://example.com/oauth-mcp",
                        "auth": "oauth",
                    },
                    {
                        "name": "local-server",
                        "command": "uvx",
                        "args": ["mcp-server", "--debug"],
                        "env": {"API_KEY": "stdio-secret"},
                    },
                    {
                        "name": "missing-token",
                        "url": "https://example.com/bad",
                        "auth": "header",
                    },
                    {
                        "name": "http-with-env",
                        "url": "https://example.com/bad-env",
                        "env": {"NOT_SUPPORTED": "value"},
                    },
                ],
            },
        )

        assert resp.status_code == 200
        assert resp.json()["mcp_written"] == 3

        root = get_hermes_home()
        profile_dir = root / "profiles" / "builder-auth"
        config_text = (profile_dir / "config.yaml").read_text(encoding="utf-8")
        config = yaml.safe_load(config_text)
        servers = config["mcp_servers"]

        assert sorted(servers) == [
            "Bearer Server",
            "local-server",
            "oauth-server",
        ]
        assert servers["Bearer Server"] == {
            "url": "https://example.com/mcp",
            "headers": {
                "Authorization": "Bearer ${MCP_BEARER_SERVER_API_KEY}",
            },
        }
        assert servers["oauth-server"] == {
            "url": "https://example.com/oauth-mcp",
            "auth": "oauth",
        }
        assert servers["local-server"] == {
            "command": "uvx",
            "args": ["mcp-server", "--debug"],
            "env": {"API_KEY": "stdio-secret"},
        }

        assert secret not in config_text
        profile_env = (profile_dir / ".env").read_text(encoding="utf-8")
        assert f"MCP_BEARER_SERVER_API_KEY={secret}" in profile_env
        assert "Bearer Bearer" not in profile_env
        assert not (root / ".env").exists()



    # --- New profiles endpoints: active / description / model / describe-auto ---












    def test_discord_toolsets_read_and_write_discord_platform(self):
        """Platform-restricted toolsets must not be saved as successful CLI no-ops."""
        from hermes_cli.config import load_config

        listing = {t["name"]: t for t in self.client.get("/api/tools/toolsets").json()}
        assert listing["discord"]["platform"] == "discord"
        assert listing["discord"]["platform_label"] == "Discord"
        assert listing["discord"]["enabled"] is False

        resp = self.client.put("/api/tools/toolsets/discord", json={"enabled": True})
        assert resp.status_code == 200
        assert resp.json() == {
            "ok": True,
            "name": "discord",
            "platform": "discord",
            "enabled": True,
        }

        config = load_config()
        assert "discord" in config["platform_toolsets"]["discord"]
        assert "discord" not in config["platform_toolsets"].get("cli", [])

        listing = {t["name"]: t for t in self.client.get("/api/tools/toolsets").json()}
        assert listing["discord"]["enabled"] is True
        assert listing["discord_admin"]["enabled"] is False

        resp = self.client.put(
            "/api/tools/toolsets/discord_admin", json={"enabled": True}
        )
        assert resp.status_code == 200
        config = load_config()
        assert {"discord", "discord_admin"} <= set(
            config["platform_toolsets"]["discord"]
        )


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

    def test_get_toolset_config_reports_truthful_provider_status(self, monkeypatch):
        """Each provider row carries a server-computed readiness `status`.

        Regression: the GUI pilled every zero-env-var row "Ready" — including
        logged-out Nous Subscription rows, xAI TTS without Grok OAuth, and
        never-installed KittenTTS/Piper. The endpoint now reports the honest
        state so keyless ≠ ready.
        """
        import hermes_cli.tools_config as tools_config
        from hermes_cli.nous_account import NousPortalAccountInfo

        # Logged out of Nous Portal → managed subscription rows need sign-in.
        monkeypatch.setattr(
            "hermes_cli.nous_subscription.get_nous_portal_account_info",
            lambda *a, **k: NousPortalAccountInfo(
                logged_in=False, source="none", fresh=False, paid_service_access=None
            ),
        )
        # No xAI credentials → the Grok OAuth-backed row needs sign-in.
        monkeypatch.setattr(tools_config, "_xai_credentials_present", lambda: False)
        # Local TTS engines not installed → their rows need setup.
        monkeypatch.setattr(tools_config, "_module_installed", lambda name: False)
        monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)

        resp = self.client.get("/api/tools/toolsets/tts/config")
        assert resp.status_code == 200
        data = resp.json()
        by_name = {p["name"]: p for p in data["providers"]}

        valid = {"ready", "needs_keys", "needs_auth", "needs_setup"}
        assert all(p["status"] in valid for p in data["providers"])
        # Genuinely-free keyless row stays Ready.
        assert by_name["Microsoft Edge TTS"]["status"] == "ready"
        # Keyless ≠ ready for gated rows:
        assert by_name["Nous Subscription"]["status"] == "needs_auth"
        assert by_name["xAI TTS"]["status"] == "needs_auth"
        assert by_name["KittenTTS"]["status"] == "needs_setup"
        assert by_name["Piper"]["status"] == "needs_setup"
        # Keyed row with the key unset:
        assert by_name["ElevenLabs"]["status"] == "needs_keys"






    def test_select_managed_nous_provider_reports_needs_nous_auth(self, monkeypatch):
        """Selecting a managed Nous row while logged out flags needs_nous_auth.

        Regression: the GUI PUT wrote browser.cloud_provider + use_gateway
        but skipped the Portal entitlement handshake the CLI runs inline
        (ensure_nous_portal_access) — so the row never activated and nothing
        told the user to sign in. The endpoint now reports the entitlement
        gap so the client can drive the existing Nous OAuth flow.
        """
        from hermes_cli.nous_account import NousPortalAccountInfo

        monkeypatch.setattr(
            "hermes_cli.nous_subscription.get_nous_portal_account_info",
            lambda *a, **k: NousPortalAccountInfo(
                logged_in=False, source="none", fresh=False, paid_service_access=None
            ),
        )

        resp = self.client.put(
            "/api/tools/toolsets/browser/provider",
            json={"provider": "Nous Subscription (Browser Use cloud)"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["needs_nous_auth"] is True
        assert data["feature"] == "browser"
        # The selection is still persisted — activation is what's gated.
        from hermes_cli.config import load_config
        cfg = load_config()
        assert cfg["browser"]["cloud_provider"] == "browser-use"


    # -- Web capability split (search vs extract backends) ------------------



    def test_select_web_search_backend_matches_runtime_resolution(self, monkeypatch):
        """PUT provider with capability=search writes web.search_backend and the
        runtime search dispatcher resolves to it — while extract is untouched."""
        # Make SearXNG available so both the endpoint gate and the runtime
        # availability check agree it's usable.
        monkeypatch.setenv("SEARXNG_URL", "http://localhost:8888")
        # Give extract an explicit shared backend so the assertion isn't
        # hostage to whatever creds exist on the machine running the tests.
        monkeypatch.setenv("FIRECRAWL_API_URL", "http://localhost:3002")
        base = self.client.put(
            "/api/tools/toolsets/web/provider",
            json={"provider": "Firecrawl Self-Hosted"},
        )
        assert base.status_code == 200

        resp = self.client.put(
            "/api/tools/toolsets/web/provider",
            json={"provider": "SearXNG", "capability": "search"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["capability"] == "search"

        from hermes_cli.config import load_config
        cfg = load_config()
        assert cfg["web"]["search_backend"] == "searxng"
        # The shared backend selected first must be preserved for extract.
        assert cfg["web"]["backend"] == "firecrawl"

        # The REAL runtime resolution — not a parallel reimplementation.
        from tools.web_tools import _get_extract_backend, _get_search_backend
        assert _get_search_backend() == "searxng"
        assert _get_extract_backend() == "firecrawl"

        # And the config endpoint reports the same split.
        data = self.client.get("/api/tools/toolsets/web/config").json()
        assert data["active_search_backend"] == "searxng"
        assert data["active_extract_backend"] == "firecrawl"


    # -- Terminal execution backend picker ---------------------------------





    def test_terminal_ssh_probe_ready_when_configured(self, monkeypatch):
        """SSH host + user in config.yaml -> ready."""
        import hermes_cli.web_server as web_server
        from hermes_cli.config import load_config, save_config

        monkeypatch.setattr(web_server.shutil, "which", lambda name: None)
        config = load_config()
        config.setdefault("terminal", {})
        config["terminal"]["ssh_host"] = "devbox.example.com"
        config["terminal"]["ssh_user"] = "hermes"
        save_config(config)

        body = self.client.get("/api/tools/terminal/backends").json()
        ssh = next(r for r in body["backends"] if r["name"] == "ssh")
        assert ssh["status"] == "ready"
        assert "hermes@devbox.example.com" in ssh["detail"]









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


class TestDenormalizeProviderSwitch:
    """The flat Config-page Model field carries no provider info. When the
    model string changes to one served by a different provider, the saved
    provider must follow it (issue #14058)."""

    def test_vendor_slug_switches_off_non_aggregator_provider(self):
        """ollama-local + a vendor/model slug → switch to openrouter and drop
        the stale local base_url (the issue's exact repro)."""
        from hermes_cli.web_server import _denormalize_config_from_web
        from hermes_cli.config import save_config

        save_config({
            "model": {
                "default": "llama3.2",
                "provider": "ollama-local",
                "base_url": "http://localhost:11434/v1",
                "api_mode": "chat_completions",
            }
        })

        result = _denormalize_config_from_web({"model": "google/gemini-2.5-flash"})
        model = result["model"]
        assert model["provider"] == "openrouter"
        assert model["default"] == "google/gemini-2.5-flash"
        # The old ollama-local endpoint must not carry over to openrouter.
        assert not model.get("base_url")


    def test_context_length_override_survives_provider_switch(self):
        """An explicit context-length override must persist alongside a
        provider switch."""
        from hermes_cli.web_server import _denormalize_config_from_web
        from hermes_cli.config import save_config

        save_config({"model": {"default": "llama3.2", "provider": "ollama-local"}})

        result = _denormalize_config_from_web({
            "model": "google/gemini-2.5-flash",
            "model_context_length": 128000,
        })
        model = result["model"]
        assert model["provider"] == "openrouter"
        assert model["context_length"] == 128000


class TestModelContextLengthSchema:
    """Tests for model_context_length placement in CONFIG_SCHEMA."""


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


    def test_probe_uses_configured_short_timeout(self, monkeypatch):
        """The HTTP probe must not fall through to the OS TCP timeout."""
        import hermes_cli.web_server as ws

        monkeypatch.setattr(ws, "_GATEWAY_HEALTH_URL", "http://gw:8642")
        monkeypatch.setattr(ws, "_GATEWAY_HEALTH_TIMEOUT", 0.75)
        timeouts = []

        def mock_urlopen(req, **kwargs):
            timeouts.append(kwargs.get("timeout"))
            raise TimeoutError("mock timeout")

        monkeypatch.setattr(ws.urllib.request, "urlopen", mock_urlopen)

        alive, body = ws._probe_gateway_health()

        assert alive is False
        assert body is None
        assert timeouts == [0.75, 0.75]




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

        monkeypatch.setattr(ws, "get_running_pid_cached", lambda: None)
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

    def test_status_bounds_the_complete_remote_probe(self, monkeypatch):
        """Two serial HTTP attempts cannot consume more than the route budget."""
        import hermes_cli.web_server as ws

        probe_started = threading.Event()
        release_probe = threading.Event()

        def slow_probe():
            probe_started.set()
            release_probe.wait(timeout=0.5)
            return True, {"status": "ok", "pid": 999}

        monkeypatch.setattr(ws, "get_running_pid_cached", lambda: 1234)
        monkeypatch.setattr(ws, "read_runtime_status", lambda: None)
        monkeypatch.setattr(ws, "_GATEWAY_HEALTH_URL", "http://gw:8642")
        monkeypatch.setattr(ws, "_GATEWAY_HEALTH_ROUTE_TIMEOUT", 0.02)
        monkeypatch.setattr(ws, "_probe_gateway_health", slow_probe)

        # Warm one status request without the remote rung so plugin discovery
        # and other one-time endpoint setup are outside the timeout measurement.
        assert self.client.get("/api/status").status_code == 200
        monkeypatch.setattr(ws, "get_running_pid_cached", lambda: None)

        started = time.monotonic()
        try:
            resp = self.client.get("/api/status")
            elapsed = time.monotonic() - started
        finally:
            release_probe.set()

        assert probe_started.is_set()
        assert elapsed < 0.2, f"route timeout leaked executor shutdown wait: {elapsed:.3f}s"
        assert resp.status_code == 200
        assert resp.json()["gateway_running"] is False

    def test_status_remote_probe_not_attempted_when_local_pid_found(self, monkeypatch):
        """When local PID check succeeds, the remote probe is never called."""
        import hermes_cli.web_server as ws

        monkeypatch.setattr(ws, "get_running_pid_cached", lambda: 1234)
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


    def test_status_remote_running_null_pid(self, monkeypatch):
        """Remote gateway running but PID not in response — pid should be None."""
        import hermes_cli.web_server as ws

        monkeypatch.setattr(ws, "get_running_pid_cached", lambda: None)
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


class TestGatewayBusyReadout:
    """Tests for the NAS busy/drainable readout on /api/status.

    Behaviour contracts (not snapshots): assert how gateway_busy / gateway_drainable
    must RELATE to gateway_running + gateway_state + active_agents, and that every
    field degrades to a safe falsy value when the gateway is down or its status
    file is absent. Liveness must key off gateway_running, NEVER gateway_updated_at.
    """

    @pytest.fixture(autouse=True)
    def _setup_test_client(self):
        try:
            from starlette.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi/starlette not installed")

        from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN
        self.client = TestClient(app)
        self.client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN


    def test_draining_state_is_neither_busy_nor_drainable(self, monkeypatch):
        """While draining, the gateway is not a fresh begin-drain target, and
        busy is False even with a stale active_agents>0 in the file — the state
        gate dominates."""
        import hermes_cli.web_server as ws

        monkeypatch.setattr(ws, "get_running_pid_cached", lambda: 1234)
        monkeypatch.setattr(ws, "read_runtime_status", lambda: {
            "gateway_state": "draining",
            "platforms": {},
            "active_agents": 3,
        })

        data = self.client.get("/api/status").json()
        assert data["gateway_busy"] is False
        assert data["gateway_drainable"] is False


    def test_active_agents_unparseable_in_file_degrades_to_zero(self, monkeypatch):
        """A corrupt active_agents value in the status file must not 500 or
        produce a spurious busy — it degrades to 0/not-busy."""
        import hermes_cli.web_server as ws

        monkeypatch.setattr(ws, "get_running_pid_cached", lambda: 1234)
        monkeypatch.setattr(ws, "read_runtime_status", lambda: {
            "gateway_state": "running",
            "platforms": {},
            "active_agents": "garbage",
        })

        data = self.client.get("/api/status").json()
        assert data["active_agents"] == 0
        assert data["gateway_busy"] is False


class TestGatewayUpdatedAtContract:
    """Contract tests for /api/status ``gateway_updated_at``.

    The field is promised to consumers (web/src/lib/api.ts declares
    ``string | null``) as an RFC3339 timestamp or null — NEVER a number.
    Legacy gateways wrote epoch floats into gateway_state.json and the file
    is hand-editable, so the endpoint must normalize whatever it reads.
    """

    @pytest.fixture(autouse=True)
    def _setup_test_client(self):
        try:
            from starlette.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi/starlette not installed")

        from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN
        self.client = TestClient(app)
        self.client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN

    @staticmethod
    def _assert_contract(value):
        """gateway_updated_at is None or a tz-aware-parseable ISO string."""
        from datetime import datetime

        assert not isinstance(value, bool), f"bool leaked: {value!r}"
        assert not isinstance(value, (int, float)), f"number leaked: {value!r}"
        if value is not None:
            assert isinstance(value, str)
            parsed = datetime.fromisoformat(value)
            assert parsed.tzinfo is not None, f"naive timestamp leaked: {value!r}"


    def test_local_runtime_valid_epoch_becomes_iso_string(self, monkeypatch):
        """A plausible legacy epoch value is converted, not dropped."""
        from datetime import datetime, timezone
        import hermes_cli.web_server as ws

        epoch = 1750000000
        monkeypatch.setattr(ws, "get_running_pid_cached", lambda: 1234)
        monkeypatch.setattr(ws, "read_runtime_status", lambda: {
            "gateway_state": "running",
            "platforms": {},
            "active_agents": 0,
            "updated_at": epoch,
        })

        value = self.client.get("/api/status").json()["gateway_updated_at"]
        assert isinstance(value, str)
        parsed = datetime.fromisoformat(value)
        assert parsed.tzinfo is not None
        assert parsed == datetime.fromtimestamp(epoch, tz=timezone.utc)


    def test_remote_health_numeric_updated_at_normalized(self, monkeypatch):
        """Cross-container path: the remote /health/detailed body is the
        runtime source, and a numeric updated_at from an older gateway build
        must still come out as string|null."""
        import hermes_cli.web_server as ws

        monkeypatch.setattr(ws, "get_running_pid_cached", lambda: None)
        monkeypatch.setattr(ws, "read_runtime_status", lambda: None)
        monkeypatch.setattr(ws, "_GATEWAY_HEALTH_URL", "http://gw:8642")
        monkeypatch.setattr(ws, "_probe_gateway_health", lambda: (True, {
            "status": "ok",
            "gateway_state": "running",
            "platforms": {},
            "updated_at": 1750000000.25,
            "pid": 999,
        }))

        resp = self.client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["gateway_running"] is True
        self._assert_contract(data["gateway_updated_at"])
        # A plausible epoch is converted, not nulled.
        assert isinstance(data["gateway_updated_at"], str)


# ---------------------------------------------------------------------------
# Dashboard theme normaliser tests
# ---------------------------------------------------------------------------


class TestNormaliseThemeDefinition:
    """Tests for _normalise_theme_definition() — parses YAML theme files."""


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


    def test_ignores_transient_profile_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        themes_dir = tmp_path / "dashboard-themes"
        themes_dir.mkdir()
        (themes_dir / "mine.yaml").write_text("name: mine\n")

        other = tmp_path / "other-profile"
        other.mkdir()

        from hermes_constants import (
            reset_hermes_home_override,
            set_hermes_home_override,
        )
        from hermes_cli import web_server

        token = set_hermes_home_override(str(other))
        try:
            results = web_server._discover_user_themes()
        finally:
            reset_hermes_home_override(token)

        assert [r["name"] for r in results] == ["mine"]


class TestThemeBootstrapCSS:
    """Tests for _render_active_theme_bootstrap_css() and its injection
    into index.html via _serve_index() — the critical-CSS shim that kills
    the default-teal first-paint flash for user YAML themes."""

    @staticmethod
    def _write_theme(hermes_home, name="ocean"):
        themes_dir = hermes_home / "dashboard-themes"
        themes_dir.mkdir(exist_ok=True)
        (themes_dir / f"{name}.yaml").write_text(
            f"name: {name}\n"
            "label: Ocean\n"
            "palette:\n"
            "  background:\n"
            "    hex: \"#0a1628\"\n"
            "  midground:\n"
            "    hex: \"#dbe4f0\"\n"
            "typography:\n"
            "  fontSans: \"Inter, sans-serif\"\n"
            "  baseSize: \"17px\"\n",
            encoding="utf-8",
        )

    def test_user_theme_renders_bundle_vars(self, tmp_path, monkeypatch):
        """Active user theme → style block with ONLY variable names the
        bundle actually consumes (layerVars/typographyVars tokens)."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        self._write_theme(tmp_path)
        from hermes_cli import web_server
        monkeypatch.setattr(
            web_server, "load_config", lambda: {"dashboard": {"theme": "ocean"}}
        )
        css = web_server._render_active_theme_bootstrap_css()
        assert css.startswith('<style id="hermes-theme-bootstrap">')
        assert css.endswith("</style>")
        # Real bundle tokens (web/src/themes/context.tsx + index.css).
        assert "--background-base:#0a1628;" in css
        assert "--midground-base:#dbe4f0;" in css
        assert "--theme-font-sans:Inter, sans-serif;" in css
        assert "--theme-base-size:17px;" in css
        # Names that do NOT exist in the bundle must not be emitted.
        for bogus in ("--color-background", "--color-midground",
                      "--font-sans:", "--font-base-size"):
            assert bogus not in css
        # Canvas rule flows through the variables (never goes stale when
        # applyTheme() rewrites them as inline styles at runtime).
        assert "html,body{background-color:var(--background-base);" in css
        assert "font-family:var(--theme-font-sans);" in css
        assert "font-size:var(--theme-base-size);" in css
        # No baked literal values in the html,body rule.
        assert "#0a1628" not in css.split("html,body")[1]






    @staticmethod
    def _mount_spa_client(tmp_path, monkeypatch):
        from fastapi import FastAPI
        from starlette.testclient import TestClient
        import hermes_cli.web_server as ws

        dist = tmp_path / "web_dist"
        (dist / "assets").mkdir(parents=True)
        (dist / "index.html").write_text(
            "<html><head><title>t</title></head><body>SPA</body></html>",
            encoding="utf-8",
        )
        monkeypatch.setattr(ws, "WEB_DIST", dist)
        spa_app = FastAPI()
        ws.mount_spa(spa_app)
        return TestClient(spa_app)

    def test_serve_index_injects_bootstrap_for_user_theme(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        self._write_theme(tmp_path)
        import hermes_cli.web_server as ws
        monkeypatch.setattr(
            ws, "load_config", lambda: {"dashboard": {"theme": "ocean"}}
        )
        client = self._mount_spa_client(tmp_path, monkeypatch)
        resp = client.get("/chat")
        assert resp.status_code == 200
        assert '<style id="hermes-theme-bootstrap">' in resp.text
        assert "--background-base:#0a1628;" in resp.text
        # Injected inside <head>, before the closing tag.
        head = resp.text.split("</head>")[0]
        assert "hermes-theme-bootstrap" in head




class TestNormaliseThemeExtensions:
    """Tests for the extended normaliser fields (assets, customCSS,
    componentStyles, layoutVariant) — the surfaces themes use to reskin
    the dashboard without shipping code."""





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




class TestDeleteSessionEndpoint:
    """Tests for ``DELETE /api/sessions/{session_id}`` — the single-row delete
    behind the desktop sidebar's per-session delete.

    The desktop optimistically removes the row, then RESTORES it on any error
    and surfaces the message. So a 404 on a row that is already gone (reaped by
    empty-session hygiene, or removed by a concurrent client — both common amid
    /goal + auto-compression churn that leaves transient empty rows) resurrected
    a ghost row and showed "session not found". DELETE must be idempotent and
    resolve ids like every other session endpoint.
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

    def _exists(self, sid) -> bool:
        from hermes_state import SessionDB

        db = SessionDB()
        try:
            return db.get_session(sid) is not None
        finally:
            db.close()


    def test_delete_absent_session_is_idempotent(self):
        # PREMISE / regression: deleting a row that no longer exists must NOT
        # 404 — the desktop would resurrect the ghost row and show
        # "session not found". DELETE's contract is "ensure it's gone".
        resp = self.auth_client.delete("/api/sessions/never_existed")
        assert resp.status_code == 200
        assert resp.json().get("ok") is True


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


    def test_delete_endpoint_requires_auth(self):
        """DELETE /api/sessions/empty must 401 without the session token.

        Regression guard for issue #19533 — the bulk-delete is a strictly
        destructive primitive, the middleware must gate it even if a
        future refactor introduces a non-auth path."""
        resp = self.client.delete("/api/sessions/empty")
        assert resp.status_code == 401


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

    def test_user_plugins_ignore_profile_home_override(self, tmp_path, monkeypatch):
        """Regression: user dashboard extensions are a dashboard-owned asset
        (like theme YAML), so they must stay visible after a context-local
        HERMES_HOME override scopes a request to another profile."""
        from hermes_constants import (
            reset_hermes_home_override,
            set_hermes_home_override,
        )
        launch_home = tmp_path / "launch"
        launch_home.mkdir()
        self._write_plugin(launch_home, "skin-home", {
            "name": "skin-home",
            "label": "Skin Home",
            "tab": {"path": "/skin-home"},
            "entry": "dist/index.js",
        })
        other = tmp_path / "other-profile"
        other.mkdir()

        monkeypatch.setenv("HERMES_HOME", str(launch_home))
        from hermes_cli import web_server
        token = set_hermes_home_override(str(other))
        try:
            plugins = web_server._discover_dashboard_plugins()
        finally:
            reset_hermes_home_override(token)
        assert any(p["name"] == "skin-home" for p in plugins)




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
        ws.app.state.pty_active_session_files = {}
        self.token = ws._SESSION_TOKEN
        self.client = TestClient(ws.app)

    def _url(self, token: str | None = None, **params: str) -> str:
        tok = token if token is not None else self.token
        # TestClient.websocket_connect takes the path; it reconstructs the
        # query string, so we pass it inline.
        from urllib.parse import urlencode

        q = {"token": tok, **params}
        return f"/api/pty?{urlencode(q)}"


        monkeypatch.setattr(
            main_mod,
            "_make_tui_argv",
            lambda project_root, tui_dev=False: (["node", "dist/entry.js"], "/tmp/ui-tui"),
        )

        _argv, _cwd, env = self.ws_module._resolve_chat_argv()

        assert env["HERMES_TUI_DASHBOARD"] == "1"
        assert env["HERMES_TUI_INLINE"] == "1"
        assert env["HERMES_TUI_DISABLE_MOUSE"] == "1"

    def test_resolve_chat_argv_injects_managed_autonomy_for_authenticated_assistant(self, monkeypatch):
        import hermes_cli.main as main_mod

        monkeypatch.setattr(
            main_mod,
            "_make_tui_argv",
            lambda project_root, tui_dev=False: (["node", "dist/entry.js"], "/tmp/ui-tui"),
        )
        monkeypatch.setattr(self.ws_module, "_DASHBOARD_MODE", "assistant")

        _argv, _cwd, env = self.ws_module._resolve_chat_argv(
            actor_context={
                "tenant_id": "example-tenant",
                "actor_id": "example-tenant:customer:user",
                "role": "user",
                "display_name": "Customer User",
            }
        )

        assert env is not None
        assert env["AIWERK_CUI_TENANT_ID"] == "example-tenant"
        assert env["AIWERK_CUI_ACTOR_ID"] == "example-tenant:customer:user"
        assert env["AIWERK_CUI_ACTOR_ROLE"] == "user"
        assert env["AIWERK_CUI_MANAGED_AUTONOMY"] == "1"

    def test_resolve_chat_argv_does_not_inject_managed_autonomy_in_admin_dashboard(self, monkeypatch):
        import hermes_cli.main as main_mod

        monkeypatch.setattr(
            main_mod,
            "_make_tui_argv",
            lambda project_root, tui_dev=False: (["node", "dist/entry.js"], "/tmp/ui-tui"),
        )
        monkeypatch.setattr(self.ws_module, "_DASHBOARD_MODE", "admin")
        monkeypatch.setenv("AIWERK_CUI_MANAGED_AUTONOMY", "1")

        _argv, _cwd, env = self.ws_module._resolve_chat_argv(
            actor_context={"tenant_id": "example-tenant", "actor_id": "operator:admin", "role": "admin"}
        )

        assert env is not None
        assert env["AIWERK_CUI_ACTOR_ROLE"] == "admin"
        assert "AIWERK_CUI_MANAGED_AUTONOMY" not in env

    def test_resolve_chat_argv_requires_actor_id_for_managed_autonomy(self, monkeypatch):
        import hermes_cli.main as main_mod

        monkeypatch.setattr(
            main_mod,
            "_make_tui_argv",
            lambda project_root, tui_dev=False: (["node", "dist/entry.js"], "/tmp/ui-tui"),
        )
        monkeypatch.setattr(self.ws_module, "_DASHBOARD_MODE", "assistant")

        _argv, _cwd, env = self.ws_module._resolve_chat_argv(
            actor_context={"tenant_id": "example-tenant", "role": "user"}
        )

        assert env is not None
        assert env["AIWERK_CUI_TENANT_ID"] == "example-tenant"
        assert env["AIWERK_CUI_ACTOR_ROLE"] == "user"
        assert "AIWERK_CUI_MANAGED_AUTONOMY" not in env

    def test_resolve_chat_argv_strips_inherited_cui_trust_env(self, monkeypatch):
        import hermes_cli.main as main_mod

        monkeypatch.setattr(
            main_mod,
            "_make_tui_argv",
            lambda project_root, tui_dev=False: (["node", "dist/entry.js"], "/tmp/ui-tui"),
        )
        monkeypatch.setenv("AIWERK_CUI_MANAGED_AUTONOMY", "1")
        monkeypatch.setenv("AIWERK_CUI_ACTOR_CONTEXT", '{"tenant_id":"leaked"}')

        _argv, _cwd, env = self.ws_module._resolve_chat_argv()

        assert env is not None
        assert "AIWERK_CUI_MANAGED_AUTONOMY" not in env
        assert "AIWERK_CUI_ACTOR_CONTEXT" not in env

    def test_resolve_chat_argv_backfills_colorterm_truecolor(self, monkeypatch):
        """Headless servers advertise truecolor to the TUI child."""
        import hermes_cli.main as main_mod

        monkeypatch.setattr(
            main_mod,
            "_make_tui_argv",
            lambda project_root, tui_dev=False: (["node", "dist/entry.js"], "/tmp/ui-tui"),
        )
        monkeypatch.delenv("COLORTERM", raising=False)

        _argv, _cwd, env = self.ws_module._resolve_chat_argv()

        assert env["COLORTERM"] == "truecolor"

    def test_resolve_chat_argv_keeps_operator_colorterm(self, monkeypatch):
        """An explicit operator COLORTERM wins over the backfill."""
        import hermes_cli.main as main_mod

        monkeypatch.setattr(
            main_mod,
            "_make_tui_argv",
            lambda project_root, tui_dev=False: (["node", "dist/entry.js"], "/tmp/ui-tui"),
        )
        monkeypatch.setenv("COLORTERM", "24bit")

        _argv, _cwd, env = self.ws_module._resolve_chat_argv()

        assert env["COLORTERM"] == "24bit"

    def test_resolve_chat_argv_sets_tui_python_environment(self, monkeypatch):
        """Dashboard chat gives the Node TUI the same Python env as CLI launches."""
        import hermes_cli.main as main_mod

        monkeypatch.delenv("HERMES_PYTHON_SRC_ROOT", raising=False)
        monkeypatch.delenv("HERMES_PYTHON", raising=False)
        monkeypatch.delenv("HERMES_CWD", raising=False)
        monkeypatch.setattr(
            main_mod,
            "_make_tui_argv",
            lambda project_root, tui_dev=False: (["node", "dist/entry.js"], "/tmp/ui-tui"),
        )

        _argv, _cwd, env = self.ws_module._resolve_chat_argv()

        assert env is not None
        assert env["HERMES_PYTHON_SRC_ROOT"] == str(main_mod.PROJECT_ROOT)
        assert env["HERMES_PYTHON"] == sys.executable
        assert env["HERMES_CWD"] == os.getcwd()

    def test_resolve_chat_argv_replaces_invalid_tui_python_environment(self, monkeypatch):
        """Dashboard chat does not preserve unusable inherited TUI Python env."""
        import hermes_cli.main as main_mod

        monkeypatch.setenv("HERMES_PYTHON_SRC_ROOT", "/definitely/missing/hermes-src")
        monkeypatch.setenv("HERMES_PYTHON", "/definitely/missing/python")
        monkeypatch.setenv("HERMES_CWD", "/definitely/missing/cwd")
        monkeypatch.setattr(
            main_mod,
            "_make_tui_argv",
            lambda project_root, tui_dev=False: (["node", "dist/entry.js"], "/tmp/ui-tui"),
        )

        _argv, _cwd, env = self.ws_module._resolve_chat_argv()

        assert env is not None
        assert env["HERMES_PYTHON_SRC_ROOT"] == str(main_mod.PROJECT_ROOT)
        assert env["HERMES_PYTHON"] == sys.executable
        assert env["HERMES_CWD"] == os.getcwd()

    def test_resolve_chat_argv_keeps_relative_python_under_tui_cwd(
        self, monkeypatch, tmp_path
    ):
        """Relative Python paths are resolved from the TUI child's cwd."""
        import hermes_cli.main as main_mod

        relative_python = Path(".review-venv") / "bin" / Path(sys.executable).name
        python_path = tmp_path / relative_python
        python_path.parent.mkdir(parents=True)
        # copy2, not os.link: tmp_path may sit on a different filesystem than
        # the venv (tmpfs /tmp vs disk home) where hard links raise EXDEV.
        shutil.copy2(sys.executable, python_path)
        monkeypatch.setenv("HERMES_CWD", str(tmp_path))
        monkeypatch.setenv("HERMES_PYTHON", str(relative_python))
        monkeypatch.setattr(
            main_mod,
            "_make_tui_argv",
            lambda project_root, tui_dev=False: (["node", "dist/entry.js"], "/tmp/ui-tui"),
        )

        _argv, _cwd, env = self.ws_module._resolve_chat_argv()

        assert env is not None
        assert env["HERMES_PYTHON"] == str(relative_python)

    def test_tui_python_command_uses_child_path(self, tmp_path):
        """Bare Python commands are resolved from the TUI child's PATH."""
        import hermes_cli.main as main_mod

        command = f"hermes-review-python{Path(sys.executable).suffix}"
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        executable = bin_dir / command
        # copy2, not os.link: tmp_path may sit on a different filesystem than
        # the venv (tmpfs /tmp vs disk home) where hard links raise EXDEV.
        shutil.copy2(sys.executable, executable)
        env = {
            "HERMES_CWD": str(tmp_path),
            "HERMES_PYTHON": command,
            "PATH": str(bin_dir),
        }

        main_mod._apply_tui_python_env(env)

        assert env["HERMES_PYTHON"] == command




        assert env is not None
        assert env["HERMES_CWD"] == str(tmp_path)

    def test_resolve_chat_argv_applies_terminal_backend_config(
        self, monkeypatch, _isolate_hermes_home
    ):
        import hermes_cli.main as main_mod

        config_path = Path(os.environ["HERMES_HOME"]) / "config.yaml"
        config_path.write_text(
            "\n".join(
                [
                    "terminal:",
                    "  backend: docker",
                    "  docker_image: example/hermes-tools:latest",
                    "  docker_extra_args:",
                    "    - --network=host",
                ]
            ),
            encoding="utf-8",
        )
        monkeypatch.delenv("TERMINAL_ENV", raising=False)
        monkeypatch.delenv("TERMINAL_DOCKER_IMAGE", raising=False)
        monkeypatch.delenv("TERMINAL_DOCKER_EXTRA_ARGS", raising=False)
        monkeypatch.setattr(
            main_mod,
            "_make_tui_argv",
            lambda project_root, tui_dev=False: (["node", "dist/entry.js"], "/tmp/ui-tui"),
        )

        _argv, _cwd, env = self.ws_module._resolve_chat_argv()

        assert env["TERMINAL_ENV"] == "docker"
        assert env["TERMINAL_DOCKER_IMAGE"] == "example/hermes-tools:latest"
        assert env["TERMINAL_DOCKER_EXTRA_ARGS"] == '["--network=host"]'

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
            lambda resume=None, sidecar_url=None, profile=None, actor_context=None: (["/bin/cat"], None, None),
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
            lambda resume=None, sidecar_url=None, profile=None, actor_context=None: (["/bin/cat"], None, None),
        )
        from starlette.websockets import WebSocketDisconnect

        with pytest.raises(WebSocketDisconnect) as exc:
            with self.client.websocket_connect(self._url(token="wrong")):
                pass
        assert exc.value.code == 4401

    def test_resolve_chat_argv_async_uses_worker_thread(self, monkeypatch):
        captured: dict = {}

        def fake_resolve(resume=None, sidecar_url=None, profile=None):
            captured["resume"] = resume
            captured["sidecar_url"] = sidecar_url
            captured["profile"] = profile
            return (["node", "dist/entry.js"], "/tmp/ui-tui", {"NODE_ENV": "production"})

        async def fake_to_thread(fn, *args, **kwargs):
            captured["thread_fn"] = fn
            captured["thread_args"] = args
            captured["thread_kwargs"] = kwargs
            return fn(*args, **kwargs)

        monkeypatch.setattr(self.ws_module, "_resolve_chat_argv", fake_resolve)
        monkeypatch.setattr(self.ws_module.asyncio, "to_thread", fake_to_thread)

        argv, cwd, env = asyncio.run(
            self.ws_module._resolve_chat_argv_async(
                resume="sess-42",
                sidecar_url="ws://127.0.0.1:9119/api/pub?channel=abc",
                profile="worker",
            )
        )

        assert callable(captured["thread_fn"])
        assert captured["thread_args"] == ()
        assert captured["thread_kwargs"] == {
            "resume": "sess-42",
            "sidecar_url": "ws://127.0.0.1:9119/api/pub?channel=abc",
            "profile": "worker",
        }
        assert argv == ["node", "dist/entry.js"]
        assert cwd == "/tmp/ui-tui"
        assert env == {"NODE_ENV": "production"}
        assert captured["resume"] == "sess-42"
        assert captured["sidecar_url"] == "ws://127.0.0.1:9119/api/pub?channel=abc"
        assert captured["profile"] == "worker"


    def _assert_pty_propagates(self, monkeypatch, raising_resolver, *, profile=None, expect_detail=None):
        """Drive /api/pty with a resolver that raises, and assert the error
        propagates through the real _resolve_chat_argv_async -> asyncio.to_thread
        -> lock -> re-raise chain into pty_ws's handler: the "Chat unavailable"
        notice is sent and the socket closes with code 1011 (the stable
        contract — we assert the close code, not the exact notice wording)."""
        from starlette.websockets import WebSocketDisconnect

        # Patch the REAL resolver so the whole wrapper/to_thread/lock chain runs.
        monkeypatch.setattr(self.ws_module, "_resolve_chat_argv", raising_resolver)

        url = self._url(profile=profile) if profile else self._url()
        with self.client.websocket_connect(url) as conn:
            notice = conn.receive_text()
            with pytest.raises(WebSocketDisconnect) as exc:
                conn.receive_text()
        assert "Chat unavailable" in notice
        assert exc.value.code == 1011
        if expect_detail is not None:
            assert expect_detail in notice





        def bad_profile(resume=None, sidecar_url=None, profile=None):
            raise HTTPException(status_code=404, detail="unknown profile")

        self._assert_pty_propagates(
            monkeypatch, bad_profile, profile="ghost", expect_detail="unknown profile"
        )

    def test_streams_child_stdout_to_client(self, monkeypatch):
        monkeypatch.setattr(
            self.ws_module,
            "_resolve_chat_argv",
            lambda resume=None, sidecar_url=None, profile=None, actor_context=None: (
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
            lambda resume=None, sidecar_url=None, profile=None, actor_context=None: (["/bin/cat"], None, None),
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
            lambda resume=None, sidecar_url=None, profile=None, actor_context=None: (
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
                # guard a missed-marker run blocks until a test timeout
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
            lambda resume=None, sidecar_url=None, profile=None, actor_context=None: (["/bin/cat"], None, None),
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

        def fake_resolve(resume=None, sidecar_url=None, profile=None, actor_context=None):
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

        def fake_resolve(resume=None, sidecar_url=None, profile=None, actor_context=None, active_session_file=None):
            captured["sidecar_url"] = sidecar_url
            captured["active_session_file"] = active_session_file
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
        assert captured["active_session_file"]

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


    def test_manifest_json_still_served(self):
        """JSON files remain browser-fetchable — manifests, localized
        data, source maps, etc. all sit in this bucket."""
        resp = self.client.get("/dashboard-plugins/example/manifest.json")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/json")
        # And the body is actually the manifest, not the SPA fallback.
        body = resp.json()
        assert body.get("name") == "example"


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



    def test_network_error_is_unreachable_not_blocking(self, monkeypatch):
        monkeypatch.setattr("httpx.Client", _fake_httpx_client(raise_exc=True))
        data = self._post("OPENROUTER_API_KEY", "sk-real").json()
        assert data["ok"] is False and data["reachable"] is False



    def test_local_endpoint_forwards_api_key_as_bearer(self, monkeypatch):
        """A custom endpoint that gates /v1/models behind auth must still
        enumerate models: the optional api_key is sent as a Bearer header so the
        probe doesn't come back empty (the desktop loop's root cause)."""
        captured = {}

        class _Resp:
            status_code = 200
            is_success = True

            def json(self):
                return {"data": [{"id": "gpt-oss-120b"}]}

        class _Client:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, url, *a, headers=None, **k):
                captured["url"] = url
                captured["headers"] = headers
                return _Resp()

        monkeypatch.setattr("httpx.Client", _Client)

        resp = self.client.post(
            "/api/providers/validate",
            json={
                "key": "OPENAI_BASE_URL",
                "value": "https://text.example.com/v1",
                "api_key": "sk-secret",
            },
        )
        data = resp.json()
        assert data["ok"] is True and data["reachable"] is True
        assert data["models"] == ["gpt-oss-120b"]
        assert captured["url"] == "https://text.example.com/v1/models"
        assert captured["headers"] == {"Authorization": "Bearer sk-secret"}


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
            "session.create", "session.title", "session.notes",
            "session.usage", "session.interrupt", "session.steer", "session.side.start",
            "session.side.back", "prompt.submit", "prompt.learn", "approval.respond",
        ]:
            assert gate({"id": 1, "method": method, "params": {}}) is None, method

    def test_session_resume_is_method_allowed_but_not_authorization(self):
        # session.resume is allowed as a METHOD (the chat UI needs it), but the
        # gate is NOT the authorization boundary for it: a method-level allow no
        # longer implies a customer may resume an arbitrary session_id. Per-actor
        # session visibility is enforced inside the gateway session.resume
        # handler against the dispatch actor_context (see
        # tests/tui_gateway/test_cui_actor_gateway_context.py). This test pins
        # that the gate still lets the method through (so the handler can run and
        # apply its own visibility check) rather than the gate silently being the
        # only control.
        gate = self._gate()
        assert gate({"id": 1, "method": "session.resume", "params": {}}) is None
        assert gate(
            {"id": 1, "method": "session.resume",
             "params": {"session_id": "someone-elses-session"}}
        ) is None

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

    def test_config_set_yolo_global_scope_is_refused(self):
        gate = self._gate()
        assert gate(
            {"method": "config.set", "params": {"key": "yolo", "scope": "session", "value": "1"}}
        ) is None
        reason = gate(
            {"method": "config.set", "params": {"key": "yolo", "scope": "global", "value": "1"}}
        )
        assert reason is not None and "global yolo" in reason

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


class TestTrustedCuiActorInjection:
    """The server-minted ws-ticket identity must be authoritative; a client
    cannot spoof _cui_actor_role to escalate skill visibility.
    """

    def _inject(self):
        import hermes_cli.web_server as web_server

        return web_server._inject_trusted_cui_actor

    def test_client_supplied_role_is_overwritten_not_kept(self):
        inject = self._inject()
        # Customer tries to spoof admin; server ticket says role=user.
        params = {"_cui_actor_role": "admin", "actor_role": "admin", "method_arg": 1}
        inject(params, {"role": "user", "actor_id": "customer-1", "tenant_id": "example-tenant"})
        assert params["_cui_actor_role"] == "user"  # server wins
        assert params["_cui_actor_id"] == "customer-1"
        assert params["_cui_tenant_id"] == "example-tenant"
        # The legacy alias key the consumer also reads must be stripped.
        assert "actor_role" not in params
        # Unrelated params are preserved.
        assert params["method_arg"] == 1

    def test_client_supplied_actor_and_tenant_are_overwritten(self):
        inject = self._inject()
        params = {"_cui_actor_id": "victim", "_cui_tenant_id": "other-co"}
        inject(params, {"role": "user", "actor_id": "customer-1", "tenant_id": "example-tenant"})
        assert params["_cui_actor_id"] == "customer-1"
        assert params["_cui_tenant_id"] == "example-tenant"

    def test_no_trusted_identity_strips_client_keys(self):
        # If the ticket carries no identity, client-supplied keys must NOT
        # survive (default-safe: no role => not admin).
        inject = self._inject()
        params = {"_cui_actor_role": "admin"}
        inject(params, {})
        assert "_cui_actor_role" not in params

    def test_spoofed_admin_does_not_make_actor_admin(self):
        # End-to-end through the gateway helpers: a spoofed admin role does not
        # make tui_gateway treat the actor as admin for skill visibility.
        from tui_gateway import server as gw

        inject = self._inject()
        params = {"_cui_actor_role": "admin"}
        inject(params, {"role": "user", "actor_id": "customer-1", "tenant_id": "example-tenant"})
        assert gw._cui_actor_role_from_params(params) == "user"
        assert gw._cui_actor_is_admin(params) is False


class TestAdminApiPermissionEnforcement:
    """decide_dashboard_permission must be WIRED into a real HTTP enforcement
    point: a non-admin session is rejected for an _ADMIN_ONLY_ACTIONS endpoint.
    """

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

    def test_non_admin_session_is_denied_admin_endpoint(self):
        import hermes_cli.web_server as web_server
        from hermes_cli.dashboard_auth.base import Session

        def _sess(role):
            return Session(
                user_id="u1", email="u@x.test", display_name="U", org_id="t1",
                provider="basic", expires_at=9999999999, access_token="at",
                refresh_token="rt", tenant_id="t1", actor_id="a1", role=role,
            )

        class _State:
            pass

        class _FakeRequest:
            def __init__(self, method, path, session):
                self.method = method
                self.url = type("U", (), {"path": path})()
                self.state = _State()
                self.state.session = session

        # A real admin endpoint from the map.
        denied = web_server._enforce_admin_api_permission(
            _FakeRequest("POST", "/api/credentials/pool", _sess("user"))
        )
        assert denied is not None and denied.status_code == 403

        # An admin session is allowed through (returns None).
        allowed = web_server._enforce_admin_api_permission(
            _FakeRequest("POST", "/api/credentials/pool", _sess("admin"))
        )
        assert allowed is None

        # A non-mapped endpoint is never gated here.
        passthrough = web_server._enforce_admin_api_permission(
            _FakeRequest("GET", "/api/sessions", _sess("user"))
        )
        assert passthrough is None

        # Loopback/token mode (no verified session) is not gated by this layer.
        no_session = web_server._enforce_admin_api_permission(
            _FakeRequest("POST", "/api/credentials/pool", None)
        )
        assert no_session is None

    def test_subpath_admin_endpoint_is_gated(self):
        import hermes_cli.web_server as web_server
        from hermes_cli.dashboard_auth.base import Session

        sess = Session(
            user_id="u1", email="u@x.test", display_name="U", org_id="t1",
            provider="basic", expires_at=9999999999, access_token="at",
            refresh_token="rt", tenant_id="t1", actor_id="a1", role="user",
        )

        class _FakeRequest:
            def __init__(self, method, path):
                self.method = method
                self.url = type("U", (), {"path": path})()
                self.state = type("S", (), {"session": sess})()

        # DELETE /api/credentials/pool/{provider}/{index} matches the prefix.
        denied = web_server._enforce_admin_api_permission(
            _FakeRequest("DELETE", "/api/credentials/pool/openai/0")
        )
        assert denied is not None and denied.status_code == 403


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

        def fake_dispatch(req, transport=None, actor_context=None):
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

class TestDesktopCronTicker:
    """The dashboard backend fires cron jobs itself only when desktop-spawned."""

    def _client(self):
        try:
            from starlette.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi/starlette not installed")
        from hermes_cli.web_server import app

        return TestClient(app)

    def test_ticker_runs_when_desktop(self, monkeypatch, _isolate_hermes_home):
        import threading
        import cron.scheduler as sched

        called = threading.Event()
        monkeypatch.setattr(sched, "tick", lambda *a, **k: called.set())
        monkeypatch.setenv("HERMES_DESKTOP", "1")

        with self._client():
            assert called.wait(3.0), "expected cron tick under HERMES_DESKTOP=1"


class TestServeIndexMissingIndex:
    """_serve_index must not raise per-request when index.html vanishes
    (partial build, wiped dist) after mount_spa saw an existing dist dir.
    It should return the same JSON 404 payload mount_spa emits for a
    fully-missing dist."""

    @staticmethod
    def _client_with_dist(tmp_path, monkeypatch, *, write_index: bool):
        from fastapi import FastAPI
        from starlette.testclient import TestClient
        import hermes_cli.web_server as ws

        dist = tmp_path / "web_dist"
        (dist / "assets").mkdir(parents=True)
        if write_index:
            (dist / "index.html").write_text(
                "<html><head></head><body>SPA</body></html>", encoding="utf-8"
            )
        monkeypatch.setattr(ws, "WEB_DIST", dist)
        monkeypatch.delenv("HERMES_SERVE_HEADLESS", raising=False)
        spa_app = FastAPI()
        ws.mount_spa(spa_app)
        return TestClient(spa_app), dist

    def test_missing_index_inside_existing_dist_returns_json_404(
        self, tmp_path, monkeypatch
    ):
        client, _dist = self._client_with_dist(
            tmp_path, monkeypatch, write_index=False
        )
        for route in ("/", "/chat"):
            resp = client.get(route)
            assert resp.status_code == 404
            assert resp.json()["error"] == (
                "Frontend not built. Run: cd web && npm run build"
            )

    def test_index_deleted_after_mount_returns_json_404(self, tmp_path, monkeypatch):
        client, dist = self._client_with_dist(tmp_path, monkeypatch, write_index=True)
        assert client.get("/chat").status_code == 200  # healthy first
        (dist / "index.html").unlink()
        resp = client.get("/chat")
        assert resp.status_code == 404
        assert "Frontend not built" in resp.json()["error"]
        # And recovers once the index reappears (e.g. a rebuild finished).
        (dist / "index.html").write_text(
            "<html><head></head><body>SPA-rebuilt</body></html>", encoding="utf-8"
        )
        resp = client.get("/chat")
        assert resp.status_code == 200
        assert "SPA-rebuilt" in resp.text


class TestDashboardComponentHealth:
    """Component-health rollup: error middleware, /api/status components, self-test."""

    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch, _isolate_hermes_home):
        try:
            from starlette.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi/starlette not installed")

        import hermes_state
        from hermes_constants import get_hermes_home
        import hermes_cli.web_server as ws

        monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", get_hermes_home() / "state.db")
        # Fresh state holder per test so counters don't leak across tests.
        monkeypatch.setattr(ws, "DASHBOARD_HEALTH", ws.DashboardHealth())
        self.ws = ws
        self.client = TestClient(ws.app, raise_server_exceptions=False)
        self.client.headers[ws._SESSION_HEADER_NAME] = ws._SESSION_TOKEN

    # -- middleware -------------------------------------------------------




    # -- /api/status components ------------------------------------------




    def test_public_component_payload_carries_no_secret_bearing_fields(self):
        """PUBLIC_API_PATHS contract: counts/enums only — no paths/messages."""
        self.ws.DASHBOARD_HEALTH.record_error("RuntimeError", "/api/secret-route?token=abc")
        resp = self.client.get("/api/status")
        payload = json.dumps(resp.json()["components"])
        assert "secret-route" not in payload
        assert "token=abc" not in payload
        assert "last_error_path" not in payload
        assert "last_error_type" not in payload
        assert "kaboom" not in payload

    # -- self-test ---------------------------------------------------------

    def test_selftest_records_failure_on_500(self, monkeypatch):
        httpx = pytest.importorskip("httpx")

        class _FakeResponse:
            status_code = 500

        class _FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, *args, **kwargs):
                return _FakeResponse()

        monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
        asyncio.run(self.ws._dashboard_selftest_once())
        assert self.ws.DASHBOARD_HEALTH.selftest_status == "failing"
        assert self.ws.DASHBOARD_HEALTH.selftest_http_status == 500
        assert self.ws.DASHBOARD_HEALTH.snapshot()["status"] == "degraded"
