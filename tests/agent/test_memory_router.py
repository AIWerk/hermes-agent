"""Tests for deterministic memory routing policy."""

import time

import pytest

from agent.memory_router import (
    MemoryDestination,
    MemorySensitivity,
    _SECRET_RE,
    classify_memory_route,
    contains_secret,
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
    # The bounded _SECRET_RE scans the FULL text in linear time, so a 100KB
    # blob (the shape of a prompt-injected memory_tool add) classifies quickly
    # without resorting to a prefix window.
    start = time.perf_counter()
    classify_memory_route("a" * 100000, target="user")
    elapsed = time.perf_counter() - start

    assert elapsed < 0.10, f"classify_memory_route was slow: {elapsed:.3f}s"


def test_credential_at_blob_top_is_still_discarded():
    # A secret near the start of an oversized blob must be caught.
    secret = "GOCSPX-" + "aBcDeFgHiJkLmNoPq"
    ok, route = should_write_builtin_memory(
        f"User key {secret} " + "padding " * 5000,
        target="user",
    )

    assert ok is False
    assert route.has(MemoryDestination.DISCARD)
    assert route.sensitivity == MemorySensitivity.CREDENTIAL


def test_credential_after_long_padding_is_still_discarded():
    # Regression for the prefix-scan bypass: a credential placed AFTER benign
    # preference text + >8KB of padding must still route to credential/discard.
    # An earlier 8192-byte scan window let such a secret evade detection and
    # route to inject + store_honcho — breaking the memory-router invariant
    # that credentials never enter prompt-injected or durable memory.
    secret = "GOCSPX-" + "aBcDeFgHiJkLmNoPq"
    payload = (
        "User prefers concise answers and dark mode. "
        + ("x" * 9000)
        + f" my api key is {secret}"
    )
    ok, route = should_write_builtin_memory(payload, target="user")

    assert ok is False
    assert route.has(MemoryDestination.DISCARD)
    assert route.sensitivity == MemorySensitivity.CREDENTIAL
    # Must NOT leak into prompt-injected or durable memory.
    assert not route.has(MemoryDestination.INJECT)
    assert not route.has(MemoryDestination.STORE_HONCHO)
    assert route.inject_allowed is False


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


# --- Unified secret detection: the durable-memory gate must catch every bare
# value-only vendor token the session-notes index redactor catches, so the gate
# protecting the MOST durable destination is never weaker than the index. ------


@pytest.mark.parametrize(
    "secret",
    [
        "xai-" + "abcdefghijklmnopqrstuvwxyz0123456789ABCD",          # xAI / Grok
        "SG." + "abcdefghij1234567890." + "ABCDEFGHIJ1234567890abcdefghij",  # SendGrid
        "hf_" + "abcdefghijklmnopqrstuvwxyz1234",                     # HuggingFace
        "pplx-" + "abcdefghijklmnopqrstuvwxyz1234",                   # Perplexity
        "tvly-" + "abcdefghijklmnopqrstuvwxyz",                       # Tavily
        "wJalrXUtnFEMI/" + "K7MDENG/bPxRfiCYEXAMPLEKEY",             # bare AWS secret (40 chars, base64)
        "bot123456789:" + "AAEabcdefghijklmnopqrstuvwxyz1234567",     # Telegram bot token
        "123456789:" + "AAEabcdefghijklmnopqrstuvwxyz1234567",        # bare Telegram token
        "Authorization: Basic " + "dXNlcjpwYXNz" + "d29yZA==",       # Authorization: Basic header
    ],
)
def test_contains_secret_catches_bare_vendor_tokens(secret):
    # These value-only shapes have no adjacent keyword. Before unifying detection
    # the router missed every one of them, so a pasted/echoed token routed to
    # inject + durable Honcho memory.
    assert contains_secret(secret) is True, f"contains_secret missed {secret!r}"


@pytest.mark.parametrize(
    "secret",
    [
        "xai-" + "abcdefghijklmnopqrstuvwxyz0123456789ABCD",
        "SG." + "abcdefghij1234567890." + "ABCDEFGHIJ1234567890abcdefghij",
        "hf_" + "abcdefghijklmnopqrstuvwxyz1234",
        "pplx-" + "abcdefghijklmnopqrstuvwxyz1234",
        "tvly-" + "abcdefghijklmnopqrstuvwxyz",
        "wJalrXUtnFEMI/" + "K7MDENG/bPxRfiCYEXAMPLEKEY",
        "bot123456789:" + "AAEabcdefghijklmnopqrstuvwxyz1234567",
        "Authorization: Basic " + "dXNlcjpwYXNz" + "d29yZA==",
    ],
)
def test_bare_vendor_tokens_are_discarded_not_mirrored(secret):
    # A turn carrying only the bare token (no keyword) must be withheld from
    # both built-in injection and durable Honcho mirroring.
    ok, route = should_mirror_to_honcho(secret, target="user")
    assert ok is False, f"vendor token mirrored to Honcho: {secret!r}"
    assert route.has(MemoryDestination.DISCARD)
    assert route.sensitivity == MemorySensitivity.CREDENTIAL
    assert route.honcho_store_allowed is False
    assert route.inject_allowed is False


def test_contains_secret_does_not_flag_lowercase_hex_git_sha():
    # A 40-char lowercase-hex git SHA-1 must not trip the bare-AWS-secret matcher
    # (which requires an uppercase / '/' / '+' char to qualify).
    sha = "a1b2c3d4e5f6a7b8c9d0" + "e1f2a3b4c5d6e7f8a9b0"
    assert len(sha) == 40
    assert contains_secret("The commit hash is " + sha) is False


def test_tenant_metadata_with_preference_verb_routes_to_tenant_private():
    # Explicit customer_id metadata is the strongest tenant signal and is
    # authoritative: a preference verb ("prefers") must NOT flip a customer PII
    # fact onto the INJECT + STORE_HONCHO branch.
    route = classify_memory_route(
        "Customer ACME prefers we keep their phone 079 123 45 67 on file.",
        target="user",
        metadata={"customer_id": "acme"},
    )

    assert route.has(MemoryDestination.TENANT_PRIVATE)
    assert route.tenant_private_required is True
    assert route.sensitivity == MemorySensitivity.CUSTOMER
    assert not route.has(MemoryDestination.INJECT)
    assert not route.has(MemoryDestination.STORE_HONCHO)
    assert route.inject_allowed is False
    assert route.honcho_store_allowed is False

    ok, _ = should_mirror_to_honcho(
        "Customer ACME prefers we keep their phone 079 123 45 67 on file.",
        target="user",
        metadata={"customer_id": "acme"},
    )
    assert ok is False


def test_tenant_metadata_with_product_keyword_still_routes_tenant_private():
    # Even when the customer fact mentions an AIWerk product keyword, explicit
    # tenant metadata keeps it tenant-private (not WIKI_CANDIDATE).
    route = classify_memory_route(
        "Customer ACME wants their Smart Website onboarding kept on file.",
        target="user",
        metadata={"tenant_id": "tenant-acme"},
    )

    assert route.has(MemoryDestination.TENANT_PRIVATE)
    assert not route.has(MemoryDestination.WIKI_CANDIDATE)
    assert not route.has(MemoryDestination.INJECT)
    assert route.inject_allowed is False
    assert route.honcho_store_allowed is False


def test_keyword_only_customer_with_pref_verb_is_not_forced_tenant_private():
    # Without tenant metadata, the keyword-only customer path keeps its existing
    # disambiguation: a preference-verb phrasing routes to the user-preference
    # branch, not tenant-private (this is the behavior the metadata override
    # deliberately does NOT change).
    route = classify_memory_route(
        "The client prefers concise weekly status updates.",
        target="user",
    )

    assert not route.has(MemoryDestination.TENANT_PRIVATE)
    assert route.has(MemoryDestination.INJECT)
