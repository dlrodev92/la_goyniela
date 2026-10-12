"""Tests for :mod:`app.services.jornada_manager`.

Slices 2 + 4 of the gestor-jornadas plan (``docs/producto/gestor-jornadas.md``
§ 3.1 + § 4.1). The manager is pure SQLAlchemy + math (no HTTP, no auth);
these tests use the real Postgres test DB and the
:func:`compute_window` + :func:`compute_effective_phase` helpers have
their own pure-function tests that never touch the DB.

Each test owns its own data and wipes it after the run via an
autouse ``_cleanup_rows`` fixture (same shape as ``test_jornadas.py``
and ``test_team_matcher.py``). The ``client`` fixture from
``tests/conftest.py`` is intentionally NOT used — the manager is
HTTP-free.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select, text as sql_text

from app.db.models import (
    Composicion,
    Jornada,
    JornadaStatus,
    TeamIdentityMap,
)
from app.db.session import AsyncSessionLocal, engine
from app.services.jornada_manager import (
    ESCRUTINIO_AFTER_LAST_KICKOFF,
    IncompleteJornadaError,
    JornadaManager,
    JornadaNotFoundError,
    PARTIDOS_PER_JORNADA,
    VOTING_CLOSES_BEFORE_LAST_KICKOFF,
    compute_effective_phase,
    compute_window,
)


# ==========================================================================
# Fixtures.
# ==========================================================================


@pytest_asyncio.fixture(autouse=True)
async def _cleanup_rows():
    """Wipe composicion + jornada + team rows after each test.

    Order matters: jornada_comentario and jornada_escrutinio FK to
    jornada, composicion FK to jornada. team_identity_map rows we created
    are filtered by the ``test-`` / ``auto-`` prefix.
    """
    yield
    async with engine.begin() as conn:
        await conn.execute(sql_text("DELETE FROM jornada_escrutinio"))
        await conn.execute(sql_text("DELETE FROM jornada_comentario"))
        await conn.execute(sql_text("DELETE FROM composicion"))
        await conn.execute(sql_text("DELETE FROM fetch_session"))
        await conn.execute(sql_text("DELETE FROM jornada"))
        await conn.execute(
            sql_text(
                "DELETE FROM team_identity_map "
                "WHERE entity_id LIKE 'test-%' OR entity_id LIKE 'auto-%'"
            )
        )


@pytest_asyncio.fixture
async def make_jornada():
    """Create one jornada in any status; returns its id."""

    async def _create(
        *,
        numero: int = 11,
        temporada: int = 2027,
        status: JornadaStatus = JornadaStatus.draft,
    ) -> int:
        async with AsyncSessionLocal() as session:
            j = Jornada(
                temporada=temporada,
                temporada_str="2026/2027",
                numero=numero,
                status=status,
            )
            session.add(j)
            await session.commit()
            await session.refresh(j)
            return j.id

    return _create


@pytest_asyncio.fixture
async def populated_jornada():
    """One draft jornada + 2 teams + 15 composicion rows.

    Returns ``(jornada_id, [local_team_id, visitante_team_id])``. The
    manager only cares about ``Composicion.kickoff_at``, so the team
    pairing is arbitrary — the two teams are reused across all 15 rows.
    """

    async def _create(
        *,
        numero: int = 11,
        status: JornadaStatus = JornadaStatus.draft,
    ) -> tuple[int, list[int]]:
        async with AsyncSessionLocal() as session:
            t1 = TeamIdentityMap(
                entity_id="test-team-a",
                competition_id="laliga-ea-sports",
                canonical_name="Team A",
            )
            t2 = TeamIdentityMap(
                entity_id="test-team-b",
                competition_id="laliga-ea-sports",
                canonical_name="Team B",
            )
            session.add_all([t1, t2])
            await session.flush()
            t1_id, t2_id = t1.id, t2.id

            j = Jornada(
                temporada=2027,
                temporada_str="2026/2027",
                numero=numero,
                status=status,
            )
            session.add(j)
            await session.flush()
            jid = j.id

            for pos in range(1, PARTIDOS_PER_JORNADA + 1):
                session.add(
                    Composicion(
                        jornada_id=jid,
                        posicion=pos,
                        local_team_id=t1_id,
                        visitante_team_id=t2_id,
                        local_raw="Team A",
                        visitante_raw="Team B",
                    ),
                )
            await session.commit()
            return jid, [t1_id, t2_id]

    return _create


# ==========================================================================
# Helpers.
# ==========================================================================


def _make_kickoffs(
    *,
    first: datetime,
    interval_minutes: int = 60,
) -> list[datetime]:
    """Generate 15 tz-aware kickoffs spaced ``interval_minutes`` apart."""
    return [
        first + timedelta(minutes=i * interval_minutes)
        for i in range(PARTIDOS_PER_JORNADA)
    ]


# ==========================================================================
# Pure helper — compute_window.
# ==========================================================================


def test_compute_window_pure_returns_derived_fields():
    """15 inputs → 4 outputs, no DB.

    Verifies the four hard rules:
      - kickoff_first = min(inputs)
      - kickoff_last  = max(inputs)
      - voting_closes_at = kickoff_last - 1h
      - escrutinio_expected_at = kickoff_last + 2.5h
    """
    base = datetime(2026, 10, 11, 14, 0, tzinfo=timezone.utc)
    kickoffs = _make_kickoffs(first=base, interval_minutes=30)

    out = compute_window(kickoffs)

    assert out["kickoff_first_match_at"] == kickoffs[0]
    assert out["kickoff_last_match_at"] == kickoffs[-1]
    assert out["voting_closes_at"] == kickoffs[-1] - timedelta(hours=1)
    assert (
        out["escrutinio_expected_at"]
        == kickoffs[-1] + timedelta(hours=2, minutes=30)
    )

    # The constants are what the design doc says they are.
    assert VOTING_CLOSES_BEFORE_LAST_KICKOFF == timedelta(hours=1)
    assert ESCRUTINIO_AFTER_LAST_KICKOFF == timedelta(hours=2, minutes=30)


def test_compute_window_handles_none_entries():
    """None entries are skipped from min/max; all-None → all-None fields."""
    empty_out = {
        "kickoff_first_match_at": None,
        "kickoff_last_match_at": None,
        "voting_closes_at": None,
        "escrutinio_expected_at": None,
    }
    assert compute_window([]) == empty_out
    assert compute_window([None] * PARTIDOS_PER_JORNADA) == empty_out

    base = datetime(2026, 10, 11, 14, 0, tzinfo=timezone.utc)
    later = base + timedelta(hours=2)
    sparse: list[object] = (
        [None, base, None, None, later] + [None] * 10
    )
    out = compute_window(sparse)  # type: ignore[arg-type]
    assert out["kickoff_first_match_at"] == base
    assert out["kickoff_last_match_at"] == later
    assert out["voting_closes_at"] == later - timedelta(hours=1)
    assert (
        out["escrutinio_expected_at"]
        == later + timedelta(hours=2, minutes=30)
    )


def test_compute_window_rejects_naive_datetimes():
    """Naive datetimes raise ValueError; mix of naive + aware also raises."""
    naive = datetime(2026, 10, 11, 14, 0)  # no tz
    aware = datetime(2026, 10, 11, 14, 0, tzinfo=timezone.utc)

    with pytest.raises(ValueError, match="timezone-aware"):
        compute_window([naive] * PARTIDOS_PER_JORNADA)
    # The mix catches the bug where most inputs are fine but one slipped.
    with pytest.raises(ValueError, match="timezone-aware"):
        compute_window([aware] * (PARTIDOS_PER_JORNADA - 1) + [naive])


# ==========================================================================
# configure_dates.
# ==========================================================================


async def test_configure_dates_persists_all_fields(populated_jornada):
    """Happy path: 15 kickoffs + sale_opens_at → all 4 derived fields persisted.

    Verifies:
      - the returned object has the right values
      - re-reading from the DB shows them persisted
      - composicion.kickoff_at is updated for every row
    """
    jid, _ = await populated_jornada(numero=11)

    base = datetime(2026, 10, 11, 14, 0, tzinfo=timezone.utc)
    kickoffs = _make_kickoffs(first=base, interval_minutes=60)
    sale_opens_at = datetime(2026, 10, 8, 19, 0, tzinfo=timezone.utc)

    async with AsyncSessionLocal() as session:
        mgr = JornadaManager()
        j = await mgr.configure_dates(
            session,
            jid,
            kickoff_per_partido=kickoffs,
            sale_opens_at=sale_opens_at,
        )

    # Returned object.
    assert j.id == jid
    assert j.sale_opens_at == sale_opens_at
    assert j.kickoff_first_match_at == kickoffs[0]
    assert j.kickoff_last_match_at == kickoffs[-1]
    assert j.voting_closes_at == kickoffs[-1] - timedelta(hours=1)
    assert (
        j.escrutinio_expected_at
        == kickoffs[-1] + timedelta(hours=2, minutes=30)
    )

    # Re-read from the DB — persistence check.
    async with AsyncSessionLocal() as session:
        j2 = await session.get(Jornada, jid)
        assert j2 is not None
        assert j2.sale_opens_at == sale_opens_at
        assert j2.kickoff_first_match_at == kickoffs[0]
        assert j2.kickoff_last_match_at == kickoffs[-1]
        assert j2.voting_closes_at == kickoffs[-1] - timedelta(hours=1)
        assert (
            j2.escrutinio_expected_at
            == kickoffs[-1] + timedelta(hours=2, minutes=30)
        )

    # Each composicion row has its own kickoff_at persisted.
    async with AsyncSessionLocal() as session:
        compos = (
            await session.execute(
                select(Composicion)
                .where(Composicion.jornada_id == jid)
                .order_by(Composicion.posicion)
            )
        ).scalars().all()
    assert len(compos) == PARTIDOS_PER_JORNADA
    for c, k in zip(compos, kickoffs, strict=True):
        assert c.kickoff_at == k


async def test_configure_dates_rejects_wrong_count(populated_jornada):
    """``len(kickoff_per_partido) != 15`` raises ``ValueError``.

    Both 14 and 16 are tested to guard the upper and lower boundaries.
    """
    jid, _ = await populated_jornada(numero=11)
    sample = datetime(2026, 10, 11, 14, 0, tzinfo=timezone.utc)
    sale_opens_at = datetime(2026, 10, 8, 19, 0, tzinfo=timezone.utc)

    async with AsyncSessionLocal() as session:
        mgr = JornadaManager()
        with pytest.raises(ValueError, match="must have 15"):
            await mgr.configure_dates(
                session, jid,
                kickoff_per_partido=[sample] * 14,
                sale_opens_at=sale_opens_at,
            )
        with pytest.raises(ValueError, match="must have 15"):
            await mgr.configure_dates(
                session, jid,
                kickoff_per_partido=[sample] * 16,
                sale_opens_at=sale_opens_at,
            )


async def test_configure_dates_transitions_draft_to_active(populated_jornada):
    """A draft jornada with all 15 kickoffs + sale_opens_at becomes active."""
    jid, _ = await populated_jornada(
        numero=11, status=JornadaStatus.draft,
    )

    base = datetime(2026, 10, 11, 14, 0, tzinfo=timezone.utc)
    kickoffs = _make_kickoffs(first=base, interval_minutes=60)
    sale_opens_at = datetime(2026, 10, 8, 19, 0, tzinfo=timezone.utc)

    async with AsyncSessionLocal() as session:
        mgr = JornadaManager()
        j = await mgr.configure_dates(
            session, jid,
            kickoff_per_partido=kickoffs,
            sale_opens_at=sale_opens_at,
        )
    assert j.status == JornadaStatus.active

    # Persisted.
    async with AsyncSessionLocal() as session:
        j2 = await session.get(Jornada, jid)
        assert j2 is not None
        assert j2.status == JornadaStatus.active


async def test_configure_dates_keeps_active_when_already_active(populated_jornada):
    """Already-active stays active (does not bounce back to draft)."""
    jid, _ = await populated_jornada(
        numero=11, status=JornadaStatus.active,
    )

    base = datetime(2026, 10, 11, 14, 0, tzinfo=timezone.utc)
    kickoffs = _make_kickoffs(first=base, interval_minutes=60)
    sale_opens_at = datetime(2026, 10, 8, 19, 0, tzinfo=timezone.utc)

    async with AsyncSessionLocal() as session:
        mgr = JornadaManager()
        j = await mgr.configure_dates(
            session, jid,
            kickoff_per_partido=kickoffs,
            sale_opens_at=sale_opens_at,
        )
    assert j.status == JornadaStatus.active

    async with AsyncSessionLocal() as session:
        j2 = await session.get(Jornada, jid)
        assert j2 is not None
        assert j2.status == JornadaStatus.active


async def test_configure_dates_raises_when_compos_missing(make_jornada):
    """A jornada with no composicion rows raises ``IncompleteJornadaError``.

    The manager refuses to operate before partidos are ingested.
    """
    jid = await make_jornada(numero=11)
    sample = datetime(2026, 10, 11, 14, 0, tzinfo=timezone.utc)
    sale_opens_at = datetime(2026, 10, 8, 19, 0, tzinfo=timezone.utc)

    async with AsyncSessionLocal() as session:
        mgr = JornadaManager()
        with pytest.raises(IncompleteJornadaError):
            await mgr.configure_dates(
                session, jid,
                kickoff_per_partido=[sample] * PARTIDOS_PER_JORNADA,
                sale_opens_at=sale_opens_at,
            )


async def test_configure_dates_raises_when_jornada_missing():
    """A jornada id with no DB row raises ``JornadaNotFoundError``."""
    sample = datetime(2026, 10, 11, 14, 0, tzinfo=timezone.utc)
    sale_opens_at = datetime(2026, 10, 8, 19, 0, tzinfo=timezone.utc)

    async with AsyncSessionLocal() as session:
        mgr = JornadaManager()
        with pytest.raises(JornadaNotFoundError):
            await mgr.configure_dates(
                session, 999_999,
                kickoff_per_partido=[sample] * PARTIDOS_PER_JORNADA,
                sale_opens_at=sale_opens_at,
            )


async def test_configure_dates_rejects_naive_sale_opens_at(populated_jornada):
    """``sale_opens_at`` must be tz-aware."""
    jid, _ = await populated_jornada(numero=11)
    sample = datetime(2026, 10, 11, 14, 0, tzinfo=timezone.utc)

    async with AsyncSessionLocal() as session:
        mgr = JornadaManager()
        with pytest.raises(ValueError, match="sale_opens_at"):
            await mgr.configure_dates(
                session, jid,
                kickoff_per_partido=[sample] * PARTIDOS_PER_JORNADA,
                sale_opens_at=datetime(2026, 10, 8, 19, 0),  # naive
            )


async def test_configure_dates_rejects_naive_kickoff(populated_jornada):
    """A single naive kickoff (mixed with tz-aware) raises ``ValueError``."""
    jid, _ = await populated_jornada(numero=11)
    sale_opens_at = datetime(2026, 10, 8, 19, 0, tzinfo=timezone.utc)
    aware = datetime(2026, 10, 11, 14, 0, tzinfo=timezone.utc)
    bad: list[datetime] = [aware] * (PARTIDOS_PER_JORNADA - 1) + [
        datetime(2026, 10, 12, 14, 0),  # naive
    ]

    async with AsyncSessionLocal() as session:
        mgr = JornadaManager()
        with pytest.raises(ValueError, match="timezone-aware"):
            await mgr.configure_dates(
                session, jid,
                kickoff_per_partido=bad,
                sale_opens_at=sale_opens_at,
            )


# ==========================================================================
# recompute_window.
# ==========================================================================


async def test_recompute_window_derives_from_existing_composicion(populated_jornada):
    """Pre-existing compos with kickoffs → recompute derives the 4 fields.

    No sale_opens_at is set (we never call configure_dates). The
    recompute must derive the 4 window fields from composicion rows
    alone, and must NOT invent a sale_opens_at.
    """
    jid, _ = await populated_jornada(numero=11)

    base = datetime(2026, 10, 11, 14, 0, tzinfo=timezone.utc)
    kickoffs = _make_kickoffs(first=base, interval_minutes=60)

    # Pre-persist kickoffs on the composicion rows (this is what
    # recompute_window reads from).
    async with AsyncSessionLocal() as session:
        compos = (
            await session.execute(
                select(Composicion)
                .where(Composicion.jornada_id == jid)
                .order_by(Composicion.posicion)
            )
        ).scalars().all()
        for c, k in zip(compos, kickoffs, strict=True):
            c.kickoff_at = k
        await session.commit()

    async with AsyncSessionLocal() as session:
        mgr = JornadaManager()
        j = await mgr.recompute_window(session, jid)

    assert j.kickoff_first_match_at == kickoffs[0]
    assert j.kickoff_last_match_at == kickoffs[-1]
    assert j.voting_closes_at == kickoffs[-1] - timedelta(hours=1)
    assert (
        j.escrutinio_expected_at
        == kickoffs[-1] + timedelta(hours=2, minutes=30)
    )
    # sale_opens_at was never set — must remain None.
    assert j.sale_opens_at is None

    # Status was not transitioned either (recompute_window never does).
    assert j.status == JornadaStatus.draft


async def test_recompute_window_does_not_touch_sale_opens_at(populated_jornada):
    """A subsequent recompute leaves the editorial ``sale_opens_at`` unchanged.

    Sequence:
      1. configure_dates → sale_opens_at = T0 (the admin's choice).
      2. Update composicion.posicion=15's kickoff to a later datetime.
      3. recompute_window → the 4 derived fields reflect the new kickoff,
         but sale_opens_at stays at T0.
    """
    jid, _ = await populated_jornada(numero=11)
    sale_opens_at = datetime(2026, 10, 8, 19, 0, tzinfo=timezone.utc)

    base = datetime(2026, 10, 11, 14, 0, tzinfo=timezone.utc)
    kickoffs = _make_kickoffs(first=base, interval_minutes=60)

    # Step 1.
    async with AsyncSessionLocal() as session:
        mgr = JornadaManager()
        await mgr.configure_dates(
            session, jid,
            kickoff_per_partido=kickoffs,
            sale_opens_at=sale_opens_at,
        )

    # Step 2: move the last kickoff 2 hours later.
    new_kickoff = kickoffs[-1] + timedelta(hours=2)
    async with AsyncSessionLocal() as session:
        last = (
            await session.execute(
                select(Composicion).where(
                    Composicion.jornada_id == jid,
                    Composicion.posicion == PARTIDOS_PER_JORNADA,
                )
            )
        ).scalar_one()
        last.kickoff_at = new_kickoff
        await session.commit()

    # Step 3.
    async with AsyncSessionLocal() as session:
        mgr = JornadaManager()
        j = await mgr.recompute_window(session, jid)

    # sale_opens_at unchanged.
    assert j.sale_opens_at == sale_opens_at
    # But the derived fields DID update.
    assert j.kickoff_last_match_at == new_kickoff
    assert j.voting_closes_at == new_kickoff - timedelta(hours=1)
    assert (
        j.escrutinio_expected_at
        == new_kickoff + timedelta(hours=2, minutes=30)
    )


async def test_recompute_window_raises_when_jornada_missing():
    """``JornadaNotFoundError`` when jornada id has no DB row."""
    async with AsyncSessionLocal() as session:
        mgr = JornadaManager()
        with pytest.raises(JornadaNotFoundError):
            await mgr.recompute_window(session, 999_999)


async def test_recompute_window_raises_when_compos_missing(make_jornada):
    """``IncompleteJornadaError`` when jornada exists but has no compos."""
    jid = await make_jornada(numero=11)
    async with AsyncSessionLocal() as session:
        mgr = JornadaManager()
        with pytest.raises(IncompleteJornadaError):
            await mgr.recompute_window(session, jid)


# ==========================================================================
# compute_effective_phase (pure — slice 4).
# ==========================================================================


@pytest_asyncio.fixture
async def make_jornada_with_window():
    """Create a jornada with all 4 window fields populated.

    Returns a callable that takes ``status``, ``sale_opens_at``,
    ``voting_closes_at`` and returns the persisted :class:`Jornada`. The
    default values set a clean ``in_window`` (sale in the past,
    voting_closes in the future). Tests override one field at a time to
    exercise each phase.
    """

    async def _create(
        status: JornadaStatus = JornadaStatus.active,
        sale_opens_at: datetime | None = datetime(
            2026, 10, 8, 19, 0, tzinfo=timezone.utc,
        ),
        voting_closes_at: datetime | None = datetime(
            2026, 10, 11, 14, 0, tzinfo=timezone.utc,
        ),
    ) -> Jornada:
        async with AsyncSessionLocal() as session:
            j = Jornada(
                temporada=2097,
                temporada_str="2096/2097",
                numero=11,
                status=status,
                sale_opens_at=sale_opens_at,
                voting_closes_at=voting_closes_at,
            )
            session.add(j)
            await session.commit()
            await session.refresh(j)
            return j

    return _create


def _in_memory_jornada(
    *,
    status: JornadaStatus = JornadaStatus.active,
    sale_opens_at: datetime | None = datetime(
        2026, 10, 8, 19, 0, tzinfo=timezone.utc,
    ),
    voting_closes_at: datetime | None = datetime(
        2026, 10, 11, 14, 0, tzinfo=timezone.utc,
    ),
) -> Jornada:
    """Build a :class:`Jornada` in memory (no DB) for pure-function tests.

    ``compute_effective_phase`` reads only ``status``, ``sale_opens_at``
    and ``voting_closes_at`` — none of the fields it touches require a
    DB round-trip, so we can avoid the AsyncSessionLocal overhead and
    keep these tests synchronous.
    """
    return Jornada(
        temporada=2097,
        temporada_str="2096/2097",
        numero=11,
        status=status,
        sale_opens_at=sale_opens_at,
        voting_closes_at=voting_closes_at,
    )


def test_compute_effective_phase_en_preparacion():
    """``draft`` → always ``"en_preparacion"`` regardless of dates or now."""
    j = _in_memory_jornada(status=JornadaStatus.draft)
    assert compute_effective_phase(j) == "en_preparacion"
    assert (
        compute_effective_phase(
            j, now=datetime(2030, 1, 1, tzinfo=timezone.utc),
        ) == "en_preparacion"
    )


def test_compute_effective_phase_lista_abre_pronto():
    """``active`` + now < sale_opens_at → ``"lista_abre_pronto"``."""
    j = _in_memory_jornada(status=JornadaStatus.active)
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)  # before sale_opens_at
    assert compute_effective_phase(j, now=now) == "lista_abre_pronto"


def test_compute_effective_phase_in_window():
    """``active`` + sale_opens_at ≤ now < voting_closes_at → ``"in_window"``."""
    j = _in_memory_jornada(status=JornadaStatus.active)
    # sale_opens_at = 2026-10-08 19:00, voting_closes_at = 2026-10-11 14:00
    for now_str in (
        "2026-10-08T19:00:00+00:00",  # exact boundary
        "2026-10-10T12:00:00+00:00",  # mid-window
        "2026-10-11T13:59:59+00:00",  # 1ms before closing
    ):
        now = datetime.fromisoformat(now_str)
        assert compute_effective_phase(j, now=now) == "in_window", now_str


def test_compute_effective_phase_fuera_de_ventana():
    """``active`` + now >= voting_closes_at → ``"fuera_de_ventana"``."""
    j = _in_memory_jornada(status=JornadaStatus.active)
    for now_str in (
        "2026-10-11T14:00:00+00:00",  # exact boundary
        "2026-10-11T22:30:00+00:00",  # mid-escrutinio window
    ):
        now = datetime.fromisoformat(now_str)
        assert (
            compute_effective_phase(j, now=now) == "fuera_de_ventana"
        ), now_str


def test_compute_effective_phase_past_defaults_to_escrutinio_disponible():
    """``past`` → ``"escrutinio_disponible"`` regardless of dates/now.

    Slice 1 of the superadmin-panel sprint collapsed the old
    ``closed``/``completed`` pair into a single ``past`` state. The
    helper can't see the ``jornada_escrutinio`` table, so it defaults
    to ``"escrutinio_disponible"`` here; the public ``/jornadas/{id}``
    endpoint overrides this with the actual escrutinio presence via a
    separate DB read. This test pins the helper's contract.
    """
    j = _in_memory_jornada(status=JornadaStatus.past)
    assert compute_effective_phase(j) == "escrutinio_disponible"
    assert (
        compute_effective_phase(
            j, now=datetime(2030, 1, 1, tzinfo=timezone.utc),
        ) == "escrutinio_disponible"
    )


def test_compute_effective_phase_active_without_dates_is_fuera_de_ventana():
    """An ``active`` jornada with no window dates → ``"fuera_de_ventana"``.

    Defensive: the helper cannot establish if voting is open, so it
    reports the conservative outside-of-window phase. Same behaviour
    when only one of the two dates is set.
    """
    for sale_v, closes_v in (
        (None, None),
        (datetime(2026, 10, 8, tzinfo=timezone.utc), None),
        (None, datetime(2026, 10, 11, tzinfo=timezone.utc)),
    ):
        j = _in_memory_jornada(
            status=JornadaStatus.active,
            sale_opens_at=sale_v,
            voting_closes_at=closes_v,
        )
        assert (
            compute_effective_phase(j) == "fuera_de_ventana"
        ), (sale_v, closes_v)


def test_compute_effective_phase_draft_without_dates_is_en_preparacion():
    """``draft`` with no dates still reports ``"en_preparacion"``."""
    j = _in_memory_jornada(
        status=JornadaStatus.draft,
        sale_opens_at=None,
        voting_closes_at=None,
    )
    assert compute_effective_phase(j) == "en_preparacion"


def test_compute_effective_phase_defaults_now_to_utc():
    """When ``now`` is omitted, ``datetime.now(tz=UTC)`` is used.

    We can't pin the exact value (wall-clock drift), but we can verify the
    function does not blow up and the output is one of the 6 phases.
    """
    from app.services.jornada_manager import EffectivePhase

    j = _in_memory_jornada(status=JornadaStatus.active)
    phase = compute_effective_phase(j)
    assert phase in EffectivePhase.__args__  # type: ignore[attr-defined]


def test_compute_effective_phase_naive_now_is_treated_as_utc():
    """A naive ``now`` is treated as UTC (column is tz-aware)."""
    j = _in_memory_jornada(status=JornadaStatus.active)
    # Naive now equivalent to the exact sale_opens_at instant.
    naive_now = datetime(2026, 10, 8, 19, 0)
    assert compute_effective_phase(j, now=naive_now) == "in_window"


# ==========================================================================
# assert_jornada_accepts (slice 4 — the 4 mutation endpoints use this).
# ==========================================================================


async def test_assert_jornada_accepts_bets_in_window_ok():
    """``in_window`` + action="bets" → no exception (returns None)."""
    j = _in_memory_jornada(status=JornadaStatus.active)
    now = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
    async with AsyncSessionLocal() as session:
        mgr = JornadaManager()
        result = await mgr.assert_jornada_accepts(
            session, j, action="bets", now=now,
        )
    assert result is None


async def test_assert_jornada_accepts_bets_fuera_de_ventana_409():
    """``fuera_de_ventana`` + action="bets" → 409 with structured detail."""
    j = _in_memory_jornada(status=JornadaStatus.active)
    now = datetime(2026, 10, 11, 15, 0, tzinfo=timezone.utc)  # past close
    async with AsyncSessionLocal() as session:
        mgr = JornadaManager()
        with pytest.raises(HTTPException) as exc_info:
            await mgr.assert_jornada_accepts(
                session, j, action="bets", now=now,
            )
    exc = exc_info.value
    assert exc.status_code == 409
    assert isinstance(exc.detail, dict)
    assert exc.detail["effective_phase"] == "fuera_de_ventana"
    assert exc.detail["code"] == "voting_window_closed"


async def test_assert_jornada_accepts_bets_lista_abre_pronto_409():
    """``lista_abre_pronto`` + action="bets" → 409 (sale hasn't opened)."""
    j = _in_memory_jornada(status=JornadaStatus.active)
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)  # before sale_opens_at
    async with AsyncSessionLocal() as session:
        mgr = JornadaManager()
        with pytest.raises(HTTPException) as exc_info:
            await mgr.assert_jornada_accepts(
                session, j, action="bets", now=now,
            )
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "sale_not_yet_opened"
    assert exc_info.value.detail["effective_phase"] == "lista_abre_pronto"


async def test_assert_jornada_accepts_bets_past_409():
    """``past`` + action="bets" → 409 (no more voting, jornada is history).

    Slice 1 collapsed ``closed``/``completed`` into ``past``. The phase
    for a past jornada is now ``"escrutinio_disponible"`` (the helper
    defaults to that string; the public response overrides it with the
    real escrutinio presence). The error code stays the same as the
    old ``completed`` path because the underlying UX is the same:
    "voting is over, game is done."
    """
    j = _in_memory_jornada(status=JornadaStatus.past)
    async with AsyncSessionLocal() as session:
        mgr = JornadaManager()
        with pytest.raises(HTTPException) as exc_info:
            await mgr.assert_jornada_accepts(session, j, action="bets")
    assert exc_info.value.status_code == 409
    assert (
        exc_info.value.detail["code"] == "escrutinio_disponible"
    )
    assert (
        exc_info.value.detail["effective_phase"]
        == "escrutinio_disponible"
    )


async def test_assert_jornada_accepts_edits_in_window_ok():
    """``in_window`` + action="edits" → OK."""
    j = _in_memory_jornada(status=JornadaStatus.active)
    now = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
    async with AsyncSessionLocal() as session:
        mgr = JornadaManager()
        assert (
            await mgr.assert_jornada_accepts(
                session, j, action="edits", now=now,
            )
            is None
        )


async def test_assert_jornada_accepts_edits_lista_abre_pronto_ok():
    """``lista_abre_pronto`` + action="edits" → OK (admin can pre-setup)."""
    j = _in_memory_jornada(status=JornadaStatus.active)
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)  # before sale_opens_at
    async with AsyncSessionLocal() as session:
        mgr = JornadaManager()
        assert (
            await mgr.assert_jornada_accepts(
                session, j, action="edits", now=now,
            )
            is None
        )


