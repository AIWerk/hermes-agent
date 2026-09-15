"""Protected retained Honcho final-wire contract.

Derived from tests/agent/test_api_content_sidecar.py on 22f1e5d37b9a2fe5cf7056ab5e1bb211c3f1a74d.
Real config, Honcho formatter, MemoryManager, agent turn and localhost LLM HTTP.
Honcho backend retrieval is stubbed; no Honcho SDK/service fidelity claim.
Do not load candidate pytest configuration, conftest or tests.
"""
import json
import os
import shutil
import tempfile
import threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import MagicMock, patch
import pytest
from agent.memory_manager import MemoryManager, build_memory_context_block
from hermes_state import SessionDB
from plugins.memory.honcho import HonchoMemoryProvider
from plugins.memory.honcho.client import HonchoClientConfig



class _MockHandler(BaseHTTPRequestHandler):
    captured_requests: list = []
    response_queue: list = []

    def do_POST(self):  # noqa: N802 (http.server API)
        length = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(length).decode())
        type(self).captured_requests.append(req)
        is_stream = req.get("stream") is True
        if type(self).response_queue:
            resp = type(self).response_queue.pop(0)
        else:
            resp = _text_resp("DONE")
        msg = resp["choices"][0]["message"]
        if is_stream:
            content = msg.get("content") or ""
            tcs = msg.get("tool_calls")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunks = [{"id": "m", "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]}]
            if content:
                chunks.append({"id": "m", "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}]})
            if tcs:
                for ti, tc in enumerate(tcs):
                    chunks.append({"id": "m", "choices": [{"index": 0, "delta": {"tool_calls": [{
                        "index": ti, "id": tc["id"], "type": "function",
                        "function": {"name": tc["function"]["name"], "arguments": tc["function"]["arguments"]}}]}, "finish_reason": None}]})
            chunks.append({"id": "m", "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if tcs else "stop"}]})
            for c in chunks:
                self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        else:
            body = json.dumps(resp).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, *a, **kw):
        pass


def _text_resp(text: str) -> dict:
    return {
        "id": "m",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 0, "total_tokens": 10},
    }


@pytest.fixture()
def wire_env():
    """Mock provider + isolated HERMES_HOME + a shared SessionDB.

    Yields (make_agent, handler, db, sid): ``make_agent()`` builds a fresh
    AIAgent bound to the shared DB/session, so a second call models a
    process-restart turn N+1 that reloads history from the store.
    """
    _MockHandler.captured_requests = []
    _MockHandler.response_queue = []
    srv = HTTPServer(("127.0.0.1", 0), _MockHandler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()

    test_home = tempfile.mkdtemp(prefix="hermes_api_content_")
    os.makedirs(os.path.join(test_home, ".hermes"))
    prev_home = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = os.path.join(test_home, ".hermes")

    from run_agent import AIAgent

    from pathlib import Path

    db = SessionDB(db_path=Path(test_home) / "state.db")
    sid = "sess-wire"

    def make_agent():
        agent = AIAgent(
            api_key="test-key", base_url=f"http://127.0.0.1:{port}/v1",
            provider="openai-compat", model="test-model",
            max_iterations=10, enabled_toolsets=[],
            quiet_mode=True, skip_context_files=True, skip_memory=True,
            save_trajectories=False, platform="cli",
            session_db=db, session_id=sid,
        )
        agent.valid_tool_names = {"read_file"}
        return agent

    try:
        with patch(
            "hermes_cli.plugins.invoke_hook",
            side_effect=lambda hook, **kw: (
                [{"context": "PLUGIN-CTX"}] if hook == "pre_llm_call" else []
            ),
        ):
            yield make_agent, _MockHandler, db, sid
    finally:
        srv.shutdown()
        db.close()
        shutil.rmtree(test_home, ignore_errors=True)
        if prev_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = prev_home


def _chat_requests(handler) -> list:
    # The model context-length probe also hits the mock; keep only
    # chat-completions payloads.
    return [r for r in handler.captured_requests if "messages" in r]


def _user_messages(req: dict) -> list:
    return [m for m in req.get("messages", []) if m.get("role") == "user"]


@pytest.mark.parametrize("policy_source", ["root", "host"])
def test_honcho_section_policy_reaches_final_user_content(wire_env, tmp_path, policy_source):
    # Real provider/aggregator and local LLM HTTP; only the Honcho backend
    # is stubbed. This does not exercise the Honcho SDK or remote service.
    make_agent, handler, db, sid = wire_env
    original = "Which memories matter for this request?"
    produced = {
        "summary": "FORBIDDEN SUMMARY",
        "representation": "FORBIDDEN USER REPRESENTATION",
        "card": "ALLOWED USER CARD",
        "ai_representation": "FORBIDDEN AI REPRESENTATION",
        "ai_card": "ALLOWED AI CARD",
    }

    provider = HonchoMemoryProvider()
    injection = {
        "includeSummary": False,
        "includeUserRepresentation": False,
        "includeUserCard": True,
        "includeAiRepresentation": False,
        "includeAiCard": True,
        "includeDialectic": False,
    }
    raw = {"enabled": True, "saveMessages": False, "timeout": 1,
           "baseUrl": "http://127.0.0.1:1", "injection": injection}
    if policy_source == "host":
        raw["injection"] = {key: not value for key, value in injection.items()}
        raw["hosts"] = {"hermes": {"injection": injection}}
    config_path = tmp_path / "honcho.json"
    config_path.write_text(json.dumps(raw))
    provider._config = HonchoClientConfig.from_global_config(
        host="hermes", config_path=config_path,
    )
    provider._manager = MagicMock()
    provider._manager.get_prefetch_context.return_value = produced
    provider._manager.pop_auth_notice.return_value = None
    provider._session_key = sid
    provider._session_initialized = True
    provider._last_dialectic_turn = 0

    memory_manager = MemoryManager(external_prefetch_timeout=2)
    memory_manager.add_provider(provider)
    agent = make_agent()
    agent._memory_manager = memory_manager
    try:
        agent.run_conversation(original, conversation_history=[], task_id="honcho-wire")
    finally:
        memory_manager.shutdown_all()

    provider._manager.get_prefetch_context.assert_called_once_with(sid, original)
    provider._manager.stop_async_writer.assert_called_once_with()
    provider._manager.shutdown.assert_not_called()
    assert not any(t.is_alive() for t in (
        provider._prefetch_thread, provider._sync_thread, provider._memwrite_thread
    ) if t is not None)

    requests = _chat_requests(handler)
    assert len(requests) == 1
    user_messages = _user_messages(requests[0])
    assert len(user_messages) == 1
    sent_message = user_messages[0]
    for forbidden in (
        "FORBIDDEN SUMMARY", "## Session Summary",
        "FORBIDDEN USER REPRESENTATION", "## User Representation",
        "FORBIDDEN AI REPRESENTATION", "## AI Self-Representation",
    ):
        assert forbidden not in sent_message["content"]
    expected_raw = (
        "## User Peer Card\nALLOWED USER CARD\n\n"
        "## AI Identity Card\nALLOWED AI CARD"
    )
    expected = (
        original
        + "\n\n"
        + build_memory_context_block(expected_raw)
        + "\n\nPLUGIN-CTX"
    )
    assert sent_message["content"] == expected
    assert "api_content" not in sent_message
    assert sent_message["content"].count("<memory-context>") == 1
    assert sent_message["content"].count("</memory-context>") == 1
    user_rows = [r for r in db.get_messages(sid) if r["role"] == "user"]
    assert len(user_rows) == 1
    assert user_rows[0]["content"] == original
    assert user_rows[0]["api_content"] == sent_message["content"]
