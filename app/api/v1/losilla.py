"""Eduardo Losilla endpoints.

Thin wrapper over ``app.clients.eduardolosilla.EduardoLosillaClient``.
Three read-only endpoints:

- ``GET /api/v1/losilla/jornada/{jornada}`` — full payload, ALWAYS live
  (no DB cache; useful for the in-flight jornada where the live view
  changes minute-to-minute). For persisted data, use the other two.
- ``GET /api/v1/losilla/jornada/{jornada}/escrutinio`` — DB-first read
  of the official escrutinio block. Falls back to upstream if no row
  has been ingested yet (T-data-4 ingest).
- ``GET /api/v1/losilla/jornada/{jornada}/comentarios`` — DB-first read
  of the editorial comentarios. Falls back to upstream if no rows have
  been ingested yet.

All endpoints accept ``?temporada=<YYYY>`` (default 2027 for 2026/27).
The DB-first endpoints return a wrapper with a ``source`` field so
callers can tell whether the data came from the persisted row or from
upstream. The live endpoint returns the raw ``JornadaLosilla`` shape
unchanged.

The client is a module-level singleton with an in-memory TTL cache
(5 min for the in-flight jornada, 1 h for closed). Override via
``app.core.config.settings`` if needed.

Slice 7 (gestor-jornadas.md §6.2): ``/escrutinio`` also surfaces the
per-partido live ``PartidoEstimacion`` list (``escrutinio.estimaciones``).
The DB-first path reads the stored JSONB as-is. The upstream-fallback
path builds the list from the live ``PartidoLosilla`` entries so a
calibration in flight (DB has no row yet) still serves callers the live
percentages.
"""
from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path as PathlibPath

import httpx
from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.eduardolosilla import EduardoLosillaClient
from app.core.config import settings
from app.db.models import (
    Jornada,
    JornadaComentario,
    JornadaEscrutinio,
)
from app.db.session import AsyncSessionLocal
from app.schemas.losilla import (
    ComentariosResponse,
    EscrutinioOficial,
    EscrutinioResponse,
    JornadaLosilla,
    PartidoEstimacion,
    PartidoLosilla,
)

LOG = logging.getLogger("api.losilla")
router = APIRouter(prefix="/api/v1/losilla", tags=["losilla"])


# ---------- Client singleton ----------

_client: EduardoLosillaClient | None = None


def _get_client() -> EduardoLosillaClient:
    global _client
    if _client is None:
        cache_dir = PathlibPath("/tmp/la-goyniela-losilla-http")
        _client = EduardoLosillaClient(cache_dir=cache_dir)
    return _client


def get_losilla_client() -> EduardoLosillaClient:
    """FastAPI dependency. Returns the shared client (lazy init)."""
    return _get_client()


async def _close_client() -> None:
    global _client
    if _client is not None:
        await _client.__aexit__(None, None, None)
        _client = None


# ---------- DB-first helpers ----------


async def _get_session() -> AsyncSession:
    """Open an ``AsyncSession`` for the DB-first lookups.

    Kept as a tiny helper (rather than a Depends) so the FastAPI handler
    signature stays simple and tests can override the session factory
    without dependency_overrides gymnastics.
    """
    return AsyncSessionLocal()


async def _find_jornada_id(
    temporada: int, jornada: int,
) -> int | None:
    """Return ``jornada.id`` for (temporada, numero) or None if missing or
    soft-deleted.

    Soft-delete filter (slice 1 of the superadmin-panel sprint): a
    soft-deleted jornada is treated as not-found by the public read
    endpoints. Admin-only endpoints (slice 2) may surface a different
    status.
    """
    async with AsyncSessionLocal() as session:
        return await session.scalar(
            select(Jornada.id).where(
                Jornada.temporada == temporada,
                Jornada.numero == jornada,
                Jornada.deleted_at.is_(None),
            )
        )


async def _load_escrutinio_from_db(
    jornada_id: int,
) -> EscrutinioOficial | None:
    """Read the persisted ``EscrutinioOficial`` JSONB back into the schema.

    Returns None if no row exists OR if the JSON cannot be deserialised
    (we surface the bad-row case as "no row" rather than crash — the
    audit trail in ``fetch_session.source_stats`` is the source of truth
    for diagnosing it).
    """
    async with AsyncSessionLocal() as session:
        row = await session.scalar(
            select(JornadaEscrutinio).where(
                JornadaEscrutinio.jornada_id == jornada_id,
            )
        )
    if row is None:
        return None
    try:
        return EscrutinioOficial.model_validate(row.payload)
    except Exception as exc:  # noqa: BLE001 — see docstring
        LOG.warning(
            "jornada_escrutinio.payload for jornada_id=%s is invalid: %s",
            jornada_id, exc,
        )
        return None


