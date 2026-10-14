"""Password hashing (Argon2) and signed, expiring tokens (JWT)."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

ALGORITHM = "HS256"

_hasher = PasswordHasher()

# Verified against when an account doesn't exist, so a login attempt takes about
# the same time whether or not the email is registered.
DUMMY_HASH = _hasher.hash("route-optimizer-dummy-password")


class TokenError(Exception):
    """The token is missing, malformed, expired, tampered with, or the wrong kind."""


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_fingerprint(password_hash: str) -> str:
    """Short digest of the current hash. Baked into reset tokens so a token stops
    working the moment the password changes (single use)."""
    return hashlib.sha256(password_hash.encode("utf-8")).hexdigest()[:16]


def create_token(
    secret: str,
    *,
    subject: str,
    purpose: str,
    expires_in: timedelta,
    extra: dict | None = None,
) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": subject,
        "purpose": purpose,
        "iat": now,
        "exp": now + expires_in,
        **(extra or {}),
    }
    return jwt.encode(payload, secret, algorithm=ALGORITHM)


def decode_token(secret: str, token: str, *, purpose: str) -> dict:
    """Return the payload, or raise TokenError. A token minted for one purpose
    (e.g. email verification) is never accepted for another (e.g. login)."""
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=[ALGORITHM],
            options={"require": ["exp", "sub", "purpose"]},
        )
    except jwt.PyJWTError as exc:
        raise TokenError("Invalid or expired token") from exc
    if payload.get("purpose") != purpose:
        raise TokenError("Token cannot be used for this action")
    return payload
