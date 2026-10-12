"""Scheduled jobs — the actual work the scheduler triggers.

Public functions:

- :func:`ingest_today_quiniwin_job` — the cron-driven entrypoint. It
  discovers what jornadas have already been ingested for the active
  temporada, then walks forward ``[max+1 .. max+CAP]`` and runs
  :func:`app.scripts.ingest_quiniwin.run_ingest` for each jornada that
  is not yet known. One bad jornada (404 / parse failure) does NOT abort
  the others; the failure is recorded in ``source_stats`` per jornada.
  After the Quiniwin pass, also chains :func:`_run_losilla_for_inserted_jornadas`
  for any jornada that was just inserted, so the cron does the same
  "full job" the manual admin trigger does (Quiniwin data + Losilla
  comentarios + estimaciones).

- :func:`losilla_persist_recent_job` — Monday morning cron. For each
  jornada in the active temporada that does NOT yet have an
  ``jornada_escrutinio`` row, runs :func:`app.scripts.losilla_ingest.run_ingest`
  to fetch escrutinio + 14 comentarios from Losilla and persist them.
  Idempotent and per-jornada tolerant: one failure does not abort the
  others.

- :func:`losilla_poll_recent_closed_job` — daily 23:00 cron. For each
  jornada in the active temporada where ``kickoff_last_match_at + 3h <
  now`` AND which does NOT yet have a ``jornada_escrutinio`` row, runs
  :func:`app.scripts.losilla_ingest.run_ingest`. Closes the gap between
  Tuesday and Sunday so a jornada that closes on, say, Wednesday does
  not sit ``closed`` until the following Monday's weekly backstop. The
  weekly job continues to act as a backstop for extreme cases
  (upstream downtime, transient errors).

- :func:`fetch_and_record` — utility used by the daily job to wrap a
  coroutine in a ``fetch_session`` row (start/finish timestamps + a
  JSONB ``source_stats`` payload). Designed so future jobs (escrutinio,
  comentarios, ...) can reuse the same audit-trail primitive.

Conventions:

- Logging is stdlib ``logging`` (matches the rest of the project — no
  structlog here; ``structlog`` is wired up at the API layer in a future
  task).
- All network/parse failures inside the job are caught and recorded
  in the per-jornada dict. The job itself never raises unless the
  ``fetch_session`` row cannot be persisted (which is fatal — we have no
  audit trail).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clients.quiniwin import QuiniwinClient
from app.db.models import FetchSession, Jornada, JornadaEscrutinio, Source
from app.db.session import AsyncSessionLocal
from app.scripts.ingest_quiniwin import run_ingest
# ``losilla_run_ingest`` is imported lazily inside the job function to
# avoid a circular import (losilla_ingest.py imports ``fetch_and_record``
# from this module).


LOG = logging.getLogger("scheduler.jobs")


# Source code we look up in the ``source`` table when persisting
# ``fetch_session.source_id``. Matches the convention used by the
# ingest pipeline diagrams in ``docs/proyecto/scheduler.md``.
SOURCE_CODE_QUINIWIN = "quiniwin"
SOURCE_CODE_LOSILLA = "losilla"


# ---------- fetch_session audit primitive ----------


async def _resolve_source_id(
    session: AsyncSession, source_code: str,
) -> int | None:
    """Look up ``source.id`` from ``source.code``.

    Returns None if the row does not exist (the FK is nullable; we want to
    keep the audit row even when the source catalog has not been loaded).
    """
    return await session.scalar(
        select(Source.id).where(Source.code == source_code),
    )


async def fetch_and_record(
    session_factory: async_sessionmaker[AsyncSession],
    source_code: str,
    coroutine_factory: Callable[[], Awaitable[dict[str, Any]]],
    *,
    notes: str | None = None,
) -> dict[str, Any]:
    """Run a job, wrapping it in a single ``fetch_session`` row.

    Parameters
    ----------
    session_factory:
        Async sessionmaker used both to insert the fetch_session row and
        (optionally) to run the wrapped coroutine. The wrapped coroutine
        is responsible for its own DB work — we only write the audit row.
    source_code:
        Label persisted as ``fetch_session.source_id`` (resolved via
        ``source.code``).
    coroutine_factory:
        Zero-arg callable returning the coroutine that does the actual
        work. We invoke it inside a ``try`` so the coroutine raises
        naturally; we still record the ``fetch_session`` row in
        ``finally`` so the audit trail is never lost.
    notes:
        Optional free-text note persisted on ``fetch_session.notes``.

    Returns
    -------
    dict
        The ``source_stats`` payload returned by the coroutine (or
        ``{"error": ...}`` if it raised). Useful for tests and for
        chaining in a future orchestrator.
    """
    fetch_session_id = uuid4()
    started_at = datetime.now(tz=timezone.utc)
    stats: dict[str, Any] = {}
    error: str | None = None

    async with session_factory() as session:
        source_id = await _resolve_source_id(session, source_code)
        # Insert the fetch_session row up-front so the audit trail exists
        # even if the wrapped coroutine crashes before recording its own
        # stats.
        session.add(FetchSession(
            id=fetch_session_id,
            source_id=source_id,
            started_at=started_at,
            finished_at=None,
            source_stats=None,
            notes=notes,
        ))
        try:
            await session.commit()
        except SQLAlchemyError:
            LOG.exception(
                "Could not insert fetch_session row id=%s; aborting job",
                fetch_session_id,
            )
            raise

    try:
        stats = await coroutine_factory()
        if not isinstance(stats, dict):
            # Defensive: callers must return a JSON-serialisable dict.
            stats = {"raw_return": repr(stats)}
    except Exception as exc:  # noqa: BLE001 — record-and-rethrow
        error = f"{type(exc).__name__}: {exc}"
        LOG.exception(
            "Job for source=%s crashed: %s", source_code, error,
        )
        stats = {"error": error}
    finally:
        finished_at = datetime.now(tz=timezone.utc)
        async with session_factory() as session:
            row = await session.get(FetchSession, fetch_session_id)
            if row is None:
                # Should not happen — we just inserted it.
                LOG.error(
                    "fetch_session id=%s vanished between insert and update",
                    fetch_session_id,
                )
                return stats
            row.finished_at = finished_at
            row.source_stats = stats
            if error is not None and notes is None:
                row.notes = error
            elif notes is not None and error is not None:
                row.notes = f"{notes} | {error}"
            await session.commit()

    return stats


# ---------- Helpers: discover what's "next" ----------


async def _max_known_jornada(
    session: AsyncSession, temporada: int,
) -> int | None:
    """Return the highest ``jornada.numero`` already persisted for the season.

    None if no jornada is known. Used by the daily job to decide where to
    resume ingest (we want forward-only — ``max + 1``, ``max + 2`` …).
    """
    return await session.scalar(
        select(Jornada.numero)
        .where(Jornada.temporada == temporada)
        .order_by(Jornada.numero.desc())
        .limit(1)
    )


def _discover_forward_window(
    max_known: int | None,
    *,
    range_start: int,
    range_end: int,
    forward_cap: int,
) -> list[int]:
    """Compute the jornadas to try in this tick.

    If we have never seen the season (max_known is None), we start at
    ``range_start``. Otherwise we start at ``max_known + 1`` and never
    exceed ``range_end`` or ``max_known + forward_cap``.

    The result is inclusive on both ends and at most ``forward_cap`` long.
    """
    if max_known is None:
        first = range_start
    else:
        first = max_known + 1

    if first > range_end:
        # Season already exhausted (or persisted through). Empty walk.
        return []

    last = min(range_end, first + forward_cap - 1)
    return list(range(first, last + 1))


# ---------- The cron job ----------


async def _run_one_jornada(
    client: QuiniwinClient,
    temporada: int,
    jornada: int,
    *,
    fetch_timeout_sec: float,
) -> dict[str, Any]:
    """Run :func:`run_ingest` for a single jornada and return a per-jornada summary.

    Returns a dict shaped ``{status, jornada, ...}`` so the daily job
    can fold it into ``source_stats`` without having to know the
    internals of :class:`IngestSummary`.

    We rebuild the client's timeout from ``fetch_timeout_sec`` (instead of
    letting APScheduler construct the client) so tests can inject a
    pre-configured client.
    """
    # Apply the timeout via the asyncio wait_for wrapper rather than
    # rebuilding the httpx client (which is owned by the caller). The
    # safety net here is for very slow networks — the underlying client
    # already has its own timeout.
    try:
        summary = await asyncio.wait_for(
            run_ingest(temporada, [jornada], client=client),
            timeout=fetch_timeout_sec + 30.0,
        )
    except asyncio.TimeoutError:
        LOG.warning(
            "ingest timeout for temporada=%d jornada=%d (>%.0fs)",
            temporada, jornada, fetch_timeout_sec + 30.0,
        )
        return {
            "status": "timeout",
            "jornada": jornada,
            "jornadas_inserted": 0,
            "jornadas_existing": 0,
            "composicion_inserted": 0,
            "composicion_existing": 0,
        }
    except Exception as exc:  # noqa: BLE001 — record per-jornada only
        LOG.warning(
            "ingest failure for temporada=%d jornada=%d: %s",
            temporada, jornada, exc,
        )
        return {
            "status": "error",
            "jornada": jornada,
            "error": f"{type(exc).__name__}: {exc}",
        }

    failed_jornadas = set(summary.network_failures)
    failed = jornada in failed_jornadas
    return {
        "status": "error" if failed else "ok",
        "jornada": jornada,
        "jornadas_inserted": summary.jornadas_inserted,
        "jornadas_existing": summary.jornadas_existing,
        "composicion_inserted": summary.composicion_inserted,
        "composicion_existing": summary.composicion_existing,
        "stubs_created": summary.stubs_created,
    }


async def ingest_today_quiniwin_job(
    *,
    temporada: int | None = None,
    fetch_timeout_sec: float = 20.0,
    jornada_range_start: int = 1,
    jornada_range_end: int = 42,
    forward_cap: int = 5,
    chain_losilla: bool = True,
) -> dict[str, Any]:
    """Daily cron job: ingest the next unknown Quiniwin jornadas.

    Steps:

    1. Discover the active temporada. Default is the year of the
       scheduler's "current season" (the start year). For 2026 the season
       is 2026/2027 → temporada=2026.
    2. Look up the highest ``jornada.numero`` already known for that
       temporada and walk forward from ``max+1`` up to
       ``forward_cap`` jornadas.
    3. For each candidate jornada, call :func:`run_ingest` (which fetches
       Quiniwin, parses, matches teams, upserts idempotently). One bad
       jornada is recorded in ``source_stats[jornada]`` and the loop
       continues.
    4. Persist a single ``fetch_session`` row with
       ``source_stats.quiniwin.jornadas`` = ``[<per-jornada dict>, ...]``
       so the audit trail has the full per-tick breakdown.

    The job is registered by the runner; you normally do NOT call it
    directly. Tests call it to exercise the tolerance logic.

    Parameters
    ----------
    temporada:
        Override for the active temporada. Defaults to
        ``SCHEDULER_INGEST_TEMPORADA`` if set, else the current UTC year
        (the start year of the in-flight season).
    fetch_timeout_sec, jornada_range_start, jornada_range_end, forward_cap:
        Forwarded from the scheduler settings; exposed as kwargs so the
        cron job can be tested with different shapes.
    """
    if temporada is None:
        # Default to the start year of the current season. This is good
        # enough for v1 — a future task can compute the "active season"
        # from kickoff_at windows on existing jornadas.
        temporada = datetime.now(tz=timezone.utc).year

    LOG.info("ingest_today_quiniwin_job starting temporada=%d", temporada)

    async with AsyncSessionLocal() as session:
        max_known = await _max_known_jornada(session, temporada)

    targets = _discover_forward_window(
        max_known,
        range_start=jornada_range_start,
        range_end=jornada_range_end,
        forward_cap=forward_cap,
    )
    LOG.info(
        "ingest_today_quiniwin_job: max_known=%s targets=%s",
        max_known, targets,
    )

    per_jornada: list[dict[str, Any]] = []
    ok_count = 0
    fail_count = 0

    if not targets:
        # Either the season is exhausted (max_known == range_end) or no
        # new jornada is in the forward window. Still record a fetch_session
        # so the audit trail has "yes, the scheduler woke up; nothing to do".
        source_stats = {
            "quiniwin": {
                "status": "noop",
                "temporada": temporada,
                "max_known_jornada": max_known,
                "jornadas": [],
                "ok_count": 0,
                "fail_count": 0,
            },
        }
    else:
        # QuiniwinClient owns its own httpx timeout (DEFAULT_TIMEOUT=20s).
        # We respect the configured fetch_timeout_sec via asyncio.wait_for
        # in ``_run_one_jornada`` so a runaway call can't park the tick.
        client = QuiniwinClient()
        try:
            for jornada in targets:
                row = await _run_one_jornada(
                    client, temporada, jornada,
                    fetch_timeout_sec=fetch_timeout_sec,
                )
                per_jornada.append(row)
                if row["status"] == "ok":
                    ok_count += 1
                else:
                    fail_count += 1
        finally:
            await client.close()

        source_stats = {
            "quiniwin": {
                "status": "ok" if fail_count == 0 else "partial",
                "temporada": temporada,
                "max_known_jornada": max_known,
                "jornadas": per_jornada,
                "ok_count": ok_count,
                "fail_count": fail_count,
            },
        }

    # Chain Losilla live ingest for any jornada we just inserted, so
    # the cron does the same "full job" the manual admin trigger does
    # (Quiniwin data + Losilla comentarios + estimaciones). Tests can
    # opt out with ``chain_losilla=False`` to avoid mocking the Losilla
    # upstream alongside Quiniwin.
    if chain_losilla:
        inserted_jornadas: list[int] = [
            row["jornada"]
            for row in per_jornada
            if row.get("status") == "ok" and row.get("jornadas_inserted", 0) > 0
        ]
        losilla_per_jornada = await _run_losilla_for_inserted_jornadas(
            inserted_jornadas, temporada,
        )
    else:
        losilla_per_jornada = {}
    source_stats["losilla_per_jornada"] = losilla_per_jornada

    notes = (
        f"temporada={temporada} max_known={max_known} "
        f"targets={targets} ok={ok_count} fail={fail_count}"
    )
    # Wrap the whole thing in a fetch_session via fetch_and_record so the
    # audit trail is identical to future scheduled jobs (escrutinio,
    # comentarios, ...).
    final_stats = await fetch_and_record(
        AsyncSessionLocal,
        SOURCE_CODE_QUINIWIN,
        # Wrap the synchronous-style dict in a coroutine so the audit
        # primitive can capture start/finish/source_stats uniformly.
        lambda: _async_value(source_stats),
        notes=notes,
    )
    return final_stats


async def _async_value(value: Any) -> Any:
    """Helper: turn a plain value into an awaitable. Used by
    :func:`fetch_and_record` callers that already have their result."""
    return value


# ---------- Chained Losilla for newly-inserted jornadas ----------


async def _run_losilla_for_inserted_jornadas(
    jornada_numeros: list[int],
    temporada: int,
) -> dict[int, dict[str, Any]]:
    """Run the Losilla live ingest for each newly-inserted jornada.

    Used by both the cron and the manual admin trigger to make
    "ingest a new jornada" do the full job: Quiniwin data PLUS
    Losilla comentarios + estimaciones. Idempotent — re-running on a
    jornada that already has comentarios just returns
    ``comentarios_existing=N``.

    Per-jornada failures are recorded as ``{"status": "error", "error": ...}``
    in the result dict; the whole function does NOT raise.

    Lazy-imports the losilla_ingest module inside the body to break the
    circular import: losilla_ingest.py imports ``fetch_and_record``
    from this module.
    """
    if not jornada_numeros:
        return {}

    from app.clients.eduardolosilla import EduardoLosillaClient
    from app.scripts.losilla_ingest import run_ingest as losilla_run_ingest

    results: dict[int, dict[str, Any]] = {}
    async with EduardoLosillaClient() as client:
        for j_num in jornada_numeros:
            try:
                summary = await losilla_run_ingest(
                    j_num, temporada, client=client,
                )
                results[j_num] = {
                    "status": "ok",
                    "escrutinio_inserted": summary.escrutinio_inserted,
                    "escrutinio_existed": summary.escrutinio_existed,
                    "comentarios_inserted": summary.comentarios_inserted,
                    "comentarios_updated": summary.comentarios_updated,
                    "comentarios_existing": summary.comentarios_existing,
                    "network_failures": summary.network_failures,
                }
            except Exception as exc:  # noqa: BLE001
                LOG.warning(
                    "chained Losilla ingest: jornada=%d raised %s: %s",
                    j_num, type(exc).__name__, exc,
                )
                results[j_num] = {
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                }
    return results


# ---------- Losilla post-jornada job ----------


async def _jornadas_without_escrutinio(
    session: AsyncSession, temporada: int,
) -> list[tuple[int, int]]:
    """Return ``[(jornada_id, jornada_numero), ...]`` for the given temporada
    that have no ``jornada_escrutinio`` row yet, ordered by numero ASC.

    Self-healing: if a previous run died after writing only some
    escrutinio rows, the next tick re-tries the missing ones. Idempotent
    on the persist side (the losilla_ingest CLI is idempotent too).
    """
    rows = (await session.execute(
        select(Jornada.id, Jornada.numero)
        .where(Jornada.temporada == temporada)
        .order_by(Jornada.numero.asc())
    )).all()
    if not rows:
        return []

    candidate_ids = [r[0] for r in rows]
    done_ids = set((await session.execute(
        select(JornadaEscrutinio.jornada_id)
        .where(JornadaEscrutinio.jornada_id.in_(candidate_ids))
    )).scalars().all())

    return [
        (r[0], r[1])
        for r in rows
        if r[0] not in done_ids
    ]


async def losilla_persist_recent_job(
    *,
    temporada: int | None = None,
) -> dict[str, Any]:
    """Monday morning cron: persist escrutinio + comentarios for any closed
    jornada that doesn't have them yet.

    Steps:

    1. Look up all jornadas in the active temporada that lack an
       ``jornada_escrutinio`` row (see :func:`_jornadas_without_escrutinio`).
    2. For each candidate ``(jornada_id, jornada_numero)``, call
       :func:`app.scripts.losilla_ingest.run_ingest`. One failure is
       recorded and the loop continues — a transient Losilla outage must
       not poison the whole Monday-morning run.
    3. Persist a single ``fetch_session`` row with
       ``source_stats.losilla.jornadas`` = ``[<per-jornada dict>, ...]``
       so the audit trail mirrors the Quiniwin job's shape.

    Parameters
    ----------
    temporada:
        Active temporada. Defaults to current UTC year (same heuristic
        as the Quiniwin job; consistent for the 2026/27 deployment).
    """
    if temporada is None:
        temporada = datetime.now(tz=timezone.utc).year

    LOG.info("losilla_persist_recent_job starting temporada=%d", temporada)

    async with AsyncSessionLocal() as session:
        pending = await _jornadas_without_escrutinio(session, temporada)

    LOG.info(
        "losilla_persist_recent_job: pending=%s",
        [(jid, n) for jid, n in pending],
    )

    per_jornada: list[dict[str, Any]] = []
    ok_count = 0
    fail_count = 0
    skipped_count = 0

    if not pending:
        source_stats = {
            "losilla": {
                "status": "noop",
                "temporada": temporada,
                "jornadas": [],
                "ok_count": 0,
                "fail_count": 0,
                "skipped_count": 0,
            },
        }
    else:
        # Lazy import to break the cycle: losilla_ingest.py imports
        # ``fetch_and_record`` from this module.
        from app.scripts.losilla_ingest import run_ingest as losilla_run_ingest

        for jornada_id, jornada_numero in pending:
            try:
                summary = await losilla_run_ingest(
                    jornada=jornada_numero,
                    temporada=temporada,
                )
            except Exception as exc:  # noqa: BLE001 — record-and-bubble
                LOG.warning(
                    "losilla_persist_recent_job: jornada=%d raised %s: %s",
                    jornada_numero, type(exc).__name__, exc,
                )
                per_jornada.append({
                    "jornada": jornada_numero,
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                })
                fail_count += 1
                continue

            # Precondition errors (missing jornada row, missing source row)
            # surface as "skipped" rather than "fail" — they are operator
            # actions, not transient.
            if getattr(summary, "network_failures", []):
                per_jornada.append({
                    "jornada": jornada_numero,
                    "status": "fail",
                    "error": "; ".join(summary.network_failures),
                })
                fail_count += 1
            else:
                per_jornada.append({
                    "jornada": jornada_numero,
                    "status": "ok",
                    "escrutinio_existed": summary.escrutinio_existed,
                    "comentarios_inserted": summary.comentarios_inserted,
                    "comentarios_existing": summary.comentarios_existing,
                })
                ok_count += 1

        source_stats = {
            "losilla": {
                "status": "ok" if fail_count == 0 else "partial",
                "temporada": temporada,
                "jornadas": per_jornada,
                "ok_count": ok_count,
                "fail_count": fail_count,
                "skipped_count": skipped_count,
            },
        }

    notes = (
        f"temporada={temporada} pending={len(pending)} "
        f"ok={ok_count} fail={fail_count}"
    )
    final_stats = await fetch_and_record(
        AsyncSessionLocal,
        SOURCE_CODE_LOSILLA,
        lambda: _async_value(source_stats),
        notes=notes,
    )
    return final_stats


__all__ = [
    "SOURCE_CODE_QUINIWIN",
    "SOURCE_CODE_LOSILLA",
    "fetch_and_record",
    "ingest_today_quiniwin_job",
    "losilla_persist_recent_job",
    "losilla_poll_recent_closed_job",
]


# ---------- Losilla daily poll job (slice 9, §6.4) ----------


# How long after ``kickoff_last_match_at`` we wait before polling. The cron
# fires at 23:00 Madrid (22:00 / 21:00 UTC depending on DST), so a 3-hour
# window comfortably covers any jornada whose last match finished on the
# same day, including the late-evening match. Past this window Losilla
# has had enough time to publish the escrutinio; if the row is still
# missing, it is a real upstream-side gap (or a weekday between Tuesday
# and Sunday, which is the whole point of this job).
LOSILLA_DAILY_POLL_GRACE_HOURS = 3


async def _recently_closed_without_escrutinio(
    session: AsyncSession,
    temporada: int,
    *,
    now: datetime,
    grace_hours: int = LOSILLA_DAILY_POLL_GRACE_HOURS,
) -> list[tuple[int, int]]:
    """Return ``[(jornada_id, jornada_numero), ...]`` for the given temporada
    where:

    - ``kickoff_last_match_at`` is not NULL (i.e. the janela is set)
    - ``kickoff_last_match_at + grace_hours < now`` (the jornada finished
      more than ``grace_hours`` ago, giving Losilla time to publish)
    - there is no ``jornada_escrutinio`` row yet

    Ordered by ``jornada.numero`` ASC.

    Self-healing: re-running the job on a jornada that already had its
    escrutinio persisted will skip it because the IS NULL check fails.
    The IS NULL check is done in Python (same shape as
    :func:`_jornadas_without_escrutinio`) to keep the join with the
    JornadaEscrutinio table simple — temporada is bounded (~42 rows)
    so the in-Python filter is cheap.
    """
    rows = (await session.execute(
        select(
            Jornada.id, Jornada.numero, Jornada.kickoff_last_match_at,
        )
        .where(
            Jornada.temporada == temporada,
            Jornada.kickoff_last_match_at.is_not(None),
        )
        .order_by(Jornada.numero.asc())
    )).all()
    if not rows:
        return []

    finished = [
        (r[0], r[1])
        for r in rows
        if r[2] is not None
        and (r[2] + timedelta(hours=grace_hours)) < now
    ]
    if not finished:
        return []

    candidate_ids = [r[0] for r in finished]
    done_ids = set((await session.execute(
        select(JornadaEscrutinio.jornada_id)
        .where(JornadaEscrutinio.jornada_id.in_(candidate_ids))
    )).scalars().all())

    return [
        (r[0], r[1])
        for r in finished
        if r[0] not in done_ids
    ]


async def losilla_poll_recent_closed_job(
    *,
    temporada: int | None = None,
    now: datetime | None = None,
    grace_hours: int = LOSILLA_DAILY_POLL_GRACE_HOURS,
) -> dict[str, Any]:
    """Daily 23:00 cron: persist escrutinio + comentarios for any jornada
    that closed in the last 3 hours and still lacks an escrutinio.

    The weekly Monday job (:func:`losilla_persist_recent_job`) is the
    backstop; this daily job exists to avoid making a Tuesday-through-
    Sunday jornada sit ``closed`` until the following Monday morning.

    Steps:

    1. Look up all jornadas in the active temporada whose
       ``kickoff_last_match_at + grace_hours < now`` AND which do not
       yet have a ``jornada_escrutinio`` row (see
       :func:`_recently_closed_without_escrutinio`).
    2. For each candidate ``(jornada_id, jornada_numero)``, call
       :func:`app.scripts.losilla_ingest.run_ingest`. One failure is
       recorded and the loop continues — a transient Losilla outage
       must not poison the whole nightly run.
    3. Persist a single ``fetch_session`` row with
       ``source_stats.losilla.jornadas`` = ``[<per-jornada dict>, ...]``
       so the audit trail mirrors both the Quiniwin job and the weekly
       Losilla job.

    Idempotent and self-healing: re-running the job on a jornada that
    already completed it will skip it (the IS NULL check fails). Empty
    candidate set is a clean no-op — one ``fetch_session`` row is
    written with ``status="noop"`` and ``jornadas=[]`` so the audit
    trail still records "yes, the scheduler woke up; nothing to do".

    Parameters
    ----------
    temporada:
        Active temporada. Defaults to current UTC year (same heuristic
        as the Quiniwin and weekly Losilla jobs; consistent for the
        2026/27 deployment).
    now:
        Override for "current time". Defaults to ``datetime.now(UTC)``.
        Exposed for tests so we can pin down the filter window.
    grace_hours:
        How long after ``kickoff_last_match_at`` we wait before polling.
        Defaults to :data:`LOSILLA_DAILY_POLL_GRACE_HOURS` (=3). Exposed
        for tests.
    """
    if temporada is None:
        temporada = datetime.now(tz=timezone.utc).year
    if now is None:
        now = datetime.now(tz=timezone.utc)

    LOG.info(
        "losilla_poll_recent_closed_job starting temporada=%d now=%s "
        "grace_hours=%d",
        temporada, now.isoformat(), grace_hours,
    )

    async with AsyncSessionLocal() as session:
        candidates = await _recently_closed_without_escrutinio(
            session, temporada, now=now, grace_hours=grace_hours,
        )

    LOG.info(
        "losilla_poll_recent_closed_job: candidates=%s",
        [(jid, n) for jid, n in candidates],
    )
    if not candidates:
        LOG.info("losilla_poll_recent_closed_job: 0 jornadas found")

    per_jornada: list[dict[str, Any]] = []
    ok_count = 0
    fail_count = 0
    skipped_count = 0

    if not candidates:
        source_stats = {
            "losilla": {
                "status": "noop",
                "temporada": temporada,
                "grace_hours": grace_hours,
                "jornadas": [],
                "ok_count": 0,
                "fail_count": 0,
                "skipped_count": 0,
            },
        }
    else:
        # Lazy import to break the cycle: losilla_ingest.py imports
        # ``fetch_and_record`` from this module.
        from app.scripts.losilla_ingest import run_ingest as losilla_run_ingest

        for jornada_id, jornada_numero in candidates:
            try:
                summary = await losilla_run_ingest(
                    jornada=jornada_numero,
                    temporada=temporada,
                )
            except Exception as exc:  # noqa: BLE001 — record-and-bubble
                LOG.warning(
                    "losilla_poll_recent_closed_job: jornada=%d raised %s: %s",
                    jornada_numero, type(exc).__name__, exc,
                )
                per_jornada.append({
                    "jornada": jornada_numero,
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                })
                fail_count += 1
                continue

            # Precondition errors (missing jornada row, missing source row)
            # surface as "fail" rather than "ok" — they are operator
            # actions, not transient. Same shape as the weekly job.
            if getattr(summary, "network_failures", []):
                per_jornada.append({
                    "jornada": jornada_numero,
                    "status": "fail",
                    "error": "; ".join(summary.network_failures),
                })
                fail_count += 1
            else:
                per_jornada.append({
                    "jornada": jornada_numero,
                    "status": "ok",
                    "escrutinio_existed": summary.escrutinio_existed,
                    "comentarios_inserted": summary.comentarios_inserted,
                    "comentarios_existing": summary.comentarios_existing,
                })
                ok_count += 1

        source_stats = {
            "losilla": {
                "status": "ok" if fail_count == 0 else "partial",
                "temporada": temporada,
                "grace_hours": grace_hours,
                "jornadas": per_jornada,
                "ok_count": ok_count,
                "fail_count": fail_count,
                "skipped_count": skipped_count,
            },
        }

    notes = (
        f"daily_poll temporada={temporada} candidates={len(candidates)} "
        f"ok={ok_count} fail={fail_count}"
    )
    final_stats = await fetch_and_record(
        AsyncSessionLocal,
        SOURCE_CODE_LOSILLA,
        lambda: _async_value(source_stats),
        notes=notes,
    )
    return final_stats