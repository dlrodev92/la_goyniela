"""Superadmin user-management endpoints.

Mounted under ``/api/v1/admin/users``. Slice 2 of the superadmin-panel
sprint. Auth contract: every endpoint calls :func:`require_superadmin`
as its first line, so the only caller that gets past the 403 is a user
with ``is_superadmin=True`` on the ``user`` row (seeded in the slice 1
migration; granted via the PATCH endpoint below once the panel ships).

Endpoints:

- ``GET   /api/v1/admin/users``           — list users; optional ``?only_superadmins=true``.
- ``PATCH /api/v1/admin/users/{user_id}`` — toggle ``is_superadmin`` and/or ``is_active``;
                                             blocks the last-superadmin demotion with 400.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.admin import require_superadmin
from app.api.v1.auth import get_current_user
from app.db.models import User
from app.db.session import get_session
from app.schemas.admin import UserAdminOut, UserPatchIn


router = APIRouter(prefix="/api/v1/admin/users", tags=["admin-users"])


@router.get(
    "",
    response_model=list[UserAdminOut],
    summary="[superadmin] List users",
    responses={
        401: {"description": "Missing / invalid Bearer token"},
        403: {"description": "Actor is not a superadmin"},
    },
)
async def list_users(
    only_superadmins: bool = Query(
        False,
        description=(
            "When true, only returns users with ``is_superadmin=True``. "
            "Used by the panel to find the current superadmin cohort."
        ),
    ),
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[UserAdminOut]:
    """Return paginated users (newest first). Superadmin-only."""
    await require_superadmin(current_user)

    stmt = select(User)
    if only_superadmins:
        stmt = stmt.where(User.is_superadmin.is_(True))
    stmt = stmt.order_by(User.id.desc()).limit(limit).offset(offset)
    rows = (await session.execute(stmt)).scalars().all()
    return [UserAdminOut.model_validate(r) for r in rows]


@router.patch(
    "/{user_id}",
    response_model=UserAdminOut,
    summary="[superadmin] Toggle superadmin / is_active on a user",
    description=(
        "Fields are optional — only the ones present in the body are "
        "updated. Demoting a superadmin (setting ``is_superadmin=false``) "
        "is refused with 400 when no other superadmin would remain; we "
        "never let the system reach zero superadmins."
    ),
    responses={
        400: {"description": "Demotion would leave zero superadmins"},
        401: {"description": "Missing / invalid Bearer token"},
        403: {"description": "Actor is not a superadmin"},
        404: {"description": "User not found"},
    },
)
async def patch_user(
    user_id: int,
    body: UserPatchIn,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> UserAdminOut:
    """Toggle ``is_superadmin`` / ``is_active`` on a user. Superadmin-only."""
    await require_superadmin(current_user)

    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"user_id={user_id} not found",
        )

    # Demotion guard: if we're flipping is_superadmin True → False, count
    # remaining superadmins (excluding this row) and refuse if it would
    # leave zero. We count AFTER the demotion in our heads; equivalent
    # to "current superadmin count == 1 AND this user is the only one
    # AND they are being demoted".
    demoting_to_false = (
        body.is_superadmin is False and user.is_superadmin is True
    )
    if demoting_to_false:
        remaining = await session.scalar(
            select(func.count()).select_from(User).where(
                User.is_superadmin.is_(True),
                User.id != user_id,
            )
        ) or 0
        if remaining == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "cannot demote the last superadmin; promote another "
                    "user first"
                ),
            )

    if body.is_superadmin is not None:
        user.is_superadmin = body.is_superadmin
    if body.is_active is not None:
        user.is_active = body.is_active

    await session.commit()
    await session.refresh(user)
    return UserAdminOut.model_validate(user)
