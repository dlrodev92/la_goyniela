"""Manual ingest trigger endpoints (superadmin only).

This is the manual fallback for the cron jobs. If the scheduler is
down, mis-configured, or the superadmin just wants to force a run,
they can POST to these endpoints and the same ingest functions the
crons use run synchronously.

Chained behavior: the Quiniwin endpoint also runs Losilla for any
jornada that Quiniwin just created. This way a single click does
"the whole job" — Quiniwin teams + matches, plus Losilla live data
(comentarios editoriales + porcentajes / estimaciones). The
escrutinio oficial block is only available AFTER the jornada
closes — Losilla returns None for it while the jornada is still
open or in_preparacion.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.admin import require_superadmin
from app.api.v1.auth import get_current_user
from app.clients.eduardolosilla import EduardoLosillaClient
from app.db.models import User
from app.db.session import get_session
from app.scheduler.jobs import (
    _run_losilla_for_inserted_jornadas,
    ingest_today_quiniwin_job,
)


router = APIRouter(prefix="/api/v1/admin/ingest", tags=["admin-ingest"])


# ---------- Quiniwin (+ chained Losilla for new jornadas) ----------


class QuiniwinIngestIn(BaseModel):
    """All fields optional. Defaults match the cron behavior."""
    model_config = ConfigDict(extra="forbid")
    temporada: int | None = Field(
        None, ge=1998, le=2100,
        description="Temporada end-year (e.g. 2026 = 26/27). Default: current year.",
    )
    jornada_range_start: int = Field(
        1, ge=1, le=42,
        description="First jornada to attempt (inclusive).",
    )
    jornada_range_end: int = Field(
        42, ge=1, le=42,
        description="Last jornada to attempt (inclusive).",
    )
    forward_cap: int = Field(
        5, ge=1, le=42,
        description="Stop after this many new (not-yet-known) jornadas are found.",
    )
    fetch_timeout_sec: float = Field(
        20.0, ge=1.0, le=120.0,
        description="Per-request HTTP timeout against Quiniwin.",
    )
    run_losilla: bool = Field(
        True,
        description=(
            "If true, also runs the Losilla live ingest for every "
            "jornada that Quiniwin just created (comentarios editoriales "
            "+ estimaciones / porcentajes). Set false to skip Losilla."
        ),
    )


@router.post(
    "/quiniwin",
    summary="[superadmin] Trigger Quiniwin ingest (+ chained Losilla)",
    description=(
        "Runs the same function the daily cron uses to ingest new "
        "jornadas from Quiniwin (teams + matches). By default it ALSO "
        "runs the Losilla live ingest for each newly-created jornada, "
        "so a single call gives you the full picture: Quiniwin data "
        "plus Losilla comentarios + estimaciones.\n\n"
        "Losilla escrutinio_oficial is only available AFTER the jornada "
        "closes — for an open jornada, Losilla returns None for that "
        "block but still persists comentarios and estimaciones.\n\n"
        "Synchronous: blocks until the result is ready. Default "
        "forward_cap=5 means up to 5 new jornadas per call."
    ),
)
async def trigger_quiniwin_ingest(
    body: QuiniwinIngestIn = QuiniwinIngestIn(),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await require_superadmin(current_user)

    result = await ingest_today_quiniwin_job(
        temporada=body.temporada,
        jornada_range_start=body.jornada_range_start,
        jornada_range_end=body.jornada_range_end,
        forward_cap=body.forward_cap,
        fetch_timeout_sec=body.fetch_timeout_sec,
    )

    # If the user opted in and Quiniwin created at least one new
    # jornada, run the Losilla live ingest for each one. The helper
    # is the same one the cron uses (DRY).
    losilla_per_jornada: dict[int, dict[str, Any]] = {}
    if body.run_losilla:
        quiniwin_block = result.get("quiniwin", {})
        inserted_jornadas: list[int] = [
            entry["jornada"]
            for entry in quiniwin_block.get("jornadas", [])
            if entry.get("status") == "ok" and entry.get("jornadas_inserted", 0) > 0
        ]
        temporada_used: int | None = quiniwin_block.get("temporada")
        if inserted_jornadas and temporada_used is not None:
            losilla_per_jornada = await _run_losilla_for_inserted_jornadas(
                inserted_jornadas, temporada_used,
            )

    return {
        **result,
        "losilla_per_jornada": losilla_per_jornada,
    }