async def test_assert_jornada_accepts_edits_past_409():
    """``past`` + action="edits" → 409 (game is over, edits are rejected).

    Slice 1 collapsed ``closed``/``completed`` into ``past``; both old
    states rejected edits, so the new ``past`` state also rejects.
    """
    j = _in_memory_jornada(status=JornadaStatus.past)
    async with AsyncSessionLocal() as session:
        mgr = JornadaManager()
        with pytest.raises(HTTPException) as exc_info:
            await mgr.assert_jornada_accepts(session, j, action="edits")
    assert exc_info.value.status_code == 409
    assert (
        exc_info.value.detail["code"] == "escrutinio_disponible"
    )


async def test_assert_jornada_accepts_edits_fuera_de_ventana_409():
    """``fuera_de_ventana`` + action="edits" → 409 (window is past)."""
    j = _in_memory_jornada(status=JornadaStatus.active)
    now = datetime(2026, 10, 11, 22, 0, tzinfo=timezone.utc)  # past close
    async with AsyncSessionLocal() as session:
        mgr = JornadaManager()
        with pytest.raises(HTTPException) as exc_info:
            await mgr.assert_jornada_accepts(
                session, j, action="edits", now=now,
            )
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["effective_phase"] == "fuera_de_ventana"


async def test_assert_jornada_accepts_final_confirmation_in_window_ok():
    """``in_window`` + action="final_confirmation" → OK."""
    j = _in_memory_jornada(status=JornadaStatus.active)
    now = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
    async with AsyncSessionLocal() as session:
        mgr = JornadaManager()
        assert (
            await mgr.assert_jornada_accepts(
                session, j, action="final_confirmation", now=now,
            )
            is None
        )


