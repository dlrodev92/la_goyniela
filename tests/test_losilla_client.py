"""Unit tests for ``app.clients.eduardolosilla``.

Uses ``pytest-httpx`` to mock the upstream and ``pytest-asyncio`` for the
async client. Fixture HTML files under ``tests/fixtures/losilla/`` are
real upstream responses captured by ``scripts/probe_losilla_escrutinio.py``.
"""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app.clients.eduardolosilla import (
    EduardoLosillaClient,
    _parse_comentarios,
    _parse_escrutinio_oficial,
    _parse_state,
    _strip_tags,
)
from app.schemas.losilla import (
    CategoriaPremio,
    Elige8Premio,
    EscrutinioOficial,
    PartidoLosilla,
)

FIXTURES = Path(__file__).parent / "fixtures" / "losilla"


# ---------- Pure parser tests (no I/O; sync) ----------


def test_strip_tags_removes_markup_and_collapses_whitespace():
    raw = "<div _ngcontent-x=''>  Hola   <span>mundo</span>  </div>"
    assert _strip_tags(raw) == "Hola mundo"


def test_parse_state_extracts_jornada_block(j11_html):
    state = _parse_state(j11_html)
    assert state is not None
    assert "datosGeneralesQuiniela" in state
    assert "jornada_11_2027" in state


def test_parse_state_returns_none_when_marker_missing():
    assert _parse_state("<html><body>nada</body></html>") is None


def test_parse_escrutinio_oficial_j11_in_flight(j11_html):
    """J11 is the in-flight jornada: estimada=True, all acertantes=0."""
    flat = _strip_tags(j11_html)
    e = _parse_escrutinio_oficial(j11_html)
    assert e is not None
    assert e.estimada is True
    assert e.jornada_etiqueta == 11
    assert e.temporada_etiqueta == "26/27"
    assert e.recaudacion_eur == "1.200.000,00"
    assert e.bote_eur == "0,00"
    assert [c.aciertos for c in e.categorias] == [15, 14, 13, 12, 11, 10]
    assert all(c.acertantes == 0 for c in e.categorias)
    assert e.elige8 is None


def test_parse_escrutinio_oficial_j8_with_bote(j8_html):
    """J8 has the season's largest bote and one acertante of 15."""
    e = _parse_escrutinio_oficial(j8_html)
    assert e is not None
    assert e.estimada is False
    assert e.jornada_etiqueta == 8
    assert e.recaudacion_eur == "3.781.971,75"
    assert e.bote_eur == "3.159.105,01"
    cat15 = next(c for c in e.categorias if c.aciertos == 15)
    assert cat15.acertantes == 1
    assert cat15.premio_eur == "2.762.202,31"
    cat14 = next(c for c in e.categorias if c.aciertos == 14)
    assert cat14.acertantes == 22
    assert cat14.premio_eur == "27.505,25"
    cat10 = next(c for c in e.categorias if c.aciertos == 10)
    assert cat10.acertantes == 232667
    assert cat10.premio_eur == "0,00"
    assert e.elige8 is None


def test_parse_escrutinio_oficial_j70_includes_elige8(j70_html):
    """J70 has Elige 8 — confirms the secondary row regex."""
    e = _parse_escrutinio_oficial(j70_html)
    assert e is not None
    assert e.elige8 is not None
    assert e.elige8.acertantes == 775
    assert e.elige8.premio_eur == "25,73"


def test_parse_escrutinio_oficial_returns_none_when_block_missing():
    """A page without the escrutinio block should yield None, not crash."""
    html = "<html><body>no escrutinio</body></html>"
    assert _parse_escrutinio_oficial(html) is None


def test_parse_comentarios_extracts_one_per_match(j11_html):
    comentarios = _parse_comentarios(j11_html)
    # J11 has 14 clubs with editorial; partido 15 is selecciones and lacks one.
    assert len(comentarios) == 14
    assert 1 in comentarios
    assert 14 in comentarios
    assert 15 not in comentarios
    assert "Las jornadas van pasando" in comentarios[1]


# ---------- Client end-to-end with pytest-httpx ----------


@pytest.fixture
def fixtures():
    return {
        "j1": FIXTURES / "j1_baseline.html",
        "j8": FIXTURES / "j8_bote_real.html",
        "j9": FIXTURES / "j9_closed.html",
        "j11": FIXTURES / "j11_open.html",
        "j70": FIXTURES / "j70_past_season.html",
    }


