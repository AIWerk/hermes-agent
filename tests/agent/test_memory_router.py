"""Tests for deterministic memory routing policy."""

from agent.memory_router import (
    MemoryDestination,
    MemorySensitivity,
    classify_memory_route,
    should_mirror_to_honcho,
    should_write_builtin_memory,
)


def test_user_preference_routes_to_injected_memory_and_honcho():
    ok, route = should_write_builtin_memory(
        "Attila prefers concise terminal responses.",
        target="user",
    )

    assert ok is True
    assert route.inject_allowed is True
    assert route.honcho_store_allowed is True
    assert route.has(MemoryDestination.INJECT)
    assert route.has(MemoryDestination.STORE_HONCHO)
    assert route.sensitivity == MemorySensitivity.PERSONAL


def test_credentials_are_discarded_and_never_injected():
    ok, route = should_write_builtin_memory(
        "The API key is sk-abc1234567890secretvalue.",
        target="memory",
    )

    assert ok is False
    assert route.has(MemoryDestination.DISCARD)
    assert route.sensitivity == MemorySensitivity.CREDENTIAL
    assert route.inject_allowed is False
    assert route.honcho_store_allowed is False


def test_customer_facts_route_to_tenant_private_only():
    route = classify_memory_route(
        "Customer ACME call handling script uses this private phone number.",
        target="memory",
        metadata={"tenant_id": "tenant-acme"},
    )

    assert route.has(MemoryDestination.TENANT_PRIVATE)
    assert route.tenant_private_required is True
    assert route.inject_allowed is False
    assert route.shared_wiki_allowed is False


def test_customer_private_address_never_routes_to_wiki_or_injection():
    route = classify_memory_route(
        "Customer private address is Bahnhofstrasse 1, Zurich.",
        target="memory",
        metadata={"customer_id": "customer-1"},
    )

    assert route.has(MemoryDestination.TENANT_PRIVATE)
    assert not route.has(MemoryDestination.WIKI_CANDIDATE)
    assert not route.has(MemoryDestination.INJECT)
    assert route.shared_wiki_allowed is False
    assert route.inject_allowed is False


def test_aiwerk_architecture_routes_to_wiki_candidate_not_memory():
    ok, route = should_write_builtin_memory(
        "AIWerk architecture: Smart Website is the customer-facing surface.",
        target="memory",
    )

    assert ok is False
    assert route.has(MemoryDestination.WIKI_CANDIDATE)
    assert route.shared_wiki_allowed is True
    assert route.target_hint == "wiki_candidate"


def test_reusable_workflow_routes_to_skill_candidate():
    ok, route = should_write_builtin_memory(
        "Reusable workflow: preflight, backup, targeted tests, then rollout.",
        target="memory",
    )

    assert ok is False
    assert route.has(MemoryDestination.SKILL_CANDIDATE)
    assert route.target_hint == "skill_candidate"


def test_session_progress_routes_to_session_index():
    ok, route = should_write_builtin_memory(
        "Implemented PR #123 and completed phase 4 today.",
        target="memory",
    )

    assert ok is False
    assert route.has(MemoryDestination.SESSION_INDEX)
    assert route.target_hint == "session_search"


def test_raw_conversation_dump_routes_to_session_index_or_discard_only():
    route = classify_memory_route(
        "Full raw transcript conversation dump from a private customer setup call.",
        target="memory",
    )

    assert route.has(MemoryDestination.SESSION_INDEX)
    assert route.has(MemoryDestination.DISCARD)
    assert not route.has(MemoryDestination.INJECT)
    assert route.inject_allowed is False
    assert route.honcho_store_allowed is False


def test_stable_environment_fact_can_route_to_builtin_memory():
    ok, route = should_write_builtin_memory(
        "Project uses pytest with xdist for parallel test runs.",
        target="memory",
    )

    assert ok is True
    assert route.has(MemoryDestination.INJECT)
    assert route.inject_allowed is True


def test_honcho_mirror_uses_same_high_signal_boundary():
    ok, route = should_mirror_to_honcho(
        "Attila prefers no raw customer data in shared memory.",
        target="user",
    )

    assert ok is True
    assert route.has(MemoryDestination.STORE_HONCHO)

    ok, route = should_mirror_to_honcho(
        "Raw transcript memory-context dump from the session.",
        target="user",
    )
    assert ok is False
    assert route.has(MemoryDestination.SESSION_INDEX)


def test_profile_metadata_does_not_override_customer_private_routing():
    route = classify_memory_route(
        "Customer private address is Bahnhofstrasse 1, Zurich.",
        target="memory",
        metadata={"profile": "cody", "customer_id": "customer-1"},
    )

    assert route.has(MemoryDestination.TENANT_PRIVATE)
    assert not route.has(MemoryDestination.INJECT)
    assert route.inject_allowed is False
    assert route.target_hint == "tenant_private"


def test_profile_metadata_does_not_create_specialist_memory_route():
    ok, route = should_write_builtin_memory(
        "User prefers concise replies.",
        target="user",
        metadata={"profile": "data"},
    )

    assert ok is True
    assert route.has(MemoryDestination.INJECT)
    assert route.target_hint == "user"