async def _load_comentarios_from_db(
    jornada_id: int,
) -> list[PartidoLosilla] | None:
    """Read the persisted comentario rows back into ``PartidoLosilla`` list.

    Returns ``None`` if the jornada has NO persisted comentario rows
    (different from "has rows but empty" — Losilla always has at least
    some editorials for a closed jornada, so None means "not ingested").
    The caller treats None as "fall back to upstream".

    We build the minimal PartidoLosilla shape: ``orden``, ``local``,
    ``visitante``, ``comentario``. The other fields (num, local_corto,
    escudo, dia, hora, etc.) are intentionally None because the DB does
    not carry them — only Losilla's HTML render does, and only the live
    upstream view keeps them up-to-date.
    """
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(
            select(JornadaComentario)
            .where(JornadaComentario.jornada_id == jornada_id)
            .order_by(JornadaComentario.partido_orden)
        )).scalars().all()

    if not rows:
        return None

    return [
        PartidoLosilla(
            orden=r.partido_orden,
            num=r.partido_orden,
            local=r.local_raw,
            visitante=r.visitante_raw,
            comentario=r.comentario,
        )
        for r in rows
    ]


# ---------- Endpoints ----------


@router.get(
    "/jornada/{jornada}",
    response_model=JornadaLosilla,
    summary="Full Losilla payload for one jornada (always live)",
    description=(
        "**Always live** — this endpoint never reads from the DB. It "
        "returns the live SSR numbers + the official escrutinio block "
        "(if the page rendered one) + the 15 matches with editorial "
        "comentarios. Use it when you want the in-flight view. For "
        "persisted closed-jornada data use ``/escrutinio`` and "
        "``/comentarios`` (DB-first). "
        "``jornada`` is the requested number; ``ssr_shipped_jornada`` is "
        "what the upstream state JSON actually carried (may differ — the "
        "page is a live view, see the ``warning`` field)."
    ),
    responses={
        404: {"description": "Upstream did not return any jornada key"},
        502: {"description": "Upstream request failed"},
    },
)
async def get_jornada(
    jornada: int = Path(..., ge=1, description="Jornada number (1-based)"),
    temporada: int = Query(
        2027, ge=1998, le=2100,
        description="Season end-year (e.g. 2027 for 2026/27)"),
    force_fresh: bool = Query(
        False, description="Bypass the in-memory cache for this call"),
    client: EduardoLosillaClient = Depends(get_losilla_client),
) -> JornadaLosilla:
    try:
        return await client.get_jornada(
            jornada, temporada, force_fresh=force_fresh)
    except httpx.HTTPStatusError as exc:
        LOG.warning("upstream %s for jornada=%s temporada=%s: %s",
                    exc.response.status_code, jornada, temporada, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(f"Upstream returned {exc.response.status_code} for "
                    f"jornada={jornada} temporada={temporada}")) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Upstream request failed: {type(exc).__name__}: {exc}",
        ) from exc


