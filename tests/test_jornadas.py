"""Tests for T8 — jornada schema + endpoints + dedup invariant.

Uses the real Postgres DB on port 5433 with the migrated schema. The autouse
``_cleanup_rows`` fixture wipes the test-created rows after each test, so
tests do not leak into each other or into the rest of the suite.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.db.models import (
    Composicion,
    Jornada,
    JornadaEscrutinio,
    JornadaStatus,
    Source,
    TeamIdentityMap,
)
from app.db.session import AsyncSessionLocal, engine


# ---------- Fixtures ----------


@pytest_asyncio.fixture(autouse=True)
async def _cleanup_rows():
    """Delete rows created during each test. CASCADE on composicion/jornada
    takes care of hijos. We also clean fetch_session (FK to source is
    SET NULL, so no cascade)."""
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


@pytest_asyncio.fixture
async def make_team():
    """Returns a coroutine that creates a team and yields (id, name)."""

    async def _create(
        canonical: str, comp_id: str = "laliga-ea-sports",
    ) -> tuple[int, str]:
        async with AsyncSessionLocal() as session:
            team = TeamIdentityMap(
                entity_id=f"test-{canonical.lower().replace(' ', '-').replace('á', 'a')}",
                competition_id=comp_id,
                canonical_name=canonical,
            )
            session.add(team)
            await session.commit()
            await session.refresh(team)
            return team.id, team.canonical_name

    return _create


@pytest_asyncio.fixture
async def make_jornada():
    """Returns a coroutine that creates a jornada and yields its id."""

    async def _create(
        numero: int = 11,
        temporada: int = 2027,
        status: JornadaStatus = JornadaStatus.draft,
        *,
        sale_opens_at: datetime | None = None,
        voting_closes_at: datetime | None = None,
        kickoff_first_match_at: datetime | None = None,
        kickoff_last_match_at: datetime | None = None,
        escrutinio_expected_at: datetime | None = None,
    ) -> int:
        async with AsyncSessionLocal() as session:
            j = Jornada(
                temporada=temporada,
                temporada_str="2026/2027",
                numero=numero,
                status=status,
                sale_opens_at=sale_opens_at,
                voting_closes_at=voting_closes_at,
                kickoff_first_match_at=kickoff_first_match_at,
                kickoff_last_match_at=kickoff_last_match_at,
                escrutinio_expected_at=escrutinio_expected_at,
            )
            session.add(j)
            await session.commit()
            await session.refresh(j)
            return j.id

    return _create


@pytest_asyncio.fixture
async def losilla_source_id() -> int:
    """Return the ``source.id`` for ``code='losilla'``.

    Skips the test if the catalog row is missing — the user must run
    ``python -m app.scripts.load_team_catalog`` to seed it. Same skip
    pattern as ``tests/test_losilla_persistence.py``.
    """
    from sqlalchemy import select
    async with AsyncSessionLocal() as session:
        sid = await session.scalar(
            select(Source.id).where(Source.code == "losilla"),
        )
    if sid is None:
        pytest.skip(
            "source row with code='losilla' is missing — run "
            "`python -m app.scripts.load_team_catalog` first",
        )
    return sid


# ---------- List endpoint ----------


async def test_list_jornadas_empty(client):
    r = await client.get("/jornadas")
    assert r.status_code == 200
    assert r.json() == []


async def test_list_jornadas_returns_created(client, make_jornada):
    jid = await make_jornada(numero=11)
    r = await client.get("/jornadas")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["id"] == jid
    assert body[0]["numero"] == 11
    assert body[0]["temporada"] == 2027
    assert body[0]["status"] == "draft"
    # fetched_at must be present (server default populated)
    assert body[0]["fetched_at"] is not None


async def test_list_jornadas_filter_by_season(client, make_jornada):
    await make_jornada(numero=11, temporada=2027)
    await make_jornada(numero=10, temporada=2027)
    await make_jornada(numero=42, temporada=2026)
    r = await client.get("/jornadas?season=2027")
    assert r.status_code == 200
    body = r.json()
    nums = {j["numero"] for j in body}
    assert nums == {11, 10}
    assert all(j["temporada"] == 2027 for j in body)


async def test_list_jornadas_filter_by_status(client, make_jornada):
    await make_jornada(numero=11, status=JornadaStatus.draft)
    await make_jornada(numero=12, status=JornadaStatus.active)
    await make_jornada(numero=13, status=JornadaStatus.past)
    r = await client.get("/jornadas?status=active")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["numero"] == 12
    assert body[0]["status"] == "active"


async def test_list_jornadas_pagination(client, make_jornada):
    for n in range(1, 6):
        await make_jornada(numero=n)
    r = await client.get("/jornadas?limit=2&offset=0")
    assert r.status_code == 200
    assert len(r.json()) == 2
    r2 = await client.get("/jornadas?limit=2&offset=2")
    assert len(r2.json()) == 2
    # Different offset returns different rows (ordered by temporada desc, numero desc)
    assert r.json()[0]["id"] != r2.json()[0]["id"]


# ---------- Detail endpoint ----------


async def test_get_jornada_returns_detail(client, make_jornada, make_team):
    jid = await make_jornada(numero=11)
    local_id, _ = await make_team("Adult 1")
    visit_id, _ = await make_team("Adult 2")

    async with AsyncSessionLocal() as session:
        comp1 = Composicion(
            jornada_id=jid,
            posicion=1,
            local_team_id=local_id,
            visitante_team_id=visit_id,
            local_raw="Adult 1",
            visitante_raw="Adult 2",
        )
        comp2 = Composicion(
            jornada_id=jid,
            posicion=2,
            local_team_id=visit_id,
            visitante_team_id=local_id,
            local_raw="Adult 2",
            visitante_raw="Adult 1",
        )
        session.add_all([comp1, comp2])
        await session.commit()

    r = await client.get(f"/jornadas/{jid}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == jid
    assert body["numero"] == 11
    assert len(body["partidos"]) == 2
    assert body["partidos"][0]["posicion"] == 1
    assert body["partidos"][0]["local_raw"] == "Adult 1"
    assert body["partidos"][0]["local_team_id"] == local_id
    assert body["partidos"][1]["posicion"] == 2


async def test_get_jornada_returns_empty_partidos_when_no_composicion(
    client, make_jornada,
):
    jid = await make_jornada(numero=11)
    r = await client.get(f"/jornadas/{jid}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == jid
    assert body["partidos"] == []


async def test_get_jornada_404(client):
    r = await client.get("/jornadas/99999")
    assert r.status_code == 404


async def test_get_jornada_invalid_id_422(client):
    r = await client.get("/jornadas/not-a-number")
    assert r.status_code == 422


# ---------- DB-level constraints ----------


async def test_jornada_constraint_unique_temporada_numero(client, make_jornada):
    """UNIQUE(temporada, numero) — second jornada with same (temporada, numero) fails."""
    await make_jornada(numero=11, temporada=2027)
    async with AsyncSessionLocal() as session:
        try:
            session.add(
                Jornada(temporada=2027, temporada_str="2026/2027", numero=11),
            )
            await session.commit()
            raised = False
        except IntegrityError:
            raised = True
        await session.rollback()
    assert raised, "UNIQUE constraint not enforced"


async def test_composicion_constraint_unique_jornada_posicion(
    client, make_jornada, make_team,
):
    """UNIQUE(jornada_id, posicion) — same posicion twice in same jornada fails."""
    jid = await make_jornada(numero=11)
    local_id, _ = await make_team("Adult 1")
    visit_id, _ = await make_team("Adult 2")
    async with AsyncSessionLocal() as session:
        session.add(Composicion(
            jornada_id=jid, posicion=1,
            local_team_id=local_id, visitante_team_id=visit_id,
            local_raw="Adult 1", visitante_raw="Adult 2",
        ))
        await session.commit()
        try:
            session.add(Composicion(
                jornada_id=jid, posicion=1,  # duplicate
                local_team_id=visit_id, visitante_team_id=local_id,
                local_raw="Adult 2", visitante_raw="Adult 1",
            ))
            await session.commit()
            raised = False
        except IntegrityError:
            raised = True
        await session.rollback()
    assert raised, "UNIQUE constraint not enforced"


async def test_composicion_check_posicion_range(client, make_jornada, make_team):
    """CHECK posicion BETWEEN 1 AND 15 — posicion=0 must fail."""
    jid = await make_jornada(numero=11)
    local_id, _ = await make_team("Adult 1")
    visit_id, _ = await make_team("Adult 2")
    async with AsyncSessionLocal() as session:
        try:
            session.add(Composicion(
                jornada_id=jid, posicion=0,
                local_team_id=local_id, visitante_team_id=visit_id,
                local_raw="Adult 1", visitante_raw="Adult 2",
            ))
            await session.commit()
            raised = False
        except IntegrityError:
            raised = True
        await session.rollback()
    assert raised, "CHECK constraint not enforced"


# ---------- Dedup index (team_identity_map) ----------


async def test_team_dedup_via_unique_index_case_insensitive():
    """Inserting the same normalized name twice must fail at the DB level."""
    async with AsyncSessionLocal() as session:
        session.add(TeamIdentityMap(
            entity_id="test-albacete-1",
            competition_id="laliga-ea-sports",
            canonical_name="Albacete",
        ))
        await session.commit()
        try:
            session.add(TeamIdentityMap(
                entity_id="test-albacete-2",
                competition_id="laliga-ea-sports",
                canonical_name="albacete",  # lowercase — same normalized
            ))
            await session.commit()
            raised = False
        except IntegrityError:
            raised = True
        await session.rollback()
    assert raised, "Functional UNIQUE constraint not enforced"


async def test_team_dedup_allows_different_competition():
    """Same normalized name in different competition is OK."""
    async with AsyncSessionLocal() as session:
        session.add_all([
            TeamIdentityMap(
                entity_id="test-barcelona-m",
                competition_id="laliga-ea-sports",
                canonical_name="Barcelona",
            ),
            TeamIdentityMap(
                entity_id="test-barcelona-f",
                competition_id="liga-f",
                canonical_name="Barcelona",
            ),
        ])
        await session.commit()
        # No exception = OK


async def test_team_dedup_allows_different_name_same_competition():
    """Different names in same competition are independent rows."""
    async with AsyncSessionLocal() as session:
        session.add_all([
            TeamIdentityMap(
                entity_id="test-real-madrid",
                competition_id="laliga-ea-sports",
                canonical_name="Real Madrid",
            ),
            TeamIdentityMap(
                entity_id="test-barcelona",
                competition_id="laliga-ea-sports",
                canonical_name="Barcelona",
            ),
        ])
        await session.commit()
        # No exception = OK


async def test_team_dedup_accent_insensitive():
    """unaccent() should normalize accents — 'Málaga' and 'Malaga' collide."""
    async with AsyncSessionLocal() as session:
        session.add(TeamIdentityMap(
            entity_id="test-malaga-1",
            competition_id="laliga-ea-sports",
            canonical_name="Málaga",
        ))
        await session.commit()
        try:
            session.add(TeamIdentityMap(
                entity_id="test-malaga-2",
                competition_id="laliga-ea-sports",
                canonical_name="Malaga",  # no accent
            ))
            await session.commit()
            raised = False
        except IntegrityError:
            raised = True
        await session.rollback()
    assert raised, "Accent-insensitive UNIQUE not enforced"


# ---------- Slice 1: jornada window fields ----------


async def test_jornada_has_kickoff_last_match_at_match_at_field(
    client, make_jornada,
):
    """jornada.kickoff_last_match_at: nullable tz datetime, set-able."""
    jid = await make_jornada(numero=11)

    # Default: column is NULL on a fresh row.
    async with AsyncSessionLocal() as session:
        j = await session.get(Jornada, jid)
        assert j.kickoff_last_match_at is None

    # Persist a tz-aware datetime and read it back round-trip.
    expected = datetime(2026, 10, 11, 22, 0, tzinfo=timezone.utc)
    async with AsyncSessionLocal() as session:
        j = await session.get(Jornada, jid)
        j.kickoff_last_match_at = expected
        await session.commit()

    async with AsyncSessionLocal() as session:
        j = await session.get(Jornada, jid)
        assert j.kickoff_last_match_at == expected
        # The column is tz-aware — the round-trip must preserve the offset.
        assert j.kickoff_last_match_at.tzinfo is not None
        assert j.kickoff_last_match_at == datetime(
            2026, 10, 11, 22, 0, tzinfo=timezone.utc,
        )


async def test_jornada_has_escrutinio_expected_at_field(
    client, make_jornada,
):
    """jornada.escrutinio_expected_at: nullable tz datetime, set-able."""
    jid = await make_jornada(numero=11)

    # Default: column is NULL on a fresh row.
    async with AsyncSessionLocal() as session:
        j = await session.get(Jornada, jid)
        assert j.escrutinio_expected_at is None

    # Persist a tz-aware datetime and read it back round-trip.
    expected = datetime(2026, 10, 12, 0, 30, tzinfo=timezone.utc)
    async with AsyncSessionLocal() as session:
        j = await session.get(Jornada, jid)
        j.escrutinio_expected_at = expected
        await session.commit()

    async with AsyncSessionLocal() as session:
        j = await session.get(Jornada, jid)
        assert j.escrutinio_expected_at == expected
        assert j.escrutinio_expected_at.tzinfo is not None
        assert j.escrutinio_expected_at == datetime(
            2026, 10, 12, 0, 30, tzinfo=timezone.utc,
        )


# ---------- Slice 8: list + detail expose new fields ----------


async def test_list_jornadas_exposes_effective_phase_now_and_window_fields(
    client, make_jornada,
):
    """Slice 8 (§7.1): the list endpoint surfaces
    ``effective_phase``, ``kickoff_last_match_at``, ``escrutinio_expected_at``,
    and ``now`` on every row.

    For a draft jornada the two window fields are ``None`` and the phase is
    the literal string ``"en_preparacion"``. ``now`` is a tz-aware UTC
    datetime close to the request moment (clock-skew helper per §7.1).
    """
    await make_jornada(numero=11)

    request_started = datetime.now(timezone.utc)
    r = await client.get("/jornadas")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    row = body[0]

    assert row["effective_phase"] == "en_preparacion"
    assert row["kickoff_last_match_at"] is None
    assert row["escrutinio_expected_at"] is None

    # ``now`` is tz-aware UTC and was captured between request start and a
    # second post-request checkpoint (5s slack is plenty for the test
    # runner — the field is a clock-skew helper, not a hard contract).
    now_str = row["now"]
    assert now_str is not None
    now_dt = datetime.fromisoformat(now_str)
    assert now_dt.tzinfo is not None
    delta = abs((datetime.now(timezone.utc) - now_dt).total_seconds())
    assert delta < 5, f"`now` drifted by {delta}s from request start"


async def test_list_jornadas_effective_phase_in_window_when_window_straddles_now(
    client, make_jornada,
):
    """Active jornada with ``sale_opens_at <= now < voting_closes_at``
    surfaces ``"in_window"``. Pin the window around the request time so the
    phase is deterministic regardless of when the test runs.
    """
    now = datetime.now(timezone.utc)
    await make_jornada(
        numero=11,
        status=JornadaStatus.active,
        sale_opens_at=now - timedelta(hours=2),
        voting_closes_at=now + timedelta(hours=2),
        kickoff_first_match_at=now + timedelta(hours=3),
        kickoff_last_match_at=now + timedelta(hours=10),
        escrutinio_expected_at=now + timedelta(hours=12, minutes=30),
    )

    r = await client.get("/jornadas")
    assert r.status_code == 200
    body = r.json()
    assert body[0]["effective_phase"] == "in_window"
    # Window fields round-trip verbatim from the DB.
    assert body[0]["kickoff_last_match_at"] is not None
    assert body[0]["escrutinio_expected_at"] is not None


async def test_list_jornadas_effective_phase_lista_abre_pronto_before_sale(
    client, make_jornada,
):
    """Active jornada with ``now < sale_opens_at`` surfaces
    ``"lista_abre_pronto"``."""
    now = datetime.now(timezone.utc)
    await make_jornada(
        numero=12,
        status=JornadaStatus.active,
        sale_opens_at=now + timedelta(hours=2),
        voting_closes_at=now + timedelta(hours=4),
        kickoff_first_match_at=now + timedelta(hours=5),
        kickoff_last_match_at=now + timedelta(hours=12),
        escrutinio_expected_at=now + timedelta(hours=14, minutes=30),
    )

    r = await client.get("/jornadas")
    assert r.status_code == 200
    assert r.json()[0]["effective_phase"] == "lista_abre_pronto"


async def test_list_jornadas_effective_phase_past(
    client, make_jornada,
):
    """Past → ``"escrutinio_disponible"`` (helper default for the
    collapsed closed/completed state).

    Slice 1 of the superadmin-panel sprint collapsed
    ``closed``/``completed`` into a single ``past`` state. The helper
    returns ``"escrutinio_disponible"`` for any ``past`` jornada (it
    can't see the ``jornada_escrutinio`` table); the public
    ``/jornadas/{id}`` endpoint overrides this with the real escrutinio
    presence via a separate DB read.
    """
    await make_jornada(numero=13, status=JornadaStatus.past)

    r = await client.get("/jornadas")
    assert r.status_code == 200
    by_num = {row["numero"]: row for row in r.json()}
    assert by_num[13]["effective_phase"] == "escrutinio_disponible"


async def test_get_jornada_detail_exposes_new_fields(
    client, make_jornada, make_team,
):
    """Slice 8 (§7.2): the detail endpoint inherits the list fields and
    adds ``estimaciones`` (always present, default empty list).
    """
    now = datetime.now(timezone.utc)
    jid = await make_jornada(
        numero=11,
        status=JornadaStatus.active,
        sale_opens_at=now - timedelta(hours=1),
        voting_closes_at=now + timedelta(hours=2),
        kickoff_first_match_at=now + timedelta(hours=3),
        kickoff_last_match_at=now + timedelta(hours=10),
        escrutinio_expected_at=now + timedelta(hours=12, minutes=30),
    )
    local_id, _ = await make_team("Adult 1")
    visit_id, _ = await make_team("Adult 2")
    async with AsyncSessionLocal() as session:
        session.add(Composicion(
            jornada_id=jid, posicion=1,
            local_team_id=local_id, visitante_team_id=visit_id,
            local_raw="Adult 1", visitante_raw="Adult 2",
        ))
        await session.commit()

    r = await client.get(f"/jornadas/{jid}")
    assert r.status_code == 200
    body = r.json()

    # Effective phase + window fields from §7.1.
    assert body["effective_phase"] == "in_window"
    assert body["kickoff_last_match_at"] is not None
    assert body["escrutinio_expected_at"] is not None
    assert body["now"] is not None

    # Estimaciones is always present on the detail; empty when no
    # escrutinio row exists (NOT a 404 — §7.2 / §1.3 contract).
    assert "estimaciones" in body
    assert body["estimaciones"] == []

    # Embedded TeamSummary surfaces ``gender_known`` for the slice 5/8
    # contract — ``True`` for rows the catalog seeded or the matcher
    # constructed with a gender marker.
    assert body["partidos"][0]["local_team"]["gender_known"] is True
    assert body["partidos"][0]["visitante_team"]["gender_known"] is True


async def test_get_jornada_detail_embeds_estimaciones_from_escrutinio_payload(
    client, make_jornada, losilla_source_id,
):
    """When a ``jornada_escrutinio`` row exists for the jornada, the detail
    embeds ``payload.estimaciones`` (slice 7 → 8 hand-off) verbatim.

    One PartidoEstimacion for partido 1 (1X2 shape) and one for partido
    15 (Pleno splits) — the two shapes defined in
    ``app.schemas.losilla.PartidoEstimacion``.
    """
    jid = await make_jornada(numero=11, temporada=2027)

    fetched_at = datetime(2026, 10, 11, 15, 30, tzinfo=timezone.utc)
    payload = {
        "estimada": False,
        "recaudacion_eur": "0,00",
        "bote_eur": "0,00",
        "jornada_etiqueta": 11,
        "temporada_etiqueta": "2026/2027",
        "fecha_jornada": "2026-10-11",
        "categorias": [],
        "estimaciones": [
            {
                "partido_orden": 1,
                "uno_porcentaje": 35.0,
                "x_porcentaje": 28.0,
                "dos_porcentaje": 37.0,
                "pleno_local": None,
                "pleno_visitante": None,
                "fuente": "losilla_live",
                "fetched_at": fetched_at.isoformat(),
            },
            {
                "partido_orden": 15,
                "uno_porcentaje": None,
                "x_porcentaje": None,
                "dos_porcentaje": None,
                "pleno_local": {"0": 2.0, "1": 7.0, "2": 14.0, "M": 77.0},
                "pleno_visitante": {
                    "0": 59.0, "1": 31.0, "2": 8.0, "M": 2.0,
                },
                "fuente": "losilla_live",
                "fetched_at": fetched_at.isoformat(),
            },
        ],
    }
    async with AsyncSessionLocal() as session:
        session.add(JornadaEscrutinio(
            jornada_id=jid,
            source_id=losilla_source_id,
            payload=payload,
        ))
        await session.commit()

    r = await client.get(f"/jornadas/{jid}")
    assert r.status_code == 200, r.text
    body = r.json()

    assert "estimaciones" in body
    assert len(body["estimaciones"]) == 2
    by_orden = {e["partido_orden"]: e for e in body["estimaciones"]}
    # Partido 1 — 1X2 shape.
    p1 = by_orden[1]
    assert p1["uno_porcentaje"] == 35.0
    assert p1["x_porcentaje"] == 28.0
    assert p1["dos_porcentaje"] == 37.0
    assert p1["pleno_local"] is None
    assert p1["pleno_visitante"] is None
    assert p1["fuente"] == "losilla_live"
    # Partido 15 — Pleno splits shape.
    p15 = by_orden[15]
    assert p15["uno_porcentaje"] is None
    assert p15["pleno_local"] == {
        "0": 2.0, "1": 7.0, "2": 14.0, "M": 77.0,
    }
    assert p15["pleno_visitante"] == {
        "0": 59.0, "1": 31.0, "2": 8.0, "M": 2.0,
    }


async def test_get_jornada_detail_estimaciones_empty_when_escrutinio_payload_null(
    client, make_jornada, losilla_source_id,
):
    """Legacy escrutinio row persisted with ``"estimaciones": null`` (pre
    slice 7 hand-written JSON) surfaces as ``[]`` thanks to
    ``EscrutinioOficial._coerce_none_estimaciones``. Pins the spec'd
    behaviour: clients always get a list — never 4xx, never key error.
    """
    jid = await make_jornada(numero=11, temporada=2027)
    payload = {
        "estimada": False,
        "recaudacion_eur": "0,00",
        "bote_eur": "0,00",
        "jornada_etiqueta": 11,
        "temporada_etiqueta": "2026/2027",
        "fecha_jornada": "2026-10-11",
        "categorias": [],
        "estimaciones": None,  # legacy hand-written
    }
    async with AsyncSessionLocal() as session:
        session.add(JornadaEscrutinio(
            jornada_id=jid,
            source_id=losilla_source_id,
            payload=payload,
        ))
        await session.commit()

    r = await client.get(f"/jornadas/{jid}")
    assert r.status_code == 200, r.text
    assert r.json()["estimaciones"] == []