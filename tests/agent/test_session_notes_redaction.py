"""Regression tests for agent.session_notes.redact_sensitive_text coverage.

Synthetic secrets are assembled from fragments at runtime so the contiguous
token literal never appears in this file (avoids GitHub secret-scanning
push-protection false positives) while still exercising the regexes.
"""

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
