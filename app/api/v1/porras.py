"""Porra endpoints (MVP-05 — replaces MVP-04 quiniela modes).

Routes under prefix ``/groups/{group_id}/porras`` and
``/groups/{group_id}/porras/{porra_id}``. The admin/member check is
applied at the route level via ``_group_helpers``.

The algoritmo de la media lives in ``app.services.propuesta`` (pure
functions, no DB). This module is the HTTP shell that loads the picks,
calls the algorithm, and serialises the result.

Boleto lifecycle: a porra has at most one persisted boleto (1:1).
``PUT /boleto`` overwrites the previous one; ``GET /boleto`` returns
404 if no boleto has been saved yet.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1._group_helpers import (
    get_admin_or_403,
    get_group_member_or_404,
)
from app.api.v1.auth import get_current_user
from app.db.models import (
    Composicion,
    Group,
    GroupMember,
    Jornada,
    JornadaEscrutinio,
    Pick,
    Porra,
    PorraModo,
    PorraStatus,
    TeamIdentityMap,
    User,
)
from app.db.session import get_session
from app.schemas.jornada import TeamSummary
from app.schemas.porra import (
    BoletoColumnaIn,
    BoletoIn,
    BoletoMatchIn,
    BoletoOut,
    IntuicionOptionOut,
    IntuicionOptionsOut,
    MemberPicksOut,
    PickOut,
    PicksOut,
    PicksSubmitIn,
    PlenoIn,
    PorraCreateIn,
    PorraDetailOut,
    PorraFormatOut,
    PorraOut,
    PorraPatchIn,
    PropuestaColumnaOut,
    PropuestaOut,
)
from app.services.propuesta import (
    PartidoVote,
    PlenoVote,
    column_cost,
    compute_propuesta_for_porra,
    feasible_formats,
    total_cost,
)
from app.services.reducciones import REDUCCIONES, get_reduccion
from app.services.boleto_pdf import render_boleto_pdf
from app.services.jornada_manager import JornadaManager


LOG = logging.getLogger("api.porras")
router = APIRouter(
    prefix="/groups/{group_id}/porras",
    tags=["porras"],
)


# ---------- Defaults / pricing ----------


# Default per-column price when Losilla has nothing live. Quiniela real
# charges 0.75€ in 2026/27 — the spec asks us to fall back to 0.50
# only if the live price is unknown.
DEFAULT_PRECIO_APUESTA_EUR = Decimal("0.50")


# Budget-advisor table for ``modo = "intuicion"``. (columnas, fijos) sorted
# by columnas asc. Each row costs ``columnas * 0.75€``. The advisor returns
# rows that fit the admin's budget.
#
# Rules baked in (slice-1 spec):
# - Min 2 columns at 1.50€  (the cheapest Quiniela boleto).
# - Max 12 columns at 9.00€  (12 × 0.75€).
# - ``fijos`` decreases as ``columnas`` grows so there's always at least
#   one variable: ``fijos < columnas``.
# - The full table covers 11 rows from (2, 13) up to (12, 7). The advisor
#   returns the rows whose cost ≤ the admin's budget (the 2-col row only
#   appears for budgets ≥ €1.50; the 12-col row only for budgets ≥ €9.00).
INTUICION_OPTIONS: list[tuple[int, int]] = [
    (2, 13),  (3, 12),  (4, 11),  (5, 11),  (6, 10),
    (7, 10),  (8, 9),   (9, 9),   (10, 8),  (11, 8),
    (12, 7),
]


def _porra_columns_and_cost(porra: Porra) -> tuple[int, Decimal]:
    """Compute ``(columns, cost_eur)`` for a Porra row across all modes.

    Switches on ``modo`` so the read shape stays consistent no matter
    which mode the porra was created in.

    - ``intuicion`` / ``sencilla`` — ``columns = porra.columnas`` (the
      admin's explicit count). ``cost = columnas × precio``.
    - ``multiple`` — ``columns = 2^D × 3^T`` via ``column_cost``.
    - ``reducida`` — ``columns = spec.apuestas`` (the official reduction
      count, NOT 2^D × 3^T; the reduction guarantees fewer apuestas
      than the full expansion). Falls back to ``column_cost(D, T)`` if
      the spec can't be resolved (defensive — should never happen at
      runtime since ``ck_porra_reduccion_only_reducida`` ensures
      ``reduccion`` is set when ``modo='reducida'``).
    """
    if porra.modo in (PorraModo.intuicion, PorraModo.sencilla):
        cols = porra.columnas if porra.columnas is not None else 0
        return cols, Decimal(cols) * porra.precio_apuesta_eur
    if porra.modo == PorraModo.reducida:
        try:
            spec = get_reduccion(porra.reduccion or "")
        except ValueError:
            return column_cost(porra.dobles, porra.triples), total_cost(
                porra.dobles, porra.triples, porra.precio_apuesta_eur,
            )
        return spec.apuestas, Decimal(spec.apuestas) * porra.precio_apuesta_eur
    # multiple
    cols = column_cost(porra.dobles, porra.triples)
    return cols, total_cost(porra.dobles, porra.triples, porra.precio_apuesta_eur)


async def _resolve_precio_apuesta_eur(
    jornada_id: int, session: AsyncSession,
) -> Decimal:
    """Read ``precio_apuesta_eur`` from the persisted Losilla escrutinio.

    The price lives inside ``payload.recaudacion_bote_live.coste_apuesta_eur``
    (the Losilla live SSR block). The ingest (oct 2026) now persists
    that block alongside the escrutinio, so a re-ingest via the
    superadmin panel populates it for previously-orphaned jornadas.
    Pre-oct-2026 rows have no live block in the JSONB — they fall back
    to ``DEFAULT_PRECIO_APUESTA_EUR`` (0.50€) and the client must
    either re-ingest or pass an explicit ``precio_apuesta_eur`` on
    ``POST /porras``.

    Falls back to ``DEFAULT_PRECIO_APUESTA_EUR`` when:
    - No escrutinio row exists for the jornada.
    - The JSONB doesn't carry ``recaudacion_bote_live``.
    - ``coste_apuesta_eur`` is missing or not a valid Decimal.

    The live Losilla client is NOT called here — the porra is always
    priced from what we have persisted (the live call would couple the
    POST /porras latency to upstream). The live path is only used by
    the dedicated ``/api/v1/losilla/...`` endpoints.
    """
    row = await session.scalar(
        select(JornadaEscrutinio).where(JornadaEscrutinio.jornada_id == jornada_id),
    )
    if row is None:
        return DEFAULT_PRECIO_APUESTA_EUR
    payload = row.payload
    if not isinstance(payload, dict):
        return DEFAULT_PRECIO_APUESTA_EUR
    live_block = payload.get("recaudacion_bote_live") or {}
    raw = live_block.get("coste_apuesta_eur")
    if raw is None:
        return DEFAULT_PRECIO_APUESTA_EUR
    try:
        return Decimal(str(raw))
    except (TypeError, ValueError):
        LOG.warning(
            "jornada_escrutinio.payload.recaudacion_bote_live.coste_apuesta_eur "
            "for jornada_id=%s is not a valid Decimal: %r", jornada_id, raw,
        )
        return DEFAULT_PRECIO_APUESTA_EUR


# ---------- DB helpers ----------


async def _load_porra_or_404(
    group_id: int, porra_id: int, session: AsyncSession,
) -> Porra:
    """Return the Porra row or raise 404. Distinguishes "no such porra"
    (404) from "wrong group" (404 too — the porra_id is opaque without
    the group context)."""
    porra = await session.get(Porra, porra_id)
    if porra is None or porra.group_id != group_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="porra not found",
        )
    return porra


async def _load_jornada_or_404(jornada_id: int, session: AsyncSession) -> Jornada:
    row = await session.get(Jornada, jornada_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="jornada not found",
        )
    return row


async def _load_partido_context(
    jornada_id: int, session: AsyncSession,
) -> tuple[dict[int, Composicion], dict[int, TeamIdentityMap]]:
    """Fetch composicion + teams for one jornada to enrich pick outputs."""
    rows = (
        await session.execute(
            select(Composicion).where(Composicion.jornada_id == jornada_id)
        )
    ).scalars().all()
    composicion_by_orden = {r.posicion: r for r in rows}
    team_ids: set[int] = set()
    for r in rows:
        team_ids.add(r.local_team_id)
        team_ids.add(r.visitante_team_id)
    teams_by_id: dict[int, TeamIdentityMap] = {}
    if team_ids:
        teams = (
            await session.execute(
                select(TeamIdentityMap).where(TeamIdentityMap.id.in_(team_ids))
            )
        ).scalars().all()
        teams_by_id = {t.id: t for t in teams}
    return composicion_by_orden, teams_by_id


def _build_pick_out(
    pick: Pick,
    composicion_by_orden: dict[int, Composicion],
    teams_by_id: dict[int, TeamIdentityMap],
) -> PickOut:
    """Materialise a ``PickOut`` for one ``Pick`` row.

    Falls back to ``-1`` for the team IDs and ``None`` for the embedded
    summaries when the jornada is missing its composicion rows (the FK
    guarantees the rows exist, but defensive guards keep the endpoint
    from 500ing on a malformed jornada).
    """
    comp = composicion_by_orden.get(pick.partido_orden)
    if comp is None:
        return PickOut(
            partido_orden=pick.partido_orden,
            signs=pick.signs,
            pleno_local=pick.pleno_local,
            pleno_visitante=pick.pleno_visitante,
            local_team_id=-1,
            visitante_team_id=-1,
            local_team=None,
            visitante_team=None,
        )
    local_t = teams_by_id.get(comp.local_team_id)
    visit_t = teams_by_id.get(comp.visitante_team_id)
    return PickOut(
        partido_orden=pick.partido_orden,
        signs=pick.signs,
        pleno_local=pick.pleno_local,
        pleno_visitante=pick.pleno_visitante,
        local_team_id=comp.local_team_id,
        visitante_team_id=comp.visitante_team_id,
        local_team=TeamSummary.model_validate(local_t) if local_t is not None else None,
        visitante_team=(
            TeamSummary.model_validate(visit_t) if visit_t is not None else None
        ),
    )


def _porra_to_out(porra: Porra) -> PorraOut:
    """Build a ``PorraOut`` from a Porra row. Computes columns + cost.

    Switches on ``modo`` (slice 1 of slice 1 of the intuicion sprint):

    - ``multiple`` / ``reducida`` — ``columns = 2^D × 3^T`` (the legacy
      math via ``column_cost``).
    - ``sencilla`` / ``intuicion`` — ``columns = porra.columnas`` (the
      admin's explicit count).

    The model CHECK constraint guarantees ``D + T ≤ 14`` and
    ``precio_apuesta_eur > 0``, so the math never blows up.
    ``ck_porra_intuicion_fields`` guarantees ``porra.columnas`` is set
    when ``modo='intuicion'``.
    """
    cols, cost = _porra_columns_and_cost(porra)
    return PorraOut(
        id=porra.id,
        group_id=porra.group_id,
        jornada_id=porra.jornada_id,
        created_by_user_id=porra.created_by_user_id,
        budget_eur=porra.budget_eur,
        dobles=porra.dobles,
        triples=porra.triples,
        precio_apuesta_eur=porra.precio_apuesta_eur,
        modo=porra.modo,
        fijos=porra.fijos,
        columnas=porra.columnas,
        status=porra.status.value,
        created_at=porra.created_at,
        updated_at=porra.updated_at,
        columns=cols,
        cost_eur=cost,
        has_boleto=porra.boleto_matches is not None,
    )


async def _participation(
    group_id: int, porra_id: int, session: AsyncSession,
) -> dict:
    """Build the participation block: voted / total_members / missing_voters."""
    # Total members of the peña.
    total = await session.scalar(
        select(func.count()).select_from(GroupMember).where(GroupMember.group_id == group_id),
    ) or 0
    # Distinct voters for this porra.
    voted_rows = (await session.execute(
        select(Pick.user_id).where(Pick.porra_id == porra_id).distinct()
    )).all()
    voted_user_ids = {r[0] for r in voted_rows}
    voted = len(voted_user_ids)

    # Missing voters = members without any pick for this porra.
    missing_rows = (await session.execute(
        select(GroupMember.user_id, User.display_name)
        .join(User, User.id == GroupMember.user_id)
        .where(
            GroupMember.group_id == group_id,
            GroupMember.user_id.notin_(voted_user_ids) if voted_user_ids else True,
        )
        .order_by(GroupMember.joined_at)
    )).all()
    missing = [
        {"user_id": uid, "display_name": name}
        for uid, name in missing_rows
    ]

    return {
        "voted": voted,
        "total_members": total,
        "missing_voters": missing,
    }


# ---------- Endpoints ----------


@router.get(
    "",
    response_model=list[PorraOut],
    summary="List porras for this group (filter by jornada).",
    responses={403: {"description": "Not a member of the group"}},
)
async def list_porras(
    group_id: int,
    jornada_id: int | None = Query(
        None, ge=1,
        description="Optional filter: only porras for this jornada",
    ),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[PorraOut]:
    await get_group_member_or_404(group_id, current_user.id, session)

    stmt = select(Porra).where(Porra.group_id == group_id)
    if jornada_id is not None:
        stmt = stmt.where(Porra.jornada_id == jornada_id)
    stmt = stmt.order_by(Porra.created_at.desc())
    rows = (await session.execute(stmt)).scalars().all()
    return [_porra_to_out(p) for p in rows]


@router.get(
    "/porra-formats",
    response_model=list[PorraFormatOut],
    summary="[admin] Feasible (D, T) pairs at the given budget + price.",
    responses={403: {"description": "Not admin"}},
)
async def list_porra_formats(
    group_id: int,
    jornada_id: int = Query(..., ge=1),
    budget_eur: Decimal = Query(..., gt=0),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[PorraFormatOut]:
    """Return all feasible formats (D doubles, T triples) for the given
    budget + the jornada's price (from Losilla when available).

    Ordered columns DESC so the admin sees "best coverage first" and
    can pick the format that maximises the column count for the budget.
    """
    await get_admin_or_403(group_id, current_user, session)
    # Validate the jornada exists — without this check we'd 200 with an
    # empty list for any typo'd jornada_id, which is confusing.
    await _load_jornada_or_404(jornada_id, session)

    precio = await _resolve_precio_apuesta_eur(jornada_id, session)
    formats = feasible_formats(
        budget_eur=budget_eur, precio_apuesta_eur=precio,
    )
    return [PorraFormatOut(**f) for f in formats]


@router.get(
    "/intuicion-options",
    response_model=IntuicionOptionsOut,
    summary="[any member] Get the (columnas, fijos) options for a budget.",
    description=(
        "Given a budget, returns the list of valid (columnas, fijos) "
        "pairs for the ``intuicion`` mode. The admin picks one when "
        "creating the porra. Sorted by columnas desc (more coverage first). "
        "Sensible defaults: 2 columns at €1.50 up to 12 columns at €9.00. "
        "Open to any group member (no admin role needed) so the form can "
        "preload before the user logs in as admin."
    ),
)
async def list_intuicion_options(
    group_id: int,
    budget_eur: Decimal = Query(..., gt=0, le=10000, decimal_places=2),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> IntuicionOptionsOut:
    """Budget-advisor for ``modo = 'intuicion'``.

    Returns every (columnas, fijos) pair from :data:`INTUICION_OPTIONS`
    whose cost fits the admin's budget. The pair is the same shape the
    admin POSTs on create (minus the variable count, which the client
    computes for the UI as ``columnas - fijos``).

    The static table is the slice-1 spec — 11 rows from 2/13 up to
    12/7, all priced at ``columnas × 0.75€``. Future slices will
    generalise it (the rate depends on the jornada's
    ``precio_apuesta_eur`` from Losilla).
    """
    await get_group_member_or_404(group_id, current_user.id, session)
    options: list[IntuicionOptionOut] = []
    for cols, fijos in INTUICION_OPTIONS:
        cost = Decimal(cols) * Decimal("0.75")
        if cost <= budget_eur:
            options.append(IntuicionOptionOut(
                columnas=cols,
                fijos=fijos,
                variables=14 - fijos,
                cost_eur=cost,
            ))
    # Sort by columnas desc (more coverage first), then fijos asc to
    # stabilise the order — the static table is already sorted, but
    # keeping the sort explicit protects against future edits.
    options.sort(key=lambda o: (-o.columnas, o.fijos))
    return IntuicionOptionsOut(
        budget_eur=budget_eur,
        options=options,
    )


@router.post(
    "",
    response_model=PorraDetailOut,
    status_code=status.HTTP_201_CREATED,
    summary="[admin] Create a new porra for this group+jornada.",
    responses={
        403: {"description": "Not admin"},
        404: {"description": "Jornada not found"},
        409: {
            "description": (
                "Porra already exists for this (group, jornada), or the "
                "jornada is outside the betting window (effective_phase "
                "≠ in_window). Body has structured detail with code + "
                "window timestamps + effective_phase."
            ),
        },
        422: {"description": "D + T > 14, or format doesn't fit the budget"},
    },
)
async def create_porra(
    group_id: int,
    body: PorraCreateIn,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> PorraDetailOut:
    await get_admin_or_403(group_id, current_user, session)
    jornada = await _load_jornada_or_404(body.jornada_id, session)
    # Reject create outside the voting window (draft / pre-sale / past
    # close / collected). The porra is the peña's betting container — it
    # only makes sense to create it while voting is open.
    await JornadaManager().assert_jornada_accepts(
        session, jornada, action="bets",
    )

    # 409 if a porra already exists for this (group, jornada). The UNIQUE
    # constraint catches it too, but checking here lets us return a
    # 409 with a useful message instead of an opaque IntegrityError.
    existing = await session.scalar(
        select(Porra).where(
            Porra.group_id == group_id,
            Porra.jornada_id == body.jornada_id,
        ),
    )
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"porra already exists for group_id={group_id} "
                f"jornada_id={body.jornada_id} (porra_id={existing.id})"
            ),
        )

    precio = (
        body.precio_apuesta_eur
        if body.precio_apuesta_eur is not None
        else await _resolve_precio_apuesta_eur(body.jornada_id, session)
    )

    # Format fits the budget? Mode-aware: ``multiple``/``reducida`` use
    # the legacy 2^D × 3^T math (or the spec's ``apuestas`` for
    # ``reducida``); ``intuicion``/``sencilla`` use the admin's explicit
    # ``columnas`` (already enforced at the schema validator layer).
    # For ``multiple`` the cost check still guards against budget overruns.
    if body.modo == PorraModo.intuicion:
        # Schema validator already enforced ``columnas × precio ≤ budget``
        # for ``intuicion`` (when ``precio_apuesta_eur`` was set on the
        # body) and field-level bounds on ``fijos``/``columnas``.
        cost = Decimal(body.columnas or 0) * precio
    elif body.modo == PorraModo.sencilla:
        # Same as intuicion — schema validator enforced the budget
        # check (columnas × precio ≤ budget) when ``precio_apuesta_eur``
        # was on the body. When it was None, the validator fell back
        # to 0.50€ for the cost math; we recompute here with the
        # actual resolved ``precio`` so the response is consistent.
        cost = Decimal(body.columnas or 0) * precio
    elif body.modo == PorraModo.reducida:
        # Reduccion's spec sets the exact (D, T, apuestas) — we stamp
        # the spec onto the porra (the client may send 0/0; we
        # overwrite). Cost = spec.apuestas × precio.
        spec = REDUCCIONES[body.reduccion or ""]  # schema validated this
        cost = Decimal(spec.apuestas) * precio
    else:
        cost = total_cost(body.dobles, body.triples, precio)
        if cost > body.budget_eur:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"format (D={body.dobles}, T={body.triples}) costs "
                    f"{cost}€ (columns={column_cost(body.dobles, body.triples)} "
                f"× {precio}€), which exceeds the budget {body.budget_eur}€"
                ),
            )

    # For ``reducida``, overwrite the doubles/triples with the spec's
    # values (the client may have sent 0/0; the spec is the truth).
    if body.modo == PorraModo.reducida:
        spec = REDUCCIONES[body.reduccion or ""]
        efectivos_dobles = spec.dobles
        efectivos_triples = spec.triples
    else:
        efectivos_dobles = body.dobles
        efectivos_triples = body.triples

    # Multi-column default (oct 2026): when the admin doesn't supply
    # ``columnas`` for a ``sencilla`` / ``intuicion`` porra and the
    # jornada has a ``default_columnas`` configured, apply it. The
    # admin can still override per-porra by sending ``columnas`` in
    # the POST body. If BOTH are missing, 422 — the DB CHECK requires
    # ``porra.columnas`` for those modes.
    columnas_value: int | None = (
        body.columnas
        if body.modo in (PorraModo.intuicion, PorraModo.sencilla)
        else None
    )
    if (
        columnas_value is None
        and body.modo in (PorraModo.sencilla, PorraModo.intuicion)
        and jornada.default_columnas is not None
    ):
        columnas_value = jornada.default_columnas
    if (
        columnas_value is None
        and body.modo in (PorraModo.sencilla, PorraModo.intuicion)
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"modo='{body.modo.value}' requires 'columnas'. Either "
                f"include it in the POST body or set "
                f"jornada.default_columnas via "
                f"PATCH /admin/jornadas/{{id}}/default-columnas."
            ),
        )

    porra = Porra(
        group_id=group_id,
        jornada_id=body.jornada_id,
        created_by_user_id=current_user.id,
        budget_eur=body.budget_eur,
        dobles=efectivos_dobles,
        triples=efectivos_triples,
        modo=body.modo,
        fijos=body.fijos if body.modo == PorraModo.intuicion else None,
        columnas=columnas_value,
        reduccion=(
            body.reduccion if body.modo == PorraModo.reducida else None
        ),
        precio_apuesta_eur=precio,
        status=PorraStatus.open,
    )
    session.add(porra)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        # Defense in depth: the CHECK constraints (ck_porra_min_columns,
        # ck_porra_intuicion_fields, ck_porra_reduccion_only_reducida, etc.)
        # are the source of truth, but if any of them fire we want a 422
        # with a useful message — not an opaque 500.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"porra rejected by a database check constraint: {exc.orig}. "
                "Verify the porra fields match the modo you picked "
                "(e.g. intuicion requires fijos and columnas; reducida "
                "requires a valid reduccion code)."
            ),
        )
    await session.refresh(porra)

    formats = feasible_formats(
        budget_eur=body.budget_eur, precio_apuesta_eur=precio,
    )
    return PorraDetailOut(
        **_porra_to_out(porra).model_dump(),
        formats_preview=[PorraFormatOut(**f) for f in formats],
        participation=await _participation(group_id, porra.id, session),
    )


@router.get(
    "/{porra_id}",
    response_model=PorraDetailOut,
    summary="Get one porra with formats preview + participation.",
    responses={
        403: {"description": "Not a member of the group"},
        404: {"description": "Porra not found"},
    },
)
async def get_porra(
    group_id: int,
    porra_id: int,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> PorraDetailOut:
    await get_group_member_or_404(group_id, current_user.id, session)
    porra = await _load_porra_or_404(group_id, porra_id, session)

    formats = feasible_formats(
        budget_eur=porra.budget_eur, precio_apuesta_eur=porra.precio_apuesta_eur,
    )
    return PorraDetailOut(
        **_porra_to_out(porra).model_dump(),
        formats_preview=[PorraFormatOut(**f) for f in formats],
        participation=await _participation(group_id, porra.id, session),
    )


@router.patch(
    "/{porra_id}",
    response_model=PorraDetailOut,
    summary="[admin] Patch budget / format while open.",
    responses={
        403: {"description": "Not admin"},
        404: {"description": "Porra not found"},
        409: {
            "description": (
                "Porra closed, or jornada outside the editable window "
                "(effective_phase ∉ {in_window, lista_abre_pronto}). "
                "Body has structured detail with code + window "
                "timestamps + effective_phase."
            ),
        },
        422: {"description": "D + T > 14, or new format doesn't fit the budget"},
    },
)
async def patch_porra(
    group_id: int,
    porra_id: int,
    body: PorraPatchIn,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> PorraDetailOut:
    await get_admin_or_403(group_id, current_user, session)
    porra = await _load_porra_or_404(group_id, porra_id, session)

    if porra.status == PorraStatus.closed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="porra is closed — cannot patch",
        )

    jornada = await _load_jornada_or_404(porra.jornada_id, session)
    # Reject patches outside the editable window. The porra-level
    # ``closed`` guard above is independent of the jornada's phase
    # (admin can close the porra before voting closes); both are kept.
    await JornadaManager().assert_jornada_accepts(
        session, jornada, action="edits",
    )

    # Apply patches (None = leave unchanged). Order matters: ``modo``
    # first, then the mode-specific fields, so the post-patch validation
    # below sees a consistent state.
    if body.budget_eur is not None:
        porra.budget_eur = body.budget_eur
    if body.modo is not None:
        porra.modo = body.modo
        # When the admin changes the modo, clear the fields that no
        # longer apply. Otherwise a flip intuicion→multiple leaves
        # stale ``fijos=10, columnas=6`` on the row, and a flip
        # multiple→intuicion leaves a stale ``reduccion='R4'``. The
        # mode-specific fields are re-set from the body (or validated
        # as required) in the per-mode blocks below.
        if porra.modo == PorraModo.multiple:
            porra.fijos = None
            porra.columnas = None
            porra.reduccion = None
        elif porra.modo == PorraModo.reducida:
            porra.fijos = None
            porra.columnas = None
            porra.dobles = 0
            porra.triples = 0
        elif porra.modo == PorraModo.sencilla:
            porra.fijos = None
            porra.reduccion = None
            porra.dobles = 0
            porra.triples = 0
        elif porra.modo == PorraModo.intuicion:
            porra.reduccion = None
            porra.dobles = 0
            porra.triples = 0
    if body.dobles is not None:
        porra.dobles = body.dobles
    if body.triples is not None:
        porra.triples = body.triples
    if body.fijos is not None:
        porra.fijos = body.fijos
    if body.columnas is not None:
        porra.columnas = body.columnas
    if body.reduccion is not None:
        porra.reduccion = body.reduccion

    # Mode-aware post-patch re-check. The schema validator can't catch
    # the case where ONE of the fields is being left as-is (None) — only
    # the post-patch state is known here.
    if porra.modo == PorraModo.intuicion:
        # Columnas/fijos are required for intuicion. If the admin left
        # either unset while patching, reject. (We don't enforce
        # ``fijos < columnas`` here either — see PorraCreateIn.)
        if porra.columnas is None or porra.fijos is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "modo='intuicion' requires 'columnas' and 'fijos'; "
                    "include both in the patch."
                ),
            )
        # Cost check against the post-patch budget.
        new_cost = Decimal(porra.columnas) * porra.precio_apuesta_eur
        if new_cost > porra.budget_eur:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"new intuicion cost {new_cost}€ (columnas="
                    f"{porra.columnas} × {porra.precio_apuesta_eur}€) "
                    f"exceeds the budget {porra.budget_eur}€"
                ),
            )
    elif porra.modo == PorraModo.sencilla:
        # Sencilla: ``columnas`` is required; ``dobles``/``triples`` are
        # 0 (no doubles/triples — every match is a simple). Clear
        # ``fijos`` since intuicion's field is not used here.
        if porra.columnas is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "modo='sencilla' requires 'columnas'; include it in the patch."
                ),
            )
        porra.dobles = 0
        porra.triples = 0
        porra.fijos = None
        new_cost = Decimal(porra.columnas) * porra.precio_apuesta_eur
        if new_cost > porra.budget_eur:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"new sencilla cost {new_cost}€ (columnas="
                    f"{porra.columnas} × {porra.precio_apuesta_eur}€) "
                    f"exceeds the budget {porra.budget_eur}€"
                ),
            )
    elif porra.modo == PorraModo.reducida:
        # Reducida: the spec sets the exact (D, T, apuestas). We stamp
        # the spec values onto the porra so all downstream code
        # (BoletoOut columns + cost, PDF, etc.) reads a consistent
        # shape. ``reduccion`` is required — if the admin is flipping
        # to ``reducida`` without sending one, reject.
        if porra.reduccion is None or porra.reduccion not in REDUCCIONES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "modo='reducida' requires 'reduccion' (one of "
                    f"{sorted(REDUCCIONES.keys())}); include it in the patch."
                ),
            )
        spec = REDUCCIONES[porra.reduccion]
        porra.dobles = spec.dobles
        porra.triples = spec.triples
        porra.fijos = None
        porra.columnas = None
        new_cost = Decimal(spec.apuestas) * porra.precio_apuesta_eur
        if new_cost > porra.budget_eur:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"new reducida {porra.reduccion} cost {new_cost}€ "
                    f"(apuestas={spec.apuestas} × {porra.precio_apuesta_eur}€) "
                    f"exceeds the budget {porra.budget_eur}€"
                ),
            )
    else:
        # multiple — the legacy 2^D × 3^T math. Minimum 2 columns.
        # Also clear mode-specific fields that don't apply to multiple.
        porra.fijos = None
        porra.columnas = None
        porra.reduccion = None
        # If the admin flipped TO multiple without providing D/T, the
        # post-state has D=T=0. That's invalid for multiple (need at
        # least 1 doble or 1 triple). Reject with an actionable message.
        if porra.dobles == 0 and porra.triples == 0:
            if body.modo == PorraModo.multiple:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=(
                        "modo='multiple' requires at least 1 doble or 1 "
                        "triple. Include 'dobles' and/or 'triples' in "
                        "the patch body."
                    ),
                )
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "minimum format is 2 columns (1 doble OR 1 triple). "
                    "Quiniela does not sell 1-column boletos."
                ),
            )

        # If a boleto is persisted, its D/T must still match the new format
        # (the saved columns must continue to match the format).
        if porra.boleto_matches is not None:
            n_dobles = sum(1 for m in porra.boleto_matches if len(m["signs"]) == 2)
            n_triples = sum(1 for m in porra.boleto_matches if len(m["signs"]) == 3)
            if n_dobles != porra.dobles or n_triples != porra.triples:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=(
                        f"new format (D={porra.dobles}, T={porra.triples}) does not "
                        f"match the persisted boleto (D={n_dobles}, T={n_triples}). "
                        f"Delete the boleto first (or POST /propuesta + PUT /boleto)."
                    ),
                )

        # New format must still fit the budget.
        new_cost = total_cost(porra.dobles, porra.triples, porra.precio_apuesta_eur)
        if new_cost > porra.budget_eur:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"new format costs {new_cost}€ which exceeds the budget "
                    f"{porra.budget_eur}€"
                ),
            )

    porra.updated_at = datetime.now(tz=timezone.utc)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"porra rejected by a database check constraint: {exc.orig}. "
                "Verify the porra fields match the modo (e.g. intuicion "
                "requires fijos and columnas; reducida requires a valid "
                "reduccion code)."
            ),
        )
    await session.refresh(porra)

    formats = feasible_formats(
        budget_eur=porra.budget_eur, precio_apuesta_eur=porra.precio_apuesta_eur,
    )
    return PorraDetailOut(
        **_porra_to_out(porra).model_dump(),
        formats_preview=[PorraFormatOut(**f) for f in formats],
        participation=await _participation(group_id, porra.id, session),
    )


# ---------- Picks endpoints ----------


@router.post(
    "/{porra_id}/picks",
    response_model=PicksOut,
    status_code=status.HTTP_200_OK,
    summary="Submit / replace the current user's picks for this porra.",
    responses={
        403: {"description": "Not a member of the group, or porra closed"},
        404: {"description": "Porra or jornada not found"},
        409: {
            "description": (
                "Jornada outside the voting window (effective_phase "
                "≠ in_window). Body has structured detail with code + "
                "window timestamps + effective_phase."
            ),
        },
        422: {
            "description": (
                "Picks shape invalid (wrong signs, dupes, missing pleno, "
                "wrong column count for the porra's modo, fijo partido "
                "sign differs across columns in intuicion, D/T count "
                "mismatch in multiple/reducida, ...)"
            ),
        },
    },
)
async def submit_picks(
    group_id: int,
    porra_id: int,
    body: PicksSubmitIn,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> PicksOut:
    """Submit / replace the current user's picks for one porra.

    **Multi-column shape (oct 2026 sprint)**: members submit N columns
    per porra (1 column for ``multiple`` / ``reducida``, ``porra.columnas``
    columns for ``sencilla`` / ``intuicion``). Each column carries 14
    rows for partidos 1..14 + one shared pleno al 15 (the Pleno is NOT
    per-column — the same ``pleno.local`` / ``pleno.visitante`` pair is
    stored once per (user, porra, columna_idx)).

    Idempotent: we DELETE all of the user's existing rows for this
    porra, then INSERT ``N × 15`` rows in one transaction. No partial
    writes can leak. Legacy single-column clients can keep sending
    ``matches: [14]`` (wrapped into ``columnas: [{matches: [14]}]``
    internally).

    Picks are FREE on each partido: a member can vote 1, 2, or 3 signs
    regardless of the porra's format (D, T). The format only governs
    the FINAL BOLETO the admin saves.

    Two layers of guards:

    1. The porra-level ``status == closed`` guard (admin can close the
       porra via PATCH; members can't vote on a closed porra regardless
       of the jornada state).
    2. The jornada-level ``assert_jornada_accepts(... action="bets")``
       helper, which enforces "voting only while in_window" via the
       effective phase (``in_window`` ↔ ``status=active`` AND
       ``sale_opens_at <= now < voting_closes_at``).

    Per-mode structural validation (delegated to the endpoint because the
    schema only sees columns, not mode):

    - ``multiple`` — exactly 1 column. Picks are FREE per row (a member
      can vote 1, 2, or 3 signs on any partido regardless of the
      porra's ``dobles``/``triples``). The format governs the FINAL
      BOLETO the admin saves (PUT /boleto), NOT the votes.
    - ``reducida`` — exactly 1 column. Same as ``multiple``: picks are
      free-form; the reduction's D/T spec is enforced in PUT /boleto.
    - ``sencilla`` — exactly ``porra.columnas`` columns; every match
      in every column has exactly 1 sign (Pydantic-side ``BoletoMatchIn``
      validator covers length 1..3, the endpoint enforces ==1).
    - ``intuicion`` — exactly ``porra.columnas`` columns; the first
      ``porra.fijos`` parties share the same sign across ALL columns;
      every match in every column has exactly 1 sign.
    """
    await get_group_member_or_404(group_id, current_user.id, session)
    porra = await _load_porra_or_404(group_id, porra_id, session)

    if porra.status == PorraStatus.closed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="porra is closed",
        )

    jornada = await _load_jornada_or_404(porra.jornada_id, session)
    # Replaces the previous two inline guards (voting_closes_at +
    # jornada.status.value == "active"). The helper raises 409 with the
    # structured body when the effective phase is anything other than
    # ``in_window``. The porra-level closed check above is independent
    # and is intentionally NOT subsumed — admin may close a porra while
    # the jornada is still ``in_window``.
    await JornadaManager().assert_jornada_accepts(
        session, jornada, action="bets",
    )

    # Coalesce ``matches`` (legacy single-column alias) into
    # ``columnas`` (the canonical N-column shape). The Pydantic
    # ``_coalesce`` validator already rejected "neither provided" and
    # "both provided" — at this point exactly one of the two is set.
    if body.columnas is not None:
        columnas_in: list[BoletoColumnaIn] = list(body.columnas)
    elif body.matches is not None:
        columnas_in = [BoletoColumnaIn(matches=body.matches)]
    else:
        # Defensive — should be unreachable because of the validator.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "must provide 'columnas' or 'matches' (legacy alias)",
            ),
        )

    # Mode-specific expected column count.
    if porra.modo in (PorraModo.multiple, PorraModo.reducida):
        expected_cols = 1
    elif porra.modo in (PorraModo.sencilla, PorraModo.intuicion):
        # ``columnas`` is enforced by Pydantic + the DB CHECK for these
        # modes — the create endpoint won't let a porra reach here
        # without it set. Defensive fallback to 1 keeps us from
        # crashing if the porra row was corrupted.
        expected_cols = porra.columnas if porra.columnas is not None else 1
    else:
        expected_cols = 1
    if len(columnas_in) != expected_cols:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"modo='{porra.modo.value}' requires exactly "
                f"{expected_cols} column(s), got {len(columnas_in)}"
            ),
        )

    # Per-mode structural validation. ``sencilla`` enforces 1 sign per
    # match (in every column); ``intuicion`` enforces that too AND
    # checks the fijos share the same sign across all columns.
    # ``multiple`` / ``reducida`` enforce the D/T counts.
    if porra.modo == PorraModo.intuicion:
        fijos = porra.fijos
        if fijos is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "intuicion mode requires 'fijos' on the porra "
                    "(should be set at create time)"
                ),
            )
        # Fijos: for each fijo party, the sign is the same across ALL
        # columns (column 0 is the canonical definition).
        fijo_parties = [
            m for m in columnas_in[0].matches if m.partido_orden <= fijos
        ]
        for fijo in fijo_parties:
            signs_in_cols = [
                col.matches[fijo.partido_orden - 1].signs[0]
                for col in columnas_in
            ]
            if len(set(signs_in_cols)) != 1:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=(
                        f"intuicion: fijo partido {fijo.partido_orden} "
                        f"must have the same sign in all columns, got "
                        f"{signs_in_cols!r}"
                    ),
                )
        # Every match in every column must be a simple (1 sign).
        for col_idx, col in enumerate(columnas_in, start=1):
            for m in col.matches:
                if len(m.signs) != 1:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=(
                            f"intuicion: every match must have exactly 1 "
                            f"sign, got {len(m.signs)} at partido "
                            f"{m.partido_orden} (column {col_idx})"
                        ),
                    )
    elif porra.modo == PorraModo.sencilla:
        # Every match must be a simple (1 sign).
        for col_idx, col in enumerate(columnas_in, start=1):
            for m in col.matches:
                if len(m.signs) != 1:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=(
                            f"sencilla: every match must have exactly 1 "
                            f"sign, got {len(m.signs)} at partido "
                            f"{m.partido_orden} (column {col_idx})"
                        ),
                    )
    elif porra.modo == PorraModo.multiple:
        # Picks are FREE on each partido — a member can vote 1, 2, or 3
        # signs regardless of the porra's D/T. The format governs the
        # FINAL BOLETO the admin saves (PUT /boleto), NOT the votes. See
        # the ``Pick`` model docstring + the legacy pick endpoint which
        # never enforced D/T here. The D/T check DOES fire in
        # ``put_boleto`` — that's the right place for it.
        pass
    elif porra.modo == PorraModo.reducida:
        # Same as multiple: picks are free-form. The reduction's D/T is
        # enforced when the admin saves the FINAL BOLETO (PUT /boleto).
        pass

    # Wipe existing picks for this (user, porra) — the multi-column
    # shape means we DELETE all N rows at once, not 1.
    await session.execute(
        delete(Pick).where(
            Pick.porra_id == porra_id,
            Pick.user_id == current_user.id,
        ),
    )

    # Build N × 15 rows: for each column, 14 rows for partidos 1..14
    # + 1 row for partido 15 (the Pleno, shared across columns —
    # identical values written once per columna_idx).
    #
    # We OMIT ``signs`` / ``pleno_local`` / ``pleno_visitante`` from
    # the row dict when they are the NULL target, because SQLAlchemy's
    # bulk INSERT path serialises ``signs=None`` to JSON ``null`` (NOT
    # SQL NULL) which would trip the CHECK.
    partido_rows: list[dict] = []
    pleno_rows: list[dict] = []
    for col_idx, col in enumerate(columnas_in, start=1):
        for m in col.matches:
            partido_rows.append({
                "group_id": group_id,
                "user_id": current_user.id,
                "jornada_id": porra.jornada_id,
                "porra_id": porra_id,
                "partido_orden": m.partido_orden,
                "columna_idx": col_idx,
                "signs": list(m.signs),
            })
        pleno_rows.append({
            "group_id": group_id,
            "user_id": current_user.id,
            "jornada_id": porra.jornada_id,
            "porra_id": porra_id,
            "partido_orden": 15,
            "columna_idx": col_idx,
            "pleno_local": body.pleno.local,
            "pleno_visitante": body.pleno.visitante,
        })

    # Bulk insert. Order: 14 partido rows first, then the N pleno rows
    # last so the FK + CHECK constraints see a clean state per row.
    if partido_rows:
        await session.execute(Pick.__table__.insert(), partido_rows)
    await session.execute(Pick.__table__.insert(), pleno_rows)

    await session.commit()

    # Return the member view, like MVP-02 did.
    return await _build_picks_view(group_id, porra_id, session, current_user.id)


@router.get(
    "/{porra_id}/picks",
    response_model=PicksOut,
    summary="List picks for all members (respects show_picks).",
    responses={
        403: {"description": "Not a member of the group"},
        404: {"description": "Porra not found"},
    },
)
async def list_picks(
    group_id: int,
    porra_id: int,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> PicksOut:
    await get_group_member_or_404(group_id, current_user.id, session)
    await _load_porra_or_404(group_id, porra_id, session)
    return await _build_picks_view(group_id, porra_id, session, current_user.id)


@router.get(
    "/{porra_id}/picks/me",
    response_model=list[PickOut],
    summary="Get the current user's picks for this porra.",
    responses={
        403: {"description": "Not a member of the group"},
        404: {"description": "Porra not found, or no picks yet"},
    },
)
async def list_my_picks(
    group_id: int,
    porra_id: int,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[PickOut]:
    await get_group_member_or_404(group_id, current_user.id, session)
    await _load_porra_or_404(group_id, porra_id, session)

    rows = (await session.execute(
        select(Pick).where(
            Pick.porra_id == porra_id,
            Pick.user_id == current_user.id,
        ).order_by(Pick.partido_orden)
    )).scalars().all()

    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no picks yet for this user/porra",
        )

    # Load the porra to get the jornada_id (we need it for the team embed).
    porra = await _load_porra_or_404(group_id, porra_id, session)
    composicion_by_orden, teams_by_id = await _load_partido_context(
        porra.jornada_id, session,
    )
    return [
        _build_pick_out(p, composicion_by_orden, teams_by_id) for p in rows
    ]


async def _build_picks_view(
    group_id: int, porra_id: int, session: AsyncSession, _caller_id: int,
) -> PicksOut:
    """Internal: build the PicksOut for GET /picks + POST /picks.

    Same shape as the MVP-02 ``GroupPicksOut`` but renamed to ``PicksOut``
    and scoped to a porra. ``picks`` is populated only when ``show_picks``
    is True AND the member voted — otherwise null.
    """
    group = await session.get(Group, group_id)
    show_picks = bool(group.show_picks) if group is not None else False

    # Members + their display names.
    members_rows = (await session.execute(
        select(GroupMember, User)
        .join(User, User.id == GroupMember.user_id)
        .where(GroupMember.group_id == group_id)
        .order_by(GroupMember.joined_at)
    )).all()

    # Picks for this porra.
    pick_rows = (await session.execute(
        select(Pick).where(Pick.porra_id == porra_id)
    )).scalars().all()

    porra = await _load_porra_or_404(group_id, porra_id, session)
    composicion_by_orden, teams_by_id = await _load_partido_context(
        porra.jornada_id, session,
    )

    by_user: dict[int, list[Pick]] = {}
    for p in pick_rows:
        by_user.setdefault(p.user_id, []).append(p)

    members_out: list[MemberPicksOut] = []
    for gm, u in members_rows:
        rows = by_user.get(u.id, [])
        voted = len(rows) > 0
        voted_at = max((p.updated_at for p in rows), default=None) if voted else None
        picks_out: list[PickOut] | None
        if voted and show_picks:
            picks_out = [
                _build_pick_out(p, composicion_by_orden, teams_by_id)
                for p in sorted(rows, key=lambda r: r.partido_orden)
            ]
        else:
            picks_out = None
        members_out.append(MemberPicksOut(
            user_id=u.id,
            display_name=u.display_name,
            voted=voted,
            voted_at=voted_at,
            picks=picks_out,
        ))
    return PicksOut(
        porra_id=porra_id,
        members=members_out,
        show_picks=show_picks,
    )


# ---------- Propuesta (computed media, not persisted) ----------


@router.post(
    "/{porra_id}/propuesta",
    response_model=PropuestaOut,
    summary="[admin] Compute the media (propuesta), don't persist.",
    responses={
        403: {"description": "Not admin"},
        404: {"description": "Porra not found"},
    },
)
async def compute_propuesta(
    group_id: int,
    porra_id: int,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> PropuestaOut:
    """Compute the propuesta — the admin uses this as the starting point
    for the final boleto (PUT /boleto).

    **Multi-column shape (oct 2026, slice 2):** the per-column votes are
    grouped by ``columna_idx`` so the algorithm produces one
    ``PropuestaColumnaOut`` per column. The Pleno al 15 is shared
    across columns — there's ONE top-level ``pleno`` field, computed
    from the deduplicated-by-user Pleno votes (each member submits the
    same ``(local, visitante)`` once per columna_idx; the dedup keeps
    one row per user).

    Does NOT persist. Can be called even with missing voters (the spec
    is explicit: P3 — yes, even with missing voters).
    """
    await get_admin_or_403(group_id, current_user, session)
    porra = await _load_porra_or_404(group_id, porra_id, session)

    # Load all picks for this porra.
    pick_rows = (await session.execute(
        select(Pick).where(Pick.porra_id == porra_id)
    )).scalars().all()

    # Group by (columna_idx, partido_orden). Partido 15 (Pleno) goes to a
    # flat list — the algoritmo deduplicates by user before aggregating,
    # since each member's N rows carry the same (local, visitante).
    partido_votes_by_column: dict[int, dict[int, list[PartidoVote]]] = {}
    pleno_votes: list[PlenoVote] = []
    for p in pick_rows:
        if p.partido_orden == 15:
            if p.pleno_local is not None and p.pleno_visitante is not None:
                pleno_votes.append(PlenoVote(
                    user_id=p.user_id,
                    columna_idx=p.columna_idx,
                    local=p.pleno_local,
                    visitante=p.pleno_visitante,
                ))
        else:
            if p.signs:
                partido_votes_by_column \
                    .setdefault(p.columna_idx, {}) \
                    .setdefault(p.partido_orden, []) \
                    .append(PartidoVote(
                        user_id=p.user_id,
                        columna_idx=p.columna_idx,
                        signs=tuple(p.signs),
                    ))

    raw = compute_propuesta_for_porra(
        porra_id=porra.id,
        modo=porra.modo,
        fijos=porra.fijos,
        dobles=porra.dobles,
        triples=porra.triples,
        precio_apuesta_eur=porra.precio_apuesta_eur,
        partido_votes_by_column=partido_votes_by_column,
        pleno_votes=pleno_votes,
    )

    # Embed participation (missing voters + voted count).
    participation = await _participation(group_id, porra_id, session)

    # Serialise via Pydantic so the shape is locked.
    columnas_out = [PropuestaColumnaOut(**c) for c in raw["columnas"]]
    return PropuestaOut(
        porra_id=raw["porra_id"],
        modo=raw["modo"],
        fijos=raw["fijos"],
        columnas=columnas_out,
        pleno=PlenoIn(**raw["pleno"]),
        participation=participation,
        columns=raw["columns"],
        cost_eur=raw["cost_eur"],
    )


# ---------- Boleto (the FINAL boleto, persisted on the porra) ----------


@router.get(
    "/{porra_id}/boleto.pdf",
    response_class=Response,
    responses={
        200: {
            "description": "PDF render of the persisted final boleto",
            "content": {"application/pdf": {}},
        },
        403: {"description": "Not a member of the group"},
        404: {"description": "Porra not found, or no boleto saved yet"},
    },
    summary="[member] Download the final boleto as PDF.",
)
async def get_boleto_pdf(
    group_id: int,
    porra_id: int,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Render the persisted boleto as a one-page A4 PDF and stream it back.

    Member-only (same gate as ``GET /boleto``) — the PDF is just another
    view of the same data, no extra privilege.

    The route is mounted BEFORE ``/{porra_id}/boleto`` (registration-order
    guard) even though the literal ``.pdf`` suffix makes them disjoint
    paths under FastAPI/Starlette's matching — the prompt author was
    defensive about future ``/{porra_id}/boleto.*`` siblings.

    Cost + columns are computed ONCE from the porra's format (D, T) and
    ``precio_apuesta_eur`` — no double render, no placeholder fallthrough.
    """
    await get_group_member_or_404(group_id, current_user.id, session)
    porra = await _load_porra_or_404(group_id, porra_id, session)

    if porra.boleto_columns is None and porra.boleto_matches is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no boleto saved yet",
        )

    # Load display-only header fields.
    group = await session.get(Group, group_id)
    jornada = await session.get(Jornada, porra.jornada_id)

    # Enrich each persisted row with the team names from composicion.
    # The JSONB stores partido_orden + signs only; the team strings live
    # in composicion (per-jornada, posiciones 1..14). Partido 15 is the
    # pleno and never appears in ``matches``.
    composicion_rows = (await session.execute(
        select(Composicion).where(Composicion.jornada_id == porra.jornada_id)
    )).scalars().all()
    comp_by_orden = {c.posicion: c for c in composicion_rows}

    def _enrich_match(m: dict) -> dict:
        comp = comp_by_orden.get(m["partido_orden"])
        return {
            "partido_orden": m["partido_orden"],
            "local_raw": comp.local_raw if comp else "?",
            "visitante_raw": comp.visitante_raw if comp else "?",
            "signs": m["signs"],
        }

    # Build the columns shape the renderer expects. Prefer
    # ``boleto_columns`` (the canonical N-column JSONB); fall back to
    # wrapping ``boleto_matches`` for rows persisted by the
    # pre-N-column code path.
    if porra.boleto_columns is not None:
        columnas: list[list[dict]] = [
            [_enrich_match(m) for m in col["matches"]]
            for col in porra.boleto_columns
        ]
    else:
        legacy = [_enrich_match(m) for m in (porra.boleto_matches or [])]
        columnas = [legacy] if legacy else []

    # Column count + cost come from the porra's mode-aware formula —
    # intuicion / sencilla use the explicit ``porra.columnas`` count,
    # multiple uses ``2^D × 3^T``, reducida uses ``spec.apuestas``.
    # Centralised in ``_porra_columns_and_cost`` so the read shape
    # stays consistent with the JSON endpoint.
    columns_count, cost = _porra_columns_and_cost(porra)

    pdf_bytes = render_boleto_pdf(
        group_name=group.name if group else "?",
        jornada_numero=jornada.numero if jornada else 0,
        temporada_str=jornada.temporada_str if jornada else "",
        columnas=columnas,
        pleno=(porra.pleno_local or "?", porra.pleno_visitante or "?"),
        columns_count=columns_count,
        cost_eur=float(cost),
        precio_apuesta_eur=float(porra.precio_apuesta_eur),
        porra_id=porra.id,
    )

    filename_j = jornada.numero if jornada else "?"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": (
                f'attachment; filename="boleto_j{filename_j}'
                f'_g{group_id}_p{porra_id}.pdf"'
            ),
        },
    )


