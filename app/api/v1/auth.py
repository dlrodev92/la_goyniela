"""Auth endpoints: register, login, Google, X, me."""
from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from google.auth.exceptions import GoogleAuthError
from jwt import PyJWTError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import (
    create_access_token,
    fetch_x_userinfo,
    hash_password,
    verify_password,
    verify_google_id_token,
)
from app.db.models import User
from app.db.session import get_session
from app.schemas.auth import (
    GoogleAuthIn,
    LoginIn,
    RegisterIn,
    TokenOut,
    UserOut,
    XAuthIn,
)

# stdlib logger (project convention). All upstream failures from Google/X
# land here — the public detail is always a fixed user-facing string.
_LOG = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

# HTTPBearer parses `Authorization: Bearer <token>` automatically; auto_error=False
# so we can return a friendlier 401 message ourselves.
_bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(get_session),
) -> User:
    """FastAPI dependency: extract user from the Bearer token."""
    if creds is None or not creds.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization Bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        import jwt as _jwt
        payload = _jwt.decode(
            creds.credentials, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
        )
    except PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid or expired token: {type(exc).__name__}",
            headers={"WWW-Authenticate": "Bearer"},
        )

    sub = payload.get("sub")
    if sub is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token missing sub claim")

    user = await session.scalar(select(User).where(User.id == int(sub)))
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")
    return user


# ---------- Endpoints ----------

@router.post(
    "/register",
    response_model=TokenOut,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new user with email + password (local provider)",
)
async def register(body: RegisterIn, session: AsyncSession = Depends(get_session)) -> TokenOut:
    email_norm = body.email.lower().strip()
    existing = await session.scalar(select(User).where(User.email == email_norm))
    if existing is not None:
        # Note: we deliberately do NOT echo the email back in `detail` — the
        # client already has it, and echoing it leaks PII through logs and
        # error-monitoring surfaces that capture full responses.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="email already registered",
        )
    user = User(
        email=email_norm,
        display_name=body.display_name,
        password_hash=hash_password(body.password),
    )
    session.add(user)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="email already registered")
    await session.refresh(user)

    token, expires_in = create_access_token(subject=user.id)
    return TokenOut(
        access_token=token,
        expires_in=expires_in,
        user=UserOut.from_user(user),
    )


@router.post(
    "/login",
    response_model=TokenOut,
    summary="Authenticate with email + password (local provider)",
)
async def login(body: LoginIn, session: AsyncSession = Depends(get_session)) -> TokenOut:
    email_norm = body.email.lower().strip()
    user = await session.scalar(select(User).where(User.email == email_norm))
    if user is None or user.password_hash is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account inactive")

    token, expires_in = create_access_token(subject=user.id)
    return TokenOut(
        access_token=token,
        expires_in=expires_in,
        user=UserOut.from_user(user),
    )


@router.post(
    "/google",
    response_model=TokenOut,
    summary="Authenticate with a Google id_token (auto-links by email)",
)
async def google_login(body: GoogleAuthIn, session: AsyncSession = Depends(get_session)) -> TokenOut:
    try:
        idinfo = verify_google_id_token(body.id_token)
    except GoogleAuthError as exc:
        # Fixed user-facing message; the actual exception (incl. token shape
        # and reason) goes to the internal log so operators can debug it
        # without leaking that detail to the client.
        _LOG.exception("google id_token verification failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="google authentication failed",
        )

    email_norm = idinfo["email"].lower().strip()
    google_id = idinfo["sub"]
    display_name = idinfo.get("name") or email_norm.split("@")[0]
    avatar_url = idinfo.get("picture")

    user = await session.scalar(select(User).where(User.google_id == google_id))
    if user is None:
        # Auto-link: maybe the user exists by email
        user = await session.scalar(select(User).where(User.email == email_norm))
        if user is not None:
            user.google_id = google_id
            if not user.avatar_url and avatar_url:
                user.avatar_url = avatar_url
        else:
            user = User(
                email=email_norm,
                display_name=display_name,
                google_id=google_id,
                avatar_url=avatar_url,
            )
            session.add(user)
    else:
        # Existing google user — refresh avatar if missing
        if not user.avatar_url and avatar_url:
            user.avatar_url = avatar_url
        if user.display_name != display_name:
            user.display_name = display_name

    await session.commit()
    await session.refresh(user)

    token, expires_in = create_access_token(subject=user.id)
    return TokenOut(
        access_token=token,
        expires_in=expires_in,
        user=UserOut.from_user(user),
    )


@router.post(
    "/x",
    response_model=TokenOut,
    summary="Authenticate with an X (Twitter) access_token (auto-links by email)",
)
async def x_login(body: XAuthIn, session: AsyncSession = Depends(get_session)) -> TokenOut:
    try:
        x_data = await fetch_x_userinfo(body.access_token)
    except httpx.HTTPStatusError as exc:
        # X itself rejected the credentials (typically 401/403) — the token
        # is bad. This is a client-visible auth error.
        _LOG.warning("X rejected access_token: status=%s", exc.response.status_code)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid x credentials",
        )
    except (
        httpx.ConnectError,
        httpx.TimeoutException,
        httpx.RequestError,
    ) as exc:
        # We could not reach X at all (DNS, TCP, timeout, TLS, etc.) —
        # 502 because the problem is upstream, not the user's credentials.
        _LOG.exception("X upstream unreachable: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="x service unavailable",
        )
    except Exception as exc:
        # Unexpected — log everything but keep the public detail stable so
        # the client never sees a leaky stack-trace style string.
        _LOG.exception("X login: unhandled error: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="x authentication failed",
        )

    x_id = x_data["id"]
    x_name = x_data.get("name") or x_data.get("username") or "X User"
    avatar_url = x_data.get("profile_image_url")

    # X /2/users/me by default returns just id+name+username; we requested profile_image_url.
    # Email is sometimes available via `user.fields=email` if granted scope — if not, we use x_username as fallback identifier.

    user = await session.scalar(select(User).where(User.x_id == x_id))
    if user is None:
        # No email from X in v1 — we can't auto-link by email.
        # Just create the user. (Future: request `email` scope from X.)
        user = User(
            email=f"x_{x_id}@x.placeholder",  # placeholder; future: real email from X
            display_name=x_name,
            x_id=x_id,
            avatar_url=avatar_url,
        )
        session.add(user)
    else:
        if not user.avatar_url and avatar_url:
            user.avatar_url = avatar_url
        if user.display_name != x_name:
            user.display_name = x_name

    await session.commit()
    await session.refresh(user)

    token, expires_in = create_access_token(subject=user.id)
    return TokenOut(
        access_token=token,
        expires_in=expires_in,
        user=UserOut.from_user(user),
    )


@router.get(
    "/me",
    response_model=UserOut,
    summary="Get the currently authenticated user (requires Bearer token)",
)
async def get_me(current_user: User = Depends(get_current_user)) -> UserOut:
    return UserOut.from_user(current_user)