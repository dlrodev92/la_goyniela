"""Superadmin composicion-correction endpoints.

Mounted under ``/api/v1/admin/jornadas/{id}/composicion``. Slice 2 of
the superadmin-panel sprint. Auth contract: every endpoint calls
:func:`require_superadmin` as its first line.

Endpoints:

- ``GET   /api/v1/admin/jornadas/{jornada_id}/composicion``        — list 15 rows.
- ``PATCH /api/v1/admin/jornadas/{jornada_id}/composicion/{pos}`` — edit a single row.
  After a successful update we call
  :meth:`app.services.jornada_manager.JornadaManager.recompute_window`
  so ``kickoff_last_match_at``, ``voting_closes_at`` and
  ``escrutinio_expected_at`` stay in sync with the (possibly
  changed) kickoff list.

Validation on PATCH:

- ``pos`` must be in 1..15.
- The jornada must exist (and not be soft-deleted — we treat soft-
  deleted jornadas as not-found here, even though the
  ``/api/v1/admin/jornadas/{id}`` GET surfaces them).
- If the body sets ``local_team_id`` or ``visitante_team_id``, the
  referenced team must exist and not be soft-deleted.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Path, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.admin import require_superadmin
from app.api.v1.auth import get_current_user
from app.db.models import Composicion, Jornada, TeamIdentityMap, User
from app.db.session import get_session
from app.schemas.admin import ComposicionAdminOut, ComposicionPatchIn
from app.services.jornada_manager import (
    JornadaManager,
    JornadaNotFoundError,
    PARTIDOS_PER_JORNADA,
)


router = APIRouter(
    prefix="/api/v1/admin/jornadas", tags=["admin-jornadas"],
)


# ---------- Endpoints ----------


@router.get(
    "/{jornada_id}/composicion",
    response_model=list[ComposicionAdminOut],
    summary="[superadmin] List the 15 compos rows of a jornada",
    responses={
        401: {"description": "Missing / invalid Bearer token"},
        403: {"description": "Actor is not a superadmin"},
        404: {"description": "Jornada not found (or soft-deleted)"},
    },
)
async def list_composicion(
    jornada_id: int,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[ComposicionAdminOut]:
    """Return the 15 compos rows ordered by ``posicion``. Superadmin-only."""
    await require_superadmin(current_user)

    j = await session.get(Jornada, jornada_id)
    if j is None or j.deleted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"jornada_id={jornada_id} not found",
        )

    rows = (
        await session.execute(
            select(Composicion)
            .where(Composicion.jornada_id == jornada_id)
            .order_by(Composicion.posicion)
        )
    ).scalars().all()
    return [ComposicionAdminOut.model_validate(r) for r in rows]


@router.patch(
    "/{jornada_id}/composicion/{pos}",
    response_model=ComposicionAdminOut,
    summary="[superadmin] Edit one compos row + recompute the jornada window",
    description=(
        "Partial update of one compos row. Any of ``local_team_id``, "
        "``visitante_team_id``, ``kickoff_at``, ``competicion_raw`` may "
        "be sent; absent fields are left as-is. After the update the "
        "endpoint calls :meth:`JornadaManager.recompute_window` so the "
        "jornada's derived fields stay in sync with the composicion "
        "rows (e.g. ``kickoff_last_match_at`` shifts when a ``kickoff_at`` "
        "changes)."
    ),
    responses={
        400: {
            "description": (
                "Validation error: no fields to update, or invalid team "
                "FK (team soft-deleted or missing)"
            )
        },
        401: {"description": "Missing / invalid Bearer token"},
        403: {"description": "Actor is not a superadmin"},
        404: {"description": "Jornada or compos row not found"},
        422: {"description": "Jornada's composicion is incomplete"},
    },
)
async def patch_composicion(
    jornada_id: int,
    pos: int = Path(
        ..., ge=1, le=PARTIDOS_PER_JORNADA,
        description="Position of the partido in the boleto (1..15).",
    ),
    body: ComposicionPatchIn = ...,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ComposicionAdminOut:
    """Edit a single composicion row and recompute the jornada window.

    Validates that the jornada is not soft-deleted, that the row at
    ``pos`` exists, and that any new ``local_team_id`` /
    ``visitante_team_id`` points at a live (not soft-deleted) team
    row.
    """
    await require_superadmin(current_user)

    j = await session.get(Jornada, jornada_id)
    if j is None or j.deleted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"jornada_id={jornada_id} not found",
        )

    compos = await session.scalar(
        select(Composicion).where(
            Composicion.jornada_id == jornada_id,
            Composicion.posicion == pos,
        )
    )
    if compos is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"composicion row not found for jornada_id={jornada_id} "
                f"posicion={pos}"
            ),
        )

    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="no fields to update; PATCH body must include at least one",
        )

    # FK validation: if either team id is being changed, the new target
    # must exist and not be soft-deleted. Both checks happen in this
    # session so the resulting state is consistent on commit.
    for field in ("local_team_id", "visitante_team_id"):
        if field in updates and updates[field] is not None:
            team = await session.get(TeamIdentityMap, updates[field])
            if team is None or team.deleted_at is not None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"{field}={updates[field]} is missing or "
                        f"soft-deleted"
                    ),
                )

    for field, value in updates.items():
        setattr(compos, field, value)

    await session.commit()
    await session.refresh(compos)

    # Sync the derived window fields. The manager refuses on a jornada
    # that doesn't have all 15 compos rows (which would be a different
    # bug — we just confirmed pos is in 1..15 above, so the full set
    # must exist for us to be here). The 404 / 422 mapping is preserved
    # so a race with another admin deleting a row surfaces correctly.
    try:
        await JornadaManager().recompute_window(session, jornada_id)
    except JornadaNotFoundError:
        # Another admin hard-deleted the jornada between our load and
        # the manager call. Surface as 404.
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"jornada_id={jornada_id} disappeared during update",
        )

    return ComposicionAdminOut.model_validate(compos)
