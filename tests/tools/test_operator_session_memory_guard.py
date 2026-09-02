import json

from tools.memory_tool import MemoryStore, apply_memory_pending, memory_tool


def _operator_store():
    store = MemoryStore()
    store.operator_session_context = {
        "mode": "operator",
        "actor_id": "attila",
        "role": "operator",
        "acting_for": "aiwerk",
        "memory_scope": "operator",
        "verified_at": 1,
        "expires_at": 9999999999,
    }
    return store


def test_operator_session_blocks_builtin_prompt_memory_writes():
    store = _operator_store()

    result = json.loads(
        memory_tool(
            action="add",
            target="memory",
            content="durable AIWerk operator fact",
            store=store,
        )
    )

    assert result["success"] is False
    assert "Operator sessions cannot write" in result["error"]
    assert store.memory_entries == []


def test_operator_session_blocks_builtin_prompt_memory_remove():
    store = _operator_store()
    store.memory_entries = ["existing"]

    result = json.loads(
        memory_tool(
            action="remove",
            target="memory",
            old_text="existing",
            store=store,
        )
    )

    assert result["success"] is False
    assert "Operator sessions cannot write" in result["error"]
    assert store.memory_entries == ["existing"]


def test_operator_session_blocks_pending_memory_replay():
    for payload in [
        {"action": "add", "target": "memory", "content": "x"},
        {"action": "replace", "target": "memory", "old_text": "existing", "content": "x"},
        {"action": "remove", "target": "memory", "old_text": "existing"},
    ]:
        store = _operator_store()
        store.memory_entries = ["existing"]

        result = apply_memory_pending(payload, store)

        assert result["success"] is False
        assert "Operator sessions cannot write" in result["error"]
        assert store.memory_entries == ["existing"]