@router.get(
    "/jornada/{jornada}/escrutinio",
    response_model=EscrutinioResponse,
    summary="Official escrutinio block (DB-first)",
    description=(
        "DB-first read of the persisted ``EscrutinioOficial``. Returns "
        "``source='db'`` when the row was ingested via the Losilla "
        "ingest CLI (``app.scripts.losilla_ingest``); falls back to "
        "``source='upstream'`` when no row exists yet (the upstream "
        "fetch still happens). "
        "Returns 204 when even the live upstream page does not render "
        "an escrutinio block (typically: the jornada is in-flight). "
        "**Slice 7 (gestor-jornadas.md §6.2):** the response embeds "
        "``escrutinio.estimaciones: PartidoEstimacion[]`` — one entry "
        "per partido (1..14 1X2 + 15 Pleno al 15), captured from the "
        "SSR probabilidades block at ingest time. The DB-first path "
        "serves what was persisted; the upstream-fallback path builds "
        "the list from the live ``PartidoLosilla`` rows so an in-flight "
        "calibracion is observable to the API too."
    ),
    responses={
        200: {"description": "Escrutinio block found (DB or upstream)"},
        204: {"description": "No escrutinio block on the live page"},
        404: {"description": "Jornada not in DB and upstream failed"},
        502: {"description": "Upstream request failed"},
    },
)
async def get_escrutinio(
    response: Response,
    jornada: int = Path(..., ge=1),
    temporada: int = Query(2027, ge=1998, le=2100),
    client: EduardoLosillaClient = Depends(get_losilla_client),
) -> EscrutinioResponse:
    jornada_id = await _find_jornada_id(temporada, jornada)

    if jornada_id is not None:
        db_e = await _load_escrutinio_from_db(jornada_id)
        if db_e is not None:
            response.headers["X-Losilla-Source"] = "db"
            return EscrutinioResponse(source="db", escrutinio=db_e)

    # DB miss (or invalid payload). Fall back to upstream.
    payload = await _fetch_upstream_with_errors(client, jornada, temporada)
    if payload.escrutinio_oficial is None:
        raise HTTPException(
            status_code=status.HTTP_204_NO_CONTENT,
            detail=("Upstream page did not render an escrutinio block; "
                    "either the jornada is in-flight or upstream changed."),
        )
    # Slice 7: surface live estimations even on the upstream path. The
    # client populates ``uno_porcentaje``/``x_porcentaje``/
    # ``dos_porcentaje`` (1..14) and ``pleno_local``/``pleno_visitante``
    # (15) on ``PartidoLosilla``; we delegate to the schema classmethod
    # so the persist-side and live-side shape stay in lockstep.
    estimaciones = PartidoEstimacion.from_partidos(
        payload.partidos,
        fetched_at=dt.datetime.now(dt.timezone.utc),
    )
    upstream_live = payload.escrutinio_oficial.model_copy(
        update={"estimaciones": estimaciones},
    )
    response.headers["X-Losilla-Source"] = "upstream"
    return EscrutinioResponse(
        source="upstream", escrutinio=upstream_live,
    )


@router.get(
    "/jornada/{jornada}/comentarios",
    response_model=ComentariosResponse,
    summary="Editorial comentarios (DB-first)",
    description=(
        "DB-first read of the persisted 15 ``PartidoLosilla`` rows with "
        "editorial. Devuelve los 15 comentarios (gestor-jornadas.md "
        "§7.3): partidos 1..14 carry the upstream editorial; partido 15 "
        "carries the synthetic Pleno al 15 string (empty string when "
        "upstream shipped no percentages). Returns ``source='db'`` when "
        "rows were ingested via the Losilla ingest CLI; falls back to "
        "``source='upstream'`` (filtered to matches with non-null "
        "comentario — i.e. the 14 clubs with editorial) when the DB has "
        "no rows yet."
    ),
    responses={
        200: {"description": "List of partidos with their comentario"},
        502: {"description": "Upstream request failed"},
    },
)
async def get_comentarios(
    response: Response,
    jornada: int = Path(..., ge=1),
    temporada: int = Query(2027, ge=1998, le=2100),
    client: EduardoLosillaClient = Depends(get_losilla_client),
) -> ComentariosResponse:
    jornada_id = await _find_jornada_id(temporada, jornada)

    if jornada_id is not None:
        db_list = await _load_comentarios_from_db(jornada_id)
        if db_list is not None:
            response.headers["X-Losilla-Source"] = "db"
            return ComentariosResponse(source="db", comentarios=db_list)

    # DB miss. Fall back to upstream.
    payload = await _fetch_upstream_with_errors(client, jornada, temporada)
    upstream_list = [p for p in payload.partidos if p.comentario]
    response.headers["X-Losilla-Source"] = "upstream"
    return ComentariosResponse(source="upstream", comentarios=upstream_list)


async def _fetch_upstream_with_errors(
    client: EduardoLosillaClient, jornada: int, temporada: int,
) -> JornadaLosilla:
    """Wrap ``client.get_jornada`` with the upstream-error → HTTP mapping.

    The live ``get_jornada`` endpoint already does this inline; the DB-first
    endpoints need the same shape, so we factor it out here.
    """
    try:
        return await client.get_jornada(jornada, temporada)
    except httpx.HTTPStatusError as exc:
        LOG.warning("upstream %s for jornada=%s temporada=%s: %s",
                    exc.response.status_code, jornada, temporada, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(f"Upstream returned {exc.response.status_code} for "
                    f"jornada={jornada} temporada={temporada}")) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Upstream request failed: {type(exc).__name__}: {exc}",
        ) from exc


__all__ = ["router", "get_losilla_client", "_close_client"]