@router.get(
    "/{porra_id}/boleto",
    response_model=BoletoOut,
    summary="Get the persisted final boleto (or 404 if not yet saved).",
    responses={
        403: {"description": "Not a member of the group"},
        404: {"description": "Porra not found, or no boleto saved yet"},
    },
)
async def get_boleto(
    group_id: int,
    porra_id: int,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> BoletoOut:
    await get_group_member_or_404(group_id, current_user.id, session)
    porra = await _load_porra_or_404(group_id, porra_id, session)

    if porra.boleto_columns is None and porra.boleto_matches is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no boleto saved yet",
        )

    # Build the response. Prefer the canonical ``boleto_columns`` JSONB
    # (the N-column shape); fall back to wrapping ``boleto_matches`` in
    # a one-element list for rows persisted by the pre-N-column code
    # path. Re-validate via BoletoColumnaIn / BoletoMatchIn so we never
    # leak a malformed row.
    if porra.boleto_columns is not None:
        columnas: list[BoletoColumnaIn] = [
            BoletoColumnaIn(matches=col["matches"]) for col in porra.boleto_columns
        ]
    else:
        # Legacy: a single 14-row list lives in ``boleto_matches`` (the
        # mirror that the new PUT also writes). Wrap into one column.
        legacy_matches = [
            BoletoMatchIn(**m) for m in (porra.boleto_matches or [])
        ]
        columnas = [BoletoColumnaIn(matches=legacy_matches)]

    pleno = PlenoIn(local=porra.pleno_local, visitante=porra.pleno_visitante)  # type: ignore[arg-type]

    cols, cost = _porra_columns_and_cost(porra)
    return BoletoOut(
        porra_id=porra.id,
        modo=porra.modo,
        set_at=porra.boleto_set_at,  # type: ignore[arg-type]
        set_by_user_id=porra.boleto_set_by_user_id,
        columnas=columnas,
        pleno=pleno,
        columns=cols,
        cost_eur=cost,
        reduccion=porra.reduccion,
    )


