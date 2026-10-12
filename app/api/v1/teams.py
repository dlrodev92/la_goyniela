"""Team endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import TeamIdentityMap
from app.db.session import get_session
from app.schemas.team import TeamListOut, TeamOut

router = APIRouter(prefix="/api/v1/teams", tags=["teams"])


@router.get(
    "",
    response_model=TeamListOut,
    summary="List teams",
    description=(
        "Returns teams. Optional filters: by competition (e.g. 'laliga-ea-sports'), "
        "by gender ('men' or 'women'), by 3-letter country code (e.g. 'ESP'), "
        "by substring match on canonical_name (case-insensitive). "
        "Note: the substring match uses ILIKE, which is accent-sensitive in "
        "Postgres by default; 'Madrid' will not match 'Maldonado'. Full "
        "accent-insensitive search needs the unaccent extension (planned T7+)."
    ),
)
async def list_teams(
    competition_id: str | None = Query(
        None, description="Filter by competition_id (e.g. 'laliga-ea-sports')"
    ),
    gender: str | None = Query(
        None, description="Filter by gender ('men' or 'women')"
    ),
    country: str | None = Query(
        None, description="3-letter ISO country code (e.g. 'ESP')"
    ),
    q: str | None = Query(
        None,
        description=(
            "Substring match on canonical_name (ILIKE, case-insensitive, "
            "accent-sensitive)"
        ),
    ),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> TeamListOut:
    stmt = select(TeamIdentityMap)

    # Soft-delete filter (slice 1 of the superadmin-panel sprint): the
    # public list endpoint must never expose soft-deleted teams. The
    # admin-only DELETE endpoint is a separate path (slice 2).
    stmt = stmt.where(TeamIdentityMap.deleted_at.is_(None))

    if competition_id is not None:
        stmt = stmt.where(TeamIdentityMap.competition_id == competition_id)
    if gender is not None:
        stmt = stmt.where(TeamIdentityMap.gender == gender)
    if country is not None:
        stmt = stmt.where(TeamIdentityMap.country == country)
    if q is not None:
        pattern = f"%{q}%"
        stmt = stmt.where(TeamIdentityMap.canonical_name.ilike(pattern))

    total = await session.scalar(
        select(func.count()).select_from(stmt.subquery())
    ) or 0

    rows = (
        await session.execute(
            stmt.order_by(TeamIdentityMap.canonical_name).limit(limit).offset(offset)
        )
    ).scalars().all()

    return TeamListOut(
        items=[TeamOut.model_validate(r) for r in rows],
        total=int(total),
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{team_id}",
    response_model=TeamOut,
    summary="Get one team by numeric id",
    description=(
        "Returns one team by numeric primary key. Path parameter is parsed as int, "
        "so non-numeric slugs (e.g. entity_id strings) will return a 422 from "
        "FastAPI's path validation rather than being treated as entity_id lookups. "
        "An entity_id-based endpoint can be added under '/teams/by-entity/{entity_id}' "
        "in a later task."
    ),
    responses={404: {"description": "Team not found"}},
)
async def get_team(
    team_id: int,
    session: AsyncSession = Depends(get_session),
) -> TeamOut:
    row = await session.scalar(
        select(TeamIdentityMap).where(
            TeamIdentityMap.id == team_id,
            # Soft-delete filter (slice 1 of the superadmin-panel
            # sprint): a soft-deleted team is treated as not-found.
            TeamIdentityMap.deleted_at.is_(None),
        )
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"team_id={team_id} not found",
        )
    return TeamOut.model_validate(row)