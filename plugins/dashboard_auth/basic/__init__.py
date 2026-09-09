"""BasicAuthProvider — username/password dashboard auth (no OAuth IDP).

Login is a credential form (``supports_password`` + ``complete_password_login``); cookies,
verify, refresh, ws-tickets and logout are the shared framework. Sessions are stateless
HMAC-signed tokens (no IDP, no database); passwords use stdlib scrypt and login always hashes
even for an unknown username (no username-enumeration timing oracle). Config: ``dashboard.
basic_auth.{username,password_hash|password,secret,session_ttl_seconds}`` or the
``HERMES_DASHBOARD_BASIC_AUTH_*`` env vars (env wins when non-empty; see ``_settings``).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from typing import Any, Callable, Optional

from hermes_cli.dashboard_auth import DashboardAuthProvider, InvalidCredentialsError, RefreshExpiredError, Session
from plugins.dashboard_auth._shared import (
    NonInteractiveMixin, SkipRegistration, load_config_section, register_provider, resolve_env_or_cfg)

logger = logging.getLogger(__name__)
_TAG = "dashboard-auth-basic"

# The middleware transparently refreshes via the 30-day refresh token when the
# access token lapses, so the TTL controls refresh frequency, not login length.
_DEFAULT_TTL_SECONDS = 12 * 60 * 60
_REFRESH_TTL_SECONDS = 30 * 24 * 60 * 60

# Interactive-login scrypt parameters (~16 MiB, a few ms); n must be a power of two.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SCRYPT_SALT_BYTES = 16

# HMAC-SHA256 digest is appended to signed tokens as a fixed-length suffix
# (no separator — binary HMAC bytes can't be confused with a delimiter).
_SIG_LEN = hashlib.sha256().digest_size

LAST_SKIP_REASON: str = ""
_ALLOWED_ROLES = frozenset({"admin", "user"})


# ---- Password hashing (stdlib scrypt) ----

def hash_password(password: str) -> str:
    """Return a ``scrypt$n$r$p$<salt_b64>$<dk_b64>`` hash string. Public so operators can
    precompute ``password_hash`` for config.yaml (the plaintext then never sits at rest):
    ``python -c "from plugins.dashboard_auth.basic import hash_password; print(hash_password('pw'))"``."""
    salt = secrets.token_bytes(_SCRYPT_SALT_BYTES)
    dk = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_SCRYPT_DKLEN, maxmem=0)
    salt_b64, dk_b64 = base64.b64encode(salt).decode(), base64.b64encode(dk).decode()
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt_b64}${dk_b64}"


def _verify_password(password: str, encoded: str) -> bool:
    """Constant-time scrypt verify. False on any malformed hash string."""
    try:
        scheme, n_s, r_s, p_s, salt_b64, dk_b64 = encoded.split("$")
        if scheme != "scrypt":
            return False
        n, r, p = int(n_s), int(r_s), int(p_s)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(dk_b64)
    except (ValueError, TypeError):
        return False
    try:
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(expected), maxmem=0)
    except (ValueError, MemoryError):
        return False
    return hmac.compare_digest(actual, expected)


# Verified against when the username is unknown so "no such user" and "wrong
# password" take comparable time.
_DUMMY_HASH = hash_password("dummy-password-for-constant-time-verify")


# ---- Token signing (stateless HMAC-signed blobs) ----

def _sign(payload: dict, secret: bytes) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    sig = hmac.new(secret, raw, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(raw + sig).decode()


def _unsign(token: str, secret: bytes, kind: str) -> Optional[dict]:
    """Return the payload if the signature is valid, ``kind`` matches and it
    is unexpired; ``None`` otherwise (including on any decode error)."""
    try:
        blob = base64.urlsafe_b64decode(token.encode())
        if len(blob) <= _SIG_LEN:
            return None
        raw, sig = blob[:-_SIG_LEN], blob[-_SIG_LEN:]
        expected = hmac.new(secret, raw, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(raw)
    except Exception:
        return None
    if payload.get("kind") != kind or payload.get("exp", 0) <= int(time.time()):
        return None
    return payload


# ---- Provider ----

class BasicAuthProvider(NonInteractiveMixin, DashboardAuthProvider):
    """Username/password provider with stateless HMAC-signed sessions."""

    name = "basic"
    display_name = "Username & Password"
    supports_password = True
    _NOT_INTERACTIVE = "BasicAuthProvider is password-only; use complete_password_login."
    _NO_START_LOGIN = (
        "BasicAuthProvider is password-only; there is no OAuth redirect flow. "
        "The login page POSTs to /auth/password-login instead.")

    def __init__(
        self,
        *,
        secret: bytes,
        username: str = "",
        password_hash: str = "",
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
        users: Optional[dict[str, dict[str, str]]] = None,
        authority_resolver: Optional[
            Callable[[], dict[str, dict[str, str]]]
        ] = None,
    ) -> None:
        if len(secret) < 16:
            raise ValueError("secret must be at least 16 bytes")
        static_fallback_users: dict[str, dict[str, str]] = {}
        if users:
            static_users: dict[str, dict[str, str]] = {}
            for configured_username, record in users.items():
                if not configured_username or not isinstance(record, dict):
                    raise ValueError("each user must have a username and record")
                static_users[configured_username] = self._copy_valid_record(record)
            if username and password_hash:
                if username in static_users:
                    logger.warning(
                        "dashboard-auth-basic: top-level username %r also appears "
                        "in the users table; keeping the explicit users entry and "
                        "ignoring the top-level credential.",
                        username,
                    )
                else:
                    logger.warning(
                        "dashboard-auth-basic: both a users table and a top-level "
                        "username/password were configured; merging the top-level "
                        "credential %r as an admin user.",
                        username,
                    )
                    fallback_record = {
                        "password_hash": password_hash,
                        "display_name": username,
                        "actor_id": username,
                        "role": "admin",
                        "tenant_id": "",
                    }
                    static_users[username] = fallback_record
                    static_fallback_users[username] = fallback_record
        else:
            if not username:
                raise ValueError("username must be non-empty")
            if not password_hash:
                raise ValueError("password_hash must be non-empty")
            static_users = {
                username: {
                    "password_hash": password_hash,
                    "display_name": username,
                    "actor_id": username,
                    "role": "admin",
                    "tenant_id": "",
                }
            }
        # Legacy env/single-user authority is process-static. Copy every
        # caller-owned record so later mutations cannot change authority.
        self._static_users = static_users
        self._static_fallback_users = static_fallback_users
        self._authority_resolver = authority_resolver
        self._secret = secret
        self._ttl = max(60, int(ttl_seconds))

    # ---- password login ----------------------------------------------------

    def complete_password_login(
        self, *, username: str, password: str
    ) -> Session:
        # Constant-time-ish: always run a scrypt verify (against the real
        # hash if the username exists, else a dummy hash) so an unknown
        # username and a wrong password take comparable time.
        record, authority_source = self._resolve_record_with_source(username)
        username_ok = record is not None
        target_hash = (record or {}).get("password_hash", _DUMMY_HASH)
        password_ok = _verify_password(password, target_hash)
        if not (username_ok and password_ok):
            raise InvalidCredentialsError("invalid username or password")
        return self._mint_session(username, record or {}, authority_source or "")

    # ---- session lifecycle -------------------------------------------------

    def verify_session(self, *, access_token: str) -> Optional[Session]:
        payload = _unsign(access_token, self._secret, "access")
        if payload is None:
            return None
        user_id = str(payload.get("sub", ""))
        authority_source = payload.get("authority_source")
        if authority_source is None and self._static_fallback_users:
            return None
        if authority_source not in (None, "live", "static", "fallback"):
            return None
        record, _ = self._resolve_record_with_source(
            user_id, required_source=authority_source
        )
        if not record or not record.get("password_hash"):
            return None
        return self._session_from_record(
            access_token, "", int(payload["exp"]), user_id, record
        )

    def refresh_session(self, *, refresh_token: str) -> Session:
        if not refresh_token:
            raise RefreshExpiredError("no refresh token present in session")
        payload = _unsign(refresh_token, self._secret, "refresh")
        if payload is None:
            raise RefreshExpiredError("refresh token expired or invalid")
        user_id = str(payload.get("sub", ""))
        authority_source = payload.get("authority_source")
        if authority_source is None and self._static_fallback_users:
            raise RefreshExpiredError("session authority source is missing")
        if authority_source not in (None, "live", "static", "fallback"):
            raise RefreshExpiredError("session authority source is invalid")
        record, resolved_source = self._resolve_record_with_source(
            user_id, required_source=authority_source
        )
        if not record or not record.get("password_hash"):
            raise RefreshExpiredError("session membership no longer exists")
        return self._mint_session(user_id, record, resolved_source or "")

    def revoke_session(self, *, refresh_token: str) -> None:
        # Stateless tokens — nothing to revoke server-side; the session expires within its TTL. Must not raise.
        return None

    # ---- internals ---------------------------------------------------------

    @staticmethod
    def _copy_valid_record(record: dict[str, str]) -> dict[str, str]:
        """Copy one authority record and reject malformed privilege data."""
        copied = dict(record)
        password_hash = copied.get("password_hash")
        if not isinstance(password_hash, str) or not password_hash:
            raise ValueError("each user must have a password_hash")
        role = copied.get("role", "user")
        if not isinstance(role, str) or role.strip().lower() not in _ALLOWED_ROLES:
            raise ValueError("each user role must be admin or user")
        for key in ("tenant_id", "actor_id", "display_name", "email"):
            value = copied.get(key, "")
            if not isinstance(value, str):
                raise ValueError(f"each user {key} must be a string")
        copied["role"] = role.strip().lower()
        return copied

    def _resolve_record_with_source(
        self, username: str, *, required_source: object = None
    ) -> tuple[Optional[dict[str, str]], Optional[str]]:
        """Resolve one record and bind it to its current authority source."""
        try:
            snapshot = (
                self._authority_resolver()
                if self._authority_resolver is not None
                else self._static_users
            )
            if not isinstance(snapshot, dict):
                return None, None
            live_source = "live" if self._authority_resolver is not None else "static"
            live_record = snapshot.get(username)
            if required_source in ("live", "static"):
                if required_source != live_source:
                    return None, None
                record = live_record
                resolved_source = live_source
            elif required_source == "fallback":
                if live_record is not None:
                    return None, None
                record = self._static_fallback_users.get(username)
                resolved_source = "fallback"
            else:
                record = live_record
                resolved_source = live_source
                if record is None:
                    record = self._static_fallback_users.get(username)
                    resolved_source = "fallback"
            if not isinstance(record, dict):
                return None, None
            return self._copy_valid_record(record), resolved_source
        except Exception as exc:  # noqa: BLE001 - authority errors fail closed
            logger.warning(
                "dashboard-auth-basic: current authority resolution failed: %s",
                exc,
            )
            return None, None

    def _mint_session(
        self, user_id: str, record: dict[str, str], authority_source: str
    ) -> Session:
        now = int(time.time())
        exp = now + self._ttl
        access_token = _sign(
            {
                "sub": user_id,
                "kind": "access",
                "exp": exp,
                "authority_source": authority_source,
            },
            self._secret,
        )
        refresh_token = _sign(
            {
                "sub": user_id,
                "kind": "refresh",
                "exp": now + _REFRESH_TTL_SECONDS,
                "authority_source": authority_source,
            },
            self._secret,
        )
        return self._session_from_record(
            access_token, refresh_token, exp, user_id, record
        )

    def _session_from_record(
        self,
        access_token: str,
        refresh_token: str,
        expires_at: int,
        user_id: str,
        record: dict[str, str],
    ) -> Session:
        tenant_id = str(record.get("tenant_id", "") or "")
        actor_id = str(record.get("actor_id", "") or user_id)
        role = str(record.get("role", "user") or "user").lower()
        return Session(
            user_id=user_id,
            email=str(record.get("email", "") or ""),
            display_name=str(record.get("display_name", "") or user_id),
            org_id=tenant_id,
            provider=self.name,
            expires_at=expires_at,
            access_token=access_token,
            refresh_token=refresh_token,
            tenant_id=tenant_id,
            actor_id=actor_id,
            role=role,
        )

    def _session(self, user_id: str, exp: int, access_token: str, refresh_token: str) -> Session:
        return Session(
            user_id=user_id, email="", display_name=user_id, org_id="", provider=self.name,
            expires_at=exp, access_token=access_token, refresh_token=refresh_token)


# ---- Plugin entry point ----

def _load_config_basic_auth_section() -> dict:
    return load_config_section(logger, _TAG, "dashboard", "basic_auth")


def _load_users_from_config(section: dict) -> dict[str, dict[str, str]]:
    """Return valid role-bearing records from dashboard.basic_auth.users."""
    raw_users = section.get("users") or []
    if isinstance(raw_users, dict):
        raw_users = [
            {"username": username, **(record if isinstance(record, dict) else {})}
            for username, record in raw_users.items()
        ]
    if not isinstance(raw_users, list):
        return {}

    users: dict[str, dict[str, str]] = {}
    for item in raw_users:
        if not isinstance(item, dict):
            continue
        username = str(item.get("username", "") or "").strip()
        password_hash = str(item.get("password_hash", "") or "").strip()
        if not username or not password_hash:
            continue
        users[username] = {
            "password_hash": password_hash,
            "tenant_id": str(item.get("tenant_id", "") or "").strip(),
            "actor_id": str(item.get("actor_id", "") or username).strip(),
            "role": str(item.get("role", "user") or "user").strip().lower(),
            "display_name": str(
                item.get("display_name", "") or username
            ).strip(),
            "email": str(item.get("email", "") or "").strip(),
        }
    return users


def _resolve_secret(cfg_section: dict) -> bytes:
    """Resolve the token-signing secret (base64, hex, or raw text). When unset, generates
    a random per-process secret (sessions then don't survive a restart or span multiple
    workers — logged at INFO)."""
    raw = resolve_env_or_cfg("HERMES_DASHBOARD_BASIC_AUTH_SECRET", cfg_section.get("secret"))
    if not raw:
        logger.info(
            "dashboard-auth-basic: no 'secret' configured; generating a random "
            "per-process signing key. Sessions will not survive a restart or span "
            "multiple workers. Set dashboard.basic_auth.secret (or "
            "HERMES_DASHBOARD_BASIC_AUTH_SECRET) for stable sessions.")
        return secrets.token_bytes(32)
    for decoder in (base64.b64decode, bytes.fromhex):
        try:
            decoded = decoder(raw)
            if len(decoded) >= 16:
                return decoded
        except (ValueError, TypeError):
            pass
    return raw.encode("utf-8")


def _settings() -> dict:
    """Resolve BasicAuthProvider kwargs from env/config; raises ``SkipRegistration``."""
    global LAST_SKIP_REASON
    section = _load_config_basic_auth_section()
    username = resolve_env_or_cfg("HERMES_DASHBOARD_BASIC_AUTH_USERNAME", section.get("username", ""))
    password_hash = resolve_env_or_cfg("HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH", section.get("password_hash", ""))
    plaintext = resolve_env_or_cfg("HERMES_DASHBOARD_BASIC_AUTH_PASSWORD", section.get("password", ""))
    ttl_raw = resolve_env_or_cfg("HERMES_DASHBOARD_BASIC_AUTH_TTL_SECONDS", section.get("session_ttl_seconds", ""))
    users = _load_users_from_config(section)

    if not users and not username:
        LAST_SKIP_REASON = (
            "dashboard.basic_auth.username is not set and "
            "dashboard.basic_auth.users is empty (and "
            "HERMES_DASHBOARD_BASIC_AUTH_USERNAME is empty). Configure a "
            "username/password or role-bearing users under dashboard.basic_auth."
        )
        logger.debug("dashboard-auth-basic: %s", LAST_SKIP_REASON)
        raise SkipRegistration(LAST_SKIP_REASON)

    if not users and not password_hash and not plaintext:
        LAST_SKIP_REASON = (
            "dashboard.basic_auth.username is set but neither password_hash "
            "nor password is configured. Provide one of them (password_hash "
            "is preferred — compute it with "
            "plugins.dashboard_auth.basic.hash_password)."
        )
        logger.warning("dashboard-auth-basic: %s", LAST_SKIP_REASON)
        raise SkipRegistration(LAST_SKIP_REASON, level="warning")

    # Precedence (env-wins convention): a password supplied via the
    # HERMES_DASHBOARD_BASIC_AUTH_PASSWORD env var overrides a config.yaml
    # password_hash, so an operator can rotate the password by setting an
    # env var without editing config. A password_hash (precomputed) wins
    # over a config-only plaintext password at the same tier — it's the
    # preferred at-rest form. Concretely:
    #   * env password set        → hash it (overrides any config hash)
    #   * else config password_hash set → use it
    #   * else config plaintext password → hash it in-memory
    plaintext_from_env = os.environ.get(
        "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD", ""
    ).strip()
    if plaintext_from_env:
        password_hash = hash_password(plaintext_from_env)
        logger.info(
            "dashboard-auth-basic: hashed env-supplied password in-memory "
            "(overrides any config password_hash)."
        )
    elif password_hash:
        pass
    elif plaintext:
        # config-only plaintext password.
        password_hash = hash_password(plaintext)
        logger.info(
            "dashboard-auth-basic: hashed plaintext password in-memory. "
            "For production, precompute dashboard.basic_auth.password_hash "
            "and remove the plaintext password from config."
        )
    elif users:
        # A users table remains usable on its own. Never manufacture a
        # top-level administrator credential from an absent password.
        if username:
            logger.warning(
                "dashboard-auth-basic: top-level username %r is configured "
                "without a password or password_hash; ignoring that incomplete "
                "credential and registering only the users table.",
                username,
            )
        username = ""
        password_hash = ""

    secret = _resolve_secret(section)

    try:
        ttl = int(ttl_raw) if ttl_raw else _DEFAULT_TTL_SECONDS
    except ValueError:
        ttl = _DEFAULT_TTL_SECONDS
    return {
        "username": username,
        "password_hash": password_hash,
        "secret": secret,
        "ttl_seconds": ttl,
        "users": users or None,
        "authority_resolver": (
            (lambda: _load_users_from_config(_load_config_basic_auth_section()))
            if users
            else None
        ),
    }


def register(ctx) -> None:
    """Register ``BasicAuthProvider`` when password auth is configured."""
    global LAST_SKIP_REASON
    LAST_SKIP_REASON = ""
    kwargs, LAST_SKIP_REASON = register_provider(ctx, logger, _TAG, BasicAuthProvider, _settings)
    if kwargs is not None:
        users = kwargs.get("users")
        logger.info(
            "dashboard-auth-basic: registered password provider (users=%s)",
            len(users) if users else 1,
        )
