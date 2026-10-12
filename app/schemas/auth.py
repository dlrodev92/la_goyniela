"""Auth-related Pydantic DTOs."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


class RegisterIn(BaseModel):
    """POST /auth/register body."""
    email: EmailStr = Field(..., description="User email; lowercased and trimmed server-side")
    password: str = Field(..., min_length=8, max_length=128, description="Plaintext password (min 8 chars)")
    display_name: str = Field(..., min_length=2, max_length=60)

    @field_validator("display_name")
    @classmethod
    def trim_display_name(cls, v: str) -> str:
        return v.strip()


class LoginIn(BaseModel):
    """POST /auth/login body."""
    email: EmailStr
    password: str = Field(..., min_length=1, max_length=128)


class GoogleAuthIn(BaseModel):
    """POST /auth/google body."""
    id_token: str = Field(..., min_length=10, description="id_token JWT from Google Identity Services")


class XAuthIn(BaseModel):
    """POST /auth/x body."""
    access_token: str = Field(..., min_length=10, description="access_token from X OAuth 2.0")


class UserOut(BaseModel):
    """Public user representation returned by /auth/me and embedded in TokenOut."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    display_name: str
    avatar_url: str | None = None
    is_active: bool = Field(
        default=True,
        description="Whether the account is active (deactivated accounts can't log in)",
    )
    created_at: datetime = Field(
        ..., description="UTC timestamp when the user was created",
    )
    auth_providers: list[str] = Field(
        default_factory=list,
        description="List of linked auth providers on this account (e.g. ['local','google'])",
    )

    @classmethod
    def from_user(cls, user) -> "UserOut":
        providers: list[str] = []
        if user.password_hash:
            providers.append("local")
        if user.google_id:
            providers.append("google")
        if user.x_id:
            providers.append("x")
        return cls(
            id=user.id,
            email=user.email,
            display_name=user.display_name,
            avatar_url=user.avatar_url,
            is_active=bool(user.is_active),
            created_at=user.created_at,
            auth_providers=providers,
        )


class TokenOut(BaseModel):
    """Response for register/login/google/x."""
    access_token: str
    token_type: str = "Bearer"
    expires_in: int = Field(..., description="Seconds until token expiration")
    user: UserOut