@router.put(
    "/{porra_id}/boleto",
    response_model=BoletoOut,
    summary="[admin] Save the FINAL boleto (validates against budget).",
    responses={
        403: {"description": "Not admin"},
        404: {"description": "Porra not found"},
        409: {
            "description": (
                "Jornada outside the final-confirmation window "
                "(effective_phase ∉ {in_window, escrutinio_pendiente}; "
                "escrutinio already published). Body has structured "
                "detail with code + window timestamps + effective_phase."
            ),
        },
        422: {"description": "D/T counts wrong, or cost exceeds budget"},
    },
)
async def put_boleto(
    group_id: int,
    porra_id: int,
    body: BoletoIn,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> BoletoOut:
    """Persist the FINAL boleto on the porra. Validates per mode:

    - ``multiple``: 1 column; D/T counts match ``porra.dobles``/
      ``porra.triples``.
    - ``reducida``: 1 column; D/T counts match the
      ``reduccion`` spec's D/T (from ``app.services.reducciones``).
    - ``sencilla``: exactly ``porra.columnas`` columns; every match
      in every column has exactly 1 sign (simples only).
    - ``intuicion``: exactly ``porra.columnas`` columns; the first
      ``porra.fijos`` parties share the same sign across all columns;
      every match has exactly 1 sign (variation lives across columns,
      not within a row).

    The jornada's effective_phase must be ``in_window`` or
    ``escrutinio_pendiente`` (the admin can save the FINAL boleto while
    voting is open, and even after voting closes but before the
    escrutinio is published). Saving once the escrutinio is
    persisted (completed) is rejected with 409.
    """
    await get_admin_or_403(group_id, current_user, session)
    porra = await _load_porra_or_404(group_id, porra_id, session)
    jornada = await _load_jornada_or_404(porra.jornada_id, session)
    await JornadaManager().assert_jornada_accepts(
        session, jornada, action="final_confirmation",
    )

    # Coalesce ``matches`` (legacy) into ``columnas`` (new). The legacy
    # shape is a single 14-row list; we wrap it into a one-element
    # list. New clients send ``columnas: [...]`` directly.
    if body.columnas is not None:
        columnas_in: list[BoletoColumnaIn] = list(body.columnas)
    elif body.matches is not None:
        columnas_in = [BoletoColumnaIn(matches=body.matches)]
    else:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="must provide 'columnas' or 'matches' (legacy alias)",
        )

    # Mode-specific validation. Each branch enforces the structural
    # rules of the mode; the unified cost + budget check at the end
    # is shared.
    if porra.modo == PorraModo.multiple:
        if len(columnas_in) != 1:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"multiple mode requires exactly 1 column, got {len(columnas_in)}"
                ),
            )
        n_d = sum(1 for m in columnas_in[0].matches if len(m.signs) == 2)
        n_t = sum(1 for m in columnas_in[0].matches if len(m.signs) == 3)
        if (n_d, n_t) != (porra.dobles, porra.triples):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"column signs don't match D/T: expected "
                    f"{porra.dobles} doubles + {porra.triples} triples, "
                    f"got {n_d} + {n_t}"
                ),
            )
    elif porra.modo == PorraModo.reducida:
        if len(columnas_in) != 1:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"reducida mode requires exactly 1 column, got {len(columnas_in)}"
                ),
            )
        n_d = sum(1 for m in columnas_in[0].matches if len(m.signs) == 2)
        n_t = sum(1 for m in columnas_in[0].matches if len(m.signs) == 3)
        try:
            spec = get_reduccion(porra.reduccion or "")
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"porra has modo='reducida' but reduccion="
                    f"{porra.reduccion!r} is not a known code"
                ),
            )
        if (n_d, n_t) != (spec.dobles, spec.triples):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"reduccion {porra.reduccion} requires "
                    f"{spec.dobles} doubles + {spec.triples} triples, "
                    f"got {n_d} + {n_t}"
                ),
            )
    elif porra.modo == PorraModo.sencilla:
        if porra.columnas is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "sencilla mode requires 'columnas' on the porra "
                    "(should be set at create time)"
                ),
            )
        if len(columnas_in) != porra.columnas:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"sencilla mode requires exactly {porra.columnas} columns, "
                    f"got {len(columnas_in)}"
                ),
            )
        for col in columnas_in:
            for m in col.matches:
                if len(m.signs) != 1:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=(
                            f"sencilla: every match must have exactly 1 sign, "
                            f"got {len(m.signs)} at partido {m.partido_orden}"
                        ),
                    )
    elif porra.modo == PorraModo.intuicion:
        if porra.fijos is None or porra.columnas is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "intuicion mode requires 'fijos' and 'columnas' on the porra "
                    "(should be set at create time)"
                ),
            )
        if len(columnas_in) != porra.columnas:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"intuicion mode requires {porra.columnas} columns, "
                    f"got {len(columnas_in)}"
                ),
            )
        # Validate fijos: for each fijo party, the sign is the same in
        # all columns. We trust column 0 as the canonical fijo
        # definition (the spec's "lock to one sign across columns").
        fijo_parties = [m for m in columnas_in[0].matches if m.partido_orden <= porra.fijos]
        for fijo in fijo_parties:
            signs_in_cols = [
                col.matches[fijo.partido_orden - 1].signs[0] for col in columnas_in
            ]
            if len(set(signs_in_cols)) != 1:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=(
                        f"intuicion: fijo partido {fijo.partido_orden} must have the "
                        f"same sign in all columns, got {signs_in_cols!r}"
                    ),
                )
        # Variables (partidos > fijos) carry 1 sign (the variation
        # happens across columns, not within a row).
        for col in columnas_in:
            for m in col.matches:
                if len(m.signs) != 1:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=(
                            f"intuicion: every match must have exactly 1 sign, "
                            f"got {len(m.signs)} at partido {m.partido_orden}"
                        ),
                    )

    # Mode-aware cost + budget check. ``_porra_columns_and_cost`` is
    # the single source of truth for "columns × precio" (multiple uses
    # 2^D×3^T, reducida uses spec.apuestas, the others use
    # porra.columnas). It's also the same helper the read shape uses,
    # so the response stays consistent.
    cols, cost = _porra_columns_and_cost(porra)
    if cost > porra.budget_eur:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"boleto costs {cost}€ (columns={cols} × "
                f"{porra.precio_apuesta_eur}€), exceeds the budget "
                f"{porra.budget_eur}€"
            ),
        )

    # Persist. ``boleto_columns`` is the canonical N-column shape;
    # ``boleto_matches`` mirrors the FIRST column (legacy single-column
    # consumers — the renderer and the pre-oct-2026 tests — still work
    # without rewriting).
    porra.boleto_columns = [
        {"matches": [
            {"partido_orden": m.partido_orden, "signs": list(m.signs)}
            for m in col.matches
        ]}
        for col in columnas_in
    ]
    porra.boleto_matches = [
        {"partido_orden": m.partido_orden, "signs": list(m.signs)}
        for m in columnas_in[0].matches
    ] if columnas_in else None
    porra.pleno_local = body.pleno.local
    porra.pleno_visitante = body.pleno.visitante
    porra.boleto_set_at = datetime.now(tz=timezone.utc)
    porra.boleto_set_by_user_id = current_user.id
    porra.updated_at = porra.boleto_set_at
    await session.commit()
    await session.refresh(porra)

    cols, _ = _porra_columns_and_cost(porra)
    return BoletoOut(
        porra_id=porra.id,
        modo=porra.modo,
        set_at=porra.boleto_set_at,  # type: ignore[arg-type]
        set_by_user_id=porra.boleto_set_by_user_id,
        columnas=columnas_in,
        pleno=body.pleno,
        columns=cols,
        cost_eur=cost,
        reduccion=porra.reduccion,
    )


