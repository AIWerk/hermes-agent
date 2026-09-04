import asyncio
from io import BytesIO
from pathlib import Path
import re
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, UploadFile


@pytest.fixture
def assistant_identity() -> dict[str, str]:
    return {
        "role": "user",
        "actor_id": "tenant-a:user-1",
        "tenant_id": "tenant-a",
    }


@pytest.fixture
def web_server(monkeypatch):
    import hermes_cli.web_server as module

    monkeypatch.setattr(module, "_DASHBOARD_MODE", "admin", raising=False)
    return module


def test_assistant_http_allowlist_is_method_specific_and_default_deny(web_server) -> None:
    assert hasattr(web_server, "_assistant_api_allowed")
    allowed = web_server._assistant_api_allowed

    assert allowed("/api/status", "GET") is True
    assert allowed("/api/auth/me", "GET") is True
    assert allowed("/api/auth/ws-ticket", "POST") is True
    assert allowed("/api/auth/ws-ticket", "GET") is False
    for path in (
        "/api/model/info",
        "/api/sessions",
        "/api/sessions/customer-session",
        "/api/dashboard/themes",
        "/api/dashboard/font",
    ):
        assert allowed(path, "GET") is True
        assert allowed(path, "HEAD") is True
        assert allowed(path, "OPTIONS") is True
    assert allowed("/api/sessions/customer-session", "DELETE") is False
    assert allowed("/api/sessions/customer-session", "PATCH") is False
    assert allowed("/api/sessions/prune", "POST") is False
    assert allowed("/api/sessions/bulk-delete", "POST") is False
    assert allowed("/api/config", "GET") is False
    assert allowed("/api/env", "GET") is False
    assert allowed("/api/plugins", "GET") is False
    assert allowed("/api/status/extra", "GET") is False


def test_assistant_frontend_http_startup_contract_is_source_derived(web_server) -> None:
    root = Path(__file__).resolve().parents[2]
    page_source = (root / "web/src/pages/AiwerkAssistantPage.tsx").read_text(
        encoding="utf-8"
    )
    theme_source = (root / "web/src/themes/context.tsx").read_text(encoding="utf-8")
    status_source = (root / "web/src/hooks/useSidebarStatus.ts").read_text(encoding="utf-8")
    api_source = (root / "web/src/lib/api.ts").read_text(encoding="utf-8")

    api_call_pattern = r"\bapi\s*\.\s*([A-Za-z0-9_]+)\("
    page_methods = set(re.findall(api_call_pattern, page_source))
    theme_methods = set(re.findall(api_call_pattern, theme_source))
    status_methods = set(re.findall(api_call_pattern, status_source))
    assert page_methods == {
        "addAssistantTodo",
        "attachAssistantResource",
        "createCuiContact",
        "editAssistantTodo",
        "getAssistantResources",
        "getAuthMe",
        "getModelInfo",
        "getSessionMessages",
        "getSessions",
        "hideCuiContact",
        "openAssistantSharedFolder",
        "searchCuiContacts",
        "sendAssistantSupport",
        "synthesizeAssistantSpeech",
        "transcribeAssistantAudio",
        "updateAssistantTodo",
        "uploadAssistantAttachments",
    }
    assert theme_methods == {"getFontPref", "getThemes", "setFontPref", "setTheme"}
    assert status_methods == {"getStatus"}
    assert {
        "getAuthMe",
        "getSessions",
        "getAssistantResources",
        "getModelInfo",
    } <= page_methods
    assert {"getThemes", "getFontPref"} <= theme_methods
    assert "getStatus" in status_methods
    assert "getWsTicket()" in api_source

    startup_paths = {
        "/api/status",
        "/api/auth/me",
        "/api/auth/ws-ticket",
        "/api/assistant/resources",
        "/api/model/info",
        "/api/sessions",
        "/api/dashboard/themes",
        "/api/dashboard/font",
    }
    for path in startup_paths:
        assert path in api_source
        method = "POST" if path == "/api/auth/ws-ticket" else "GET"
        assert web_server._assistant_api_allowed(path, method) is True


