"""Regression coverage for AIWerk customer SPA bootstrap values."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI
from starlette.testclient import TestClient


def test_served_spa_injects_customer_locale_and_names(tmp_path: Path, monkeypatch) -> None:
    import hermes_cli.web_server as web_server

    dist = tmp_path / "web-dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        "<html><head><title>AI Assistant</title></head><body>SPA</body></html>",
        encoding="utf-8",
    )
    config = {
        "display": {"agent_name": "Mira"},
        "dashboard": {"cui_locale": "hu"},
    }
    monkeypatch.setattr(web_server, "WEB_DIST", dist)
    monkeypatch.setattr(web_server, "_DASHBOARD_MODE", "assistant")
    monkeypatch.setattr(web_server, "load_config", lambda: config)
    monkeypatch.setattr(web_server, "_assistant_user_display_name", lambda: "Attila")
    monkeypatch.delenv("HERMES_SERVE_HEADLESS", raising=False)

    app = FastAPI()
    app.state.auth_required = True
    web_server.mount_spa(app)
    response = TestClient(app).get("/")

    assert response.status_code == 200
    html = response.text
    assert f"window.__AIWERK_CUI_LOCALE__={json.dumps('hu')};" in html
    assert f"window.__HERMES_AGENT_DISPLAY_NAME__={json.dumps('Mira')};" in html
    assert f"window.__HERMES_USER_DISPLAY_NAME__={json.dumps('Attila')};" in html