async def test_assert_jornada_accepts_final_confirmation_past_409():
    """``past`` + action="final_confirmation" → 409 (escrutinio path).

    Slice 1 collapsed ``closed``/``completed`` into ``past``. The old
    ``closed`` state allowed ``final_confirmation`` (via the
    ``escrutinio_pendiente`` phase); ``completed`` rejected it. The
    new ``past`` state has a single phase ``escrutinio_disponible`` and
    the helper rejects ``final_confirmation`` — the design is "once
    the admin has flipped the jornada to past, the boleto is history,
    no more confirmations." This test pins that contract.
    """
    j = _in_memory_jornada(status=JornadaStatus.past)
    async with AsyncSessionLocal() as session:
        mgr = JornadaManager()
        with pytest.raises(HTTPException) as exc_info:
            await mgr.assert_jornada_accepts(
                session, j, action="final_confirmation",
            )
    assert exc_info.value.status_code == 409
    assert (
        exc_info.value.detail["effective_phase"]
        == "escrutinio_disponible"
    )


async def test_assert_jornada_accepts_final_confirmation_en_preparacion_409():
    """``en_preparacion`` + action="final_confirmation" → 409.

    A ``draft`` jornada has no partidos ingested, no propuesta, and no
    votes to confirm — the admin must first call ``configure_dates``
    (which moves the jornada to ``active``).
    """
    j = _in_memory_jornada(status=JornadaStatus.draft)
    async with AsyncSessionLocal() as session:
        mgr = JornadaManager()
        with pytest.raises(HTTPException) as exc_info:
            await mgr.assert_jornada_accepts(
                session, j, action="final_confirmation",
            )
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "jornada_in_preparation"
    assert (
        exc_info.value.detail["effective_phase"] == "en_preparacion"
    )


