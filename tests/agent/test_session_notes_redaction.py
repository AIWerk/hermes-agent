"""Regression tests for agent.session_notes.redact_sensitive_text coverage.

Synthetic secrets are assembled from fragments at runtime so the contiguous
token literal never appears in this file (avoids GitHub secret-scanning
push-protection false positives) while still exercising the regexes.
"""

import time

import pytest

from agent.session_notes import redact_sensitive_text


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
