"""Tests for :mod:`app.scripts.ingest_quiniwin`.

Uses pytest-httpx to mock the Quiniwin XML feed and the real Postgres DB
(so we exercise the upsert / idempotency logic end-to-end). The autouse
``_cleanup_rows`` fixture wipes the test-created rows after each test.
"""
from __future__ import annotations

import logging

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text

from app.clients.quiniwin import QuiniwinClient
from app.db.models import Composicion, Jornada, JornadaStatus, TeamIdentityMap
from app.db.session import AsyncSessionLocal, engine
from app.scripts.ingest_quiniwin import parse_jornadas, run_ingest


SAMPLE_J8_XML = """<?xml version="1.0" encoding="UTF-8"?>
<XML>
  <QuinielaBASE>
    <DatosGeneracionFicheroXML fecha="15/09/2026" hora="12:00:00"/>
    <NumeroJornada valor="8"/>
    <Temporada valor="2026/2027"/>
    <Partidos>
      <partido numero="1"  pronostico="1" equipos="Real Madrid-Betis"/>
      <partido numero="2"  pronostico="X" equipos="Barcelona-Athletic Club"/>
      <partido numero="3"  pronostico="2" equipos="Ath Madrid-Valencia"/>
      <partido numero="4"  pronostico="1" equipos="Real Sociedad-Villarreal"/>
      <partido numero="5"  pronostico="X" equipos="Mallorca-Granada"/>
      <partido numero="6"  pronostico="2" equipos="Eibar-Levante"/>
      <partido numero="7"  pronostico="1" equipos="Sporting-Cadiz"/>
      <partido numero="8"  pronostico="X" equipos="Albacete-Zaragoza"/>
      <partido numero="9"  pronostico="2" equipos="Sevilla-Las Palmas"/>
      <partido numero="10" pronostico="1" equipos="Girona-Getafe"/>
      <partido numero="11" pronostico="X" equipos="Athletic Club (F)-Barcelona Femení"/>
      <partido numero="12" pronostico="2" equipos="Real Madrid F-Atletico Madrid (F)"/>
      <partido numero="13" pronostico="1" equipos="Logroño F-Real Sociedad F"/>
      <partido numero="14" pronostico="X" equipos="Levante F-Espanyol (F)"/>
      <partido numero="15" pronostico="M" equipos="UEFA Champions League-Juego 15" pronosticoPLENOGOLES="0-0" tipoPLENOGOLES="0"/>
    </Partidos>
    <ColumnaGanadora valor="1X21X2X1X21XXM" valorPLENOGOLES="0-0"/>
  </QuinielaBASE>
</XML>
"""


SAMPLE_J9_XML = """<?xml version="1.0" encoding="UTF-8"?>
<XML>
  <QuinielaBASE>
    <DatosGeneracionFicheroXML fecha="22/09/2026" hora="12:00:00"/>
    <NumeroJornada valor="9"/>
    <Temporada valor="2026/2027"/>
    <Partidos>
      <partido numero="1"  pronostico="1" equipos="Betis-Barcelona"/>
      <partido numero="2"  pronostico="X" equipos="Valencia-Sevilla"/>
      <partido numero="3"  pronostico="2" equipos="Getafe-Mallorca"/>
      <partido numero="4"  pronostico="1" equipos="Granada-Eibar"/>
      <partido numero="5"  pronostico="X" equipos="Las Palmas-Girona"/>
      <partido numero="6"  pronostico="2" equipos="Zaragoza-Sporting"/>
      <partido numero="7"  pronostico="1" equipos="Athletic Club-Levante"/>
      <partido numero="8"  pronostico="X" equipos="Atletico Madrid-Real Sociedad"/>
      <partido numero="9"  pronostico="2" equipos="Cadiz-Albacete"/>
      <partido numero="10" pronostico="1" equipos="Villarreal-Ath Madrid"/>
      <partido numero="11" pronostico="X" equipos="Equipo Desconocido 1-Equipo Desconocido 2"/>
      <partido numero="12" pronostico="2" equipos="Equipo Desconocido 3-Equipo Desconocido 4"/>
      <partido numero="13" pronostico="1" equipos="Equipo Desconocido 5-Equipo Desconocido 6"/>
      <partido numero="14" pronostico="X" equipos="Equipo Desconocido 7-Equipo Desconocido 8"/>
      <partido numero="15" pronostico="M" equipos="PLENO15-Juego 15" pronosticoPLENOGOLES="0-0" tipoPLENOGOLES="0"/>
    </Partidos>
    <ColumnaGanadora valor="1X21X2X1X21XXM" valorPLENOGOLES="0-0"/>
  </QuinielaBASE>
</XML>
"""


SAMPLE_MALFORMED_XML = """<?xml version="1.0"?>
<XML>
  <QuinielaBASE>
    <Partidos>
      <partido numero="1" equipos="A-B
      <!-- truncated
"""


# ---------- Cleanup ----------


