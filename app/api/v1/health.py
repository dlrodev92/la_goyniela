"""Health and readiness routes.

- GET /health     → process liveness (no external deps)
- GET /health/db  → DB readiness (SELECT 1)
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session

router = APIRouter(tags=["health"])


@router.get(
    "/health",
    summary="Liveness probe",
    description="Returns 200 if the process is up. No external dependencies.",
)
async def health_live() -> dict[str, str]:
    return {"status": "ok"}


@router.get(
    "/health/db",
    summary="DB readiness probe",
    description=(
        "Returns 200 if Postgres is reachable and responds to SELECT 1. "
        "Returns 503 if the DB is unreachable or responds unexpectedly."
    ),
    responses={
        200: {"description": "DB reachable"},
        503: {"description": "DB unreachable or query failed"},
    },
)
async def health_db(session: AsyncSession = Depends(get_session)) -> dict[str, str | int]:
    try:
        result = await session.execute(text("SELECT 1 AS one"))
        one = result.scalar_one()
        if one != 1:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"DB returned unexpected scalar: {one!r}",
            )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"DB error: {type(exc).__name__}: {exc}",
        )
    return {"status": "ok", "db": "reachable", "select_1": int(one)}