def test_assistant_get_sessions_filters_foreign_actor_before_pagination(
    web_server, monkeypatch
) -> None:
    from hermes_cli.web_routers import sessions as sessions_routes

    own = {
        "id": "own",
        "source": "web",
        "model_config": (
            '{"_cui_visibility_scope":"customer",'
            '"_cui_actor_role":"user",'
            '"_cui_actor_id":"tenant-a:user-1",'
            '"_cui_tenant_id":"tenant-a"}'
        ),
        "started_at": 1,
        "last_active": 1,
        "ended_at": None,
    }
    foreign = {
        **own,
        "id": "foreign",
        "model_config": own["model_config"].replace("tenant-a", "tenant-b"),
    }

    class DB:
        def close(self):
            return None

    request = SimpleNamespace(
        state=SimpleNamespace(
            session=SimpleNamespace(
                role="user", actor_id="tenant-a:user-1", tenant_id="tenant-a"
            )
        )
    )
    monkeypatch.setattr(web_server, "_maybe_auto_archive_for_profile", lambda _p: None)
    monkeypatch.setattr(web_server, "_open_session_db_for_profile", lambda *_a, **_k: DB())
    own_2 = {**own, "id": "own-2"}
    foreign_2 = {**foreign, "id": "foreign-2"}
    monkeypatch.setattr(
        web_server,
        "_list_sessions_rich_all",
        lambda *_a, **_k: [foreign, own, foreign_2, own_2],
    )

    result = sessions_routes.get_sessions(
        request,
        limit=1,
        offset=0,
        min_messages=0,
        archived="exclude",
        order="created",
        source=None,
        sources=None,
        exclude_sources=None,
        cwd_prefix=None,
        full=False,
        profile=None,
    )

    assert result["total"] == 2
    assert [row["id"] for row in result["sessions"]] == ["own"]

    second_page = sessions_routes.get_sessions(
        request,
        limit=1,
        offset=1,
        min_messages=0,
        archived="exclude",
        order="created",
        source=None,
        sources=None,
        exclude_sources=None,
        cwd_prefix=None,
        full=False,
        profile=None,
    )
    assert second_page["total"] == 2
    assert [row["id"] for row in second_page["sessions"]] == ["own-2"]


def test_assistant_http_middleware_allows_customer_status_and_hides_admin_api(
    web_server, monkeypatch
) -> None:
    from starlette.testclient import TestClient

    monkeypatch.setattr(web_server, "_DASHBOARD_MODE", "assistant")
    web_server.app.state.auth_required = False
    client = TestClient(web_server.app)
    client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN

    assert client.get("/api/status").status_code == 200
    assert client.get("/api/config").status_code == 404


@pytest.mark.parametrize(
    "method",
    [
        "shell.exec",
        "cli.exec",
        "cron.manage",
        "skills.manage",
        "browser.manage",
        "model.save_key",
        "unknown.method",
    ],
)
def test_assistant_ws_gate_denies_admin_and_unknown_methods(web_server, method: str) -> None:
    assert hasattr(web_server, "_assistant_ws_request_gate")

    reason = web_server._assistant_ws_request_gate({"method": method, "params": {}})

    assert reason is not None
    assert "assistant mode" in reason


@pytest.mark.parametrize(
    "method,params",
    [
        ("gateway.ping", {}),
        ("session.create", {"source": "web", "close_on_disconnect": True}),
        ("prompt.submit", {"session_id": "sid", "text": "hello"}),
        (
            "approval.respond",
            {"session_id": "sid", "request_id": "req", "choice": "once"},
        ),
        ("slash.exec", {"session_id": "sid", "command": "/stop"}),
        ("session.events.since", {"session_id": "sid", "last_seen": 0}),
    ],
)
def test_assistant_ws_gate_allows_exact_customer_contract(
    web_server, assistant_identity, method: str, params: dict
) -> None:
    request = {"method": method, "params": params}

    assert web_server._assistant_ws_request_gate(request, assistant_identity) is None
    assert request["params"]["_cui_actor_id"] == "tenant-a:user-1"


