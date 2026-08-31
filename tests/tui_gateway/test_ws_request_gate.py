import asyncio
import json
from types import SimpleNamespace


class _GatewaySocket:
    def __init__(self, request):
        self.request = request
        self.sent: list[str] = []
        self.client = SimpleNamespace(host="127.0.0.1", port=1234)
        self.scope = {}
        self._received = False

    async def accept(self, **_kwargs):
        return None

    async def send_text(self, text):
        self.sent.append(text)

    async def receive_text(self):
        if not self._received:
            self._received = True
            return json.dumps(self.request)
        from starlette.websockets import WebSocketDisconnect

        raise WebSocketDisconnect(code=1000)

    async def close(self, **_kwargs):
        return None


def test_request_gate_refuses_before_dispatch(monkeypatch) -> None:
    from tui_gateway import ws as gateway_ws

    socket = _GatewaySocket(
        {"jsonrpc": "2.0", "id": "blocked", "method": "shell.exec", "params": {}}
    )
    dispatched: list[dict] = []

    monkeypatch.setattr(
        gateway_ws.server,
        "dispatch",
        lambda request, *_args, **_kwargs: dispatched.append(request),
    )
    monkeypatch.setattr(gateway_ws.server, "resolve_skin", lambda: {})
    monkeypatch.setattr(gateway_ws.server, "_ensure_skin_watcher", lambda: None)
    monkeypatch.setattr(gateway_ws.server, "register_live_transport", lambda _transport: None)
    monkeypatch.setattr(gateway_ws.server, "unregister_live_transport", lambda _transport: None)
    monkeypatch.setattr(gateway_ws.server, "_schedule_startup_orphan_sweep", lambda: None)
    monkeypatch.setattr(gateway_ws, "_disable_nagle", lambda _ws: None)

    asyncio.run(
        gateway_ws.handle_ws(
            socket,
            request_gate=lambda _request: "method not available in assistant mode",
        )
    )

    assert dispatched == []
    frames = [json.loads(line) for line in socket.sent]
    assert frames[-1] == {
        "jsonrpc": "2.0",
        "error": {"code": -32601, "message": "method not available in assistant mode"},
        "id": "blocked",
    }
