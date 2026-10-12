"""Auth primitives: password hashing, JWT signing/verification, OAuth token checks."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import jwt
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token
from passlib.context import CryptContext

from app.core.config import settings


# ---------- Password hashing ----------

# bcrypt with cost factor 12 (default passlib default for bcrypt rounds).
# `deprecated="auto"` auto-upgrades hashes from older formats on next verify().
_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(plain: str) -> str:
    """Hash a plaintext password with bcrypt."""
    return _pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """Verify a plaintext password against a stored bcrypt hash."""
    return _pwd_context.verify(plain, hashed)


# ---------- JWT (our own) ----------

def create_access_token(*, subject: int, extra_claims: dict[str, Any] | None = None) -> tuple[str, int]:
    """Create a signed JWT for the given user.

    Returns (token, expires_in_seconds). The token's `sub` claim is the user id.
    """
    expires_seconds = settings.jwt_expiration_minutes * 60
    now = datetime.now(timezone.utc)
    exp = now + timedelta(seconds=expires_seconds)
    payload: dict[str, Any] = {
        "sub": str(subject),
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
    }
    if extra_claims:
        payload.update(extra_claims)
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, expires_seconds


def decode_access_token(token: str) -> dict[str, Any]:
    """Decode and verify a JWT. Raises jwt.PyJWTError on failure."""
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])


# ---------- Google ----------

def verify_google_id_token(id_token_str: str) -> dict[str, Any]:
    """Verify a Google id_token (JWT signed by Google).

    Returns the id_token payload dict with `email`, `sub` (Google user id),
    `name`, `picture`, etc. Raises google.auth.exceptions.GoogleAuthError on failure.
    """
    return google_id_token.verify_oauth2_token(
        id_token_str,
        google_requests.Request(),
        settings.google_client_id,
    )


# ---------- X (Twitter) ----------

X_API_BASE = "https://api.twitter.com/2"


async def fetch_x_userinfo(access_token: str) -> dict[str, Any]:
    """Call X /2/users/me with the user's access_token.

    Returns the `data` dict from the response:
      { "id": "12345", "username": "...", "profile_image_url": "...", "name": "..." }
    Raises httpx.HTTPStatusError on auth failure or non-200 response.
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            f"{X_API_BASE}/users/me",
            params={"user.fields": "id,name,username,profile_image_url"},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        r.raise_for_status()
    return r.json()["data"]