def test_assistant_frontend_startup_requests_match_ws_gate(
    web_server, assistant_identity
) -> None:
    """Exercise every startup resume/create literal shipped by the customer UI."""
    source = (
        Path(__file__).resolve().parents[2]
        / "web/src/pages/AiwerkAssistantPage.tsx"
    ).read_text(encoding="utf-8")
    assert "commands.catalog" not in source
    assert "function readStoredSessionId()" in source
    startup = source.split("async function connect()", 1)[1].split(
        "async function recoverConnection()", 1
    )[0]
    reconnect = source.split("async function recoverConnection()", 1)[1].split(
        "void connect();", 1
    )[0]
    for block in (startup, reconnect):
        assert "readStoredSessionId()" in block
        assert '"session.resume"' in block
        assert re.search(
            r'"session\.resume"\s*,\s*\{\s*session_id:\s*storedSessionId,\s*cols:\s*100\s*\}',
            block,
        )
        assert '"session.create"' in block
        assert 'source: "web"' in block
        assert "close_on_disconnect: true" in block
    create_literals = re.findall(
        r'gateway\.request(?:<[^>]+>)?\(\s*"session\.create"\s*,\s*\{([^}]*)\}'
        r'(?:\s*,\s*[\d_]+)?\s*\)',
        source,
        re.DOTALL,
    )
    assert len(create_literals) == 5
    for literal in create_literals:
        assert set(re.findall(r"([a-z_]+)\s*:", literal)) == {
            "source",
            "close_on_disconnect",
        }
        request = {
            "method": "session.create",
            "params": {
                "source": re.search(r'source:\s*"([^"]+)"', literal).group(1),
                "close_on_disconnect": bool(
                    re.search(r"close_on_disconnect:\s*true", literal)
                ),
            },
        }
        assert web_server._assistant_ws_request_gate(request, assistant_identity) is None


def test_assistant_frontend_stop_uses_allowed_slash_rpc(
    web_server, assistant_identity
) -> None:
    source = (
        Path(__file__).resolve().parents[2]
        / "web/src/pages/AiwerkAssistantPage.tsx"
    ).read_text(encoding="utf-8")
    stop_block = source.split('if (base === "/stop")', 1)[1].split(
        'if (base === "/compress")', 1
    )[0]
    assert 'gateway.request("slash.exec"' in stop_block
    assert 'command: "/stop"' in stop_block
    request = {
        "method": "slash.exec",
        "params": {"session_id": "sid", "command": "/stop"},
    }
    assert web_server._assistant_ws_request_gate(request, assistant_identity) is None


def test_assistant_frontend_prompt_submit_matches_ws_gate(
    web_server, assistant_identity
) -> None:
    source = (
        Path(__file__).resolve().parents[2]
        / "web/src/pages/AiwerkAssistantPage.tsx"
    ).read_text(encoding="utf-8")
    call = re.search(
        r'gateway\.request\(\s*"prompt\.submit"\s*,\s*\{(?P<params>[^}]*)\}\s*\)',
        source,
        re.DOTALL,
    )
    assert call is not None
    assert set(re.findall(r"([a-z_]+)\s*:", call.group("params"))) == {
        "session_id",
        "text",
    }

    request = {
        "method": "prompt.submit",
        "params": {"session_id": "sid", "text": "hello"},
    }
    assert web_server._assistant_ws_request_gate(request, assistant_identity) is None


