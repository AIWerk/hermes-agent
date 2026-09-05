"""Regression tests for agent.session_notes.redact_sensitive_text coverage.

Synthetic secrets are assembled from fragments at runtime so the contiguous
token literal never appears in this file (avoids GitHub secret-scanning
push-protection false positives) while still exercising the regexes.
"""

import time

import pytest

from agent.session_notes import _redact_value, redact_sensitive_text


@pytest.mark.parametrize(
    "prefix,body",
    [
        ("ghp_", "0123456789abcdefghij"),       # GitHub classic token
        ("github_pat_", "0123456789abcdefghijkl"),  # GitHub fine-grained PAT
        ("AKIA", "ABCDEFGHIJKLMNOP"),            # AWS access key id
        ("sk_live_", "0123456789abcdefABCD"),    # Stripe live key
        ("xoxb-", "123456789012-abcdefghijkl"),  # Slack token
    ],
)
def test_redacts_bare_high_entropy_tokens(prefix, body):
    secret = prefix + body
    out = redact_sensitive_text(f"the value is {secret} ok")
    assert secret not in out
    assert "[REDACTED]" in out


def test_redacts_jwt():
    jwt = ".".join(
        ["eyJ" + "hbGciOiJIUzI1NiJ9", "eyJ" + "zdWIiOiIxMjM0NTY3In0", "SflKxwRJSMeKKF2QT4fw"]
    )
    out = redact_sensitive_text(f"token {jwt}")
    assert jwt not in out
    assert "[REDACTED]" in out


def test_redacts_url_embedded_password_keeps_scheme_and_user():
    password = "p4ss" + "w0rd"
    out = redact_sensitive_text(f"postgres://user:{password}@db.host:5432/app")
    assert password not in out
    assert out.startswith("postgres://user:")
    assert "[REDACTED]" in out


def test_redacts_slack_webhook_url():
    webhook = "https://hooks.slack.com/services/" + "T00000000/B00000000/" + "abcdefghijABCDEFGHIJ1234"
    out = redact_sensitive_text(f"posting to {webhook}")
    assert "abcdefghijABCDEFGHIJ1234" not in out
    assert "[REDACTED]" in out


def test_normal_text_unchanged():
    text = "Implemented the session summary pipeline and ran the tests."
    assert redact_sensitive_text(text) == text


# --- v2 coverage gaps (previously leaked unredacted) ------------------------


@pytest.mark.parametrize(
    "key,value",
    [
        ("access_token", "ya29." + "A0ABCDEFG12345678901234567890"),
        ("refresh_token", "1//0" + "abcdefghijklmnopqrstuvwx"),
        ("client_secret", "GOCSPX-" + "abcdefghijklmnop123"),
        ("password", "hunter2"),
        ("api_key", "abcdef1234567890fedcba"),
    ],
)
def test_redacts_json_quoted_key_value_secrets(key, value):
    blob = '{"' + key + '":"' + value + '"}'
    out = redact_sensitive_text(blob)
    assert value not in out
    assert "[REDACTED]" in out
    # The key label is preserved, only the value is masked.
    assert key in out


def test_redacts_nested_json_authorization_header():
    token = "abc123xyz456" + "deadbeefcafef00d"
    blob = '{"headers":{"Authorization":"Bearer ' + token + '"}}'
    out = redact_sensitive_text(blob)
    assert token not in out
    assert "[REDACTED]" in out


def test_redacts_bare_aws_secret_access_key():
    # 40-char mixed-case base64 with '/' — the AWS *secret* (not the AKIA id).
    secret = "wJalrXUtnFEMI/" + "K7MDENG/bPxRfiCYEXAMPLEKEY"
    assert len(secret) == 40
    out = redact_sensitive_text("the value is " + secret + " ok")
    assert secret not in out
    assert "[REDACTED]" in out


def test_redacts_labeled_aws_secret_access_key():
    secret = "wJalrXUtnFEMI/" + "K7MDENG/bPxRfiCYEXAMPLEKEY"
    out = redact_sensitive_text("aws_secret_access_key = " + secret)
    assert secret not in out
    assert "[REDACTED]" in out
    assert out.startswith("aws_secret_access_key = ")


def test_does_not_redact_lowercase_hex_git_sha():
    # A 40-char lowercase hex git SHA-1 must NOT be treated as an AWS secret.
    sha = "a1b2c3d4e5f6a7b8c9d0" + "e1f2a3b4c5d6e7f8a9b0"
    assert len(sha) == 40
    text = "The commit hash is " + sha
    assert redact_sensitive_text(text) == text


def test_redacts_authorization_basic_header():
    cred = "dXNlcjpwYXNz" + "d29yZA=="
    out = redact_sensitive_text("Authorization: Basic " + cred)
    assert cred not in out
    assert "[REDACTED]" in out


@pytest.mark.parametrize(
    "name,value",
    [
        ("STRIPE_WEBHOOK_SECRET", "whsec_" + "abcdefghij1234567890"),
        ("MY_API_KEY", "abcdefghij1234567890"),
        ("DB_PASSWORD", "s3cr3tp4ss"),
        ("SLACK_TOKEN", "xyz0123456789abcdef"),
    ],
)
def test_redacts_secret_bearing_env_assignments(name, value):
    out = redact_sensitive_text(name + "=" + value)
    assert value not in out
    assert "[REDACTED]" in out
    assert out.startswith(name + "=")