# ---------- DELETE endpoints (oct 2026) ----------
#
# Two complementary endpoints the panel needs to recover from
# "format is locked" once a boleto is saved:
#
# - ``DELETE /boleto`` clears the persisted final boleto on a porra
#   (keeps the porra + picks). Use it when the admin wants to change
#   the format (PATCH /porras rejects a D/T change while a boleto
#   exists). After DELETE, the admin can PATCH format + PUT a new
#   boleto.
# - ``DELETE /porras/{pid}`` (a separate handler, see below) nukes the
#   porra entirely along with all picks. Use it when the admin wants
#   to start over from scratch (different budget, different format,
#   different members, etc.).


@router.delete(
    "/{porra_id}/boleto",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="[admin] Delete the persisted final boleto (keeps the porra + picks).",
    description=(
        "Clears ``boleto_matches``, ``boleto_columns``, ``pleno_local``, "
        "``pleno_visitante``, ``boleto_set_at`` and ``boleto_set_by_user_id`` "
        "on the porra. The porra row, its picks, and its format are "
        "preserved — only the FINAL persisted boleto is gone. Use this to "
        "unlock the format (PATCH /porras rejects a D/T change while a "
        "boleto exists) or to discard a wrong boleto and start over "
        "without losing picks."
    ),
    responses={
        403: {"description": "Not a group admin"},
        404: {"description": "Porra not found, or no boleto saved yet"},
    },
)
async def delete_boleto(
    group_id: int,
    porra_id: int,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    await get_admin_or_403(group_id, current_user, session)
    porra = await _load_porra_or_404(group_id, porra_id, session)

    if porra.boleto_matches is None and porra.boleto_columns is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no boleto saved yet",
        )

    porra.boleto_matches = None
    porra.boleto_columns = None
    porra.pleno_local = None
    porra.pleno_visitante = None
    porra.boleto_set_at = None
    porra.boleto_set_by_user_id = None
    porra.updated_at = datetime.now(tz=timezone.utc)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/{porra_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="[admin] Delete the porra (cascades to all picks).",
    description=(
        "Hard-deletes the porra and every Pick row that belongs to it. "
        "Use it when the admin wants to start over (different budget, "
        "different format, fresh slate) without keeping the old picks. "
        "After DELETE the group can POST a new porra for the same "
        "jornada with a different shape — the unique key on "
        "(group_id, jornada_id) is now free."
    ),
    responses={
        403: {"description": "Not a group admin"},
        404: {"description": "Porra not found"},
    },
)
async def delete_porra(
    group_id: int,
    porra_id: int,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    await get_admin_or_403(group_id, current_user, session)
    porra = await _load_porra_or_404(group_id, porra_id, session)

    # Picks are FK'd to porra with the SQLAlchemy default cascade
    # behaviour. We delete the porra row directly; the FK is
    # ``ondelete="CASCADE"`` on the schema, so the picks vanish in the
    # same transaction.
    await session.delete(porra)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)