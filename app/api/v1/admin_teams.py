"""Superadmin team-management endpoints.

Mounted under ``/api/v1/admin/teams``. Slice 2 of the superadmin-panel
sprint. Auth contract: every endpoint calls :func:`require_superadmin`
as its first line.

Endpoints:

- ``GET    /api/v1/admin/teams``                  — list; ``?include_deleted=true`` shows soft-deleted.
- ``POST   /api/v1/admin/teams``                  — create a new team identity.
- ``GET    /api/v1/admin/teams/{team_id}``        — single team (any deleted state).
- ``PATCH  /api/v1/admin/teams/{team_id}``        — edit any field.
- ``DELETE /api/v1/admin/teams/{team_id}``        — soft-delete (set ``deleted_at``).
                                                   Refused with 409 when the team is referenced
                                                   by any ``composicion`` row.
- ``POST   /api/v1/admin/teams/{team_id}/restore``— clear ``deleted_at`` (no other side effects).
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.admin import require_superadmin
from app.api.v1.auth import get_current_user
from app.db.models import Composicion, TeamIdentityMap, User
from app.db.session import get_session
from app.schemas.admin import TeamCreateIn, TeamOut, TeamPatchIn


router = APIRouter(prefix="/api/v1/admin/teams", tags=["admin-teams"])


@router.get(
    "",
    response_model=list[TeamOut],
    summary="[superadmin] List teams (optionally including soft-deleted)",
    responses={
        401: {"description": "Missing / invalid Bearer token"},
        403: {"description": "Actor is not a superadmin"},
    },
)
async def list_teams(
    include_deleted: bool = Query(
        False,
        description=(
            "When true, soft-deleted teams are also returned. Default "
            "false to match the public ``/api/v1/teams`` endpoint."
        ),
    ),
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[TeamOut]:
    """List teams. Superadmin-only."""
    await require_superadmin(current_user)

    stmt = select(TeamIdentityMap)
    if not include_deleted:
        stmt = stmt.where(TeamIdentityMap.deleted_at.is_(None))
    stmt = stmt.order_by(TeamIdentityMap.id.desc()).limit(limit).offset(offset)
    rows = (await session.execute(stmt)).scalars().all()
    return [TeamOut.model_validate(r) for r in rows]


@router.post(
    "",
    response_model=TeamOut,
    status_code=status.HTTP_201_CREATED,
    summary="[superadmin] Create a new team identity",
    responses={
        400: {"description": "Validation error"},
        401: {"description": "Missing / invalid Bearer token"},
        403: {"description": "Actor is not a superadmin"},
        409: {"description": "A team with this entity_id already exists"},
    },
)
async def create_team(
    body: TeamCreateIn,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> TeamOut:
    """Create a new ``team_identity_map`` row. Superadmin-only.

    The unique constraint on ``entity_id`` will reject duplicates with
    409. Race with the team's functional unique index
    (``(competition_id, lower(unaccent(canonical_name)))``) is also
    surfaced as 409 — the catalog seeder may have already inserted a
    row with the same normalized name.
    """
    await require_superadmin(current_user)

    team = TeamIdentityMap(
        entity_id=body.entity_id,
        canonical_name=body.canonical_name,
        short_name=body.short_name,
        gender=body.gender,
        gender_known=body.gender_known,
        country=body.country,
        logo_url=body.logo_url,
        competition_id=body.competition_id,
    )
    session.add(team)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"team with entity_id={body.entity_id!r} (or matching the "
                f"functional unique index) already exists: {exc.orig}"
            ),
        ) from exc
    await session.refresh(team)
    return TeamOut.model_validate(team)


@router.get(
    "/{team_id}",
    response_model=TeamOut,
    summary="[superadmin] Get one team (any deleted state)",
    responses={
        401: {"description": "Missing / invalid Bearer token"},
        403: {"description": "Actor is not a superadmin"},
        404: {"description": "Team not found"},
    },
)
async def get_team(
    team_id: int,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> TeamOut:
    """Single team by id. Superadmin-only; surfaces soft-deleted teams."""
    await require_superadmin(current_user)

    team = await session.get(TeamIdentityMap, team_id)
    if team is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"team_id={team_id} not found",
        )
    return TeamOut.model_validate(team)


@router.patch(
    "/{team_id}",
    response_model=TeamOut,
    summary="[superadmin] Edit any field on a team",
    responses={
        400: {"description": "Validation error (no fields sent)"},
        401: {"description": "Missing / invalid Bearer token"},
        403: {"description": "Actor is not a superadmin"},
        404: {"description": "Team not found"},
    },
)
async def patch_team(
    team_id: int,
    body: TeamPatchIn,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> TeamOut:
    """Partial update. Only fields present in the body are applied."""
    await require_superadmin(current_user)

    team = await session.get(TeamIdentityMap, team_id)
    if team is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"team_id={team_id} not found",
        )

    # Pydantic's ``exclude_unset`` preserves "absent" vs "explicitly null"
    # so PATCH semantics work as expected (null = clear the column).
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="no fields to update; PATCH body must include at least one",
        )
    for field, value in updates.items():
        setattr(team, field, value)

    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"update conflicts with the (competition_id, "
                f"unaccent(canonical_name)) unique index: {exc.orig}"
            ),
        ) from exc
    await session.refresh(team)
    return TeamOut.model_validate(team)


@router.delete(
    "/{team_id}",
    response_model=TeamOut,
    summary="[superadmin] Soft-delete a team (set deleted_at = now)",
    description=(
        "Sets ``deleted_at`` to the current UTC time. Refused with 409 "
        "if the team is referenced by any ``composicion`` row (FK "
        "guard). Restore via ``POST /api/v1/admin/teams/{team_id}/restore``."
    ),
    responses={
        401: {"description": "Missing / invalid Bearer token"},
        403: {"description": "Actor is not a superadmin"},
        404: {"description": "Team not found"},
        409: {
            "description": (
                "Team is referenced by one or more composicion rows; "
                "cannot soft-delete"
            )
        },
    },
)
async def soft_delete_team(
    team_id: int,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> TeamOut:
    """Set ``deleted_at`` to now. FK guard via composicion count. Superadmin-only."""
    await require_superadmin(current_user)

    team = await session.get(TeamIdentityMap, team_id)
    if team is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"team_id={team_id} not found",
        )

    if team.deleted_at is not None:
        # Idempotent: re-deleting an already-deleted team just refreshes
        # the timestamp. We keep it idempotent rather than 409 to make
        # the panel's "delete" button safe to click twice.
        team.deleted_at = datetime.now(tz=timezone.utc)
        await session.commit()
        await session.refresh(team)
        return TeamOut.model_validate(team)

    # FK guard — count compos rows that reference this team. The check
    # runs in the same transaction as the update; concurrent inserts
    # between the count and the commit are still safe because the FK
    # itself is RESTRICT, so a hard collision would surface as an
    # IntegrityError on commit (and we map that to 409 below).
    fk_count = await session.scalar(
        select(func.count()).select_from(Composicion).where(or_(
            Composicion.local_team_id == team_id,
            Composicion.visitante_team_id == team_id,
        ))
    ) or 0
    if fk_count > 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"team referenced by {fk_count} partido(s); cannot "
                f"soft-delete"
            ),
        )

    team.deleted_at = datetime.now(tz=timezone.utc)
    try:
        await session.commit()
    except IntegrityError as exc:
        # Race: a composicion row was inserted between the count and
        # the commit. Surface as 409 so the client knows to retry.
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"team became referenced while deleting; please retry: "
                f"{exc.orig}"
            ),
        ) from exc
    await session.refresh(team)
    return TeamOut.model_validate(team)


@router.post(
    "/{team_id}/restore",
    response_model=TeamOut,
    summary="[superadmin] Restore a soft-deleted team (clear deleted_at)",
    description=(
        "Sets ``deleted_at = NULL`` on a previously soft-deleted team. "
        "No other side effects — compos rows are not re-validated, "
        "logo_url is untouched, etc. Idempotent: restoring a live team "
        "is a no-op (200 with the current row)."
    ),
    responses={
        401: {"description": "Missing / invalid Bearer token"},
        403: {"description": "Actor is not a superadmin"},
        404: {"description": "Team not found"},
    },
)
async def restore_team(
    team_id: int,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> TeamOut:
    """Clear ``deleted_at``. Idempotent on already-live teams. Superadmin-only."""
    await require_superadmin(current_user)

    team = await session.get(TeamIdentityMap, team_id)
    if team is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"team_id={team_id} not found",
        )
    if team.deleted_at is not None:
        team.deleted_at = None
        await session.commit()
        await session.refresh(team)
    return TeamOut.model_validate(team)
