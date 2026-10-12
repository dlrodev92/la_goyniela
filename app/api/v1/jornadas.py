"""Jornada endpoints — list, get single, get with all 15 partidos.

Read-only endpoints, public. The MVP-04 quiniela-mode endpoints
(``/open``, ``/optimize``, ``/cost``) and the consensus endpoint were
removed in MVP-05: the per-jornada ``quini_config`` JSONB is gone
(replaced by the per-``(group, jornada)`` :class:`Porra` entity) and
the per-jornada ``Pick`` rows are now scoped to a porra. See
``app/api/v1/porras.py`` for the new flow.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Composicion,
    Jornada,
    JornadaEscrutinio,
    TeamIdentityMap,
)
from app.db.session import get_session
from app.schemas.jornada import (
    ComposicionOut,
    JornadaDetailOut,
    JornadaOut,
    JornadaStatus,
    TeamSummary,
)
from app.schemas.losilla import PartidoEstimacion


read_router = APIRouter(prefix="/jornadas", tags=["jornadas"])


@read_router.get(
    "",
    response_model=list[JornadaOut],
    summary="List jornadas (filter by season and status)",
)
async def list_jornadas(
    season: int | None = Query(
        None,
        description="Filter by season end-year (e.g. 2027 = 26/27)",
    ),
    status: JornadaStatus | None = Query(
        None, description="Filter by status",
    ),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> list[JornadaOut]:
    stmt = select(Jornada)
    # Soft-delete filter (slice 1 of the superadmin-panel sprint): the
    # public list endpoint must never expose soft-deleted jornadas. The
    # admin-only DELETE endpoint is a separate path (slice 2).
    stmt = stmt.where(Jornada.deleted_at.is_(None))
    if season is not None:
        stmt = stmt.where(Jornada.temporada == season)
    if status is not None:
        stmt = stmt.where(Jornada.status == status.value)
    stmt = (
        stmt.order_by(Jornada.temporada.desc(), Jornada.numero.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [JornadaOut.model_validate(r) for r in rows]


@read_router.get(
    "/{jornada_id}",
    response_model=JornadaDetailOut,
    summary="Get one jornada with all 15 partidos",
)
async def get_jornada(
    jornada_id: int,
    session: AsyncSession = Depends(get_session),
) -> JornadaDetailOut:
    row = await session.get(Jornada, jornada_id)
    if row is None or row.deleted_at is not None:
        # Soft-delete filter (slice 1 of the superadmin-panel sprint):
        # a soft-deleted jornada is treated as not-found by the public
        # detail endpoint. The admin-only endpoint in slice 2 may surface
        # a different status.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="jornada not found",
        )
    partidos_rows = (
        await session.execute(
            select(Composicion)
            .where(Composicion.jornada_id == jornada_id)
            .order_by(Composicion.posicion)
        )
    ).scalars().all()

    # Embed TeamSummary so the client can render the boleto without
    # 30 follow-up calls to /api/v1/teams/{id}. One IN-query for all 30
    # teams (15 partidos × 2), not 30 separate queries.
    team_ids: set[int] = set()
    for p in partidos_rows:
        team_ids.add(p.local_team_id)
        team_ids.add(p.visitante_team_id)

    teams_by_id: dict[int, TeamIdentityMap] = {}
    if team_ids:
        teams = (
            await session.execute(
                select(TeamIdentityMap).where(TeamIdentityMap.id.in_(team_ids))
            )
        ).scalars().all()
        teams_by_id = {t.id: t for t in teams}

    # Build the response by hand instead of ``model_validate(p)`` — the
    # ``Composicion`` ORM model declares ``local_team`` / ``visitante_team``
    # relationships and Pydantic-from-attributes would trigger a lazy-load
    # when accessing those names, which can't be awaited inside an
    # attribute extraction. Passing the plain scalars explicitly avoids
    # the greenlet error.
    partidos_out: list[ComposicionOut] = []
    for p in partidos_rows:
        local_t = teams_by_id.get(p.local_team_id)
        visit_t = teams_by_id.get(p.visitante_team_id)
        comp_out = ComposicionOut(
            posicion=p.posicion,
            local_team_id=p.local_team_id,
            visitante_team_id=p.visitante_team_id,
            local_raw=p.local_raw,
            visitante_raw=p.visitante_raw,
            kickoff_at=p.kickoff_at,
            competicion_raw=p.competicion_raw,
            local_team=(
                TeamSummary.model_validate(local_t)
                if local_t is not None else None
            ),
            visitante_team=(
                TeamSummary.model_validate(visit_t)
                if visit_t is not None else None
            ),
        )
        partidos_out.append(comp_out)

    # Slice 8 (§7.2): embed ``estimaciones`` from the jornada's
    # ``jornada_escrutinio.payload.estimaciones`` JSONB. ONE SQL query
    # for the row; ``EscrutinioOficial`` validates the payload and
    # normalises ``null`` → ``[]`` (slice 7). When no escrutinio row
    # exists (draft + active + closed-without-escrutinio), return an
    # empty list rather than 404 — the design doc is explicit about this.
    estimaciones_out: list[PartidoEstimacion] = []
    escrutinio_row = (
        await session.execute(
            select(JornadaEscrutinio).where(
                JornadaEscrutinio.jornada_id == jornada_id,
            )
        )
    ).scalars().one_or_none()
    if escrutinio_row is not None:
        from app.schemas.losilla import EscrutinioOficial  # local — schema-only
        payload = escrutinio_row.payload or {}
        # ``EscrutinioOficial`` coerces ``estimaciones`` defaults to ``[]``
        # via ``field_validator``, so legacy payloads (with the key set
        # to ``null``) come back as an empty list rather than raising.
        escrutinio = EscrutinioOficial.model_validate(payload)
        estimaciones_out = list(escrutinio.estimaciones)

    return JornadaDetailOut(
        **JornadaOut.model_validate(row).model_dump(),
        partidos=partidos_out,
        estimaciones=estimaciones_out,
    )