async def test_assert_jornada_accepts_final_confirmation_lista_abre_pronto_ok():
    """``lista_abre_pronto`` + action="final_confirmation" → OK.

    Per the chosen slice-4 option (allow 4 phases, block 2), the admin
    can pre-load the FINAL boleto while the jornada is configured but
    before sale opens.
    """
    j = _in_memory_jornada(status=JornadaStatus.active)
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)  # before sale_opens_at
    async with AsyncSessionLocal() as session:
        mgr = JornadaManager()
        assert (
            await mgr.assert_jornada_accepts(
                session, j, action="final_confirmation", now=now,
            )
            is None
        )


async def test_assert_jornada_accepts_final_confirmation_fuera_de_ventana_ok():
    """``fuera_de_ventana`` + action="final_confirmation" → OK.

    Voting is closed but the admin can still confirm the FINAL boleto in
    the post-close window before escrutinio is published. This is the
    "admin confirms after cierre de votación, before escrutinio" case
    from the design rationale.
    """
    j = _in_memory_jornada(status=JornadaStatus.active)
    now = datetime(2026, 10, 11, 22, 0, tzinfo=timezone.utc)
    async with AsyncSessionLocal() as session:
        mgr = JornadaManager()
        assert (
            await mgr.assert_jornada_accepts(
                session, j, action="final_confirmation", now=now,
            )
            is None
        )


