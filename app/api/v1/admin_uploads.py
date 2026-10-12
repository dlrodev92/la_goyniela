"""Admin image-upload endpoints: team logos.

The path is rooted at ``/admin/teams`` so we don't collide with the existing
read-only ``/api/v1/teams`` routes. Access is restricted to users who are admin
of at least one group — there is no system-level admin role yet, and a stricter
gate (``is_admin`` column on ``user``) is documented as a follow-up.

Storage keys use the team's ``entity_id`` (not the numeric ``team.id``) so a
team-logos re-upload always lands on the same path and overwrites the old file
without leaving orphans.

Endpoints
---------
- ``POST   /admin/teams/{team_id}/logo``  upload / replace a team logo
- ``DELETE /admin/teams/{team_id}/logo``  delete the logo

Both return on success; on validation failure the dedicated 4xx from
:mod:`app.storage.validation` is preserved.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.admin import require_system_admin
from app.api.v1.auth import get_current_user
from app.db.models import TeamIdentityMap, User
from app.db.session import get_session
from app.storage import get_storage
from app.storage.validation import extension_for_content_type, validate_image

router = APIRouter(prefix="/admin/teams", tags=["admin-uploads"])


# ---------- Schemas ----------


class TeamLogoOut(BaseModel):
    """POST /admin/teams/{id}/logo response body."""
    model_config = ConfigDict(from_attributes=True)

    team_id: int = Field(..., description="Numeric primary key of the team")
    entity_id: str = Field(..., description="Stable slug used as the storage key prefix")
    logo_url: str = Field(..., description="Public URL where the logo is served")
    size: int = Field(..., description="Size of the stored file in bytes")


# All possible extensions a logo could have been stored under. When the user
# uploads a logo with a different format than the previous one, the OLD file
# has to be removed too — we try every known extension; the backend treats
# missing keys as a no-op.
_LOGO_EXTS = {".png", ".jpg", ".webp"}


async def _cleanup_previous_logo(storage, entity_id: str, current_ext: str) -> None:
    """Delete any previous-format logo file for ``entity_id``.

    Same-extension uploads simply overwrite the same storage key — no cleanup
    needed. Different-extension uploads leave the old file behind unless we
    delete it explicitly, hence this helper.
    """
    for ext in _LOGO_EXTS:
        if ext == f".{current_ext}":
            continue
        await storage.delete(f"teams/{entity_id}{ext}")


# ---------- Endpoints ----------


@router.post(
    "/{team_id}/logo",
    response_model=TeamLogoOut,
    summary="[admin] Upload or replace a team logo",
    responses={
        400: {"description": "Invalid image bytes (e.g. content-type doesn't match body)"},
        401: {"description": "Missing / invalid Bearer token"},
        403: {"description": "Actor is not a system admin"},
        404: {"description": "Team not found"},
        413: {"description": "File too large (>5MB)"},
        415: {"description": "Unsupported content type"},
    },
)
async def upload_team_logo(
    team_id: int,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> TeamLogoOut:
    """Upload or replace a team's logo. ``file`` is the multipart file field."""
    await require_system_admin(current_user, session)

    team = await session.scalar(
        select(TeamIdentityMap).where(TeamIdentityMap.id == team_id)
    )
    if team is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"team_id={team_id} not found",
        )

    content_type = file.content_type or ""
    data = await file.read()
    validate_image(data, content_type)

    ext = extension_for_content_type(content_type)
    new_key = f"teams/{team.entity_id}.{ext}"

    # Clean up any previous-format logo before we save the new one. The new
    # save is allowed to overwrite a same-extension file in place.
    storage = get_storage()
    if team.logo_url:
        await _cleanup_previous_logo(storage, team.entity_id, ext)

    new_url = await storage.save(new_key, data, content_type)
    team.logo_url = new_url
    await session.commit()
    await session.refresh(team)

    return TeamLogoOut(
        team_id=team.id,
        entity_id=team.entity_id,
        logo_url=team.logo_url,
        size=len(data),
    )


@router.delete(
    "/{team_id}/logo",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="[admin] Delete a team logo (removes the file and clears logo_url)",
    responses={
        401: {"description": "Missing / invalid Bearer token"},
        403: {"description": "Actor is not a system admin"},
        404: {"description": "Team not found"},
    },
)
async def delete_team_logo(
    team_id: int,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Remove the team's logo from storage and clear the URL on the row."""
    await require_system_admin(current_user, session)

    team = await session.scalar(
        select(TeamIdentityMap).where(TeamIdentityMap.id == team_id)
    )
    if team is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"team_id={team_id} not found",
        )

    if team.logo_url:
        storage = get_storage()
        # Try every possible extension; backend treats missing keys as no-op.
        for ext in _LOGO_EXTS:
            await storage.delete(f"teams/{team.entity_id}{ext}")
        team.logo_url = None
        await session.commit()
    return None