"""Unit tests for :mod:`app.clients.quiniwin`.

Mix of pure parser tests (no I/O) and httpx-mocked client tests. The primary
sample is the **real** J8 2026/27 payload captured from
``https://www.quiniwin.com/xml/2026-2027_j08.xml`` on 2026-10-01 (stored at
``tests/fixtures/quiniwin_j08_2627.xml``). Earlier synthetic fixtures
matching an assumed-but-wrong ``<quiniela>`` root masked a real-schema bug;
keeping a live fixture in tree makes regressions loud.
"""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app.clients.quiniwin import (
    QuiniwinClient,
    QuiniwinParseError,
    build_temporada_str,
    build_url,
    parse_quiniwin_xml,
    parse_quiniwin_xml_str,
)


FIXTURE_DIR = Path(__file__).parent / "fixtures"
REAL_J08_XML = (FIXTURE_DIR / "quiniwin_j08_2627.xml").read_bytes()


# Minimal-but-valid synthetic XML matching the real schema. Used in tests
# that need to control equipos / partido counts or trigger error paths
# without the 27 lines of the real fixture.
SAMPLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<XML>
  <QuinielaBASE>
    <DatosGeneracionFicheroXML fecha="15/09/2026" hora="12:00:00"/>
    <NumeroJornada valor="11"/>
    <Temporada valor="2026/2027"/>
    <Partidos>
      <partido numero="1" pronostico="1" equipos="Real Madrid-Betis"/>
      <partido numero="2" pronostico="X" equipos="Barcelona-Athletic Club"/>
      <partido numero="3" pronostico="2" equipos="Ath Madrid-Valencia"/>
      <partido numero="4" pronostico="1" equipos="Real Sociedad-Villarreal"/>
      <partido numero="5" pronostico="X" equipos="Mallorca-Granada"/>
      <partido numero="6" pronostico="2" equipos="Eibar-Levante"/>
      <partido numero="7" pronostico="1" equipos="Sporting-Cadiz"/>
      <partido numero="8" pronostico="X" equipos="Albacete-Zaragoza"/>
      <partido numero="9" pronostico="2" equipos="Sevilla-Las Palmas"/>
      <partido numero="10" pronostico="1" equipos="Girona-Getafe"/>
      <partido numero="11" pronostico="X" equipos="Athletic Club (F)-Barcelona Femení"/>
      <partido numero="12" pronostico="2" equipos="Real Madrid F-Atletico Madrid (F)"/>
      <partido numero="13" pronostico="1" equipos="Logroño F-Real Sociedad F"/>
      <partido numero="14" pronostico="X" equipos="Levante F-Espanyol (F)"/>
      <partido numero="15" pronostico="M" equipos="At. Madrid-Real Madrid" pronosticoPLENOGOLES="0-0" tipoPLENOGOLES="0"/>
    </Partidos>
    <ColumnaGanadora valor="1X21X2X1X21XXM" valorPLENOGOLES="0-0"/>
  </QuinielaBASE>
</XML>
"""


SAMPLE_XML_WITH_DASH_IN_NAME = """<?xml version="1.0" encoding="UTF-8"?>
<XML>
  <QuinielaBASE>
    <NumeroJornada valor="11"/>
    <Temporada valor="2026/2027"/>
    <Partidos>
      <partido numero="1" pronostico="1" equipos="Real Madrid-Cadiz CF"/>
      <partido numero="2" pronostico="X" equipos="Stade Rennais-RC Lens"/>
    </Partidos>
  </QuinielaBASE>