def test_every_assistant_frontend_rpc_call_matches_exact_ws_contract(
    web_server, assistant_identity
) -> None:
    """Gate every RPC in the assistant page's transitive @/ import closure."""
    root = Path(__file__).resolve().parents[2]
    web_root = (root / "web/src").resolve()
    entrypoint = web_root / "pages/AiwerkAssistantPage.tsx"
    import_pattern = re.compile(
        r'(?:import|export)\s+(?:[^;]*?\s+from\s+)?["\']@/(?P<path>[^"\']+)["\']',
        re.MULTILINE | re.DOTALL,
    )

    def resolve_web_import(import_path: str) -> Path:
        base = web_root / import_path
        candidates = (
            base.with_suffix(".ts"),
            base.with_suffix(".tsx"),
            base / "index.ts",
            base / "index.tsx",
        )
        resolved = next((candidate.resolve() for candidate in candidates if candidate.is_file()), None)
        assert resolved is not None, f"unresolved assistant import: {import_path}"
        assert resolved.is_relative_to(web_root)
        return resolved

    closure: set[Path] = set()
    pending = [entrypoint.resolve()]
    while pending:
        path = pending.pop()
        if path in closure:
            continue
        closure.add(path)
        candidate = path.read_text(encoding="utf-8")
        pending.extend(
            resolve_web_import(match.group("path"))
            for match in import_pattern.finditer(candidate)
        )

    assert {str(path.relative_to(web_root)) for path in closure} == {
        "components/Markdown.tsx",
        "lib/aiwerk-cui-i18n.ts",
        "lib/api.ts",
        "lib/cui-approval.ts",
        "lib/cui-greeting.ts",
        "lib/cui-slash.ts",
        "lib/dashboard-auth-reload.ts",
        "lib/gatewayClient.ts",
        "lib/safe-open.ts",
        "pages/AiwerkAssistantPage.tsx",
        "themes/types.ts",
    }

    call_pattern = re.compile(
        r'\.request(?:<[^()]*>)?\(\s*"(?P<method>[^"]+)"\s*,',
        re.MULTILINE | re.DOTALL,
    )
    closure_calls: list[tuple[str, re.Match[str]]] = []
    for path in sorted(closure):
        candidate = path.read_text(encoding="utf-8")
        closure_calls.extend(
            (str(path.relative_to(web_root)), match)
            for match in call_pattern.finditer(candidate)
        )

    assert len(closure_calls) == 29
    assert {path for path, _match in closure_calls} == {
        "lib/gatewayClient.ts",
        "pages/AiwerkAssistantPage.tsx",
    }
    closure_method_calls = {
        (path, match.group("method")) for path, match in closure_calls
    }
    closure_methods = {method for _path, method in closure_method_calls}
    assert "complete.slash" not in closure_methods
    assert "command.dispatch" not in closure_methods
    assert ("lib/gatewayClient.ts", "prompt.submit") in closure_method_calls
    assert web_server._assistant_ws_request_gate(
        {"method": "prompt.submit", "params": {"session_id": "sid", "text": "hello"}},
        assistant_identity,
    ) is None

    source = entrypoint.read_text(encoding="utf-8")
    matches = [
        match
        for path, match in closure_calls
        if path == "pages/AiwerkAssistantPage.tsx"
    ]
    assert len(matches) == 28
    def object_keys_after(start: int) -> set[str]:
        brace = source.find("{", start)
        assert brace >= 0
        depth = 0
        for index in range(brace, len(source)):
            depth += source[index] == "{"
            depth -= source[index] == "}"
            if depth == 0:
                return set(
                    re.findall(
                        r"(?m)(?:^|,)\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?=:|,|$)",
                        source[brace + 1 : index],
                    )
                )
        raise AssertionError("unterminated frontend RPC params object")

    approval_source = (root / "web/src/lib/cui-approval.ts").read_text(encoding="utf-8")
    approval_body = approval_source.split("return {", 1)[1].split("};", 1)[0]
    approval_keys = set(
        re.findall(r"(?m)^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:", approval_body)
    )
    assert approval_keys == {"session_id", "request_id", "choice"}

    values = {
        "session_id": "sid",
        "source": "web",
        "close_on_disconnect": True,
        "cols": 100,
        "title": "Customer title",
        "limit": 12,
        "text": "hello",
        "request_id": "req",
        "choice": "once",
        "value": "on",
    }
    seen_methods: set[str] = set()
    for match in matches:
        method = match.group("method")
        seen_methods.add(method)
        keys = approval_keys if method == "approval.respond" else object_keys_after(match.end())
        params = {key: values[key] for key in keys if key not in {"key", "command"}}
        if "key" in keys:
            tail = source[match.end() : source.find("}", match.end())]
            literal = re.search(r'key:\s*"([^"]+)"', tail)
            params["key"] = literal.group(1) if literal else "busy"
        if "command" in keys:
            tail = source[match.end() : source.find("}", match.end())]
            literal = re.search(r'command:\s*"([^"]+)"', tail)
            assert literal is not None
            params["command"] = literal.group(1)
        request = {"method": method, "params": params}
        assert web_server._assistant_ws_request_gate(request, assistant_identity) is None, (
            method,
            keys,
        )

    assert seen_methods <= web_server._ASSISTANT_ALLOWED_RPC_METHODS
    assert web_server._ASSISTANT_ALLOWED_RPC_METHODS == {
        "gateway.ping",
        "session.create",
        "session.resume",
        "session.title",
        "session.notes",
        "session.usage",
        "session.interrupt",
        "session.steer",
        "session.side.start",
        "session.side.back",
        "session.events.since",
        "prompt.submit",
        "prompt.learn",
        "approval.respond",
        "config.get",
        "config.set",
        "commands.catalog",
        "slash.exec",
    }

    transport = (root / "apps/shared/src/json-rpc-gateway.ts").read_text(
        encoding="utf-8"
    )
    replay = re.search(
        r"'session\.events\.since'\s*,\s*"
        r"\{\s*session_id:\s*sid,\s*last_seen:\s*lastSeen\s*\}",
        transport,
        re.DOTALL,
    )
    assert replay is not None
    replay_request = {
        "method": "session.events.since",
        "params": {"session_id": "sid", "last_seen": 0},
    }
    assert web_server._assistant_ws_request_gate(
        replay_request, assistant_identity
    ) is None


