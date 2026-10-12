"""Superadmin jornada-management endpoints.

Mounted under ``/api/v1/admin/jornadas``. Slice 2 of the superadmin-panel
sprint. Auth contract: every endpoint calls :func:`require_superadmin`
as its first line.

Endpoints:

- ``GET    /api/v1/admin/jornadas``                  — list; ``?include_deleted``, ``?status``.
- ``GET    /api/v1/admin/jornadas/{id}``             — single (any deleted state).
- ``PATCH  /api/v1/admin/jornadas/{id}``             — edit status, sale_opens_at, voting_closes_at.
- ``POST   /api/v1/admin/jornadas/{id}/open``        — shortcut: draft → active (body has sale_opens_at).
- ``POST   /api/v1/admin/jornadas/{id}/close``       — shortcut: active → past.
- ``POST   /api/v1/admin/jornadas/{id}/reopen``      — shortcut: past → active.
- ``DELETE /api/v1/admin/jornadas/{id}``             — soft-delete.
- ``POST   /api/v1/admin/jornadas/{id}/restore``     — restore.

Status transitions (validated for PATCH and the three shortcut endpoints):

- ``draft``  ↔ ``active``
- ``active`` ↔ ``past``
- ``past``   → ``active``  (covered by the rule above)

Other transitions (``draft → past`` etc.) are refused with 400.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.admin import require_superadmin
from app.api.v1.auth import get_current_user
from app.db.models import Jornada, JornadaStatus, User
from app.db.session import get_session
from app.schemas.admin import (
    JornadaAdminOut,
    JornadaDefaultColumnasIn,
    JornadaPatchIn,
)


router = APIRouter(prefix="/api/v1/admin/jornadas", tags=["admin-jornadas"])


# ---------- Status transition rules ----------

# Allowed edges: ``draft <-> active`` and ``active <-> past``. Identity
# transitions (status unchanged) are also allowed. Anything else (e.g.
# ``draft → past``) is refused with 400.
_ALLOWED_TRANSITIONS: dict[JornadaStatus, frozenset[JornadaStatus]] = {
    JornadaStatus.draft: frozenset({JornadaStatus.draft, JornadaStatus.active}),
    JornadaStatus.active: frozenset({JornadaStatus.draft, JornadaStatus.active, JornadaStatus.past}),
    JornadaStatus.past: frozenset({JornadaStatus.active, JornadaStatus.past}),
}


def _is_valid_transition(
    current: JornadaStatus, target: JornadaStatus,
) -> bool:
    """True when ``current → target`` is an allowed jornada state edge."""
    return target in _ALLOWED_TRANSITIONS.get(current, frozenset())


# ---------- Local input schemas (specific to this router) ----------


class JornadaOpenIn(BaseModel):
    """Body for ``POST /api/v1/admin/jornadas/{id}/open``.

    ``sale_opens_at`` is required so the shortcut is a complete
    transition: a draft jornada with no sale_opens_at cannot become
    active (the public ``compute_effective_phase`` treats an active
    jornada without sale_opens_at as "fuera_de_ventana", which is
    almost never what the admin meant).
    """
    model_config = ConfigDict(extra="forbid")
    sale_opens_at: datetime = Field(
        ...,
        description=(
            "tz-aware datetime when voting opens. Required for the "
            "draft → active transition."
        ),
    )


# ---------- Endpoints ----------


@router.get(
    "",
    response_model=list[JornadaAdminOut],
    summary="[superadmin] List jornadas (optionally including soft-deleted)",
    responses={
        401: {"description": "Missing / invalid Bearer token"},
        403: {"description": "Actor is not a superadmin"},
    },
)
async def list_jornadas(
    include_deleted: bool = Query(
        False,
        description=(
            "When true, soft-deleted jornadas are also returned. Default "
            "false to match the public ``/api/v1/jornadas`` endpoint."
        ),
    ),
    status_filter: JornadaStatus | None = Query(
        None,
        alias="status",
        description="Filter by status (draft | active | past).",
    ),
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[JornadaAdminOut]:
    """List jornadas. Superadmin-only."""
    await require_superadmin(current_user)

    stmt = select(Jornada)
    if not include_deleted:
        stmt = stmt.where(Jornada.deleted_at.is_(None))
    if status_filter is not None:
        stmt = stmt.where(Jornada.status == status_filter)
    stmt = (
        stmt.order_by(
            Jornada.temporada.desc(), Jornada.numero.desc(),
        )
        .limit(limit)
        .offset(offset)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [JornadaAdminOut.model_validate(r) for r in rows]


@router.get(
    "/{jornada_id}",
    response_model=JornadaAdminOut,
    summary="[superadmin] Get one jornada (any deleted state)",
    responses={
        401: {"description": "Missing / invalid Bearer token"},
        403: {"description": "Actor is not a superadmin"},
        404: {"description": "Jornada not found"},
    },
)
async def get_jornada(
    jornada_id: int,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> JornadaAdminOut:
    """Single jornada by id. Superadmin-only; surfaces soft-deleted."""
    await require_superadmin(current_user)

    j = await session.get(Jornada, jornada_id)
    if j is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"jornada_id={jornada_id} not found",
        )
    return JornadaAdminOut.model_validate(j)


@router.patch(
    "/{jornada_id}",
    response_model=JornadaAdminOut,
    summary="[superadmin] Edit status / sale_opens_at / voting_closes_at",
    description=(
        "Only the fields present in the body are updated. Status "
        "transitions are validated against the rules in this module's "
        "docstring; invalid transitions are refused with 400. Setting "
        "``status`` to the same value as the current one is a no-op."
    ),
    responses={
        400: {
            "description": (
                "Validation error: invalid status transition or no "
                "fields to update"
            )
        },
        401: {"description": "Missing / invalid Bearer token"},
        403: {"description": "Actor is not a superadmin"},
        404: {"description": "Jornada not found"},
    },
)
async def patch_jornada(
    jornada_id: int,
    body: JornadaPatchIn,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> JornadaAdminOut:
    """Edit jornada fields. Status transitions validated. Superadmin-only."""
    await require_superadmin(current_user)

    j = await session.get(Jornada, jornada_id)
    if j is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"jornada_id={jornada_id} not found",
        )

    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="no fields to update; PATCH body must include at least one",
        )

    if "status" in updates and updates["status"] is not None:
        target = updates["status"]
        if not _is_valid_transition(j.status, target):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"invalid status transition {j.status.value} → "
                    f"{target.value}; allowed: draft ↔ active, "
                    f"active ↔ past"
                ),
            )
        j.status = target

    if "sale_opens_at" in updates:
        j.sale_opens_at = updates["sale_opens_at"]
    if "voting_closes_at" in updates:
        j.voting_closes_at = updates["voting_closes_at"]

    await session.commit()
    await session.refresh(j)
    return JornadaAdminOut.model_validate(j)


@router.post(
    "/{jornada_id}/open",
    response_model=JornadaAdminOut,
    summary="[superadmin] Shortcut: draft → active (with sale_opens_at)",
    responses={
        400: {
            "description": (
                "Jornada is not in draft (use PATCH or another shortcut)"
            )
        },
        401: {"description": "Missing / invalid Bearer token"},
        403: {"description": "Actor is not a superadmin"},
        404: {"description": "Jornada not found"},
    },
)
async def open_jornada(
    jornada_id: int,
    body: JornadaOpenIn,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> JornadaAdminOut:
    """Transition draft → active and set ``sale_opens_at`` atomically.

    Only valid when the current status is ``draft``. For non-draft
    jornadas, use PATCH (or ``/reopen`` for past → active). Does NOT
    recompute the window — kickoffs are not part of this shortcut.
    """
    await require_superadmin(current_user)

    j = await session.get(Jornada, jornada_id)
    if j is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"jornada_id={jornada_id} not found",
        )
    if j.status != JornadaStatus.draft:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"cannot /open a {j.status.value} jornada; use PATCH to "
                f"edit status or /reopen for past → active"
            ),
        )

    j.status = JornadaStatus.active
    j.sale_opens_at = body.sale_opens_at
    await session.commit()
    await session.refresh(j)
    return JornadaAdminOut.model_validate(j)


@router.post(
    "/{jornada_id}/close",
    response_model=JornadaAdminOut,
    summary="[superadmin] Shortcut: active → past",
    responses={
        400: {"description": "Jornada is not in active"},
        401: {"description": "Missing / invalid Bearer token"},
        403: {"description": "Actor is not a superadmin"},
        404: {"description": "Jornada not found"},
    },
)
async def close_jornada(
    jornada_id: int,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> JornadaAdminOut:
    """Transition active → past. No body. Superadmin-only."""
    await require_superadmin(current_user)

    j = await session.get(Jornada, jornada_id)
    if j is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"jornada_id={jornada_id} not found",
        )
    if j.status != JornadaStatus.active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"cannot /close a {j.status.value} jornada; only valid "
                f"on active"
            ),
        )

    j.status = JornadaStatus.past
    await session.commit()
    await session.refresh(j)
    return JornadaAdminOut.model_validate(j)


@router.post(
    "/{jornada_id}/reopen",
    response_model=JornadaAdminOut,
    summary="[superadmin] Shortcut: past → active",
    responses={
        400: {"description": "Jornada is not in past"},
        401: {"description": "Missing / invalid Bearer token"},
        403: {"description": "Actor is not a superadmin"},
        404: {"description": "Jornada not found"},
    },
)
async def reopen_jornada(
    jornada_id: int,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> JornadaAdminOut:
    """Transition past → active. No body. Superadmin-only.

    Intended for the "I closed the jornada by mistake, the boleto is
    still going" recovery path. The kickoff / voting-window fields are
    preserved as-is; if the admin needs to recompute them they can
    PATCH the compos rows in the jornada.
    """
    await require_superadmin(current_user)

    j = await session.get(Jornada, jornada_id)
    if j is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"jornada_id={jornada_id} not found",
        )
    if j.status != JornadaStatus.past:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"cannot /reopen a {j.status.value} jornada; only valid "
                f"on past"
            ),
        )

    j.status = JornadaStatus.active
    await session.commit()
    await session.refresh(j)
    return JornadaAdminOut.model_validate(j)


@router.delete(
    "/{jornada_id}",
    response_model=JornadaAdminOut,
    summary="[superadmin] Soft-delete a jornada (set deleted_at = now)",
    description=(
        "Sets ``deleted_at`` to the current UTC time. No FK guard: a "
        "jornada can be soft-deleted even when it has composicion rows. "
        "The cascade from jornada to compos is on the underlying FK, "
        "but soft delete leaves the compos rows in place — the public "
        "API filters them out via the jornada.deleted_at check at the "
        "detail endpoint. Restore via "
        "``POST /api/v1/admin/jornadas/{id}/restore``."
    ),
    responses={
        401: {"description": "Missing / invalid Bearer token"},
        403: {"description": "Actor is not a superadmin"},
        404: {"description": "Jornada not found"},
    },
)
async def soft_delete_jornada(
    jornada_id: int,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> JornadaAdminOut:
    """Set ``deleted_at`` to now. Idempotent on already-deleted. Superadmin-only."""
    await require_superadmin(current_user)

    j = await session.get(Jornada, jornada_id)
    if j is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"jornada_id={jornada_id} not found",
        )

    j.deleted_at = datetime.now(tz=timezone.utc)
    await session.commit()
    await session.refresh(j)
    return JornadaAdminOut.model_validate(j)


@router.post(
    "/{jornada_id}/restore",
    response_model=JornadaAdminOut,
    summary="[superadmin] Restore a soft-deleted jornada (clear deleted_at)",
    responses={
        401: {"description": "Missing / invalid Bearer token"},
        403: {"description": "Actor is not a superadmin"},
        404: {"description": "Jornada not found"},
    },
)
async def restore_jornada(
    jornada_id: int,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> JornadaAdminOut:
    """Clear ``deleted_at``. Idempotent on already-live jornadas."""
    await require_superadmin(current_user)

    j = await session.get(Jornada, jornada_id)
    if j is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"jornada_id={jornada_id} not found",
        )
    if j.deleted_at is not None:
        j.deleted_at = None
        await session.commit()
        await session.refresh(j)
    return JornadaAdminOut.model_validate(j)


@router.patch(
    "/{jornada_id}/default-columnas",
    response_model=JornadaAdminOut,
    summary="[superadmin] Set / clear ``Jornada.default_columnas``.",
    description=(
        "Patch the jornada-level default for ``porra.columnas`` in "
        "``sencilla`` / ``intuicion`` mode. Admin sets this when "
        "configuring the jornada (\"this is a 4-column jornada\") and "
        "any peña's porra that doesn't override ``columnas`` at "
        "create time picks it up. Sending ``null`` clears the "
        "default. Range 2..12 (same window as ``porra.columnas``). "
        "Returns the updated ``JornadaAdminOut`` (which now includes "
        "``default_columnas``)."
    ),
    responses={
        401: {"description": "Missing / invalid Bearer token"},
        403: {"description": "Actor is not a superadmin"},
        404: {"description": "Jornada not found"},
        422: {
            "description": (
                "default_columnas out of range (must be 2..12 or null)"
            )
        },
    },
)
async def set_default_columnas(
    jornada_id: int,
    body: JornadaDefaultColumnasIn,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> JornadaAdminOut:
    """Set ``Jornada.default_columnas``. Sending ``null`` clears it."""
    await require_superadmin(current_user)

    j = await session.get(Jornada, jornada_id)
    if j is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"jornada_id={jornada_id} not found",
        )

    # ``exclude_unset=True`` keeps the JSON ``null`` -> actual SQL NULL
    # behaviour so PATCHes with ``{"default_columnas": null}`` clear the
    # value (vs. NOT touching the column at all).
    updates = body.model_dump(exclude_unset=True)
    if "default_columnas" in updates:
        j.default_columnas = updates["default_columnas"]

    await session.commit()
    await session.refresh(j)
    return JornadaAdminOut.model_validate(j)
