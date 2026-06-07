"""Tests for deterministic memory routing policy."""

import time

import pytest

from agent.memory_router import (
    MemoryDestination,
    MemorySensitivity,
    _SECRET_RE,
    classify_memory_route,
    should_mirror_to_honcho,
    should_write_builtin_memory,
)


def test_connection_string_password_is_discarded_not_injected():
    # A user-preference keyword ("user") used to flip this to INJECT, leaking the
    # embedded password into prompt-injected memory. The credential gate must win.
    password = "p4ss" + "w0rd"
    ok, route = should_write_builtin_memory(
        f"connection string postgres://user:{password}@db.host:5432/app",
        target="user",
    )

    assert ok is False
    assert route.has(MemoryDestination.DISCARD)
    assert route.sensitivity == MemorySensitivity.CREDENTIAL
    assert route.inject_allowed is False
    assert route.honcho_store_allowed is False


@pytest.mark.parametrize(
    "prefix,body",
    [
        ("sk_live_", "0123456789abcdefABCD"),  # Stripe live key (underscore form)
        ("AKIA", "ABCDEFGHIJKLMNOP"),          # AWS access key id
        ("xoxb-", "123456789012-abcdefghijkl"),  # Slack token
    ],
)
def test_high_entropy_secret_formats_are_discarded(prefix, body):
    secret = prefix + body
    ok, route = should_write_builtin_memory(f"User likes this value: {secret}", target="user")

    assert ok is False
    assert route.has(MemoryDestination.DISCARD)
    assert route.sensitivity == MemorySensitivity.CREDENTIAL
    assert route.inject_allowed is False


def test_jwt_is_discarded():
    jwt = ".".join(
        ["eyJ" + "hbGciOiJIUzI1NiJ9", "eyJ" + "zdWIiOiIxMjM0NTY3In0", "SflKxwRJSMeKKF2QT4fw"]
    )
    ok, route = should_write_builtin_memory(f"User token is {jwt}", target="user")

    assert ok is False
    assert route.has(MemoryDestination.DISCARD)
    assert route.inject_allowed is False


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


def test_secret_re_does_not_backtrack_on_long_non_matching_input():
    # The URL-credential alternative used to backtrack quadratically on a long
    # unbroken lowercase run before "://" (50000 chars measured at ~16s, a
    # prompt-injected multi-KB blob stalled the process). The scheme run and
    # user/pass quantifiers are now bounded, so the match must stay near-instant.
    start = time.perf_counter()
    _SECRET_RE.search("a" * 50000)
    elapsed = time.perf_counter() - start

    assert elapsed < 0.05, f"_SECRET_RE.search backtracked: {elapsed:.3f}s"


def test_classify_route_is_fast_on_large_blob():
    # The full classify path scans only a bounded prefix, so a 100KB blob (the
    # shape of a prompt-injected memory_tool add) must classify quickly.
    start = time.perf_counter()
    classify_memory_route("a" * 100000, target="user")
    elapsed = time.perf_counter() - start

    assert elapsed < 0.05, f"classify_memory_route was slow: {elapsed:.3f}s"


def test_credential_at_blob_top_is_still_discarded():
    # A secret near the start of an oversized blob must still be caught; the
    # bounded scan window only trims the tail.
    secret = "GOCSPX-" + "aBcDeFgHiJkLmNoPq"
    ok, route = should_write_builtin_memory(
        f"User key {secret} " + "padding " * 5000,
        target="user",
    )

    assert ok is False
    assert route.has(MemoryDestination.DISCARD)
    assert route.sensitivity == MemorySensitivity.CREDENTIAL


@pytest.mark.parametrize(
    "secret",
    [
        "GOCSPX-aBcDeFgHiJkLmNoPqRsTuVwX",            # Google OAuth client secret
        "Authorization: Bearer abc123.DEF456-ghi_789",  # bearer auth header
        "npm_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8",  # npm token (40 chars)
        "redis://:mypassword@cache:6379",            # userless URL credential
        "deadbeef" * 8,                              # 64-char raw hex run
    ],
)
def test_newly_covered_credential_formats_are_discarded(secret):
    ok, route = should_write_builtin_memory(
        f"User value: {secret}", target="user"
    )

    assert ok is False, f"not blocked: {secret!r}"
    assert route.has(MemoryDestination.DISCARD)
    assert route.sensitivity == MemorySensitivity.CREDENTIAL
    assert route.inject_allowed is False
    assert route.honcho_store_allowed is False


def test_userless_url_credential_blocked_from_honcho_mirror():
    ok, route = should_mirror_to_honcho(
        "redis://:s3cr3tpass@cache:6379", target="user"
    )

    assert ok is False
    assert route.has(MemoryDestination.DISCARD)
    assert route.honcho_store_allowed is False


def test_credential_free_string_is_not_misclassified():
    # Plain prose containing a short hex token and a passwordless URL must not
    # trip the credential gate (the 64-hex and URL-credential additions stay
    # tight). This routes to INJECT as a normal user preference.
    ok, route = should_write_builtin_memory(
        "Attila prefers concise replies and reads docs at https://example.com/guide.",
        target="user",
    )

    assert route.sensitivity != MemorySensitivity.CREDENTIAL
    assert ok is True
    assert route.has(MemoryDestination.INJECT)


def test_profile_metadata_does_not_create_specialist_memory_route():
    ok, route = should_write_builtin_memory(
        "User prefers concise replies.",
        target="user",
        metadata={"profile": "data"},
    )

    assert ok is True
    assert route.has(MemoryDestination.INJECT)
    assert route.target_hint == "user"