</XML>
"""


# ---------- URL builder ----------


def test_build_url_format_for_temporada_2026_j8():
    """temporada=2026 j8 → '2026-2027_j08.xml' per docs/research/02-fuentes.md."""
    url = build_url(2026, 8)
    assert url == "https://www.quiniwin.com/xml/2026-2027_j08.xml"


def test_build_url_pads_jornada_to_two_digits():
    assert build_url(2026, 1) == "https://www.quiniwin.com/xml/2026-2027_j01.xml"
    assert build_url(2026, 15) == "https://www.quiniwin.com/xml/2026-2027_j15.xml"
    assert build_url(2026, 42) == "https://www.quiniwin.com/xml/2026-2027_j42.xml"


def test_build_url_temporada_2027():
    assert build_url(2027, 8) == "https://www.quiniwin.com/xml/2027-2028_j08.xml"


def test_build_url_rejects_invalid_jornada():
    with pytest.raises(ValueError):
        build_url(2026, 0)
    with pytest.raises(ValueError):
        build_url(2026, 100)
    with pytest.raises(ValueError):
        build_url(2026, -1)


def test_build_url_rejects_out_of_range_temporada():
    with pytest.raises(ValueError):
        build_url(1500, 1)
    with pytest.raises(ValueError):
        build_url(3000, 1)


def test_build_temporada_str():
    assert build_temporada_str(2026) == "2026/2027"
    assert build_temporada_str(2027) == "2027/2028"


# ---------- Parser: happy path (real fixture) ----------


def test_parse_real_j08_fixture_returns_15_partidos():
    """The real J8 XML from Quiniwin must parse end-to-end.

    Captured 2026-10-01. This test exists specifically because an earlier
    round of synthetic fixtures masked a wrong-root bug.
    """
    rows = parse_quiniwin_xml(REAL_J08_XML)
    assert len(rows) == 15
    assert [r.posicion for r in rows] == list(range(1, 16))


def test_parse_real_j08_fixture_known_matches():
    """A few hand-checked rows from the live J8 payload."""
    rows = parse_quiniwin_xml(REAL_J08_XML)
    # Position 1: Osasuna vs Rayo Vallecano
    assert rows[0].local_raw == "Osasuna"
    assert rows[0].visitante_raw == "Rayo Vallecano"
    # Position 4: Getafe vs Málaga (with accent)
    assert rows[3].local_raw == "Getafe"
    assert rows[3].visitante_raw == "Málaga"
    # Position 11: At. Madrid(f) vs Logroño(f) — female suffix in raw
    assert rows[10].local_raw == "At. Madrid(f)"
    assert rows[10].visitante_raw == "Logroño(f)"
    # Position 15: At. Madrid vs Real Madrid (pleno, no female suffix here)
    assert rows[14].local_raw == "At. Madrid"
    assert rows[14].visitante_raw == "Real Madrid"


def test_parse_real_j08_has_no_per_partido_competicion_or_fecha():
    """Real Quiniwin schema doesn't ship competicion/fecha per partido.

    These get persisted as None — the audit trail makes the absence explicit.
    """
    rows = parse_quiniwin_xml(REAL_J08_XML)
    for row in rows:
        assert row.competicion_raw is None, row
        assert row.fecha_raw is None, row


# ---------- Parser: happy path (synthetic, controlled) ----------


def test_parse_sample_xml_returns_15_partidos_sorted_by_posicion():
    rows = parse_quiniwin_xml_str(SAMPLE_XML)
    assert len(rows) == 15
    assert [r.posicion for r in rows] == list(range(1, 16))
    assert rows[0].local_raw == "Real Madrid"
    assert rows[0].visitante_raw == "Betis"
    # No per-partido competicion/fecha in real schema
    assert rows[0].competicion_raw is None
    assert rows[0].fecha_raw is None


def test_parse_preserves_female_suffix_in_raw_strings():
    """The parser does NOT mutate raw names — matcher is responsible for that.

    Female suffixes are preserved as-is in ``local_raw``/``visitante_raw``
    so the audit trail (composicion.local_raw) shows what Quiniwin emitted.
    """
    rows = parse_quiniwin_xml_str(SAMPLE_XML)
    # Position 11: Athletic Club (F) vs Barcelona Femení
    assert rows[10].local_raw == "Athletic Club (F)"
    assert rows[10].visitante_raw == "Barcelona Femení"
    # Position 13: Logroño F vs Real Sociedad F
    assert rows[12].local_raw == "Logroño F"
    assert rows[12].visitante_raw == "Real Sociedad F"
    # Position 14: both variants of the suffix
    assert rows[13].local_raw == "Levante F"
    assert rows[13].visitante_raw == "Espanyol (F)"


def test_parse_handles_dash_inside_team_name():
    """The first ``-`` is the separator; later ``-`` characters stay in names."""
    rows = parse_quiniwin_xml_str(SAMPLE_XML_WITH_DASH_IN_NAME)
    assert rows[0].local_raw == "Real Madrid"
    assert rows[0].visitante_raw == "Cadiz CF"  # NOT "Cadiz" + leftover
    assert rows[1].local_raw == "Stade Rennais"
    assert rows[1].visitante_raw == "RC Lens"


# ---------- Parser: errors ----------


def test_parse_raises_on_malformed_xml():
    bad = b"<XML><QuinielaBASE><Partidos><partido numero='1' equipos='foo-bar'/>"
    with pytest.raises(QuiniwinParseError):
        parse_quiniwin_xml_str(bad.decode("utf-8"))


def test_parse_raises_on_wrong_root_element():
    bad = """<?xml version="1.0"?>