async def test_assert_jornada_accepts_unknown_action_raises_value_error():
    """An unknown ``action`` raises ``ValueError`` (caller-side bug)."""
    j = _in_memory_jornada(status=JornadaStatus.active)
    async with AsyncSessionLocal() as session:
        mgr = JornadaManager()
        with pytest.raises(ValueError, match="unknown action"):
            await mgr.assert_jornada_accepts(  # type: ignore[arg-type]
                session, j, action="bogus",  # noqa: S106
            )


async def test_assert_jornada_accepts_body_shape():
    """The 409 ``detail`` carries exactly the 5 fields from § 4.3.

    Verifies the contract end-to-end: ``code``, ``sale_opens_at``,
    ``voting_closes_at``, ``now``, ``effective_phase``. Values for the
    dates are the ISO 8601 round-trip of the fields that were
    persisted; the test uses an explicit ``now`` so the comparison is
    deterministic.
    """
    sale = datetime(2026, 10, 8, 19, 0, tzinfo=timezone.utc)
    closes = datetime(2026, 10, 11, 14, 0, tzinfo=timezone.utc)
    j = _in_memory_jornada(
        status=JornadaStatus.active,
        sale_opens_at=sale,
        voting_closes_at=closes,
    )
    now = datetime(2026, 10, 11, 22, 0, tzinfo=timezone.utc)
    async with AsyncSessionLocal() as session:
        mgr = JornadaManager()
        with pytest.raises(HTTPException) as exc_info:
            await mgr.assert_jornada_accepts(
                session, j, action="bets", now=now,
            )
    detail = exc_info.value.detail
    assert isinstance(detail, dict)
    # Exact keys — the contract pins these five names.
    assert set(detail.keys()) == {
        "code",
        "sale_opens_at",
        "voting_closes_at",
        "now",
        "effective_phase",
    }
    # The string codes the client pattern-matches on.
    assert isinstance(detail["code"], str)
    assert detail["code"] == "voting_window_closed"
    # ISO 8601 timestamps, round-tripped from the DB.
    assert detail["sale_opens_at"] == sale.isoformat()
    assert detail["voting_closes_at"] == closes.isoformat()
    assert detail["now"] == now.isoformat()
    # Phase derived from the (status, dates, now) tuple.
    assert detail["effective_phase"] == "fuera_de_ventana"


async def test_assert_jornada_accepts_body_shape_with_null_dates():
    """When ``sale_opens_at`` / ``voting_closes_at`` are NULL, the body
    has them as ``null`` (not omitted)."""
    j = _in_memory_jornada(
        status=JornadaStatus.past,
        sale_opens_at=None,
        voting_closes_at=None,
    )
    async with AsyncSessionLocal() as session:
        mgr = JornadaManager()
        with pytest.raises(HTTPException) as exc_info:
            await mgr.assert_jornada_accepts(
                session, j, action="bets", now=datetime(
                    2026, 10, 11, 22, 0, tzinfo=timezone.utc,
                ),
            )
    detail = exc_info.value.detail
    assert detail["sale_opens_at"] is None
    assert detail["voting_closes_at"] is None
    assert detail["effective_phase"] == "escrutinio_disponible"