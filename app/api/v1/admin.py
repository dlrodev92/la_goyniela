"""Admin endpoints — shared helper + jornada dates configuration.

Slice 3 of the gestor-jornadas plan (``docs/producto/gestor-jornadas.md``
§ 3.2). Owns:

- :func:`require_system_admin` — the shared "is this user a system admin?"
  check used by every admin router. Moved here from
  ``admin_uploads.py`` so multiple admin endpoints share one source of
  truth. The contract is unchanged (admin of at least one group).
- :func:`configure_jornada_dates` — POST ``/admin/jornadas/{id}/dates``.
  Persists the 15 partido kickoffs + ``sale_opens_at`` via
  :class:`app.services.jornada_manager.JornadaManager`. The endpoint is
  intentionally thin: input validation lives in Pydantic, business
  validation lives in the manager, and the endpoint only translates
  exceptions to HTTP status codes.

The endpoint does NOT invent business rules — the manager owns:

- the kickoff count check,
- the tz-awareness check,
- the draft → active transition.

The endpoint only translates ``JornadaNotFoundError → 404`` and
``ValueError`` / ``IncompleteJornadaError → 422``.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.auth import get_current_user
from app.db.models import GroupMember, User
from app.db.session import get_session
from app.schemas.jornada import JornadaDatesIn, JornadaOut
from app.services.jornada_manager import (
    IncompleteJornadaError,
    JornadaManager,
    JornadaNotFoundError,
)


router = APIRouter(prefix="/admin/jornadas", tags=["admin-jornadas"])


# ---------- Shared admin guard ----------


async def require_system_admin(current_user: User, session: AsyncSession) -> None:
    """403 unless the user is admin of at least one group.

    MVP has no global admin role, so "admin of any group" is the closest
    thing to system admin we have. A stricter gate (e.g. a
    ``user.is_admin`` column) is a documented follow-up.

    Kept here (rather than re-defined per router) so admin_uploads and
    admin endpoints share the contract. Same shape as the previous
    helper in :mod:`app.api.v1.admin_uploads`.
    """
    is_admin = await session.scalar(
        select(GroupMember.id).where(
            GroupMember.user_id == current_user.id,
            GroupMember.role == "admin",
        ).limit(1)
    )
    if is_admin is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="system admin role required (be admin of at least one group)",
        )


async def require_superadmin(current_user: User) -> None:
    """403 unless the user has ``is_superadmin=True``.

    Superadmin is a system-wide role (distinct from per-group admin).
    Used for the 4 new admin routers in the superadmin-panel sprint
    (users, teams, jornadas, composicion). No DB hit: the column is on
    the user row and the auth dependency already loaded it.
    """
    if not current_user.is_superadmin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="superadmin role required",
        )


# ---------- Endpoint ----------


@router.post(
    "/{jornada_id}/dates",
    response_model=JornadaOut,
    summary="[admin] Configure jornada kickoffs + sale_opens_at",
    responses={
        401: {"description": "Missing / invalid Bearer token"},
        403: {"description": "Actor is not a system admin"},
        404: {"description": "Jornada not found"},
        422: {
            "description": (
                "Validation error (wrong kickoff count, naive datetime, "
                "missing composicion rows)."
            ),
        },
    },
)
async def configure_jornada_dates(
    jornada_id: int,
    body: JornadaDatesIn,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> JornadaOut:
    """Persist the 15 partido kickoffs + ``sale_opens_at`` via the manager.

    On a draft jornada the call transitions status to ``active`` (the
    manager owns that rule). On an already-active jornada the fields are
    updated in place. The response uses :class:`JornadaOut` for now;
    slice 8 will add the slice-8-effective fields (``kickoff_last_match_at``,
    ``escrutinio_expected_at``, etc.) to the response shape.
    """
    await require_system_admin(current_user, session)

    try:
        jornada = await JornadaManager().configure_dates(
            session,
            jornada_id,
            kickoff_per_partido=body.kickoff_per_partido,
            sale_opens_at=body.sale_opens_at,
        )
    except JornadaNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"jornada {jornada_id} not found",
        )
    except IncompleteJornadaError as exc:
        # The manager refuses to operate before Quiniwin ingest. This is
        # a validation-level failure (the admin hasn't supplied what the
        # manager needs), not a server error.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )
    except ValueError as exc:
        # Wrong kickoff count, naive datetime, etc. — the manager owns
        # these checks; the endpoint only translates to HTTP.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )

    return JornadaOut.model_validate(jornada)