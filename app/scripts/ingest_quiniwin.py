"""Bootstrap CLI: fetch Quiniwin XML for a season's first N jornadas and
populate the ``jornada`` + ``composicion`` tables.

End state::

    python -m app.scripts.ingest_quiniwin --temporada 2026 --jornadas 1..10

Idempotent. Re-running inserts no duplicate rows (relies on the
``UNIQUE(temporada, numero)`` and ``UNIQUE(jornada_id, posicion)`` indexes
plus the functional unique index on ``team_identity_map``).

What this CLI does NOT do (deferred to later tasks):

- Resolve ``competicion_raw`` onto ``competition.id``. The campo is persisted
  as-is in ``composicion.competicion_raw`` for now.
- Fetch escrutinio, comentarios, percentages, or kickoff_at enrichment
  (those come from Losilla, not Quiniwin).
- Backfill historical seasons — this is a forward-looking ingest.

Usage::

    python -m app.scripts.ingest_quiniwin --temporada 2026 --jornadas 1..10
    python -m app.scripts.ingest_quiniwin --temporada 2027 --jornadas 1,3,5,8
    python -m app.scripts.ingest_quiniwin --temporada 2026 --jornadas 1..10 --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Sequence

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.quiniwin import (
    QuiniwinClient,
    build_temporada_str,
)
from app.db.models import Composicion, Jornada, JornadaStatus
from app.db.session import AsyncSessionLocal
from app.db.team_matcher import find_or_create_team


LOG = logging.getLogger("ingest_quiniwin")


# ---------- Jornada range parsing ----------


_RANGE_RE = re.compile(r"^\s*(\d+)\s*\.\.\s*(\d+)\s*$")
_LIST_RE = re.compile(r"^\s*\d+(?:\s*,\s*\d+)*\s*$")


def parse_jornadas(spec: str) -> list[int]:
    """Parse a CLI jornada spec into a list of positive ints.

    Accepted forms:

    - ``"1..10"`` → ``[1, 2, ..., 10]`` (inclusive range)
    - ``"1,3,5,8"`` → ``[1, 3, 5, 8]`` (explicit list)
    - ``"5"`` → ``[5]`` (single)

    Raises ``SystemExit(2)`` (argparse convention) on malformed input.
    """
    if not spec:
        raise SystemExit("--jornadas: empty spec")

    if (m := _RANGE_RE.match(spec)) is not None:
        a, b = int(m.group(1)), int(m.group(2))
        if a > b:
            a, b = b, a
        return list(range(a, b + 1))

    if _LIST_RE.match(spec):
        return [int(x) for x in spec.split(",")]

    raise SystemExit(
        f"--jornadas: cannot parse {spec!r}; expected 'N', 'N..M', or 'N,M,K'"
    )


# ---------- Ingest core ----------


@dataclass
class IngestSummary:
    """Per-run summary counters for the CLI to print at the end."""
    temporada: int
    temporada_str: str
    jornadas_requested: int
    jornadas_inserted: int
    jornadas_existing: int
    composicion_inserted: int
    composicion_existing: int
    stubs_created: int
    parse_failures: list[int]
    network_failures: list[int]

    def render(self) -> str:
        lines = [
            f"=== Ingest summary ({self.temporada} / {self.temporada_str}) ===",
            f"  jornadas requested:   {self.jornadas_requested}",
            f"  jornadas inserted:    {self.jornadas_inserted}",
            f"  jornadas existing:    {self.jornadas_existing}",
            f"  composicion inserted: {self.composicion_inserted}",
            f"  composicion existing: {self.composicion_existing}",
            f"  stubs created:        {self.stubs_created}",
        ]
        if self.parse_failures:
            lines.append(f"  parse failures:       {self.parse_failures}")
        if self.network_failures:
            lines.append(f"  network failures:     {self.network_failures}")
        return "\n".join(lines)


async def _upsert_jornada(
    session: AsyncSession, temporada: int, temporada_str: str, numero: int,
) -> tuple[Jornada, bool]:
    """Insert a jornada row idempotently; return (row, inserted).

    Strategy: SELECT first. If a row exists, return it (no insert). If not,
    INSERT with ``ON CONFLICT DO NOTHING`` + ``RETURNING id`` so we get a
    reliable inserted-row count even when psycopg would otherwise report
    ``rowcount=-1``. We trust the unique constraint to catch any racing
    writer between our SELECT and our INSERT.
    """
    pre = await session.scalar(
        select(Jornada).where(
            Jornada.temporada == temporada,
            Jornada.numero == numero,
        )
    )
    if pre is not None:
        return pre, False

    stmt = pg_insert(Jornada).values(
        temporada=temporada,
        temporada_str=temporada_str,
        numero=numero,
        status=JornadaStatus.draft,
    ).on_conflict_do_nothing(
        index_elements=[Jornada.temporada, Jornada.numero],
    ).returning(Jornada.id)
    res = await session.execute(stmt)
    inserted_id = res.scalar_one_or_none()
    if inserted_id is not None:
        row = await session.get(Jornada, inserted_id)
        if row is None:
            raise RuntimeError(
                f"jornada ({temporada}, {numero}) insert returned id "
                f"{inserted_id} but row not found"
            )
        return row, True

    # Race: someone else inserted between our SELECT and INSERT. Re-fetch.
    row = await session.scalar(
        select(Jornada).where(
            Jornada.temporada == temporada,
            Jornada.numero == numero,
        )
    )
    if row is None:
        raise RuntimeError(
            f"jornada ({temporada}, {numero}) ON CONFLICT DO NOTHING "
            "returned no row, but no pre-existing row either — DB state "
            "is unexpected; please inspect"
        )
    return row, False


async def _insert_composicion_batch(
    session: AsyncSession,
    jornada_id: int,
    rows: list[dict],
) -> int:
    """Insert composicion rows idempotently. Returns count actually inserted.

    Uses SELECT-count-before and SELECT-count-after because psycopg returns
    ``rowcount=-1`` for multi-row INSERT...ON CONFLICT without RETURNING,
    which makes the cursor rowcount unreliable. The diff between
    pre-count and post-count is the number of rows we just inserted.
    """
    if not rows:
        return 0
    pre_count = await session.scalar(
        select(func.count()).select_from(Composicion).where(
            Composicion.jornada_id == jornada_id,
        )
    ) or 0
    stmt = pg_insert(Composicion).values(rows).on_conflict_do_nothing(
        index_elements=[Composicion.jornada_id, Composicion.posicion],
    )
    await session.execute(stmt)
    post_count = await session.scalar(
        select(func.count()).select_from(Composicion).where(
            Composicion.jornada_id == jornada_id,
        )
    ) or 0
    inserted = post_count - pre_count
    return max(inserted, 0)


async def _ingest_one_jornada(
    session: AsyncSession,
    client: QuiniwinClient,
    temporada: int,
    temporada_str: str,
    jornada: int,
    *,
    stub_counters: list[int],
) -> tuple[int, int, int, bool]:
    """Ingest one jornada. Returns (jornada_inserted, comp_inserted, comp_existing, ok).

    ``ok=False`` indicates the jornada should be counted as a failure (parse
    or network) rather than a successful no-op.
    """
    try:
        partidos = await client.get_jornada(jornada, temporada)
    except Exception as exc:  # httpx + parse errors
        LOG.warning(
            "network/parse failure for temporada=%d jornada=%d: %s",
            temporada, jornada, exc,
        )
        return 0, 0, 0, False

    # Filter to a clean 15-row set; Quiniwin usually emits exactly 15, but
    # edge cases (midseason template refresh) could be different.
    valid = [p for p in partidos if 1 <= p.posicion <= 15]
    if not valid:
        LOG.warning(
            "no valid <partido> rows for temporada=%d jornada=%d",
            temporada, jornada,
        )
        return 0, 0, 0, False

    jornada_row, j_inserted = await _upsert_jornada(
        session, temporada, temporada_str, jornada,
    )

    comp_rows: list[dict] = []
    for p in valid:
        local = await find_or_create_team(session, p.local_raw)
        visitante = await find_or_create_team(session, p.visitante_raw)
        if local.entity_id.startswith("auto-"):
            stub_counters[0] += 1
        if visitante.entity_id.startswith("auto-"):
            stub_counters[0] += 1
        comp_rows.append({
            "jornada_id": jornada_row.id,
            "posicion": p.posicion,
            "local_team_id": local.id,
            "visitante_team_id": visitante.id,
            "local_raw": p.local_raw,
            "visitante_raw": p.visitante_raw,
            "competicion_raw": p.competicion_raw,
        })

    inserted = await _insert_composicion_batch(session, jornada_row.id, comp_rows)
    existing = len(comp_rows) - inserted
    await session.commit()
    return (1 if j_inserted else 0), inserted, existing, True


async def run_ingest(
    temporada: int,
    jornadas: Sequence[int],
    *,
    dry_run: bool = False,
    client: QuiniwinClient | None = None,
) -> IngestSummary:
    """Drive the ingest end to end. Returns a summary for the caller to print.

    ``client`` is injected for tests; in production we build our own.
    """
    temporada_str = build_temporada_str(temporada)
    summary = IngestSummary(
        temporada=temporada,
        temporada_str=temporada_str,
        jornadas_requested=len(jornadas),
        jornadas_inserted=0,
        jornadas_existing=0,
        composicion_inserted=0,
        composicion_existing=0,
        stubs_created=0,
        parse_failures=[],
        network_failures=[],
    )

    if dry_run:
        for j in jornadas:
            try:
                build_temporada_str(temporada)  # validate temporada
            except ValueError:
                continue
            print(
                f"[DRY-RUN] would fetch temporada={temporada} "
                f"jornada={j:02d}"
            )
        return summary

    stub_counter = [0]
    own_client = client is None
    if client is None:
        client = QuiniwinClient()

    try:
        async with AsyncSessionLocal() as session:
            for j in jornadas:
                j_ins, c_ins, c_exist, ok = await _ingest_one_jornada(
                    session, client, temporada, temporada_str, j,
                    stub_counters=stub_counter,
                )
                summary.jornadas_inserted += j_ins
                summary.jornadas_existing += (1 if ok and j_ins == 0 else 0)
                summary.composicion_inserted += c_ins
                summary.composicion_existing += c_exist
                if not ok:
                    # Distinguish parse vs network by exception type at the
                    # caller of _ingest_one_jornada if needed; for now we
                    # group all failures under network_failures (parse
                    # errors also surface here in the tests).
                    summary.network_failures.append(j)
    finally:
        if own_client:
            await client.close()

    summary.stubs_created = stub_counter[0]
    return summary


# ---------- CLI entrypoint ----------


def cli() -> None:
    p = argparse.ArgumentParser(
        description=(
            "Fetch Quiniwin XML for a temporada's first N jornadas and "
            "populate jornada + composicion rows. Idempotent."
        ),
    )
    p.add_argument(
        "--temporada",
        type=int,
        required=True,
        help=(
            "Temporada end year (e.g. 2026 = season 2026/2027). "
            "URL pattern is {temporada}-{temporada+1}_j{NN}.xml"
        ),
    )
    p.add_argument(
        "--jornadas",
        type=str,
        required=True,
        help=(
            "Jornada selector: 'N' (one), 'N..M' (range, inclusive), or "
            "'N,M,K' (explicit list). E.g. '1..10' or '1,3,5,8'."
        ),
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print plan, hit no network, change no DB rows.",
    )
    args = p.parse_args()

    jornadas = parse_jornadas(args.jornadas)
    summary = asyncio.run(run_ingest(
        args.temporada, jornadas, dry_run=args.dry_run,
    ))
    print(summary.render())
    if summary.parse_failures or summary.network_failures:
        # Non-fatal: partial success is still useful. Exit 0.
        pass


if __name__ == "__main__":
    cli()