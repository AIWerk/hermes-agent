"""Canonical secret-detection and redaction patterns.

ONE shared source of credential shapes so the durable-memory gate
(``memory_router.contains_secret`` / ``_SECRET_RE``), the ephemeral
session-summary index redactor (``session_notes.redact_sensitive_text``), and
the local feedback-inbox sanitizer (``self_learning_capture._sanitize``) can no
longer drift apart. Previously each module maintained its own pattern list and
the *weakest* one guarded the *most durable* destination (durable Honcho
memory) — the security ordering was inverted. All three now build on the
fragments defined here.

ReDoS safety: every alternative below is bounded — no nested unbounded
quantifiers — so detection stays effectively linear even on attacker-controlled
multi-KB/multi-MB non-matching blobs. The bare-AWS-secret matcher is kept
*case-sensitive* (it relies on the presence of an uppercase / ``/`` / ``+``
char to distinguish a base64 AWS secret from a 40-char lowercase-hex git SHA),
so it is exposed separately rather than folded into the case-insensitive union.
"""

from __future__ import annotations

import re

# ── Vendor / value-only token shapes (the whole match IS the secret) ────────
# These are the alternatives that any value-only credential paste must hit even
# when no adjacent keyword is present. Listed as raw fragments so they can be
# reused both inside the case-insensitive detection union and as standalone
# compiled redaction patterns. Each quantifier is bounded above by the input;
# none nest unbounded quantifiers.
_VALUE_ONLY_FRAGMENTS = (
    r"sk[-_][A-Za-z0-9][A-Za-z0-9_-]{6,}",                 # OpenAI / Anthropic style
    r"(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{8,}",         # Stripe live/test/restricted
    r"xox[baprs]-[A-Za-z0-9-]{10,}",                       # Slack
    r"gh[pousr]_[A-Za-z0-9_]{12,}",                        # GitHub classic/oauth tokens
    r"github_pat_[A-Za-z0-9_]{20,}",                       # GitHub fine-grained PAT
    r"AIza[0-9A-Za-z_-]{20,}",                             # Google API key
    r"(?:AKIA|ASIA)[0-9A-Z]{16}",                          # AWS access key id
    r"GOCSPX-[A-Za-z0-9_-]{10,}",                          # Google OAuth client secret
    r"npm_[A-Za-z0-9]{36,}",                               # npm token
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+",  # JWT
    r"https://hooks\.slack\.com/services/[A-Za-z0-9/_-]+", # Slack webhook
    # Vendor key prefixes — anchored so they don't start mid-identifier.
    r"(?<![A-Za-z0-9_-])xai-[A-Za-z0-9]{20,}",             # xAI (Grok)
    r"(?<![A-Za-z0-9_-])SG\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",  # SendGrid
    r"(?<![A-Za-z0-9_-])hf_[A-Za-z0-9]{10,}",              # HuggingFace
    r"(?<![A-Za-z0-9_-])pplx-[A-Za-z0-9]{10,}",            # Perplexity
    r"(?<![A-Za-z0-9_-])tvly-[A-Za-z0-9]{10,}",            # Tavily
    # Telegram bot token: [bot]<digits>:<token>.
    r"(?<![A-Za-z0-9])(?:bot)?\d{8,}:[-A-Za-z0-9_]{30,}",
)

# Bare 40-char base64-ish AWS *secret* access key. Constrained to exactly 40
# [A-Za-z0-9/+] chars that contain at least one uppercase letter, "/" or "+", so
# a lowercase-hex git SHA-1 (40 hex chars) or a lowercase 40-char identifier is
# NOT redacted — only the mixed-case base64 shape AWS uses. Kept CASE-SENSITIVE
# (no re.IGNORECASE) precisely so the uppercase requirement is meaningful.
_AWS_BARE_SECRET_FRAGMENT = (
    r"(?<![A-Za-z0-9/+])(?=[A-Za-z0-9/+]{40}(?![A-Za-z0-9/+=]))"
    r"[A-Za-z0-9/+]*[A-Z/+][A-Za-z0-9/+]*"
)

#: Standalone case-sensitive AWS-secret matcher (shared by detector + redactors).
AWS_BARE_SECRET_RE = re.compile(_AWS_BARE_SECRET_FRAGMENT)

#: Standalone case-insensitive value-only vendor token matcher (for redactors
#: that replace the whole match with a placeholder).
VALUE_ONLY_RE = re.compile("|".join(_VALUE_ONLY_FRAGMENTS), re.IGNORECASE)

# ── Canonical detection union (boolean gate) ────────────────────────────────
# Scans the FULL content for ANY secret shape. In addition to the value-only
# vendor tokens above it also fires on adjacent keywords, Authorization
# Bearer/Basic headers, 64+ hex high-entropy runs, and scheme://user:pass@host
# URL credentials. Used by the durable-memory gate; bounded for ReDoS safety.
_SECRET_DETECT_PATTERN = (
    r"(api[_ -]?key|secret|token|password|passwd|credential|private[_ -]?key|"
    r"BEGIN (RSA|OPENSSH|EC|DSA)? ?PRIVATE KEY|"
    + "|".join(_VALUE_ONLY_FRAGMENTS)
    + r"|Authorization:\s*Bearer\s+[A-Za-z0-9._-]{8,}|"
    r"Authorization:\s*Basic\s+[A-Za-z0-9+/=._-]{8,}|"
    # raw high-entropy run: 64+ hex chars (signing secrets, key material)
    r"\b[a-fA-F0-9]{64,}\b|"
    # credentials embedded in URLs: scheme://[user]:password@host. The scheme
    # run and the user/pass quantifiers are bounded so the alternative cannot
    # backtrack quadratically on long non-matching input; the userless form
    # (scheme://:password@) is also covered.
    r"\b[a-z][a-z0-9+.-]{1,15}://[^\s:/@]{0,256}:[^@/\s]{1,256}@)"
)

#: Case-insensitive detection union. NOTE: the bare-AWS-secret shape is checked
#: separately via :data:`AWS_BARE_SECRET_RE` because it must stay
#: case-sensitive.
SECRET_DETECT_RE = re.compile(_SECRET_DETECT_PATTERN, re.IGNORECASE)


def contains_secret(content: str) -> bool:
    """Return True if ``content`` matches any canonical credential/secret shape.

    This is the single detector shared by the durable-memory gate and the
    feedback-inbox sanitizer, so a value-only vendor token (xai-, SG., hf_,
    pplx-, tvly-, bare AWS secret, Telegram bot token, Authorization: Basic)
    can never slip past one consumer while another would have caught it.
    """
    text = content or ""
    if not text:
        return False
    return bool(SECRET_DETECT_RE.search(text)) or bool(AWS_BARE_SECRET_RE.search(text))
