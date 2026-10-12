"""Competition endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Competition
from app.db.session import get_session
from app.schemas.competition import CompetitionListOut, CompetitionOut

router = APIRouter(prefix="/api/v1/competitions", tags=["competitions"])


@router.get(
    "",
    response_model=CompetitionListOut,
    summary="List competitions",
    description=(
        "Returns all known competitions. Optional filters narrow by tier (1/2/3) "
        "and country (full name, e.g. 'Spain')."
    ),
)
async def list_competitions(
    tier: int | None = Query(None, ge=1, le=3, description="Domestic tier (1, 2, or 3)"),
    country: str | None = Query(None, description="Country name (e.g. 'Spain')"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> CompetitionListOut:
    stmt = select(Competition)
    if tier is not None:
        stmt = stmt.where(Competition.tier == tier)
    if country is not None:
        stmt = stmt.where(Competition.country == country)

    # total — reuse filtered stmt via subquery
    total = await session.scalar(
        select(func.count()).select_from(stmt.subquery())
    ) or 0

    # page — order by tier (nulls last) then name
    rows = (
        await session.execute(
            stmt.order_by(
                Competition.tier.nulls_last(),
                Competition.name,
            ).limit(limit).offset(offset)
        )
    ).scalars().all()

    return CompetitionListOut(
        items=[CompetitionOut.model_validate(r) for r in rows],
        total=int(total),
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{competition_id}",
    response_model=CompetitionOut,
    summary="Get one competition by ID",
    responses={404: {"description": "Competition not found"}},
)
async def get_competition(
    competition_id: str,
    session: AsyncSession = Depends(get_session),
) -> CompetitionOut:
    row = await session.scalar(
        select(Competition).where(Competition.id == competition_id)
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"competition_id={competition_id!r} not found",
        )
    return CompetitionOut.model_validate(row)