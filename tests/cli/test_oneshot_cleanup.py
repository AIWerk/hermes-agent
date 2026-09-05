"""Regression tests for hermes -z oneshot cleanup."""

import pytest

from hermes_cli import oneshot


class _FakeAgent:
    instances = []
    fail = False

    def __init__(self, **_kwargs):
        self.suppress_status_output = False
        self.stream_delta_callback = object()
        self.tool_gen_callback = object()
        self._session_messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "OK"},
        ]
        self.shutdown_calls = []
        self.__class__.instances.append(self)

    def run_conversation(self, prompt):
        if self.fail:
            raise RuntimeError("boom")
        return {"final_response": "OK"}

    def shutdown_memory_provider(self, messages=None):
        self.shutdown_calls.append(messages)


@pytest.fixture(autouse=True)
def patch_oneshot_deps(monkeypatch):
    _FakeAgent.instances = []
    _FakeAgent.fail = False
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **_kwargs: {
            "api_key": "test",
            "base_url": "https://example.invalid",
            "provider": "test-provider",
            "api_mode": "chat_completions",
            "credential_pool": None,
        },
    )
    monkeypatch.setattr("hermes_cli.tools_config._get_platform_tools", lambda *_args: set())
    monkeypatch.setattr("run_agent.AIAgent", _FakeAgent)
    monkeypatch.setattr(oneshot, "_create_session_db_for_oneshot", lambda: None)


def test_oneshot_shuts_down_memory_provider_after_success():
    response, result = oneshot._run_agent("hi", model="test-model", provider="test-provider")

    assert response == "OK"
    assert result == {"final_response": "OK"}
    assert len(_FakeAgent.instances) == 1
    assert _FakeAgent.instances[0].shutdown_calls == [_FakeAgent.instances[0]._session_messages]


def test_oneshot_shuts_down_memory_provider_after_failure():
    _FakeAgent.fail = True

    with pytest.raises(RuntimeError, match="boom"):
        oneshot._run_agent("hi", model="test-model", provider="test-provider")

    assert len(_FakeAgent.instances) == 1
    assert _FakeAgent.instances[0].shutdown_calls == [_FakeAgent.instances[0]._session_messages]