def test_assistant_audio_rejects_unsupported_extension_before_transcription(
    web_server, monkeypatch
) -> None:
    monkeypatch.setattr(web_server, "_require_token", lambda _request: None)
    upload = UploadFile(filename="voice.exe", file=BytesIO(b"audio"))

    with pytest.raises(HTTPException) as exc:
        asyncio.run(web_server.transcribe_assistant_audio(SimpleNamespace(), upload))

    assert exc.value.status_code == 415


def test_assistant_audio_rejects_oversized_upload_before_transcription(
    web_server, monkeypatch
) -> None:
    monkeypatch.setattr(web_server, "_require_token", lambda _request: None)
    upload = UploadFile(
        filename="voice.webm",
        file=BytesIO(b"x" * (25 * 1024 * 1024 + 1)),
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(web_server.transcribe_assistant_audio(SimpleNamespace(), upload))

    assert exc.value.status_code == 413


@pytest.mark.parametrize(
    "method,params",
    [
        ("session.resume", {"session_id": "sid"}),
        ("config.get", {"session_id": "sid", "key": "model"}),
        ("config.set", {"session_id": "sid", "key": "model", "value": "x"}),
        ("session.events.since", {"session_id": "sid"}),
        ("session.events.since", {"session_id": "sid", "last_seen": -1}),
        ("session.events.since", {"session_id": "sid", "last_seen": "0"}),
        ("session.events.since", {"session_id": "sid", "last_seen": True}),
        ("session.create", {"profile": "other"}),
        ("session.create", {"cwd": "/tmp"}),
        ("prompt.submit", {"session_id": "sid", "text": "hi", "profile": "other"}),
        (
            "approval.respond",
            {"session_id": "sid", "request_id": "req", "choice": "always"},
        ),
        (
            "approval.respond",
            {"session_id": "sid", "request_id": "req", "choice": "once", "all": True},
        ),
        ("session.events.since", {"session_id": "sid", "last_seen": 0, "profile": "other"}),
    ],
)
def test_assistant_ws_gate_rejects_extra_or_privileged_contract(
    web_server, assistant_identity, method: str, params: dict
) -> None:
    assert web_server._assistant_ws_request_gate(
        {"method": method, "params": params}, assistant_identity
    ) is not None


def test_request_actor_context_rejects_unknown_authenticated_role(web_server) -> None:
    request = SimpleNamespace(
        state=SimpleNamespace(
            session=SimpleNamespace(
                tenant_id="tenant-a",
                actor_id="actor-a",
                role="unexpected",
                display_name="Configured Customer",
                user_id="user-a",
                provider="test",
            )
        )
    )

    actor = web_server._cui_actor_context_from_request(request)
    assert actor == {"_restricted": "1"}
    assert web_server._session_visible_to_cui_actor({}, actor) is False


def test_assistant_ws_gate_requires_complete_server_identity(web_server) -> None:
    request = {"method": "session.create", "params": {"source": "web", "close_on_disconnect": True}}

    for identity in (
        None,
        {},
        {"role": "user"},
        {"role": "user", "actor_id": "a"},
        {"role": "unexpected", "actor_id": "a", "tenant_id": "t"},
    ):
        assert web_server._assistant_ws_request_gate(request, identity) is not None


def test_assistant_ws_gate_restricts_slash_exec(web_server, assistant_identity) -> None:
    assert hasattr(web_server, "_assistant_ws_request_gate")
    gate = lambda request: web_server._assistant_ws_request_gate(request, assistant_identity)

    for command in ("/stop", "/compress", "/reload-mcp"):
        assert gate({"method": "slash.exec", "params": {"session_id": "sid", "command": command}}) is None
    for command in ("/config", "/model opus", "/skills install x", "/cron list", ""):
        assert gate({"method": "slash.exec", "params": {"session_id": "sid", "command": command}}) is not None


def test_assistant_ws_gate_overwrites_client_actor_spoof_with_server_identity(web_server) -> None:
    assert hasattr(web_server, "_assistant_ws_request_gate")
    request = {
        "method": "session.create",
        "params": {
            "source": "web",
            "close_on_disconnect": True,
            "_cui_actor_role": "admin",
            "_cui_actor_id": "attacker",
            "_cui_tenant_id": "other",
            "actor_role": "admin",
        },
    }
    identity = {
        "role": "user",
        "actor_id": "tenant-a:user-1",
        "tenant_id": "tenant-a",
        "user_id": "user-1",
    }

    assert web_server._assistant_ws_request_gate(request, identity) is None
    assert request["params"] == {
        "source": "web",
        "close_on_disconnect": True,
        "_cui_actor_role": "user",
        "_cui_actor_id": "tenant-a:user-1",
        "_cui_tenant_id": "tenant-a",
    }


def test_assistant_mode_default_denies_non_gateway_websockets(web_server, monkeypatch) -> None:
    monkeypatch.setattr(web_server, "_DASHBOARD_MODE", "assistant")

    class FakeWebSocket:
        client = SimpleNamespace(host="test")
        query_params = {}

        def __init__(self) -> None:
            self.closed = None

        async def close(self, *, code: int, reason: str = "") -> None:
            self.closed = (code, reason)

    for endpoint in (
        web_server.speak_stream_ws,
        web_server.console_ws,
        web_server.pub_ws,
        web_server.events_ws,
    ):
        socket = FakeWebSocket()
        asyncio.run(endpoint(socket))
        assert socket.closed == (4403, "websocket disabled in assistant mode")


def test_assistant_gateway_rejects_legacy_token_without_actor_identity(
    web_server, monkeypatch
) -> None:
    import tui_gateway.ws as gateway_transport

    socket = SimpleNamespace(_hermes_auth_identity=None, closed=None)

    async def close(*, code: int, reason: str = "") -> None:
        socket.closed = (code, reason)

    reached_transport = False

    async def fake_handle_ws(_ws, **_kwargs) -> None:
        nonlocal reached_transport
        reached_transport = True

    socket.close = close
    monkeypatch.setattr(web_server, "_DASHBOARD_MODE", "assistant")
    monkeypatch.setattr(web_server, "_ws_auth_ok", lambda _ws: True)
    monkeypatch.setattr(web_server, "_ws_request_is_allowed", lambda _ws: True)
    monkeypatch.setattr(gateway_transport, "handle_ws", fake_handle_ws)

    asyncio.run(web_server.gateway_ws(socket))

    assert socket.closed == (4403, "authenticated customer identity required")
    assert reached_transport is False


def test_assistant_mode_blocks_pty_before_auth_or_spawn(web_server, monkeypatch) -> None:
    assert hasattr(web_server, "_assistant_mode_enabled")
    monkeypatch.setattr(web_server, "_DASHBOARD_MODE", "assistant")

    class FakeWebSocket:
        client = SimpleNamespace(host="test")

        def __init__(self) -> None:
            self.closed = None

        async def close(self, *, code: int, reason: str = "") -> None:
            self.closed = (code, reason)

    socket = FakeWebSocket()
    asyncio.run(web_server.pty_ws(socket))

    assert socket.closed == (4403, "pty disabled in assistant mode")


def test_spa_bootstrap_injects_exact_dashboard_mode(web_server, monkeypatch) -> None:
    assert hasattr(web_server, "_dashboard_mode_bootstrap_js")

    monkeypatch.setattr(web_server, "_DASHBOARD_MODE", "assistant")
    assert web_server._dashboard_mode_bootstrap_js() == (
        'window.__HERMES_DASHBOARD_MODE__="assistant";'
    )
    monkeypatch.setattr(web_server, "_DASHBOARD_MODE", "admin")
    assert web_server._dashboard_mode_bootstrap_js() == (
        'window.__HERMES_DASHBOARD_MODE__="admin";'
    )


def test_set_dashboard_mode_accepts_only_explicit_modes(web_server) -> None:
    assert hasattr(web_server, "_set_dashboard_mode")

    web_server._set_dashboard_mode("assistant")
    assert web_server._DASHBOARD_MODE == "assistant"
    web_server._set_dashboard_mode("admin")
    assert web_server._DASHBOARD_MODE == "admin"
    with pytest.raises(SystemExit, match="Unsupported dashboard mode"):
        web_server._set_dashboard_mode("spoofed")


def test_gateway_ws_installs_assistant_gate_with_server_identity(
    web_server, monkeypatch
) -> None:
    import tui_gateway.ws as gateway_transport

    identity = {"role": "user", "actor_id": "actor-a", "tenant_id": "tenant-a"}
    socket = SimpleNamespace(
        _hermes_auth_identity=identity,
        _hermes_ws_subprotocol=None,
    )
    captured: dict[str, object] = {}

    monkeypatch.setattr(web_server, "_DASHBOARD_MODE", "assistant")
    monkeypatch.setattr(web_server, "_ws_auth_ok", lambda _ws: True)
    monkeypatch.setattr(web_server, "_ws_request_is_allowed", lambda _ws: True)

    async def fake_handle_ws(_ws, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(gateway_transport, "handle_ws", fake_handle_ws)

    asyncio.run(web_server.gateway_ws(socket))

    gate = captured["request_gate"]
    request = {
        "method": "session.create",
        "params": {
            "source": "web",
            "close_on_disconnect": True,
            "_cui_actor_role": "admin",
        },
    }
    assert gate(request) is None
    assert request["params"]["_cui_actor_role"] == "user"


def test_gateway_ws_keeps_admin_transport_ungated(web_server, monkeypatch) -> None:
    import tui_gateway.ws as gateway_transport

    socket = SimpleNamespace(
        _hermes_auth_identity=None,
        _hermes_ws_subprotocol=None,
    )
    captured: dict[str, object] = {}

    monkeypatch.setattr(web_server, "_DASHBOARD_MODE", "admin")
    monkeypatch.setattr(web_server, "_ws_auth_ok", lambda _ws: True)
    monkeypatch.setattr(web_server, "_ws_request_is_allowed", lambda _ws: True)

    async def fake_handle_ws(_ws, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(gateway_transport, "handle_ws", fake_handle_ws)

    asyncio.run(web_server.gateway_ws(socket))

    assert captured.get("request_gate") is None