@pytest.mark.parametrize(
    "secret",
    [
        "xai-" + "abcdefghijklmnopqrstuvwxyz0123456789ABCD",   # xAI / Grok
        "SG." + "abcdefghij1234567890." + "ABCDEFGHIJ1234567890abcdefghij",  # SendGrid
        "hf_" + "abcdefghijklmnopqrstuvwxyz1234",              # HuggingFace
        "pplx-" + "abcdefghijklmnopqrstuvwxyz1234",            # Perplexity
        "tvly-" + "abcdefghijklmnopqrstuvwxyz",                # Tavily
    ],
)
def test_redacts_vendor_prefix_tokens(secret):
    out = redact_sensitive_text("key: " + secret)
    assert secret not in out
    assert "[REDACTED]" in out


def test_redacts_telegram_bot_token():
    token = "bot123456789:" + "AAEabcdefghijklmnopqrstuvwxyz1234567"
    out = redact_sensitive_text("webhook uses " + token)
    assert "AAEabcdefghijklmnopqrstuvwxyz1234567" not in out
    assert "[REDACTED]" in out


# --- Tail-leak regression (quoted / multi-word values) ----------------------


@pytest.mark.parametrize(
    "blob,secret_words",
    [
        ('DB_PASSWORD="my secret pw value"', ["secret pw value"]),
        ("DB_PASSWORD='my secret pw value'", ["secret pw value"]),
        ('export API_SECRET="space separated secret"', ["separated secret"]),
        ("password = correct horse battery staple", ["horse", "battery", "staple"]),
        ('"password": "correct horse battery staple"', ["horse", "battery", "staple"]),
    ],
)
def test_keyed_and_env_values_do_not_leak_tail(blob, secret_words):
    # The keyed/ENV value matchers used to terminate at the first whitespace
    # (``\S+``), leaking everything after the first space of a quoted/multi-word
    # secret to the auxiliary title/summary LLM. The value class is now
    # quote-aware and consumes unquoted multi-word values to the next clear
    # delimiter.
    out = redact_sensitive_text(blob)
    for word in secret_words:
        assert word not in out, f"leaked {word!r} in {out!r}"
    assert "[REDACTED]" in out


def test_redact_value_redacts_dict_keys_too():
    # A structured event whose KEY encodes a secret used to keep the secret in
    # the key (only values were recursively redacted).
    secret_key = "AKIA" + "ABCDEFGHIJKLMNOP"
    out = _redact_value({secret_key: "used here", "normal_field": "fine"})

    assert secret_key not in str(out)
    assert "[REDACTED]" in out  # the redacted key
    assert out["normal_field"] == "fine"


# --- ReDoS regression -------------------------------------------------------


def test_url_password_pattern_no_redos_on_long_no_delimiter_input():
    """A 16k-char no-delimiter blob must redact in well under 50ms.

    The previous url-password pattern backtracked quadratically here
    (~27s in session_summarizer._format_transcript on a 60KB blob).
    """
    blob = "a://" + "b" * 16000 + ":"
    start = time.perf_counter()
    redact_sensitive_text(blob)
    elapsed_ms = (time.perf_counter() - start) * 1000
    assert elapsed_ms < 50, f"redaction took {elapsed_ms:.1f}ms (ReDoS regression)"


def test_redaction_input_capped_for_large_blobs():
    """A 60KB no-space blob must also complete fast (input is capped)."""
    blob = "x" * 60000
    start = time.perf_counter()
    redact_sensitive_text(blob)
    elapsed_ms = (time.perf_counter() - start) * 1000
    assert elapsed_ms < 50, f"redaction took {elapsed_ms:.1f}ms (ReDoS regression)"


def test_redacts_private_key_block_straddling_scan_cap():
    """A PEM block whose BEGIN is within the scan cap but END is beyond it must
    still be redacted. The cap used to truncate before the (complete-block)
    regex could match, leaving the raw header/body in the retained text.
    """
    from agent.session_notes import _MAX_SCAN

    header = "-----BEGIN TEST PRIVATE KEY-----"
    footer = "-----END TEST PRIVATE KEY-----"
    payload = "intro\n" + header + "\n" + ("A" * (_MAX_SCAN + 2000)) + "\n" + footer
    out = redact_sensitive_text(payload)

    assert "BEGIN TEST PRIVATE KEY" not in out
    assert "AAAA" not in out  # no raw key body survives
    assert "[REDACTED]" in out


def test_redacts_private_key_block_without_end_marker():
    """A BEGIN header with no END marker at all (truncated dump) is redacted to
    end-of-text rather than passing through unmatched.
    """
    from agent.session_notes import _MAX_SCAN

    payload = "-----BEGIN OPENSSH PRIVATE KEY-----\n" + ("Z" * (_MAX_SCAN + 500))
    out = redact_sensitive_text(payload)

    assert "BEGIN OPENSSH PRIVATE KEY" not in out
    assert "ZZZZ" not in out
    assert "[REDACTED]" in out
