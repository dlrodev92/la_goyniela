"""Tests for T-data-4 — Losilla escrutinio + comentarios persistence (CLI).

Covers the CLI ingest (``app.scripts.losilla_ingest.run_ingest``): happy
paths, idempotency, missing jornada, no-comentarios upstream, fetch_session
audit row, and comentario content-change updates.

Router DB-first behaviour is covered separately in
``tests/test_losilla_db_first.py``.

The tests use the real Postgres DB on port 5433 and ``httpx_mock`` to
isolate from ``eduardolosilla.es``. An autouse ``_cleanup_rows``
fixture wipes the test-created rows after each test.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text

from app.clients.eduardolosilla import EduardoLosillaClient
from app.db.models import (
    FetchSession,
    Jornada,
    JornadaComentario,
    JornadaEscrutinio,
    Source,
)
from app.db.session import AsyncSessionLocal, engine
from app.main import app
from app.scripts.losilla_ingest import (
    IngestSummary,
    run_ingest,
    _synthesize_pleno_15_comment,
)


FIXTURES = Path(__file__).parent / "fixtures" / "losilla"
pytestmark = pytest.mark.asyncio


# ---------- Cleanup ----------


@pytest_asyncio.fixture(autouse=True)
async def _cleanup_rows():
    """Wipe test-created jornada/escrutinio/comentario/fetch_session rows."""
    yield
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM jornada_escrutinio"))
        await conn.execute(text("DELETE FROM jornada_comentario"))
        await conn.execute(text("DELETE FROM fetch_session"))
        await conn.execute(text("DELETE FROM jornada"))


# ---------- Shared fixtures ----------


@pytest.fixture
def fixtures():
    return {
        "j8": FIXTURES / "j8_bote_real.html",
        "j11": FIXTURES / "j11_open.html",
    }


@pytest_asyncio.fixture
async def make_jornada():
    """Factory: create a ``jornada`` row for testing."""

    async def _create(numero: int = 8, temporada: int = 2026) -> int:
        async with AsyncSessionLocal() as session:
            j = Jornada(
                temporada=temporada,
                temporada_str=f"{temporada}/{temporada + 1}",
                numero=numero,
            )
            session.add(j)
            await session.commit()
            await session.refresh(j)
            return j.id

    return _create


@pytest_asyncio.fixture
async def losilla_source_id() -> int:
    """Return the ``source.id`` for ``code='losilla'``.

    Skips the test (with a helpful message) if the catalog row is missing
    — the user must run ``python -m app.scripts.load_team_catalog`` to
    seed it. We do NOT silently create the row here (T-data-4 explicitly
    forbids silent inserts into the source catalog).
    """
    async with AsyncSessionLocal() as session:
        sid = await session.scalar(
            select(Source.id).where(Source.code == "losilla"),
        )
    if sid is None:
        pytest.skip(
            "source row with code='losilla' is missing — run "
            "`python -m app.scripts.load_team_catalog` first"
        )
    return sid


# ============================================================
# CLI ingest tests
# ============================================================


async def test_ingest_persists_escrutinio_to_db(
    httpx_mock, fixtures, make_jornada, losilla_source_id,
):
    """Happy path: pre-create jornada + source, run CLI, assert 1 row.

    The persisted ``payload`` JSONB round-trips back to the original
    ``EscrutinioOficial`` model so we can compare semantically.
    """
    httpx_mock.add_response(
        url="https://www.eduardolosilla.es/quiniela/ayudas/escrutinio/jornada_8",
        content=fixtures["j8"].read_bytes(),
    )
    jid = await make_jornada(numero=8, temporada=2026)

    client = EduardoLosillaClient()
    summary = await run_ingest(jornada=8, temporada=2026, client=client)
    await client.close()

    assert summary.escrutinio_inserted is True
    assert summary.escrutinio_existed is False
    assert summary.network_failures == []

    async with AsyncSessionLocal() as session:
        rows = (await session.execute(
            select(JornadaEscrutinio).where(
                JornadaEscrutinio.jornada_id == jid,
            )
        )).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.source_id == losilla_source_id
    assert row.fetch_session_id is not None
    assert isinstance(row.fetch_session_id, UUID)
    # payload round-trips into the original Pydantic model
    assert row.payload["jornada_etiqueta"] == 8
    assert row.payload["recaudacion_eur"] == "3.781.971,75"
    assert row.payload["bote_eur"] == "3.159.105,01"
    cat15 = next(c for c in row.payload["categorias"] if c["aciertos"] == 15)
    assert cat15["acertantes"] == 1
    assert cat15["premio_eur"] == "2.762.202,31"


async def test_ingest_persists_15_comentarios(
    httpx_mock, fixtures, make_jornada, losilla_source_id,
):
    """Happy path: same fixture, assert 15 comentario rows.

    Gestor-jornadas.md §6.1 (slice 6) changes the behavior: partido 15
    (Pleno al 15) no longer skipped — its comentario is synthesized from
    the Pleno al 15 percentages carried on the ``PartidoLosilla`` model
    (``porc_15L_*`` / ``porc_15V_*``).
    """
    httpx_mock.add_response(
        url="https://www.eduardolosilla.es/quiniela/ayudas/escrutinio/jornada_8",
        content=fixtures["j8"].read_bytes(),
    )
    jid = await make_jornada(numero=8, temporada=2026)

    client = EduardoLosillaClient()
    summary = await run_ingest(jornada=8, temporada=2026, client=client)
    await client.close()

    assert summary.comentarios_inserted == 15
    assert summary.comentarios_existing == 0
    assert summary.comentarios_updated == 0

    async with AsyncSessionLocal() as session:
        rows = (await session.execute(
            select(JornadaComentario)
            .where(JornadaComentario.jornada_id == jid)
            .order_by(JornadaComentario.partido_orden)
        )).scalars().all()
    assert len(rows) == 15
    ordenes = [r.partido_orden for r in rows]
    assert ordenes == list(range(1, 16))  # 1..15 inclusive
    for r in rows:
        assert r.source_id == losilla_source_id
        assert r.comentario is not None  # non-None; partido 15 may be ""
        assert r.local_raw  # non-empty string
        assert r.visitante_raw  # non-empty string


async def test_ingest_persists_synthetic_p15_comentario(
    httpx_mock, fixtures, make_jornada, losilla_source_id,
):
    """Partido 15 row has the synthetic Pleno al 15 string.

    The J8 fixture ships (from ``probabilidades_8_2027.partidos.real[14]``)::
        L: 0=33, 1=37, 2=20, M=10
        V: 0=19, 1=32, 2=26, M=23

    → ``"Local: 0=33%, 1=37%, 2=20%, M=10% · Visita: 0=19%, 1=32%, 2=26%, M=23%"``.
    """
    httpx_mock.add_response(
        url="https://www.eduardolosilla.es/quiniela/ayudas/escrutinio/jornada_8",
        content=fixtures["j8"].read_bytes(),
    )
    jid = await make_jornada(numero=8, temporada=2026)

    client = EduardoLosillaClient()
    await run_ingest(jornada=8, temporada=2026, client=client)
    await client.close()

    async with AsyncSessionLocal() as session:
        p15 = (await session.scalar(
            select(JornadaComentario).where(
                JornadaComentario.jornada_id == jid,
                JornadaComentario.partido_orden == 15,
            )
        ))
    assert p15 is not None
    assert p15.comentario == (
        "Local: 0=33%, 1=37%, 2=20%, M=10% \u00b7 "
        "Visita: 0=19%, 1=32%, 2=26%, M=23%"
    )
    # Non-empty (the public column is NOT NULL; we never persist None).
    assert p15.comentario != ""
    # local/visitante come from the partido 15 entry's <local>/<visitante>
    # fields, not the Pleno splits.
    assert p15.local_raw  # whatever the J8 partido 15 names are
    assert p15.visitante_raw


async def test_ingest_skips_when_jornada_not_in_db():
    """Jornada not pre-created → the coroutine raises a precondition error.

    The CLI bails before it ever calls the upstream, so we deliberately
    do NOT register a httpx_mock response — if the CLI somehow tried to
    hit upstream, the test would fail with an ``httpx_mock`` "no match"
    error, which is the right signal.
    """
    # No httpx_mock fixture needed: the CLI bails before HTTP.
    client = EduardoLosillaClient()

    client = EduardoLosillaClient()
    with pytest.raises(Exception) as ei:
        await run_ingest(jornada=8, temporada=2026, client=client)
    await client.close()

    msg = str(ei.value)
    assert "jornada" in msg.lower()
    assert "8" in msg
    # Verify NO rows were persisted (we bailed before any DB write).
    async with AsyncSessionLocal() as session:
        n = await session.scalar(
            select(func.count()).select_from(JornadaEscrutinio)
        )
    assert n == 0


async def test_ingest_is_idempotent(
    httpx_mock, fixtures, make_jornada, losilla_source_id,
):
    """Re-run = same escrutinio row, same 15 comentario rows, 0 new inserts.

    Second run sees the rows already exist. Comentarios are unchanged so
    ``updated=0``; escrutinio payload may or may not change — we accept
    either ``existed=True`` (no change) or an updated payload. The
    invariant is: 0 new inserts across the two runs.
    """
    httpx_mock.add_response(
        url="https://www.eduardolosilla.es/quiniela/ayudas/escrutinio/jornada_8",
        content=fixtures["j8"].read_bytes(),
    )
    httpx_mock.add_response(
        url="https://www.eduardolosilla.es/quiniela/ayudas/escrutinio/jornada_8",
        content=fixtures["j8"].read_bytes(),
    )
    jid = await make_jornada(numero=8, temporada=2026)

    client = EduardoLosillaClient()
    s1 = await run_ingest(jornada=8, temporada=2026, client=client)
    await client.close()

    assert s1.escrutinio_inserted is True
    assert s1.escrutinio_existed is False
    assert s1.comentarios_inserted == 15
    assert s1.comentarios_existing == 0

    # Capture the original escrutinio payload to compare against the
    # post-second-run payload. (Should be byte-identical for J8 fixture.)
    async with AsyncSessionLocal() as session:
        first_payload = (await session.scalar(
            select(JornadaEscrutinio.payload).where(
                JornadaEscrutinio.jornada_id == jid,
            )
        ))

    client = EduardoLosillaClient()
    s2 = await run_ingest(jornada=8, temporada=2026, client=client)
    await client.close()

    # The second run did NOT create new rows.
    assert s2.escrutinio_inserted is False
    assert s2.escrutinio_existed is True
    assert s2.comentarios_inserted == 0
    assert s2.comentarios_updated == 0
    assert s2.comentarios_existing == 15

    # And the DB still has exactly 1 escrutinio + 15 comentario rows.
    async with AsyncSessionLocal() as session:
        n_e = await session.scalar(
            select(func.count()).select_from(JornadaEscrutinio).where(
                JornadaEscrutinio.jornada_id == jid,
            )
        )
        n_c = await session.scalar(
            select(func.count()).select_from(JornadaComentario).where(
                JornadaComentario.jornada_id == jid,
            )
        )
        second_payload = (await session.scalar(
            select(JornadaEscrutinio.payload).where(
                JornadaEscrutinio.jornada_id == jid,
            )
        ))
    assert n_e == 1
    assert n_c == 15
    # Payload should be byte-identical for the unchanged J8 fixture.
    assert second_payload == first_payload


async def test_ingest_idempotent_with_p15_synthetic_string(
    httpx_mock, fixtures, make_jornada, losilla_source_id,
):
    """Second run with the SAME upstream → p15 comentario unchanged.

    Verifies the idempotency contract for slice 6: re-ingest produces
    zero new rows and zero updates — the synthetic string is
    deterministic for the same SSR payload.
    """
    httpx_mock.add_response(
        url="https://www.eduardolosilla.es/quiniela/ayudas/escrutinio/jornada_8",
        content=fixtures["j8"].read_bytes(),
    )
    httpx_mock.add_response(
        url="https://www.eduardolosilla.es/quiniela/ayudas/escrutinio/jornada_8",
        content=fixtures["j8"].read_bytes(),
    )
    jid = await make_jornada(numero=8, temporada=2026)
    client = EduardoLosillaClient()
    await run_ingest(jornada=8, temporada=2026, client=client)
    await client.close()
    async with AsyncSessionLocal() as session:
        first_p15 = (await session.scalar(
            select(JornadaComentario.comentario).where(
                JornadaComentario.jornada_id == jid,
                JornadaComentario.partido_orden == 15,
            )
        ))

    client = EduardoLosillaClient()
    s2 = await run_ingest(jornada=8, temporada=2026, client=client)
    await client.close()
    assert s2.comentarios_updated == 0
    assert s2.comentarios_inserted == 0

    async with AsyncSessionLocal() as session:
        second_p15 = (await session.scalar(
            select(JornadaComentario.comentario).where(
                JornadaComentario.jornada_id == jid,
                JornadaComentario.partido_orden == 15,
            )
        ))
        # Still exactly one row for partido 15 (unique constraint holds).
        n_p15 = (await session.scalar(
            select(func.count()).select_from(JornadaComentario).where(
                JornadaComentario.jornada_id == jid,
                JornadaComentario.partido_orden == 15,
            )
        ))
    assert n_p15 == 1
    assert second_p15 == first_p15


async def test_ingest_records_fetch_session(
    httpx_mock, fixtures, make_jornada, losilla_source_id,
):
    """One ``fetch_session`` row per run, linked to source.code='losilla'."""
    httpx_mock.add_response(
        url="https://www.eduardolosilla.es/quiniela/ayudas/escrutinio/jornada_8",
        content=fixtures["j8"].read_bytes(),
    )
    await make_jornada(numero=8, temporada=2026)

    client = EduardoLosillaClient()
    await run_ingest(jornada=8, temporada=2026, client=client)
    await client.close()

    async with AsyncSessionLocal() as session:
        rows = (await session.execute(
            select(FetchSession).order_by(FetchSession.started_at)
        )).scalars().all()
    assert len(rows) == 1
    fs = rows[0]
    assert fs.source_id == losilla_source_id
    assert fs.started_at is not None
    assert fs.finished_at is not None
    assert fs.started_at <= fs.finished_at
    # source_stats: {"losilla": {...}} with the per-source counters
    assert fs.source_stats is not None
    losilla_stats: dict[str, Any] = fs.source_stats["losilla"]
    assert losilla_stats["status"] == "ok"
    assert losilla_stats["jornada"] == 8
    assert losilla_stats["temporada"] == 2026
    assert losilla_stats["escrutinio_inserted"] is True
    assert losilla_stats["comentarios_inserted"] == 15
    assert losilla_stats["network_failures"] == []


async def test_ingest_handles_no_comentarios_from_upstream(
    httpx_mock, make_jornada, losilla_source_id,
):
    """Upstream returns partidos but every comentario is None → 0 inserts.

    Losilla's old / off-season jornadas sometimes have partidos but no
    editorial divs. The CLI should persist the escrutinio (if any) but
    persist 0 comentario rows and reflect that in the summary.
    """
    synthetic = b"""
    <html><body>
      <script>{&q;datosGeneralesQuiniela&q;:{},&q;jornada_5_2026&q;:{&q;jornada&q;:5,&q;partidos&q;:[
        {&q;orden&q;:1,&q;num&q;:1,&q;local&q;:&q;X&q;,&q;visitante&q;:&q;Y&q;,&q;dia&q;:&q;SAB&q;,&q;hora&q;:&q;14:00&q;,&q;resultado&q;:&q;1-0&q;,&q;id_besoccer&q;:1},
        {&q;orden&q;:2,&q;num&q;:2,&q;local&q;:&q;A&q;,&q;visitante&q;:&q;B&q;,&q;dia&q;:&q;SAB&q;,&q;hora&q;:&q;16:00&q;,&q;resultado&q;:&q;2-1&q;,&q;id_besoccer&q;:2}
      ]}}</script>
    </body></html>
    """
    httpx_mock.add_response(
        url="https://www.eduardolosilla.es/quiniela/ayudas/escrutinio/jornada_5",
        content=synthetic,
    )
    jid = await make_jornada(numero=5, temporada=2026)

    client = EduardoLosillaClient()
    summary = await run_ingest(jornada=5, temporada=2026, client=client)
    await client.close()

    # No escrutinio block in the synthetic HTML → 0 escrutinio persisted.
    assert summary.escrutinio_inserted is False
    assert summary.escrutinio_existed is False
    # No comentarios either (no editorial divs).
    assert summary.comentarios_inserted == 0
    assert summary.comentarios_existing == 0
    assert summary.network_failures == []

    async with AsyncSessionLocal() as session:
        n_e = await session.scalar(
            select(func.count()).select_from(JornadaEscrutinio).where(
                JornadaEscrutinio.jornada_id == jid,
            )
        )
        n_c = await session.scalar(
            select(func.count()).select_from(JornadaComentario).where(
                JornadaComentario.jornada_id == jid,
            )
        )
    assert n_e == 0
    assert n_c == 0


async def test_ingest_comentario_content_change_updates_row(
    httpx_mock, fixtures, make_jornada, losilla_source_id,
):
    """If upstream changes a comentario, the row is updated (not duplicated).

    First run writes the original. Second run uses a fixture whose
    partido 1 comentario differs → summary.comentarios_updated == 1,
    row count stays at 15.
    """
    # First fetch — original J8 fixture.
    httpx_mock.add_response(
        url="https://www.eduardolosilla.es/quiniela/ayudas/escrutinio/jornada_8",
        content=fixtures["j8"].read_bytes(),
    )
    jid = await make_jornada(numero=8, temporada=2026)
    client = EduardoLosillaClient()
    s1 = await run_ingest(jornada=8, temporada=2026, client=client)
    await client.close()
    assert s1.comentarios_inserted == 15

    # Capture the original partido 1 comentario to confirm it changes.
    async with AsyncSessionLocal() as session:
        original = (await session.scalar(
            select(JornadaComentario.comentario).where(
                JornadaComentario.jornada_id == jid,
                JornadaComentario.partido_orden == 1,
            )
        ))

    # Synthesize a different J8 with partido 1 comentario altered.
    original_html = fixtures["j8"].read_text(encoding="utf-8", errors="replace")
    # Mutate the partido 1 comentario — pick a distinctive phrase that
    # appears in J8's partido 1 ("El Sadar siempre ha sido un fortín").
    marker = "El Sadar siempre ha sido un fortín"
    assert marker in original_html, "test fixture no as expected"
    mutated = original_html.replace(
        marker,
        marker + " VERSION ALTERADA PARA TEST",
        1,
    )
    assert mutated != original_html
    httpx_mock.add_response(
        url="https://www.eduardolosilla.es/quiniela/ayudas/escrutinio/jornada_8",
        content=mutated.encode("utf-8"),
    )
    client = EduardoLosillaClient()
    s2 = await run_ingest(jornada=8, temporada=2026, client=client)
    await client.close()
    assert s2.comentarios_inserted == 0
    assert s2.comentarios_updated == 1
    # 13 unchanged partidos (1..14) + 1 unchanged partido 15 synthetic
    assert s2.comentarios_existing == 14

    async with AsyncSessionLocal() as session:
        n_c = await session.scalar(
            select(func.count()).select_from(JornadaComentario).where(
                JornadaComentario.jornada_id == jid,
            )
        )
        new_p1 = (await session.scalar(
            select(JornadaComentario.comentario).where(
                JornadaComentario.jornada_id == jid,
                JornadaComentario.partido_orden == 1,
            )
        ))
    assert n_c == 15
    assert new_p1 != original
    assert "VERSION ALTERADA PARA TEST" in new_p1


async def test_ingest_persists_estimaciones_for_each_partido(
    httpx_mock, fixtures, make_jornada, losilla_source_id,
):
    """Slice 7 (gestor-jornadas.md §6.3): 15 ``PartidoEstimacion`` rows
    persisted into ``jornada_escrutinio.payload.estimaciones`` when the
    fixture ships SSR probabilidades.

    Uses the ``j11_open.html`` fixture (canonical for slice 7). The
    fixture's SSR carries ``probabilidades_11_2027.partidos.real`` with
    1X2 percentages for indices 0..13 and Pleno splits for index 14.

    Verifies:

    1. Exactly 15 entries in ``payload.estimaciones`` (one per partido).
    2. ``partido_orden`` runs 1..15 in order.
    3. Partidos 1..14 carry 1X2 percentages (``uno_porcentaje``/
       ``x_porcentaje``/``dos_porcentaje``) and have ``pleno_* = None``.
    4. Partido 15 carries ``pleno_local`` / ``pleno_visitante`` 0/1/2/M
       splits and has the 1X2 fields set to ``None``.
    5. Every entry has ``fuente == "losilla_live"`` and a populated
       ``fetched_at`` ISO timestamp.
    6. A concrete value check: partido 1's percentages match the fixture's
       upstream ``porc_1``/``porc_X``/``porc_2`` values (35/28/37 in the
       J11 fixture), and partido 15's Pleno splits match
       ``porc_15L_*`` / ``porc_15V_*`` (2/7/14/77 and 59/31/8/2).
    """
    httpx_mock.add_response(
        url="https://www.eduardolosilla.es/quiniela/ayudas/escrutinio/jornada_11",
        content=fixtures["j11"].read_bytes(),
    )
    # Fixture ships ``jornada_11_2027`` + ``probabilidades_11_2027``; the
    # canonical key match works for temporada=2027. We use temporada=2026
    # to keep the test consistent with how the existing J8/J11 cases
    # invoke the CLI — the lone-key fallback in the client picks up the
    # shipped key for both blocks.
    jid = await make_jornada(numero=11, temporada=2026)

    client = EduardoLosillaClient()
    summary = await run_ingest(jornada=11, temporada=2026, client=client)
    await client.close()

    assert summary.escrutinio_inserted is True
    assert summary.network_failures == []

    # Read the persisted payload back; round-trip through the schema so we
    # also assert the JSONB shape is valid against ``EscrutinioOficial``.
    from app.schemas.losilla import (
        EscrutinioOficial,
        PartidoEstimacion,
    )
    async with AsyncSessionLocal() as session:
        payload = await session.scalar(
            select(JornadaEscrutinio.payload).where(
                JornadaEscrutinio.jornada_id == jid,
            )
        )
    assert payload is not None

    # The Pydantic model must accept the row (validates the schema).
    model = EscrutinioOficial.model_validate(payload)
    assert len(model.estimaciones) == 15

    by_orden = {e.partido_orden: e for e in model.estimaciones}
    assert list(by_orden) == list(range(1, 16))  # 1..15 in order

    # Partidos 1..14: 1X2 fields populated; Pleno fields are None.
    for n in range(1, 15):
        e = by_orden[n]
        assert e.uno_porcentaje is not None
        assert e.x_porcentaje is not None
        assert e.dos_porcentaje is not None
        assert e.pleno_local is None
        assert e.pleno_visitante is None
        assert e.fuente == "losilla_live"
        assert e.fetched_at is not None

    # Partido 15: Pleno splits populated; 1X2 fields are None.
    p15 = by_orden[15]
    assert p15.uno_porcentaje is None
    assert p15.x_porcentaje is None
    assert p15.dos_porcentaje is None
    assert p15.pleno_local is not None
    assert p15.pleno_visitante is not None
    assert set(p15.pleno_local) == {"0", "1", "2", "M"}
    assert set(p15.pleno_visitante) == {"0", "1", "2", "M"}
    assert p15.fuente == "losilla_live"
    assert p15.fetched_at is not None

    # Concrete fixture values:
    #   J11 partido 1  : porc_1=35, porc_X=28, porc_2=37
    #   J11 partido 15 : porc_15L_0/1/2/M = 2/7/14/77
    #                    porc_15V_0/1/2/M = 59/31/8/2
    assert by_orden[1].uno_porcentaje == 35
    assert by_orden[1].x_porcentaje == 28
    assert by_orden[1].dos_porcentaje == 37
    assert p15.pleno_local == {"0": 2.0, "1": 7.0, "2": 14.0, "M": 77.0}
    assert p15.pleno_visitante == {
        "0": 59.0, "1": 31.0, "2": 8.0, "M": 2.0,
    }

    # Also confirm the JSONB dict shape (what clients see on the wire).
    assert payload["estimaciones"] == [
        e.model_dump(mode="json") for e in model.estimaciones
    ]

    # Spot-check the schema classmethod agrees with the persisted shape.
    # We can't make a second HTTP call here (httpx_mock only registered
    # one response), so we synthesise 15 ``PartidoLosilla`` rows with the
    # canonical fixture values and verify the classmethod produces a list
    # whose contents round-trip through ``PartidoEstimacion.from_partidos``.
    from app.schemas.losilla import PartidoLosilla
    synth_partidos = [
        PartidoLosilla(
            orden=n,
            num=n,
            local="L", visitante="V",
            uno_porcentaje=35 if n == 1 else None,
            x_porcentaje=28 if n == 1 else None,
            dos_porcentaje=37 if n == 1 else None,
            pleno_local={"0": 2, "1": 7, "2": 14, "M": 77} if n == 15 else None,
            pleno_visitante={"0": 59, "1": 31, "2": 8, "M": 2} if n == 15 else None,
        )
        for n in range(1, 16)
    ]
    rebuilt = PartidoEstimacion.from_partidos(
        synth_partidos,
        fetched_at=model.estimaciones[0].fetched_at,
    )
    assert len(rebuilt) == 15
    assert [r.partido_orden for r in rebuilt] == list(range(1, 16))
    # For partido 15 specifically, the classmethod drops the source-side
    # ``int`` values into ``float`` dicts; the persisted JSONB shape is
    # already ``float`` (auto-coercion on read).
    assert rebuilt[14].pleno_local == p15.pleno_local
    assert rebuilt[14].pleno_visitante == p15.pleno_visitante


# ============================================================
# Pure-function unit tests for the synthetic p15 generator.
# No I/O; sync by design. pytest-asyncio emits a harmless "is not an
# async function" warning for each (because the module-level
# ``pytestmark = pytest.mark.asyncio`` applies to every collected test);
# the warnings do not affect exit code.
# ============================================================


def test_synthesize_pleno_15_comment_full_match_design_doc():
    """Verbatim match against the example in gestor-jornadas.md §6.1."""
    assert _synthesize_pleno_15_comment(
        {"0": 6, "1": 16, "2": 23, "M": 55},
        {"0": 51, "1": 34, "2": 12, "M": 3},
    ) == (
        "Local: 0=6%, 1=16%, 2=23%, M=55% \u00b7 "
        "Visita: 0=51%, 1=34%, 2=12%, M=3%"
    )


def test_synthesize_pleno_15_comment_both_absent_returns_empty_string():
    """Both sides fully absent → empty string (column NOT NULL)."""
    assert _synthesize_pleno_15_comment(None, None) == ""
    assert _synthesize_pleno_15_comment({}, {}) == ""


def test_synthesize_pleno_15_comment_partial_local_only():
    """Only Local has data → side label 'Local:' only (no separator)."""
    assert _synthesize_pleno_15_comment(
        {"0": 6, "1": 16, "2": 23, "M": 55}, None,
    ) == "Local: 0=6%, 1=16%, 2=23%, M=55%"


def test_synthesize_pleno_15_comment_partial_visitante_only():
    """Only Visitante has data → side label 'Visita:' only."""
    assert _synthesize_pleno_15_comment(
        None, {"0": 51, "1": 34, "2": 12, "M": 3},
    ) == "Visita: 0=51%, 1=34%, 2=12%, M=3%"


def test_synthesize_pleno_15_comment_missing_keys_are_omitted():
    """Missing keys must be omitted, not fabricated."""
    # Only 'M' on local, only '0' on visitante.
    assert _synthesize_pleno_15_comment(
        {"M": 55}, {"0": 51},
    ) == "Local: M=55% \u00b7 Visita: 0=51%"


def test_synthesize_pleno_15_comment_dash_value_becomes_omitted():
    """Upstream ships ``'-'`` for some keys; output must NOT contain them.

    Parsing converts ``'-'`` to ``None`` on the model. Confirm the
    generator then omits the key entirely.
    """
    assert _synthesize_pleno_15_comment(
        {"0": 6, "1": None, "2": 23, "M": None}, {"0": 51},
    ) == "Local: 0=6%, 2=23% \u00b7 Visita: 0=51%"


def test_synthesize_pleno_15_comment_rounds_to_whole_percent():
    """Decimals are rounded to whole percent per the design spec.

    Uses the ``_dec`` siblings' decimals (5.67 → 6, 16.28 → 16, etc.).
    """
    assert _synthesize_pleno_15_comment(
        {"0": 5.67, "1": 16.28, "2": 23.35, "M": 54.7},
        {"0": 50.83, "1": 34.39, "2": 11.64, "M": 3.14},
    ) == (
        "Local: 0=6%, 1=16%, 2=23%, M=55% \u00b7 "
        "Visita: 0=51%, 1=34%, 2=12%, M=3%"
    )


def test_synthesize_pleno_15_comment_uses_middle_dot_separator():
    """Sides joined by U+00B7 + spaces (NOT a hyphen or pipe)."""
    out = _synthesize_pleno_15_comment(
        {"0": 50}, {"0": 50},
    )
    assert "\u00b7" in out
    assert "-" not in out.split("Local:")[1].split("Visita:")[0]
    # Exactly one space on each side of the middle-dot.
    assert " \u00b7 " in out