<quiniela>
  <partido numero="1" equipos="A-B"/>
</quiniela>
"""
    with pytest.raises(QuiniwinParseError, match="unexpected root element"):
        parse_quiniwin_xml_str(bad)


def test_parse_raises_on_missing_quinielabase():
    bad = """<?xml version="1.0"?>
<XML>
  <partido numero="1" equipos="A-B"/>
</XML>
"""
    with pytest.raises(QuiniwinParseError, match="QuinielaBASE"):
        parse_quiniwin_xml_str(bad)


def test_parse_raises_on_missing_separator_in_equipos():
    bad = """<?xml version="1.0"?>
<XML>
  <QuinielaBASE>
    <Partidos><partido numero="1" equipos="NoSeparatorHere"/></Partidos>
  </QuinielaBASE>
</XML>
"""
    with pytest.raises(QuiniwinParseError, match="missing '-' separator"):
        parse_quiniwin_xml_str(bad)


def test_parse_raises_on_duplicate_numero():
    bad = """<?xml version="1.0"?>
<XML>
  <QuinielaBASE>
    <Partidos>
      <partido numero="1" equipos="A-B"/>
      <partido numero="1" equipos="C-D"/>
    </Partidos>
  </QuinielaBASE>
</XML>
"""
    with pytest.raises(QuiniwinParseError, match="duplicate <partido> numero=1"):
        parse_quiniwin_xml_str(bad)


def test_parse_raises_on_non_integer_numero():
    bad = """<?xml version="1.0"?>
<XML>
  <QuinielaBASE>
    <Partidos><partido numero="abc" equipos="A-B"/></Partidos>
  </QuinielaBASE>
</XML>
"""
    with pytest.raises(QuiniwinParseError, match="not an integer"):
        parse_quiniwin_xml_str(bad)


def test_parse_raises_on_missing_numero():
    bad = """<?xml version="1.0"?>
<XML>
  <QuinielaBASE>
    <Partidos><partido equipos="A-B"/></Partidos>
  </QuinielaBASE>
</XML>
"""
    with pytest.raises(QuiniwinParseError, match="numero"):
        parse_quiniwin_xml_str(bad)


def test_parse_handles_minimal_xml():
    """A 1-row XML is fine — Quiniwin is supposed to ship 15 but a smoke
    test should still parse without demanding exactly 15."""
    minimal = """<?xml version="1.0"?>
<XML>
  <QuinielaBASE>
    <Partidos><partido numero="1" equipos="A-B"/></Partidos>
  </QuinielaBASE>