@pytest_asyncio.fixture(autouse=True)
async def _cleanup_rows():
    """Wipe jornada + composicion + auto/test team rows."""
    yield
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM jornada_escrutinio"))
        await conn.execute(text("DELETE FROM jornada_comentario"))
        await conn.execute(text("DELETE FROM composicion"))
        await conn.execute(text("DELETE FROM fetch_session"))
        await conn.execute(text("DELETE FROM jornada"))
        await conn.execute(
            text(
                "DELETE FROM team_identity_map "
                "WHERE entity_id LIKE 'test-%' OR entity_id LIKE 'auto-%'"
            )
        )


def _url_for(temporada: int, j: int) -> str:
    return f"https://www.quiniwin.com/xml/{temporada}-{temporada + 1}_j{j:02d}.xml"


# ---------- CLI arg parsing ----------


def test_parse_jornadas_single():
    assert parse_jornadas("5") == [5]


def test_parse_jornadas_range():
    assert parse_jornadas("1..10") == list(range(1, 11))


def test_parse_jornadas_range_reversed():
    """Smaller..larger normalisation (allow 10..1 too)."""
    assert parse_jornadas("10..1") == list(range(1, 11))


def test_parse_jornadas_list():
    assert parse_jornadas("1,3,5,8") == [1, 3, 5, 8]


def test_parse_jornadas_list_with_spaces():
    assert parse_jornadas("1, 3 ,5 , 8") == [1, 3, 5, 8]


def test_parse_jornadas_rejects_garbage():
    with pytest.raises(SystemExit):
        parse_jornadas("foo")


def test_parse_jornadas_rejects_empty():
    with pytest.raises(SystemExit):
        parse_jornadas("")


# ---------- Happy path ----------


async def test_ingest_one_jornada_creates_1_jornada_and_15_composicion(
    httpx_mock, caplog,
):
    caplog.set_level(logging.WARNING, logger="team_matcher")
    httpx_mock.add_response(
        url=_url_for(2026, 8),
        content=SAMPLE_J8_XML.encode("utf-8"),
    )
    client = QuiniwinClient()
    summary = await run_ingest(2026, [8], client=client)
    await client.close()

    assert summary.jornadas_inserted == 1
    assert summary.jornadas_existing == 0
    assert summary.composicion_inserted == 15
    assert summary.composicion_existing == 0
    assert summary.stubs_created >= 1  # At least the "UEFA Champions League" team is unknown
    assert summary.network_failures == []

    async with AsyncSessionLocal() as session:
        j = await session.scalar(
            select(Jornada).where(
                Jornada.temporada == 2026, Jornada.numero == 8,
            )
        )
        assert j is not None
        assert j.temporada_str == "2026/2027"
        assert j.status == JornadaStatus.draft  # default is draft

        comp_count = await session.scalar(
            select(func.count()).select_from(Composicion).where(
                Composicion.jornada_id == j.id,
            )
        )
        assert comp_count == 15


async def test_ingest_persists_competicion_raw_as_none_when_unspecified(httpx_mock):
    """The real Quiniwin schema does NOT ship competicion per partido.

    T-data-1 persists ``competicion_raw=None`` (not a placeholder string)
    so the audit trail makes the absence explicit. Resolving the competicion
    is left for a future admin task (MVP-03).
    """
    httpx_mock.add_response(
        url=_url_for(2026, 8),
        content=SAMPLE_J8_XML.encode("utf-8"),
    )
    client = QuiniwinClient()
    await run_ingest(2026, [8], client=client)
    await client.close()

    async with AsyncSessionLocal() as session:
        rows = (await session.execute(
            select(Composicion).where(Composicion.posicion == 11)
        )).scalars().all()
        assert len(rows) == 1
        assert rows[0].competicion_raw is None
        assert rows[0].kickoff_at is None


async def test_ingest_creates_female_stubs_for_teams_outside_seed(httpx_mock):
    """Stub creation still kicks in for Liga F teams the seed doesn't cover.

    After the short_name fix the matcher resolves most Liga F sides to
    seeded women rows (e.g. "Atletico Madrid (F)" → seed women row via
    short_name "Atlético Madrid"). The remaining Liga F stubs are the ones
    with NO canonical coverage. The honest witness in SAMPLE_J8 is
    "Logroño F" → "Logroño", which the seed stores as "Edf Logrono W" —
    a different canonical — so it correctly becomes a women stub.
    """
    httpx_mock.add_response(
        url=_url_for(2026, 8),
        content=SAMPLE_J8_XML.encode("utf-8"),
    )
    client = QuiniwinClient()
    summary = await run_ingest(2026, [8], client=client)
    await client.close()

    async with AsyncSessionLocal() as session:
        women_stubs = (await session.execute(
            select(TeamIdentityMap).where(
                TeamIdentityMap.entity_id.like("auto-%"),
                TeamIdentityMap.gender == "women",
            )
        )).scalars().all()

    # At least the 'Logroño' Liga F side stub must be created. Lower bound
    # of 1 keeps the test honest without over-fitting to seed shape.
    assert len(women_stubs) >= 1
    canon_names = {s.canonical_name for s in women_stubs}
    assert "Logroño" in canon_names


