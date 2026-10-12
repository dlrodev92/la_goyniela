"""Tests for the auth endpoints (register/login/google/x/me).

Local password flow uses real bcrypt + real JWT signing (in-process).
Google/X flows are tested with mocked OAuth verify — we don't hit real providers.
"""
from __future__ import annotations

import httpx
import jwt
import pytest

from app.core.config import settings
from app.core.security import create_access_token


pytestmark = pytest.mark.asyncio


# ---------- Helpers ----------

async def _delete_user_by_email(email: str) -> None:
    from sqlalchemy import text
    from app.db.session import engine
    async with engine.begin() as conn:
        result = await conn.execute(
            text("SELECT id FROM \"user\" WHERE email=:e"), {"e": email},
        )
        row = result.first()
        if row is None:
            return
        uid = row[0]
        await conn.execute(text("DELETE FROM group_member WHERE user_id=:u"), {"u": uid})
        await conn.execute(
            text("DELETE FROM \"group\" WHERE created_by_user_id=:u"), {"u": uid},
        )
        await conn.execute(text("DELETE FROM \"user\" WHERE id=:u"), {"u": uid})


# ---------- Register / login / me ----------

async def test_register_creates_user_and_returns_jwt(client):
    await _delete_user_by_email("test_reg@example.com")
    r = await client.post("/auth/register", json={
        "email": "test_reg@example.com",
        "password": "hunter22",
        "display_name": "Test Reg",
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] > 0
    assert body["user"]["email"] == "test_reg@example.com"
    assert body["user"]["display_name"] == "Test Reg"
    assert "local" in body["user"]["auth_providers"]
    # Verify the JWT decodes
    decoded = jwt.decode(body["access_token"], settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    assert decoded["sub"] == str(body["user"]["id"])


async def test_register_normalizes_email_lowercase(client):
    await _delete_user_by_email("lowerme@example.com")
    r = await client.post("/auth/register", json={
        "email": "LowerMe@Example.COM",
        "password": "hunter22",
        "display_name": "Lower Me",
    })
    assert r.status_code == 201, r.text
    assert r.json()["user"]["email"] == "lowerme@example.com"


async def test_register_duplicate_email_409(client):
    await _delete_user_by_email("dup@example.com")
    for _ in range(2):
        r = await client.post("/auth/register", json={
            "email": "dup@example.com",
            "password": "hunter22",
            "display_name": "Dup",
        })
    assert r.status_code in (201, 409)
    r2 = await client.post("/auth/register", json={
        "email": "dup@example.com",
        "password": "different",
        "display_name": "Dup2",
    })
    assert r2.status_code == 409


async def test_register_password_too_short_422(client):
    r = await client.post("/auth/register", json={
        "email": "short@example.com",
        "password": "short",
        "display_name": "Short",
    })
    assert r.status_code == 422


async def test_login_success(client):
    await _delete_user_by_email("login@example.com")
    await client.post("/auth/register", json={
        "email": "login@example.com",
        "password": "hunter22",
        "display_name": "Login",
    })
    r = await client.post("/auth/login", json={
        "email": "login@example.com",
        "password": "hunter22",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert "access_token" in body
    assert body["user"]["email"] == "login@example.com"


async def test_login_wrong_password_401(client):
    await _delete_user_by_email("wrongpass@example.com")
    await client.post("/auth/register", json={
        "email": "wrongpass@example.com",
        "password": "hunter22",
        "display_name": "Wrong",
    })
    r = await client.post("/auth/login", json={
        "email": "wrongpass@example.com",
        "password": "WRONG",
    })
    assert r.status_code == 401


async def test_login_unknown_user_401(client):
    r = await client.post("/auth/login", json={
        "email": "nobody@example.com",
        "password": "whatever",
    })
    assert r.status_code == 401


async def test_me_requires_token(client):
    r = await client.get("/auth/me")
    assert r.status_code == 401


async def test_me_invalid_token_401(client):
    r = await client.get("/auth/me", headers={"Authorization": "Bearer invalid.token.here"})
    assert r.status_code == 401


async def test_me_expired_token_401(client):
    from datetime import datetime, timezone, timedelta
    payload = {"sub": "1", "iat": 0, "exp": int((datetime.now(timezone.utc) - timedelta(hours=1)).timestamp())}
    expired = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    r = await client.get("/auth/me", headers={"Authorization": f"Bearer {expired}"})
    assert r.status_code == 401


async def test_me_returns_current_user(client):
    await _delete_user_by_email("me@example.com")
    r = await client.post("/auth/register", json={
        "email": "me@example.com",
        "password": "hunter22",
        "display_name": "Me User",
    })
    token = r.json()["access_token"]
    r2 = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r2.status_code == 200
    assert r2.json()["email"] == "me@example.com"


# ---------- Google ----------

async def test_google_login_creates_user(client, monkeypatch):
    await _delete_user_by_email("googler@example.com")
    fake_idinfo = {
        "email": "googler@example.com",
        "sub": "google-12345-fake",
        "name": "Googler",
        "picture": "https://example.com/avatar.jpg",
    }
    monkeypatch.setattr(
        "app.api.v1.auth.verify_google_id_token",
        lambda token, **kw: fake_idinfo,
    )
    r = await client.post("/auth/google", json={"id_token": "fake-id-token"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user"]["email"] == "googler@example.com"
    assert "google" in body["user"]["auth_providers"]
    assert body["user"]["avatar_url"] == "https://example.com/avatar.jpg"


async def test_google_login_links_existing_user_by_email(client, monkeypatch):
    # First create a local user
    await _delete_user_by_email("linked@example.com")
    await client.post("/auth/register", json={
        "email": "linked@example.com",
        "password": "hunter22",
        "display_name": "Linked",
    })

    # Then login with Google using same email
    fake_idinfo = {
        "email": "linked@example.com",
        "sub": "google-99999-new",
        "name": "Linked Now",
        "picture": "https://example.com/pic.jpg",
    }
    monkeypatch.setattr(
        "app.api.v1.auth.verify_google_id_token",
        lambda token, **kw: fake_idinfo,
    )
    r = await client.post("/auth/google", json={"id_token": "fake-token"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user"]["email"] == "linked@example.com"
    # Now has BOTH providers
    assert "local" in body["user"]["auth_providers"]
    assert "google" in body["user"]["auth_providers"]


async def test_google_login_invalid_token_401(client, monkeypatch):
    def fake_verify(token, **kw):
        from google.auth.exceptions import GoogleAuthError
        raise GoogleAuthError("bad token")
    monkeypatch.setattr("app.api.v1.auth.verify_google_id_token", fake_verify)
    r = await client.post("/auth/google", json={"id_token": "faketoken1234567"})
    assert r.status_code == 401


# ---------- X ----------

async def test_x_login_creates_user(client, httpx_mock):
    httpx_mock.add_response(
        method="GET",
        url="https://api.twitter.com/2/users/me?user.fields=id%2Cname%2Cusername%2Cprofile_image_url",
        json={
            "data": {
                "id": "12345",
                "username": "xuser",
                "name": "X User",
                "profile_image_url": "https://example.com/x.jpg",
            }
        },
    )

    r = await client.post("/auth/x", json={"access_token": "fake-x-token"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user"]["display_name"] == "X User"
    assert body["user"]["avatar_url"] == "https://example.com/x.jpg"
    assert "x" in body["user"]["auth_providers"]


async def test_x_login_invalid_token_401(client, httpx_mock):
    httpx_mock.add_response(
        method="GET",
        url="https://api.twitter.com/2/users/me?user.fields=id%2Cname%2Cusername%2Cprofile_image_url",
        status_code=401,
        json={"errors": [{"message": "Unauthorized"}]},
    )
    r = await client.post("/auth/x", json={"access_token": "bad-token-1234"})
    assert r.status_code == 401


# ---------- UserOut contract (Fix #6) ----------


async def test_user_out_includes_created_at(client):
    """The UserOut schema must surface ``created_at`` (and ``is_active``)
    on both /auth/register and /auth/me, matching docs/API.md §2."""
    await _delete_user_by_email("created_at@example.com")
    r = await client.post("/auth/register", json={
        "email": "created_at@example.com",
        "password": "hunter22",
        "display_name": "Created At",
    })
    assert r.status_code == 201, r.text
    user = r.json()["user"]
    assert "created_at" in user, user
    assert user["created_at"]  # non-empty ISO timestamp
    # is_active should also be present (default true)
    assert user.get("is_active") is True

    token = r.json()["access_token"]
    r2 = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r2.status_code == 200
    me = r2.json()
    assert "created_at" in me
    assert me["created_at"] == user["created_at"]


# ---------- No leak of PII / exception text (Fix #7) ----------


async def test_register_409_does_not_leak_email_in_detail(client):
    """The 409 detail for a duplicate email must NOT contain the email
    (so the message body is safe to log / surface on error monitors)."""
    await _delete_user_by_email("dup_detail@example.com")
    r1 = await client.post("/auth/register", json={
        "email": "dup_detail@example.com",
        "password": "hunter22",
        "display_name": "Dup",
    })
    assert r1.status_code == 201

    r2 = await client.post("/auth/register", json={
        "email": "dup_detail@example.com",
        "password": "different",
        "display_name": "Dup2",
    })
    assert r2.status_code == 409
    detail = r2.json().get("detail", "")
    assert isinstance(detail, str), detail
    assert "dup_detail@example.com" not in detail
    assert "@" not in detail  # belt-and-suspenders: no email-looking substring


# ---------- 502 on upstream X failure (Fix #8) ----------


async def test_auth_x_returns_502_on_upstream_failure(client, httpx_mock):
    """When we cannot reach X at all (network / DNS / timeout), the endpoint
    must return 502 — NOT 401 — so the client can distinguish a credential
    problem (401) from a transient upstream outage (502)."""
    import httpx as _httpx
    httpx_mock.add_exception(
        _httpx.ConnectError("Name or service not known"),
        method="GET",
        url="https://api.twitter.com/2/users/me?user.fields=id%2Cname%2Cusername%2Cprofile_image_url",
    )
    r = await client.post("/auth/x", json={"access_token": "faketoken1234567"})
    assert r.status_code == 502, r.text
    # Public detail must be the stable user-facing string, not the raw exception.
    assert r.json()["detail"] == "x service unavailable"