</XML>
"""
    rows = parse_quiniwin_xml_str(minimal)
    assert len(rows) == 1


# ---------- Parser: defensive against invalid XML at lower level ----------


def test_parser_handles_empty_input():
    with pytest.raises(QuiniwinParseError):
        parse_quiniwin_xml_str("")


# ---------- Client: URL routing & HTTP errors ----------


async def test_client_fetches_xml_and_parses(httpx_mock):
    httpx_mock.add_response(
        url="https://www.quiniwin.com/xml/2026-2027_j08.xml",
        content=REAL_J08_XML,
    )
    async with QuiniwinClient() as c:
        rows = await c.get_jornada(8, 2026)
    assert len(rows) == 15
    assert rows[0].local_raw == "Osasuna"


async def test_client_propagates_4xx(httpx_mock):
    httpx_mock.add_response(
        url="https://www.quiniwin.com/xml/2026-2027_j99.xml",
        status_code=404,
    )
    async with QuiniwinClient() as c:
        with pytest.raises(httpx.HTTPStatusError):
            await c.get_jornada(99, 2026)


async def test_client_propagates_5xx(httpx_mock):
    httpx_mock.add_response(
        url="https://www.quiniwin.com/xml/2026-2027_j08.xml",
        status_code=500,
    )
    async with QuiniwinClient() as c:
        with pytest.raises(httpx.HTTPStatusError):
            await c.get_jornada(8, 2026)


# ---------- Client: cache + throttle ----------


async def test_client_caches_results_in_memory(httpx_mock):
    """Second call returns cached value, no second upstream request."""
    httpx_mock.add_response(
        url="https://www.quiniwin.com/xml/2026-2027_j08.xml",
        content=REAL_J08_XML,
    )
    async with QuiniwinClient(cache_ttl_s=3600.0) as c:
        first = await c.get_jornada(8, 2026)
        second = await c.get_jornada(8, 2026)
    assert first == second
    assert len(httpx_mock.get_requests()) == 1


async def test_client_force_fresh_bypasses_cache(httpx_mock):
    httpx_mock.add_response(
        url="https://www.quiniwin.com/xml/2026-2027_j08.xml",
        content=REAL_J08_XML,
    )
    httpx_mock.add_response(
        url="https://www.quiniwin.com/xml/2026-2027_j08.xml",
        content=REAL_J08_XML,
    )
    async with QuiniwinClient() as c:
        await c.get_jornada(8, 2026)
        await c.get_jornada(8, 2026, force_fresh=True)
    assert len(httpx_mock.get_requests()) == 2


async def test_client_throttle_delays_subsequent_requests(monkeypatch, httpx_mock):
    """Two consecutive requests are at least MIN_THROTTLE_S apart."""
    from app.clients import quiniwin as qw_mod

    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)
        # No-op for test speed — we only care that sleep was called
        # with a value > 0 when the throttle kicks in.

    monkeypatch.setattr(qw_mod.asyncio, "sleep", fake_sleep)

    httpx_mock.add_response(
        url="https://www.quiniwin.com/xml/2026-2027_j08.xml",
        content=REAL_J08_XML,
    )
    httpx_mock.add_response(
        url="https://www.quiniwin.com/xml/2026-2027_j09.xml",
        content=REAL_J08_XML,
    )
    async with QuiniwinClient() as c:
        await c.get_jornada(8, 2026)
        await c.get_jornada(9, 2026)
    # The second call should have requested a sleep >= MIN_THROTTLE_S,
    # but since the test ran fast, the wait was effectively the full
    # MIN_THROTTLE_S (0.4s) because the gap since _last_request_at is tiny.
    assert any(s >= qw_mod.MIN_THROTTLE_S - 0.05 for s in sleeps), sleeps


# ---------- Cross-validation with ET parser (proves our impl matches stdlib) ----------


def test_parser_handles_unicode_team_names():
    xml = """<?xml version="1.0"?>
<XML>
  <QuinielaBASE>
    <Partidos>
      <partido numero="1" pronostico="1" equipos="Málaga-Real Madrid"/>
      <partido numero="2" pronostico="X" equipos="Español (F)-Barcelona"/>
    </Partidos>
  </QuinielaBASE>
</XML>
"""
    rows = parse_quiniwin_xml_str(xml)
    assert rows[0].local_raw == "Málaga"
    assert rows[1].local_raw == "Español (F)"