"""Bootstrap CLI: fetch Losilla escrutinio + editorial comentarios for ONE
closed jornada and persist them to the DB.

End state::

    python -m app.scripts.losilla_ingest --jornada 8 --temporada 2026

Idempotent. Re-running inserts no duplicates (the unique constraints on
``jornada_escrutinio`` and ``jornada_comentario`` are the safety net; we
also explicitly SELECT first so we can report "row existed" separately
from "row inserted").

Prerequisites
-------------
- The ``jornada`` row must already exist in the DB (run the Quiniwin
  bootstrap first; this CLI does NOT create the jornada).
- The ``source`` table must have a row with ``code='losilla'`` (seeded
  by ``app/scripts/load_team_catalog.py``).

What this CLI does
------------------
1. Look up ``jornada`` by ``(temporada, numero)``. Error out with a clear
   message if missing.
2. Look up ``source`` by ``code='losilla'``. Error out loudly if missing.
3. Fetch the full payload from ``EduardoLosillaClient.get_jornada()``.
4. UPSERT the ``jornada_escrutinio`` row: insert if absent, update
   ``payload`` (the full ``EscrutinioOficial`` Pydantic model dump) and
   ``fetched_at`` if present. Source + fetch_session_id are always set.
5. UPSERT each of the 15 ``jornada_comentario`` rows: insert if absent,
   update ``local_raw``/``visitante_raw``/``comentario`` if they actually
   changed. Partido 15 (Pleno al 15) has no editorial upstream; its
   ``comentario`` is synthesized from the Pleno al 15 percentages
   (``porc_15L_*`` / ``porc_15V_*``) carried on the ``PartidoLosilla``
   model — see :func:`_synthesize_pleno_15_comment`. If both sides are
   fully absent upstream, the synthetic string is the empty string ``""``
   (the column is NOT NULL but allows empty).
6. Wrap the whole thing in ``fetch_and_record`` so the audit trail
   (``fetch_session.source_stats``) records the per-source outcome.

Usage::

    python -m app.scripts.losilla_ingest --jornada 8 --temporada 2026
    python -m app.scripts.losilla_ingest --jornada 8 --temporada 2026 --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.eduardolosilla import EduardoLosillaClient
from app.db.models import (
    Jornada,
    JornadaComentario,
    JornadaEscrutinio,
    Source,
)
from app.db.session import AsyncSessionLocal
from app.scheduler.jobs import fetch_and_record
from app.schemas.losilla import (
    PartidoEstimacion,
    PartidoLosilla,
)


LOG = logging.getLogger("losilla_ingest")

# Hardcoded companion to SOURCE_CODE_QUINIWIN in app.scheduler.jobs. Matches
# the code we resolve against ``source.code`` to find the audit FK.
SOURCE_CODE_LOSILLA = "losilla"


# ---------- Summary dataclass ----------


@dataclass
class IngestSummary:
    """Per-run summary counters for the CLI to print at the end."""
    jornada: int
    temporada: int
    escrutinio_inserted: bool
    escrutinio_existed: bool
    comentarios_inserted: int = 0
    comentarios_updated: int = 0
    comentarios_existing: int = 0
    network_failures: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [
            f"=== Losilla ingest summary (jornada={self.jornada} "
            f"temporada={self.temporada}) ===",
            f"  escrutinio inserted:    {self.escrutinio_inserted}",
            f"  escrutinio existed:     {self.escrutinio_existed}",
            f"  comentarios inserted:   {self.comentarios_inserted}",
            f"  comentarios updated:    {self.comentarios_updated}",
            f"  comentarios existing:   {self.comentarios_existing}",
        ]
        if self.network_failures:
            lines.append(f"  network failures:     {self.network_failures}")
        return "\n".join(lines)


# ---------- Core upsert helpers ----------


# Order matters: 0, 1, 2, M — same as the Quiniela Pleno al 15 boleto
# layout. The synthetic comment string is rendered in this order so it
# matches the upstream boleto display.
_PLENO_KEYS: tuple[str, ...] = ("0", "1", "2", "M")
# Middle-dot separator (U+00B7) between Local and Visita sides. Matches
# the format in design doc §6.1 of gestor-jornadas.md.
_PLENO_SIDE_SEP = " \u00b7 "


def _format_pleno_side(
    splits: dict[str, int | float | None] | None,
) -> str:
    """Render one side of Pleno al 15 as ``"0=6%, 1=16%, 2=23%, M=55%"``.

    Missing or ``None`` keys are omitted (we never fabricate a value).
    Non-int values are rounded to the nearest whole percent. Returns
    the empty string when ``splits`` is None or contains no usable keys.
    """
    if not splits:
        return ""
    parts: list[str] = []
    for k in _PLENO_KEYS:
        v = splits.get(k)
        if v is None:
            continue
        try:
            parts.append(f"{k}={round(float(v))}%")
        except (TypeError, ValueError):
            continue
    return ", ".join(parts)


def _synthesize_pleno_15_comment(
    pleno_local: dict[str, int | float | None] | None,
    pleno_visitante: dict[str, int | float | None] | None,
) -> str:
    """Build the partido 15 ``comentario`` from Pleno al 15 percentages.

    Format (``gestor-jornadas.md`` §6.1)::

        "Local: 0=6%, 1=16%, 2=23%, M=55% · Visita: 0=51%, 1=34%, 2=12%, M=3%"

    Sides are joined with middle-dot ``·`` (U+00B7). Local first, then
    Visita. A side with no usable keys is omitted entirely. If BOTH
    sides are fully absent upstream, returns the empty string ``""``
    (the column is NOT NULL; empty is the safe default — we never write
    NULL because the schema doesn't allow it).

    This function is pure and has no I/O; it lives next to the persist
    path so it's trivially unit-testable without going through full
    ingest.
    """
    local_str = _format_pleno_side(pleno_local)
    visit_str = _format_pleno_side(pleno_visitante)
    if not local_str and not visit_str:
        return ""
    if local_str and visit_str:
        return (
            f"Local: {local_str}"
            f"{_PLENO_SIDE_SEP}"
            f"Visita: {visit_str}"
        )
    if local_str:
        return f"Local: {local_str}"
    return f"Visita: {visit_str}"


async def _resolve_jornada(
    session: AsyncSession, temporada: int, jornada: int,
) -> Jornada | None:
    """Find the ``jornada`` row by (temporada, numero). None if missing."""
    return await session.scalar(
        select(Jornada).where(
            Jornada.temporada == temporada,
            Jornada.numero == jornada,
        )
    )


async def _resolve_source_id(
    session: AsyncSession, code: str,
) -> int | None:
    """Find ``source.id`` by ``code``. None if the catalog row is missing."""
    return await session.scalar(
        select(Source.id).where(Source.code == code),
    )


async def _upsert_escrutinio(
    session: AsyncSession,
    *,
    jornada_id: int,
    source_id: int,
    fetch_session_id: "object",  # uuid.UUID — kept untyped to avoid the import
    payload: dict,
    partidos: list[PartidoLosilla] | None = None,
) -> tuple[bool, bool]:
    """Upsert the ``jornada_escrutinio`` row.

    Returns ``(inserted, existed)``:

    - ``inserted=True`` — we created a new row in this call.
    - ``existed=True``  — the row was already there; we updated its payload
      (Losilla may have re-rendered since the first ingest, e.g. corrected
      a number). Both flags can be ``True`` at once.

    When ``partidos`` is provided, slice 7 (gestor-jornadas.md §6.2)
    builds a ``PartidoEstimacion`` per partido and merges it into the
    payload under ``payload["estimaciones"]``. On a re-ingest we reuse the
    existing ``fetched_at`` from the first stored estimacion so the JSONB
    is byte-identical across re-runs (idempotency contract — see
    ``test_ingest_is_idempotent``).
    """
    pre = await session.scalar(
        select(JornadaEscrutinio).where(
            JornadaEscrutinio.jornada_id == jornada_id,
        )
    )
    existed = pre is not None

    if partidos:
        # Determine the canonical ``fetched_at`` for this batch.
        # - On a brand-new row: capture ``now``.
        # - On a re-ingest: reuse the ``fetched_at`` from the existing
        #   stored estimations so the JSONB payload stays byte-identical.
        fetched_at = dt.datetime.now(dt.timezone.utc)
        if existed and isinstance(pre.payload, dict):
            existing_est = pre.payload.get("estimaciones") or []
            if existing_est and isinstance(existing_est[0], dict):
                raw_ts = existing_est[0].get("fetched_at")
                if raw_ts:
                    try:
                        # Pydantic serialises aware datetimes as ISO with
                        # ``+00:00``; tolerate either ``...Z`` or ``+00:00``.
                        ts = raw_ts.replace("Z", "+00:00") if isinstance(
                            raw_ts, str) else raw_ts
                        fetched_at = dt.datetime.fromisoformat(ts)
                    except (TypeError, ValueError):
                        # Fall back to ``now`` on a malformed timestamp.
                        pass
        estimaciones = PartidoEstimacion.from_partidos(
            partidos, fetched_at=fetched_at,
        )
        payload["estimaciones"] = [
            e.model_dump(mode="json") for e in estimaciones
        ]

    stmt = pg_insert(JornadaEscrutinio).values(
        jornada_id=jornada_id,
        source_id=source_id,
        fetch_session_id=fetch_session_id,
        payload=payload,
    ).on_conflict_do_update(
        index_elements=[JornadaEscrutinio.jornada_id],
        set_={
            "source_id": source_id,
            "fetch_session_id": fetch_session_id,
            "payload": payload,
            # fetched_at is left to the server default for inserts and to
            # its own default for updates. We do NOT bump fetched_at on
            # an "already existed" path — the original timestamp is the
            # moment we first saw this escrutinio. (Losilla re-rendering
            # is rare; if it happens, a fresh CLI run with a new fetch
            # would still bump the row via the SET clause above.)
        },
    )
    await session.execute(stmt)

    return (not existed), existed


async def _upsert_comentarios(
    session: AsyncSession,
    *,
    jornada_id: int,
    source_id: int,
    fetch_session_id: "object",
    rows: list[dict],
) -> tuple[int, int, int]:
    """Upsert 1..15 ``jornada_comentario`` rows. Partido 15's comentario
    is synthesized from ``porc_15L_*`` / ``porc_15V_*`` when missing
    upstream.

    ``rows`` is the list of dicts we want to persist — one per partido
    1..15 (partidos 1..14 carry the upstream editorial; partido 15's
    comentario is generated by :func:`_synthesize_pleno_15_comment`).

    Returns ``(inserted, updated, existing_unchanged)``. Idempotent:

    - ``inserted`` — rows that did not exist before.
    - ``updated`` — rows that existed and whose comentario/local_raw/
      visitante_raw actually changed.
    - ``existing_unchanged`` — rows that existed and were identical.

    Implementation note: we don't rely on ``cursor.rowcount`` because
    psycopg returns ``0`` for some ``INSERT ... ON CONFLICT DO NOTHING``
    cases even when the row was actually inserted (it's an
    "implementation-defined" behaviour on conflict — psycopg's API
    surface can't tell us whether the row was inserted vs. already
    existed, but the test suite needs the accurate count). So we do
    the classic pre/post SELECT-count diff the way ``ingest_quiniwin``
    does for ``composicion``: read the count before, run the INSERT,
    read the count after, diff.
    """
    if not rows:
        return 0, 0, 0

    # Pre-fetch existing rows for this jornada so we can diff against
    # the incoming payload and report "updated" vs "unchanged".
    pre_rows = (await session.execute(
        select(JornadaComentario).where(
            JornadaComentario.jornada_id == jornada_id,
        )
    )).scalars().all()
    pre_by_orden = {r.partido_orden: r for r in pre_rows}
    pre_count = len(pre_rows)

    # Apply the UPSERTs. For partidos whose content actually changed,
    # we mutate the existing ORM row (no ON CONFLICT needed; the row is
    # already there). For partidos that didn't exist before, we INSERT
    # with ON CONFLICT DO NOTHING as a safety net against a racing writer.
    updated = 0
    unchanged = 0
    rows_to_insert: list[dict] = []

    for row in rows:
        orden = row["partido_orden"]
        new_comentario = row["comentario"]
        new_local_raw = row["local_raw"]
        new_visitante_raw = row["visitante_raw"]
        existing = pre_by_orden.get(orden)

        if existing is None:
            rows_to_insert.append({
                "jornada_id": jornada_id,
                "source_id": source_id,
                "partido_orden": orden,
                "local_raw": new_local_raw,
                "visitante_raw": new_visitante_raw,
                "comentario": new_comentario,
                "fetch_session_id": fetch_session_id,
            })
        else:
            if (existing.local_raw != new_local_raw
                    or existing.visitante_raw != new_visitante_raw
                    or existing.comentario != new_comentario):
                existing.local_raw = new_local_raw
                existing.visitante_raw = new_visitante_raw
                existing.comentario = new_comentario
                existing.source_id = source_id
                existing.fetch_session_id = fetch_session_id
                updated += 1
            else:
                unchanged += 1

    # Bulk insert the new ones. ON CONFLICT DO NOTHING is a safety net
    # against a concurrent writer; the pre_count diff below catches the
    # actual count of rows we just inserted.
    if rows_to_insert:
        stmt = pg_insert(JornadaComentario).values(
            rows_to_insert,
        ).on_conflict_do_nothing(
            index_elements=[
                JornadaComentario.jornada_id,
                JornadaComentario.partido_orden,
            ],
        )
        await session.execute(stmt)

    # Diff pre vs post to get the inserted count. We trust the unique
    # constraint to catch any racing writer; if our count goes up by
    # less than the number of rows we tried to insert, the rest were
    # silently skipped by ON CONFLICT — those are NOT counted as
    # "inserted" (they were someone else's writes, not ours).
    if rows_to_insert:
        post_count = (await session.scalar(
            select(func.count()).select_from(JornadaComentario).where(
                JornadaComentario.jornada_id == jornada_id,
            )
        )) or 0
        inserted = max(post_count - pre_count, 0)
        # If we tried to insert N but the diff is less than N, the
        # remainder are race-condition duplicates. We can't tell those
        # apart from "rows we wanted to insert and someone else wrote",
        # but they still represent the jornada's persisted state, so
        # the safe accounting is: ``inserted = diff``, the rest fall
        # into the "unchanged" bucket (they have the same content we
        # wanted to write).
        if inserted < len(rows_to_insert):
            unchanged += len(rows_to_insert) - inserted
    else:
        inserted = 0

    return inserted, updated, unchanged


# ---------- Ingest driver ----------


def _build_source_stats(summary: IngestSummary) -> dict:
    """Build the JSONB payload we hand to ``fetch_and_record``.

    Kept separate so tests can introspect what got persisted under
    ``fetch_session.source_stats['losilla']``.
    """
    return {
        "losilla": {
            "status": "ok" if not summary.network_failures else "partial",
            "jornada": summary.jornada,
            "temporada": summary.temporada,
            "escrutinio_inserted": summary.escrutinio_inserted,
            "escrutinio_existed": summary.escrutinio_existed,
            "comentarios_inserted": summary.comentarios_inserted,
            "comentarios_updated": summary.comentarios_updated,
            "comentarios_existing": summary.comentarios_existing,
            "network_failures": list(summary.network_failures),
        },
    }


class IngestPreconditionError(RuntimeError):
    """Raised when a precondition (jornada row, source row) is missing.

    Caught by ``run_ingest`` and reflected in the summary; the CLI exits
    with a non-zero status so the operator sees the problem.
    """


async def run_ingest(
    jornada: int,
    temporada: int,
    *,
    dry_run: bool = False,
    client: EduardoLosillaClient | None = None,
) -> IngestSummary:
    """Drive the Losilla ingest end to end. Returns a summary for the caller.

    ``client`` is injected for tests; in production we build our own.

    Idempotency contract: re-running for the same ``(jornada, temporada)``
    MUST produce zero new ``jornada_escrutinio`` rows and zero new
    ``jornada_comentario`` rows. The escrutinio payload may be updated
    in place (we don't gate that on identity because Losilla can
    re-render corrected numbers).
    """
    summary = IngestSummary(
        jornada=jornada,
        temporada=temporada,
        escrutinio_inserted=False,
        escrutinio_existed=False,
    )

    if dry_run:
        LOG.info(
            "[DRY-RUN] would fetch temporada=%d jornada=%d from Losilla",
            temporada, jornada,
        )
        return summary

    own_client = client is None
    if client is None:
        client = EduardoLosillaClient()

    try:
        async with AsyncSessionLocal() as session:
            j_row = await _resolve_jornada(session, temporada, jornada)
            if j_row is None:
                raise IngestPreconditionError(
                    f"jornada J{jornada} of temporada={temporada} not in DB; "
                    "run Quiniwin ingest first "
                    "(`python -m app.scripts.ingest_quiniwin "
                    f"--temporada {temporada} --jornadas {jornada}`)."
                )
            source_id = await _resolve_source_id(session, SOURCE_CODE_LOSILLA)
            if source_id is None:
                raise IngestPreconditionError(
                    f"source row with code={SOURCE_CODE_LOSILLA!r} not in DB; "
                    "run `python -m app.scripts.load_team_catalog` to seed "
                    "the source catalog."
                )

        # Build the inner coroutine so fetch_and_record can wrap it in a
        # fetch_session row. We capture the variables we need by closure.
        jornada_id_ref = j_row.id

        async def _do_persist() -> dict:
            """Inner coroutine: fetch upstream + persist. Returns source_stats."""
            try:
                payload = await client.get_jornada(jornada, temporada)
            except Exception as exc:  # noqa: BLE001 — record-and-bubble
                LOG.warning(
                    "Losilla fetch failed for temporada=%d jornada=%d: %s: %s",
                    temporada, jornada, type(exc).__name__, exc,
                )
                summary.network_failures.append(
                    f"{type(exc).__name__}: {exc}")
                # Persist the failure but do NOT commit escrutinio/comentario.
                return _build_source_stats(summary)

            escrutinio_payload = (
                payload.escrutinio_oficial.model_dump(mode="json")
                if payload.escrutinio_oficial is not None else None
            )
            # Persist the live SSR block too (oct 2026 fix): the price
            # resolver reads ``precio_apuesta_eur`` from
            # ``recaudacion_bote_live.coste_apuesta_eur``, which is NOT
            # part of the canonical escrutinio block. We splice the live
            # block into the same JSONB column so a single read picks
            # up both pieces.
            if escrutinio_payload is not None and payload.recaudacion_bote_live is not None:
                escrutinio_payload["recaudacion_bote_live"] = (
                    payload.recaudacion_bote_live.model_dump(mode="json")
                )
            # Slice 7 (gestor-jornadas.md §6.2): the per-partido live
            # SSR percentages (1X2 for 1..14, Pleno splits for 15) are
            # captured into ``payload.estimaciones`` by
            # :func:`_upsert_escrutinio` below — see the ``partidos``
            # argument. Doing it inside the upsert lets us reuse the
            # original ``fetched_at`` on re-ingest (idempotency).
            comentario_rows = []
            for p in payload.partidos:
                # Partido 15 (Pleno al 15) has no editorial upstream; we
                # synthesize its comentario from the Pleno al 15 splits
                # attached to the model (``p.pleno_local`` /
                # ``p.pleno_visitante``). Always persist a row for it
                # (gestor-jornadas.md §6.1) — empty string when both
                # sides are fully absent upstream.
                if p.orden == 15:
                    sint = _synthesize_pleno_15_comment(
                        p.pleno_local, p.pleno_visitante,
                    )
                    comentario_rows.append({
                        "partido_orden": p.orden,
                        "local_raw": p.local,
                        "visitante_raw": p.visitante,
                        "comentario": sint,
                    })
                    continue
                if p.comentario is None:
                    continue
                if not (1 <= p.orden <= 14):
                    continue
                comentario_rows.append({
                    "partido_orden": p.orden,
                    "local_raw": p.local,
                    "visitante_raw": p.visitante,
                    "comentario": p.comentario,
                })

            async with AsyncSessionLocal() as session:
                # The fetch_session_id we return is set later by
                # fetch_and_record; we use a placeholder UUID here and
                # patch it after we know the real id. To keep things
                # simple and atomic we let fetch_and_record pass us the
                # real id via a wrapper — but that breaks the helper's
                # shape. Instead we open a sub-tx here with our own id
                # and update later. Simpler: generate our own id and let
                # fetch_and_record keep its own audit row id separate.
                #
                # fetch_and_record's fetch_session row is the per-batch
                # audit; the jornada_escrutinio.fetch_session_id links
                # each persisted row back to the batch that wrote it.
                # Both ids are valid UUIDs; they will differ. This is
                # acceptable for v1 — a future task can pass the batch
                # id down if cross-referencing is needed.
                from uuid import uuid4
                inner_fs_id = uuid4()

                if escrutinio_payload is not None:
                    inserted, existed = await _upsert_escrutinio(
                        session,
                        jornada_id=jornada_id_ref,
                        source_id=source_id,
                        fetch_session_id=inner_fs_id,
                        payload=escrutinio_payload,
                        partidos=payload.partidos,
                    )
                    summary.escrutinio_inserted = inserted
                    summary.escrutinio_existed = existed

                ins, upd, exist = await _upsert_comentarios(
                    session,
                    jornada_id=jornada_id_ref,
                    source_id=source_id,
                    fetch_session_id=inner_fs_id,
                    rows=comentario_rows,
                )
                summary.comentarios_inserted = ins
                summary.comentarios_updated = upd
                summary.comentarios_existing = exist

                await session.commit()

            return _build_source_stats(summary)

        final_stats = await fetch_and_record(
            AsyncSessionLocal,
            SOURCE_CODE_LOSILLA,
            _do_persist,
            notes=(
                f"temporada={temporada} jornada={jornada} "
                f"escrutinio_inserted={summary.escrutinio_inserted} "
                f"comentarios_inserted={summary.comentarios_inserted}"
            ),
        )
        LOG.debug("fetch_and_record returned %s", final_stats)
    finally:
        if own_client:
            await client.close()

    return summary


# ---------- Convenience: count existing rows (used by tests + dry-run) ----------


async def count_escrutinio_for(jornada_id: int) -> int:
    """How many ``jornada_escrutinio`` rows exist for the given jornada id."""
    async with AsyncSessionLocal() as session:
        return await session.scalar(
            select(func.count()).select_from(JornadaEscrutinio).where(
                JornadaEscrutinio.jornada_id == jornada_id,
            )
        ) or 0


async def count_comentarios_for(jornada_id: int) -> int:
    """How many ``jornada_comentario`` rows exist for the given jornada id."""
    async with AsyncSessionLocal() as session:
        return await session.scalar(
            select(func.count()).select_from(JornadaComentario).where(
                JornadaComentario.jornada_id == jornada_id,
            )
        ) or 0


# ---------- CLI entrypoint ----------


def cli() -> None:
    p = argparse.ArgumentParser(
        description=(
            "Fetch Losilla escrutinio + comentarios for ONE closed jornada "
            "and persist them to the DB. Idempotent."
        ),
    )
    p.add_argument(
        "--jornada",
        type=int,
        required=True,
        help="Jornada number (1-based).",
    )
    p.add_argument(
        "--temporada",
        type=int,
        required=True,
        help="Temporada end year (e.g. 2026 = season 2026/2027).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print plan, hit no network, change no DB rows.",
    )
    args = p.parse_args()

    try:
        summary = asyncio.run(run_ingest(
            args.jornada, args.temporada, dry_run=args.dry_run,
        ))
    except IngestPreconditionError as exc:
        LOG.error("%s", exc)
        raise SystemExit(2) from exc

    print(summary.render())


__all__ = [
    "IngestSummary",
    "IngestPreconditionError",
    "SOURCE_CODE_LOSILLA",
    "run_ingest",
    "count_escrutinio_for",
    "count_comentarios_for",
    "_synthesize_pleno_15_comment",
    "_format_pleno_side",
]


if __name__ == "__main__":
    cli()
