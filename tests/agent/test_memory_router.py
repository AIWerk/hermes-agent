"""Contract and consumer-boundary tests for deterministic memory routing."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from agent.memory_router import (
    MemoryDestination,
    MemorySensitivity,
    classify_memory_route,
    contains_secret,
    should_mirror_to_honcho,
    should_write_builtin_memory,
)
from plugins.memory.honcho import HonchoMemoryProvider
from tools import memory_tool as memory_tool_module
from tools.memory_tool import MemoryStore, apply_memory_pending, memory_tool


@pytest.mark.parametrize(
    ("content", "destination"),
    [
        ("", MemoryDestination.DISCARD),
        ("Full raw transcript conversation dump.", MemoryDestination.SESSION_INDEX),
        ("Implemented PR #123 and completed phase 4 today.", MemoryDestination.SESSION_INDEX),
        ("Reusable workflow: preflight, backup, then rollout.", MemoryDestination.SKILL_CANDIDATE),
        ("AIWerk architecture uses an isolated base-agent.", MemoryDestination.WIKI_CANDIDATE),
    ],
)
def test_non_prompt_memory_routes_away_from_builtin_and_honcho(content, destination):
    builtin_ok, builtin_route = should_write_builtin_memory(content, target="memory")
    honcho_ok, honcho_route = should_mirror_to_honcho(content, target="memory")

    assert builtin_ok is False
    assert honcho_ok is False
    assert builtin_route.has(destination)
    assert honcho_route.has(destination)
    assert builtin_route.inject_allowed is False
    assert honcho_route.honcho_store_allowed is False


def test_secret_after_long_padding_is_discarded_quickly():
    secret = "GOCSPX-" + "aBcDeFgHiJkLmNoPq"
    payload = "The user prefers concise answers. " + ("x" * 100_000) + secret

    started = time.perf_counter()
    allowed, route = should_write_builtin_memory(payload, target="user")
    elapsed = time.perf_counter() - started

    assert allowed is False
    assert route.sensitivity == MemorySensitivity.CREDENTIAL
    assert route.has(MemoryDestination.DISCARD)
    assert contains_secret(payload) is True
    assert elapsed < 0.25


@pytest.mark.parametrize(
    "content,target",
    [
        ("The user prefers concise terminal responses.", "user"),
        ("Project uses pytest with xdist for parallel test runs.", "memory"),
    ],
)
def test_only_stable_preference_and_environment_facts_are_injectable(content, target):
    allowed, route = should_write_builtin_memory(content, target=target)

    assert allowed is True
    assert route.has(MemoryDestination.INJECT)
    assert route.inject_allowed is True
    assert route.honcho_store_allowed is True


@pytest.mark.parametrize("metadata", [{"tenant_id": "acme"}, {"customer_id": "acme"}])
def test_customer_metadata_outranks_preference_and_product_keywords(metadata):
    route = classify_memory_route(
        "Customer ACME prefers Smart Website onboarding by phone.",
        target="user",
        metadata=metadata,
    )

    assert route.has(MemoryDestination.TENANT_PRIVATE)
    assert not route.has(MemoryDestination.INJECT)
    assert not route.has(MemoryDestination.WIKI_CANDIDATE)
    assert route.tenant_private_required is True
    assert route.inject_allowed is False
    assert route.honcho_store_allowed is False


@pytest.fixture
def store(tmp_path, monkeypatch):
    memory_dir = tmp_path / "memories"
    monkeypatch.setattr(memory_tool_module, "get_memory_dir", lambda: memory_dir)
    monkeypatch.setattr(memory_tool_module, "_apply_write_gate", lambda *args, **kwargs: None)
    monkeypatch.setattr(memory_tool_module, "_apply_batch_write_gate", lambda *args, **kwargs: None)
    return MemoryStore(memory_char_limit=20_000, user_char_limit=20_000)


def _result(raw: str) -> dict:
    return json.loads(raw)


def _assert_router_blocked(result: dict, forbidden_content: str) -> None:
    assert result["success"] is False
    assert "route" in result
    assert forbidden_content not in json.dumps(result)


def test_memory_tool_add_blocks_noninjectable_content_before_write(store):
    content = "Implemented PR #123 today."

    result = _result(memory_tool(action="add", target="memory", content=content, store=store))

    _assert_router_blocked(result, content)
    assert store.memory_entries == []


def test_memory_tool_replace_alias_blocks_noninjectable_content_before_write(store):
    assert _result(memory_tool(
        action="add", target="user", content="The user prefers concise replies.", store=store
    ))["success"]
    content = "Full raw transcript conversation dump."

    result = _result(memory_tool(
        action="replace",
        target="user",
        old_text="concise",
        new_text=content,
        store=store,
    ))

    _assert_router_blocked(result, content)
    assert store.user_entries == ["The user prefers concise replies."]


def test_memory_tool_batch_blocks_new_text_alias_atomically(store):
    safe = "The user prefers concise replies."
    blocked = "xai-" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcd"

    result = _result(memory_tool(
        target="user",
        operations=[
            {"action": "add", "content": safe},
            {"action": "add", "content": "", "new_text": blocked},
        ],
        store=store,
    ))

    _assert_router_blocked(result, blocked)
    assert safe not in store.user_entries


def test_approval_replay_enforces_router_for_single_and_batch_aliases(store):
    secret = "xai-" + "abcdefghijklmnopqrstuvwxyz0123456789ABCD"

    single = apply_memory_pending(
        {"action": "add", "target": "user", "content": secret}, store
    )
    batch = apply_memory_pending(
        {
            "action": "batch",
            "target": "user",
            "operations": [
                {"action": "add", "content": "", "new_text": secret}
            ],
        },
        store,
    )

    _assert_router_blocked(single, secret)
    _assert_router_blocked(batch, secret)
    assert store.user_entries == []


def test_allowed_memory_mutations_preserve_add_replace_batch_and_replay_semantics(store):
    assert _result(memory_tool(
        action="add", target="user", content="The user prefers concise replies.", store=store
    ))["success"]
    assert _result(memory_tool(
        action="replace",
        target="user",
        old_text="concise",
        new_text="The user prefers detailed replies.",
        store=store,
    ))["success"]
    assert _result(memory_tool(
        target="user",
        operations=[{"action": "add", "new_text": "The user likes dark mode."}],
        store=store,
    ))["success"]
    replay = apply_memory_pending(
        {"action": "add", "target": "memory", "content": "Project uses pytest."}, store
    )

    assert replay["success"] is True
    assert store.user_entries == [
        "The user prefers detailed replies.",
        "The user likes dark mode.",
    ]
    assert store.memory_entries == ["Project uses pytest."]


class _ImmediateThread:
    def __init__(self, target):
        self._target = target

    def start(self):
        self._target()

    def is_alive(self):
        return False

    def join(self, timeout=None):
        return None


def _ready_honcho_provider(monkeypatch):
    calls = []

    class Manager:
        def create_conclusion(self, session_key, content, **kwargs):
            calls.append((session_key, content, kwargs))
            return True

    provider = HonchoMemoryProvider()
    provider._config = SimpleNamespace(save_messages=True)
    provider._manager = Manager()
    provider._session_key = "test-session"
    provider._session_initialized = True
    monkeypatch.setattr(
        "plugins.memory.honcho.spawn_context_thread",
        lambda target, **kwargs: _ImmediateThread(target),
    )
    return provider, calls


@pytest.mark.parametrize(
    "content,metadata",
    [
        ("", None),
        ("Full raw transcript conversation dump.", None),
        ("Implemented PR #123 today.", None),
        ("Reusable workflow: preflight then rollout.", None),
        ("AIWerk architecture uses a base-agent.", None),
        ("The user prefers Smart Website updates.", {"customer_id": "acme"}),
    ],
)
def test_honcho_memory_mirror_uses_router_without_external_calls(
    content, metadata, monkeypatch
):
    provider, calls = _ready_honcho_provider(monkeypatch)

    provider.on_memory_write("add", "user", content, metadata=metadata)

    assert calls == []


def test_honcho_memory_mirror_preserves_allowed_fail_open_write(monkeypatch):
    provider, calls = _ready_honcho_provider(monkeypatch)
    content = "The user prefers concise terminal responses."

    provider.on_memory_write("add", "user", content)

    assert calls == [("test-session", content, {})]


def test_honcho_turn_sync_withholds_secret_without_external_honcho(monkeypatch):
    stored = []

    class Session:
        def add_message(self, role, content):
            stored.append((role, content))

    class Manager:
        def get_or_create(self, session_key):
            assert session_key == "test-session"
            return Session()

        def save(self, session):
            return None

    provider = HonchoMemoryProvider()
    provider._config = SimpleNamespace(save_messages=True, message_max_chars=25_000)
    provider._manager = Manager()
    provider._session_key = "test-session"
    provider._session_initialized = True
    monkeypatch.setattr(
        "plugins.memory.honcho.spawn_context_thread",
        lambda target, **kwargs: _ImmediateThread(target),
    )
    secret = "GOCSPX-" + "aBcDeFgHiJkLmNoPq"

    provider.sync_turn(f"The user pasted {secret}", "Acknowledged.")

    assert secret not in repr(stored)
    assert ("user", "[message withheld: contained a credential]") in stored


def test_honcho_conclusion_rejects_secret_without_contacting_manager(monkeypatch):
    provider, calls = _ready_honcho_provider(monkeypatch)
    secret = "hf_" + "abcdefghijklmnopqrstuvwxyz1234"

    result = json.loads(provider.handle_tool_call(
        "honcho_conclude", {"conclusion": secret, "peer": "user"}
    ))

    assert "error" in result
    assert secret not in json.dumps(result)
    assert calls == []