async def test_ingest_is_idempotent(httpx_mock):
    """Run twice — second run inserts 0 rows."""
    httpx_mock.add_response(
        url=_url_for(2026, 8),
        content=SAMPLE_J8_XML.encode("utf-8"),
    )
    client = QuiniwinClient()
    s1 = await run_ingest(2026, [8], client=client)
    assert s1.jornadas_inserted == 1
    assert s1.composicion_inserted == 15

    # Second run — same client, second httpx_mock.add_response so it doesn't 404.
    httpx_mock.add_response(
        url=_url_for(2026, 8),
        content=SAMPLE_J8_XML.encode("utf-8"),
    )
    # Fresh client so cache from run 1 doesn't taint run 2.
    await client.close()
    client = QuiniwinClient()
    s2 = await run_ingest(2026, [8], client=client)
    await client.close()

    assert s2.jornadas_inserted == 0
    assert s2.jornadas_existing == 1
    assert s2.composicion_inserted == 0
    assert s2.composicion_existing == 15


async def test_ingest_multiple_jornadas(httpx_mock):
    """Ingesting J8 + J9 in one run processes both."""
    httpx_mock.add_response(
        url=_url_for(2026, 8),
        content=SAMPLE_J8_XML.encode("utf-8"),
    )
    httpx_mock.add_response(
        url=_url_for(2026, 9),
        content=SAMPLE_J9_XML.encode("utf-8"),
    )
    client = QuiniwinClient()
    summary = await run_ingest(2026, [8, 9], client=client)
    await client.close()

    assert summary.jornadas_inserted == 2
    assert summary.composicion_inserted == 30

    async with AsyncSessionLocal() as session:
        nums = (await session.execute(
            select(Jornada.numero).where(Jornada.temporada == 2026)
        )).scalars().all()
    assert set(nums) == {8, 9}


async def test_ingest_handles_malformed_xml_as_network_failure(httpx_mock, caplog):
    """A broken XML response is recorded in network_failures without crashing the run."""
    caplog.set_level(logging.WARNING, logger="ingest_quiniwin")
    httpx_mock.add_response(
        url=_url_for(2026, 8),
        content=SAMPLE_MALFORMED_XML.encode("utf-8"),
    )
    httpx_mock.add_response(
        url=_url_for(2026, 9),
        content=SAMPLE_J9_XML.encode("utf-8"),
    )
    client = QuiniwinClient()
    summary = await run_ingest(2026, [8, 9], client=client)
    await client.close()

    # J8 fails (malformed), J9 succeeds.
    assert 8 in summary.network_failures
    assert 9 not in summary.network_failures
    assert summary.jornadas_inserted == 1  # only J9


async def test_ingest_dry_run_does_not_touch_db():
    """--dry-run prints plan, hits no DB and no upstream."""
    summary = await run_ingest(2026, [8], dry_run=True)
    assert summary.jornadas_inserted == 0
    assert summary.composicion_inserted == 0

    async with AsyncSessionLocal() as session:
        n = await session.scalar(select(func.count()).select_from(Jornada))
    assert n == 0


async def test_ingest_creates_unknown_team_stubs_for_unknown_names(httpx_mock):
    """J9 has 8 'Equipo Desconocido N' names → all become auto-* stubs."""
    httpx_mock.add_response(
        url=_url_for(2026, 9),
        content=SAMPLE_J9_XML.encode("utf-8"),
    )
    client = QuiniwinClient()
    summary = await run_ingest(2026, [9], client=client)
    await client.close()
    # 8 unique Equipo Desconocido N names = 8 stubs (Liga F side is 1-14,
    # but our sample has 8 distinct names × 2 sides = ... actually re-check).
    # The Liga F positions in SAMPLE_J9 are 11-14: 8 sides = 8 distinct stubs.
    assert summary.stubs_created >= 8

    async with AsyncSessionLocal() as session:
        auto_count = await session.scalar(
            select(func.count()).select_from(TeamIdentityMap).where(
                TeamIdentityMap.entity_id.like("auto-%"),
            )
        )
    assert auto_count >= 8


async def test_ingest_returns_summary_with_temporada_str(httpx_mock):
    httpx_mock.add_response(
        url=_url_for(2026, 8),
        content=SAMPLE_J8_XML.encode("utf-8"),
    )
    client = QuiniwinClient()
    summary = await run_ingest(2026, [8], client=client)
    await client.close()
    assert summary.temporada == 2026
    assert summary.temporada_str == "2026/2027"
    rendered = summary.render()
    assert "2026" in rendered
    assert "2026/2027" in rendered


async def test_ingest_does_not_call_upstream_for_cached_jornada(httpx_mock):
    """Ingesting J8 twice in one run only hits the network once (cache hit)."""
    httpx_mock.add_response(
        url=_url_for(2026, 8),
        content=SAMPLE_J8_XML.encode("utf-8"),
    )
    client = QuiniwinClient()
    summary = await run_ingest(2026, [8, 8], client=client)
    await client.close()
    # Two jornadaada rows requested but cache hit on the second means no
    # duplicate insert. The test asserts insert counters, not network calls.
    assert summary.jornadas_inserted == 1
    assert summary.composicion_inserted == 15
    assert len(httpx_mock.get_requests()) == 1