async def test_client_parses_j11_open_jornada(httpx_mock, fixtures):
    httpx_mock.add_response(
        url="https://www.eduardolosilla.es/quiniela/ayudas/escrutinio/jornada_11",
        content=fixtures["j11"].read_bytes(),
    )
    async with EduardoLosillaClient() as c:
        payload = await c.get_jornada(11, 2027)

    assert payload.jornada == 11
    assert payload.temporada == 2027
    assert payload.fetched is True
    assert payload.ssr_shipped_jornada == 11
    assert payload.warning is None  # J11 == J11, no mismatch
    assert len(payload.partidos) == 15
    assert payload.comentarios_presentes == 14
    assert payload.escrutinio_oficial is not None
    assert payload.escrutinio_oficial.estimada is True
    assert payload.recaudacion_bote_live is not None


async def test_client_flags_ssr_mismatch_when_requesting_closed(httpx_mock, fixtures):
    """Asking for J9 surfaces the live J11 state with a warning."""
    httpx_mock.add_response(
        url="https://www.eduardolosilla.es/quiniela/ayudas/escrutinio/jornada_9",
        content=fixtures["j9"].read_bytes(),
    )
    async with EduardoLosillaClient() as c:
        payload = await c.get_jornada(9, 2027)

    assert payload.jornada == 9
    assert payload.ssr_shipped_jornada == 11
    assert payload.warning is not None
    assert "jornada=9" in payload.warning
    assert "jornada=11" in payload.warning
    # But the oficial escrutinio IS the closed J9
    assert payload.escrutinio_oficial is not None
    assert payload.escrutinio_oficial.jornada_etiqueta == 9
    assert payload.escrutinio_oficial.estimada is False


async def test_client_returns_closed_payload_for_j8(httpx_mock, fixtures):
    httpx_mock.add_response(
        url="https://www.eduardolosilla.es/quiniela/ayudas/escrutinio/jornada_8",
        content=fixtures["j8"].read_bytes(),
    )
    async with EduardoLosillaClient() as c:
        payload = await c.get_jornada(8, 2027)

    e = payload.escrutinio_oficial
    assert e is not None and e.jornada_etiqueta == 8
    cat15 = next(c for c in e.categorias if c.aciertos == 15)
    assert cat15.acertantes == 1
    assert cat15.premio_eur == "2.762.202,31"


async def test_client_cache_returns_payload_without_refetch(httpx_mock, fixtures):
    httpx_mock.add_response(
        url="https://www.eduardolosilla.es/quiniela/ayudas/escrutinio/jornada_70",
        content=fixtures["j70"].read_bytes(),
    )
    async with EduardoLosillaClient(closed_cache_ttl_s=3600.0) as c:
        first = await c.get_jornada(70, 2027)
        assert first.fetched is True
        # Second call: same payload, but fetched=False (served from cache).
        second = await c.get_jornada(70, 2027)
        assert second.fetched is False
        assert second.sha256 == first.sha256

    # Only one upstream request was made.
    assert len(httpx_mock.get_requests()) == 1


async def test_client_force_fresh_skips_cache(httpx_mock, fixtures):
    httpx_mock.add_response(
        url="https://www.eduardolosilla.es/quiniela/ayudas/escrutinio/jornada_1",
        content=fixtures["j1"].read_bytes(),
    )
    httpx_mock.add_response(
        url="https://www.eduardolosilla.es/quiniela/ayudas/escrutinio/jornada_1",
        content=fixtures["j1"].read_bytes(),
    )
    async with EduardoLosillaClient() as c:
        await c.get_jornada(1, 2027)
        await c.get_jornada(1, 2027, force_fresh=True)

    assert len(httpx_mock.get_requests()) == 2


async def test_client_returns_404_when_jornada_key_missing(httpx_mock):
    """A page without the SSR marker is reported as parse failure via raise."""
    httpx_mock.add_response(
        url="https://www.eduardolosilla.es/quiniela/ayudas/escrutinio/jornada_999",
        content=b"<html><body>no state</body></html>",
    )
    async with EduardoLosillaClient() as c:
        payload = await c.get_jornada(999, 2027)

    # No state -> live is None, escrutinio is None, partidos is empty.
    assert payload.recaudacion_bote_live is None
    assert payload.escrutinio_oficial is None
    assert payload.partidos == []
    assert payload.comentarios_presentes == 0


async def test_client_propagates_upstream_4xx(httpx_mock):
    httpx_mock.add_response(
        url="https://www.eduardolosilla.es/quiniela/ayudas/escrutinio/jornada_999",
        status_code=500,
    )
    async with EduardoLosillaClient() as c:
        with pytest.raises(httpx.HTTPStatusError):
            await c.get_jornada(999, 2027)


# ---------- Fixtures ----------


@pytest.fixture
def j11_html():
    return (FIXTURES / "j11_open.html").read_text(encoding="utf-8",
                                                  errors="replace")


@pytest.fixture
def j8_html():
    return (FIXTURES / "j8_bote_real.html").read_text(encoding="utf-8",
                                                     errors="replace")


@pytest.fixture
def j70_html():
    return (FIXTURES / "j70_past_season.html").read_text(encoding="utf-8",
                                                         errors="replace")