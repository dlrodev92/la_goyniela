"""End-to-end tests for the ``/api/v1/losilla`` routes.

Mocks the upstream with ``httpx_mock`` and overrides ``get_losilla_client``
to inject an ``EduardoLosillaClient`` that picks up the mock transport.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.api.v1.losilla import get_losilla_client
from app.clients.eduardolosilla import EduardoLosillaClient
from app.main import app

FIXTURES = Path(__file__).parent / "fixtures" / "losilla"
pytestmark = pytest.mark.asyncio


# ---------- Fixtures ----------


@pytest.fixture
def fixtures():
    return {
        "j1": FIXTURES / "j1_baseline.html",
        "j8": FIXTURES / "j8_bote_real.html",
        "j9": FIXTURES / "j9_closed.html",
        "j11": FIXTURES / "j11_open.html",
        "j70": FIXTURES / "j70_past_season.html",
    }


@pytest.fixture
def losilla_test_client(fixtures, httpx_mock):
    """A fresh ``EduardoLosillaClient`` whose upstream is ``httpx_mock``.

    Registered as a FastAPI dependency override so the endpoint uses it
    instead of the module-level singleton.
    """
    test_client = EduardoLosillaClient()

    async def _override():
        return test_client

    app.dependency_overrides[get_losilla_client] = _override
    yield test_client
    app.dependency_overrides.pop(get_losilla_client, None)


# ---------- Endpoints ----------


async def test_get_jornada_open_returns_estimated_escrutinio(
        client, httpx_mock, fixtures, losilla_test_client):
    httpx_mock.add_response(
        url="https://www.eduardolosilla.es/quiniela/ayudas/escrutinio/jornada_11",
        content=fixtures["j11"].read_bytes(),
    )

    r = await client.get("/api/v1/losilla/jornada/11")
    assert r.status_code == 200
    body = r.json()

    assert body["jornada"] == 11
    assert body["temporada"] == 2027
    assert body["ssr_shipped_jornada"] == 11
    assert body["warning"] is None
    assert len(body["partidos"]) == 15
    assert body["comentarios_presentes"] == 14
    assert body["escrutinio_oficial"]["estimada"] is True
    assert body["escrutinio_oficial"]["recaudacion_eur"] == "1.200.000,00"


async def test_get_jornada_closed_returns_real_escrutinio_and_warning(
        client, httpx_mock, fixtures, losilla_test_client):
    """Asking for J9 surfaces the live J11 state with a warning, but the
    escrutinio_oficial is the closed J9."""
    httpx_mock.add_response(
        url="https://www.eduardolosilla.es/quiniela/ayudas/escrutinio/jornada_9",
        content=fixtures["j9"].read_bytes(),
    )

    r = await client.get("/api/v1/losilla/jornada/9")
    assert r.status_code == 200
    body = r.json()

    assert body["jornada"] == 9
    assert body["ssr_shipped_jornada"] == 11
    assert body["warning"] is not None
    assert "live view" in body["warning"]

    e = body["escrutinio_oficial"]
    assert e["jornada_etiqueta"] == 9
    assert e["estimada"] is False
    assert e["recaudacion_eur"] == "1.453.174,50"
    cat13 = next(c for c in e["categorias"] if c["aciertos"] == 13)
    assert cat13["acertantes"] == 2
    assert cat13["premio_eur"] == "51.595,23"


async def test_get_jornada_with_bote_real(client, httpx_mock, fixtures,
                                          losilla_test_client):
    httpx_mock.add_response(
        url="https://www.eduardolosilla.es/quiniela/ayudas/escrutinio/jornada_8",
        content=fixtures["j8"].read_bytes(),
    )

    r = await client.get("/api/v1/losilla/jornada/8")
    body = r.json()
    e = body["escrutinio_oficial"]
    assert e["bote_eur"] == "3.159.105,01"
    cat15 = next(c for c in e["categorias"] if c["aciertos"] == 15)
    assert cat15["acertantes"] == 1
    assert cat15["premio_eur"] == "2.762.202,31"


async def test_get_escrutinio_returns_only_block(client, httpx_mock, fixtures,
                                                losilla_test_client):
    httpx_mock.add_response(
        url="https://www.eduardolosilla.es/quiniela/ayudas/escrutinio/jornada_8",
        content=fixtures["j8"].read_bytes(),
    )

    r = await client.get("/api/v1/losilla/jornada/8/escrutinio")
    assert r.status_code == 200
    body = r.json()
    # T-data-4: response is wrapped with a `source` provenance tag.
    assert body["source"] == "upstream"
    assert r.headers["x-losilla-source"] == "upstream"
    e = body["escrutinio"]
    assert e["jornada_etiqueta"] == 8
    assert e["bote_eur"] == "3.159.105,01"
    assert [c["aciertos"] for c in e["categorias"]] == [15, 14, 13, 12, 11, 10]


async def test_get_escrutinio_returns_204_when_block_absent(
        client, httpx_mock, losilla_test_client):
    httpx_mock.add_response(
        url="https://www.eduardolosilla.es/quiniela/ayudas/escrutinio/jornada_999",
        content=b"<html><body>no state, no escrutinio</body></html>",
    )
    r = await client.get("/api/v1/losilla/jornada/999/escrutinio")
    assert r.status_code == 204


async def test_get_comentarios_filters_to_matches_with_editorial(
        client, httpx_mock, fixtures, losilla_test_client):
    httpx_mock.add_response(
        url="https://www.eduardolosilla.es/quiniela/ayudas/escrutinio/jornada_11",
        content=fixtures["j11"].read_bytes(),
    )

    r = await client.get("/api/v1/losilla/jornada/11/comentarios")
    assert r.status_code == 200
    body = r.json()
    # T-data-4: response is wrapped with a `source` provenance tag.
    assert body["source"] == "upstream"
    assert r.headers["x-losilla-source"] == "upstream"
    comentarios = body["comentarios"]
    assert len(comentarios) == 14
    assert all(p["comentario"] is not None for p in comentarios)
    ordenes = [p["orden"] for p in comentarios]
    assert 15 not in ordenes
    # Spot check a real match
    p1 = next(p for p in comentarios if p["orden"] == 1)
    assert p1["local"] == "ALBACETE"
    assert p1["visitante"] == "EIBAR"
    assert "Las jornadas van pasando" in p1["comentario"]


async def test_get_comentarios_returns_empty_for_old_jornada_without_editorial(
        client, httpx_mock, losilla_test_client):
    """If upstream has no editorial divs at all, the filter returns []."""
    synthetic = b"""
    <html><body>
      <script>{&q;datosGeneralesQuiniela&q;:{},&q;jornada_5_2027&q;:{&q;jornada&q;:5,&q;partidos&q;:[{&q;orden&q;:1,&q;num&q;:1,&q;local&q;:&q;X&q;,&q;visitante&q;:&q;Y&q;,&q;dia&q;:&q;SAB&q;,&q;hora&q;:&q;14:00&q;,&q;resultado&q;:&q;1-0&q;,&q;id_besoccer&q;:1}]}}</script>
    </body></html>
    """
    httpx_mock.add_response(
        url="https://www.eduardolosilla.es/quiniela/ayudas/escrutinio/jornada_5",
        content=synthetic,
    )
    r = await client.get("/api/v1/losilla/jornada/5/comentarios")
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "upstream"
    assert body["comentarios"] == []


async def test_get_jornada_propagates_502_on_upstream_5xx(
        client, httpx_mock, losilla_test_client):
    httpx_mock.add_response(
        url="https://www.eduardolosilla.es/quiniela/ayudas/escrutinio/jornada_11",
        status_code=503,
    )
    r = await client.get("/api/v1/losilla/jornada/11")
    assert r.status_code == 502
    assert "503" in r.json()["detail"]


async def test_get_jornada_validates_temporada_query(client, httpx_mock,
                                                     losilla_test_client):
    r = await client.get("/api/v1/losilla/jornada/11?temporada=1990")
    assert r.status_code == 422


async def test_get_jornada_validates_jornada_path(client):
    r = await client.get("/api/v1/losilla/jornada/0")
    assert r.status_code == 422


async def test_past_season_j70_returns_correct_label(
        client, httpx_mock, fixtures, losilla_test_client):
    """J70 of temporada=2027 is actually 25/26 in upstream labels."""
    httpx_mock.add_response(
        url="https://www.eduardolosilla.es/quiniela/ayudas/escrutinio/jornada_70",
        content=fixtures["j70"].read_bytes(),
    )
    r = await client.get("/api/v1/losilla/jornada/70")
    body = r.json()
    e = body["escrutinio_oficial"]
    assert e["temporada_etiqueta"] == "25/26"
    assert e["elige8"] is not None
    assert e["elige8"]["acertantes"] == 775
    assert e["elige8"]["premio_eur"] == "25,73"


async def test_openapi_documents_losilla_tag(client):
    """Smoke check: the OpenAPI doc lists the new losilla tag and routes."""
    r = await client.get("/openapi.json")
    assert r.status_code == 200
    spec = r.json()
    tags = {t["name"] for t in spec["tags"]}
    assert "losilla" in tags
    paths = set(spec["paths"].keys())
    assert "/api/v1/losilla/jornada/{jornada}" in paths
    assert "/api/v1/losilla/jornada/{jornada}/escrutinio" in paths
    assert "/api/v1/losilla/jornada/{jornada}/comentarios" in paths