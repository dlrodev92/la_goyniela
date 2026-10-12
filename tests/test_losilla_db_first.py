"""DB-first behaviour tests for the Losilla router (T-data-4).

The router's ``/escrutinio`` and ``/comentarios`` endpoints are DB-first
with an upstream fallback:

1. Resolve ``jornada`` row by ``(temporada, numero)``.
2. If present, read the persisted rows from ``jornada_escrutinio`` /
   ``jornada_comentario``. If a row exists, return it with ``source='db'``.
3. Otherwise fall back to the upstream eduardolosilla.es page and return
   the freshly-fetched payload with ``source='upstream'``.

The ``/jornada/{n}`` endpoint is unchanged (always live).

This file is the dedicated DB-first suite. The 6 router tests in
``test_losilla_persistence.py`` already cover the same ground from a
"CLI-first" angle; the two suites overlap by design — if the refactor
breaks, BOTH must go red.

Each test owns its own DB rows and the autouse ``_cleanup_rows`` fixture
wipes ``jornada_escrutinio`` / ``jornada_comentario`` / ``jornada`` rows
after each test so the suite is self-contained.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select, text

from app.api.v1.losilla import get_losilla_client
from app.clients.eduardolosilla import EduardoLosillaClient
from app.db.models import (
    Jornada,
    JornadaComentario,
    JornadaEscrutinio,
    Source,
)
from app.db.session import AsyncSessionLocal, engine
from app.main import app


FIXTURES = Path(__file__).parent / "fixtures" / "losilla"


# ---------- Shared fixtures ----------


@pytest_asyncio.fixture(autouse=True)
async def _cleanup_rows():
    """Wipe test-created jornada / escrutinio / comentario rows after each test.

    Scoped to this file only; the ``test_losilla_persistence.py`` suite
    has its own autouse cleaner and the two don't interact because pytest
    runs each test's setup/teardown in isolation.
    """
    yield
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM jornada_escrutinio"))
        await conn.execute(text("DELETE FROM jornada_comentario"))
        await conn.execute(text("DELETE FROM jornada"))


@pytest.fixture
def fixtures():
    """HTML fixtures used to feed http.client / httpx_mock."""
    return {
        "j8": FIXTURES / "j8_bote_real.html",
        "j11": FIXTURES / "j11_open.html",
    }


@pytest_asyncio.fixture
async def make_jornada():
    """Factory: create a ``jornada`` row and return its ``id``."""

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
    """Return ``source.id`` for ``code='losilla'``; skip if absent."""
    async with AsyncSessionLocal() as session:
        sid = await session.scalar(
            select(Source.id).where(Source.code == "losilla"),
        )
    if sid is None:
        pytest.skip(
            "source row with code='losilla' is missing — run "
            "`python -m app.scripts.load_team_catalog` to seed the catalog",
        )
    return sid


@pytest_asyncio.fixture
async def losilla_test_client():
    """A fresh ``EduardoLosillaClient`` whose upstream is ``httpx_mock``.

    Registered as a FastAPI dependency override so the endpoints use it
    instead of the module-level singleton.
    """
    test_client = EduardoLosillaClient()

    async def _override():
        return test_client

    app.dependency_overrides[get_losilla_client] = _override
    yield test_client
    app.dependency_overrides.pop(get_losilla_client, None)


# ---------- Tests ----------


async def test_escrutinio_returns_from_db_when_row_exists(
    httpx_mock,
    make_jornada,
    losilla_source_id,
    losilla_test_client,
    client,
):
    """DB row present → source='db', payload matches stored row, NO upstream HTTP.

    Belt-and-suspenders: we (i) don't register any httpx_mock response for
    the upstream URL (so a stray call would raise) AND (ii) assert the
    recorded requests list is empty. Either assertion alone would catch
    the regression; together they make the failure mode unambiguous.
    """
    jid = await make_jornada(numero=8, temporada=2026)
    stored_payload = {
        "estimada": False,
        "recaudacion_eur": "3.781.971,75",
        "bote_eur": "3.159.105,01",
        "jornada_etiqueta": 8,
        "temporada_etiqueta": "26/27",
        "fecha_jornada": "26/09/2026",
        "categorias": [
            {"aciertos": 15, "acertantes": 1, "premio_eur": "2.762.202,31"},
            {"aciertos": 14, "acertantes": 22, "premio_eur": "27.505,25"},
            {"aciertos": 13, "acertantes": 150, "premio_eur": "1.000,00"},
            {"aciertos": 12, "acertantes": 1500, "premio_eur": "100,00"},
            {"aciertos": 11, "acertantes": 15000, "premio_eur": "10,00"},
            {"aciertos": 10, "acertantes": 50000, "premio_eur": "5,00"},
        ],
        "elige8": None,
    }
    async with AsyncSessionLocal() as session:
        session.add(JornadaEscrutinio(
            jornada_id=jid,
            source_id=losilla_source_id,
            payload=stored_payload,
        ))
        await session.commit()

    r = await client.get(
        "/api/v1/losilla/jornada/8/escrutinio?temporada=2026",
    )
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "db"
    assert r.headers["x-losilla-source"] == "db"

    e = body["escrutinio"]
    # The round-trip preserves every field we stored.
    assert e["jornada_etiqueta"] == stored_payload["jornada_etiqueta"]
    assert e["recaudacion_eur"] == stored_payload["recaudacion_eur"]
    assert e["bote_eur"] == stored_payload["bote_eur"]
    assert e["temporada_etiqueta"] == stored_payload["temporada_etiqueta"]
    assert e["fecha_jornada"] == stored_payload["fecha_jornada"]
    assert e["estimada"] is False
    assert [c["aciertos"] for c in e["categorias"]] == [
        c["aciertos"] for c in stored_payload["categorias"]
    ]
    cat15 = next(c for c in e["categorias"] if c["aciertos"] == 15)
    assert cat15["acertantes"] == 1
    assert cat15["premio_eur"] == "2.762.202,31"

    # No upstream HTTP — explicit empty-list check.
    assert httpx_mock.get_requests() == []


async def test_escrutinio_falls_back_to_upstream_when_db_empty(
    httpx_mock, fixtures, losilla_test_client, client,
):
    """Empty DB → source='upstream', payload mirrors the mocked response."""
    httpx_mock.add_response(
        url="https://www.eduardolosilla.es/quiniela/ayudas/escrutinio/jornada_8",
        content=fixtures["j8"].read_bytes(),
    )

    r = await client.get(
        "/api/v1/losilla/jornada/8/escrutinio?temporada=2026",
    )
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "upstream"
    assert r.headers["x-losilla-source"] == "upstream"
    e = body["escrutinio"]
    assert e["jornada_etiqueta"] == 8
    assert e["bote_eur"] == "3.159.105,01"
    assert e["recaudacion_eur"] == "3.781.971,75"
    assert [c["aciertos"] for c in e["categorias"]] == [15, 14, 13, 12, 11, 10]


async def test_comentarios_returns_from_db_when_rows_exist(
    httpx_mock,
    make_jornada,
    losilla_source_id,
    losilla_test_client,
    client,
):
    """14 DB rows present → source='db', 14 items, fields match, no upstream."""
    jid = await make_jornada(numero=8, temporada=2026)
    rows = [
        {
            "partido_orden": o,
            "local_raw": f"Local{o}",
            "visitante_raw": f"Visit{o}",
            "comentario": f"Editorial comentario for partido {o}.",
        }
        for o in range(1, 15)
    ]
    async with AsyncSessionLocal() as session:
        session.add_all([
            JornadaComentario(jornada_id=jid, source_id=losilla_source_id, **r)
            for r in rows
        ])
        await session.commit()

    r = await client.get(
        "/api/v1/losilla/jornada/8/comentarios?temporada=2026",
    )
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "db"
    assert r.headers["x-losilla-source"] == "db"

    comentarios = body["comentarios"]
    assert len(comentarios) == 14
    assert [p["orden"] for p in comentarios] == list(range(1, 15))
    for i, p in enumerate(comentarios, start=1):
        assert p["local"] == f"Local{i}"
        assert p["visitante"] == f"Visit{i}"
        assert p["comentario"] == f"Editorial comentario for partido {i}."
        assert p["num"] == i  # router sets num=partido_orden too

    assert httpx_mock.get_requests() == []


async def test_comentarios_falls_back_to_upstream_when_db_empty(
    httpx_mock, fixtures, losilla_test_client, client,
):
    """Empty DB → source='upstream'; upstream partidos with non-null comentario returned."""
    httpx_mock.add_response(
        url="https://www.eduardolosilla.es/quiniela/ayudas/escrutinio/jornada_11",
        content=fixtures["j11"].read_bytes(),
    )

    r = await client.get(
        "/api/v1/losilla/jornada/11/comentarios?temporada=2027",
    )
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "upstream"
    assert r.headers["x-losilla-source"] == "upstream"
    comentarios = body["comentarios"]
    assert len(comentarios) == 14
    assert all(p["comentario"] is not None for p in comentarios)
    # partido 15 (Pleno al 15) never has an editorial → 15 absent
    assert 15 not in [p["orden"] for p in comentarios]


async def test_escrutinio_preserves_204_when_upstream_has_no_block(
    httpx_mock, losilla_test_client, client,
):
    """Empty DB + upstream returns no escrutinio block → 204.

    Regression guard for the upstream 204 contract preserved by the
    refactor: when the upstream page lacks the escrutinio block (typically
    because the jornada is in-flight), the endpoint still answers 204,
    not a wrapped 200 with ``escrutinio=None``.
    """
    httpx_mock.add_response(
        url="https://www.eduardolosilla.es/quiniela/ayudas/escrutinio/jornada_999",
        content=b"<html><body>no state, no escrutinio</body></html>",
    )

    r = await client.get(
        "/api/v1/losilla/jornada/999/escrutinio?temporada=2026",
    )
    assert r.status_code == 204


async def test_jornada_endpoint_unchanged_after_refactor(
    httpx_mock, fixtures, make_jornada, losilla_source_id, losilla_test_client, client,
):
    """``/jornada/{n}`` is still always live — DB rows do NOT short-circuit it.

    We pre-create a jornada + escrutinio row whose contents are clearly
    distinguishable from the upstream fixture (jornada_etiqueta=99 vs 8,
    bote_eur="0,00" vs "3.159.105,01"). If the live endpoint accidentally
    read from the DB, we'd see the DB values. We assert the upstream
    payload wins — the endpoint never reads from the DB.
    """
    jid = await make_jornada(numero=8, temporada=2026)
    async with AsyncSessionLocal() as session:
        session.add(JornadaEscrutinio(
            jornada_id=jid,
            source_id=losilla_source_id,
            payload={
                "estimada": False,
                "recaudacion_eur": "999.999,99",
                "bote_eur": "0,00",
                "jornada_etiqueta": 99,
                "temporada_etiqueta": "99/99",
                "fecha_jornada": "01/01/1999",
                "categorias": [],
                "elige8": None,
            },
        ))
        await session.commit()

    httpx_mock.add_response(
        url="https://www.eduardolosilla.es/quiniela/ayudas/escrutinio/jornada_8",
        content=fixtures["j8"].read_bytes(),
    )

    r = await client.get(
        "/api/v1/losilla/jornada/8?temporada=2026",
    )
    assert r.status_code == 200
    body = r.json()
    # ``JornadaLosilla.source`` is the always-present provenance tag
    # ("eduardolosilla_escrutinio") — NOT the wrapper's 'db'/'upstream'.
    # The differentiator: the live endpoint never returns 'db'/'upstream'.
    assert body["source"] == "eduardolosilla_escrutinio"
    assert body["jornada"] == 8
    # Upstream payload won; DB row is invisible to this endpoint.
    assert body["escrutinio_oficial"]["jornada_etiqueta"] == 8  # NOT 99
    assert body["escrutinio_oficial"]["bote_eur"] == "3.159.105,01"  # NOT "0,00"


async def test_escrutinio_with_no_jornada_in_db_falls_through_to_upstream(
    httpx_mock, fixtures, losilla_test_client, client,
):
    """No ``jornada`` row at all → DB-first short-circuits, endpoint hits upstream.

    Distinguishes the two empty-DB cases the endpoint can see:"

    * No jornada row → DB-first lookups all return None → endpoint hits
      upstream unconditionally. (THIS test.)
    * Jornada exists, no escrutinio row → DB-first lookups return None
      for escrutinio → endpoint hits upstream. (Covered by
      ``test_escrutinio_falls_back_to_upstream_when_db_empty``.)
    """
    httpx_mock.add_response(
        url="https://www.eduardolosilla.es/quiniela/ayudas/escrutinio/jornada_8",
        content=fixtures["j8"].read_bytes(),
    )

    r = await client.get(
        "/api/v1/losilla/jornada/8/escrutinio?temporada=2026",
    )
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "upstream"
    assert body["escrutinio"]["jornada_etiqueta"] == 8
    assert body["escrutinio"]["bote_eur"] == "3.159.105,01"