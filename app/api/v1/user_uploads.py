"""User avatar upload endpoints (``/users/me/avatar``).

Always self-scoped: there is no ``{user_id}`` path parameter. The endpoint
reads the current user from the Bearer token, so a user can only modify their
own avatar. The validation, size, and storage-key rules are the same as the
team-logo path — both go through :mod:`app.storage.validation`.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.auth import get_current_user
from app.db.models import User
from app.db.session import get_session
from app.storage import get_storage
from app.storage.validation import extension_for_content_type, validate_image

router = APIRouter(prefix="/users", tags=["users"])


class AvatarOut(BaseModel):
    """POST /users/me/avatar response body."""
    model_config = ConfigDict(from_attributes=True)

    user_id: int = Field(..., description="Numeric primary key of the user")
    avatar_url: str = Field(..., description="Public URL where the avatar is served")


# Avatar keys are deterministic on ``user_id`` so re-uploads overwrite the same
# file. Same logic as admin_uploads._cleanup_previous_logo.
_AVATAR_EXTS = {".png", ".jpg", ".webp"}


async def _cleanup_previous_avatar(storage, user_id: int, current_ext: str) -> None:
    """Delete any previous-format avatar file for ``user_id``."""
    for ext in _AVATAR_EXTS:
        if ext == f".{current_ext}":
            continue
        await storage.delete(f"users/{user_id}{ext}")


@router.post(
    "/me/avatar",
    response_model=AvatarOut,
    summary="Upload or replace the current user's avatar",
    responses={
        400: {"description": "Invalid image bytes (content-type doesn't match body)"},
        401: {"description": "Missing / invalid Bearer token"},
        413: {"description": "File too large (>5MB)"},
        415: {"description": "Unsupported content type"},
    },
)
async def upload_my_avatar(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> AvatarOut:
    """Upload / replace the authenticated user's avatar. Self-only."""
    content_type = file.content_type or ""
    data = await file.read()
    validate_image(data, content_type)

    ext = extension_for_content_type(content_type)
    new_key = f"users/{current_user.id}.{ext}"

    storage = get_storage()
    if current_user.avatar_url:
        await _cleanup_previous_avatar(storage, current_user.id, ext)

    new_url = await storage.save(new_key, data, content_type)
    current_user.avatar_url = new_url
    await session.commit()
    await session.refresh(current_user)

    return AvatarOut(user_id=current_user.id, avatar_url=current_user.avatar_url)


@router.delete(
    "/me/avatar",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete the current user's avatar",
    responses={
        401: {"description": "Missing / invalid Bearer token"},
    },
)
async def delete_my_avatar(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Remove the avatar file from storage and clear ``user.avatar_url``."""
    if current_user.avatar_url:
        storage = get_storage()
        for ext in _AVATAR_EXTS:
            await storage.delete(f"users/{current_user.id}{ext}")
        current_user.avatar_url = None
        await session.commit()
    return None