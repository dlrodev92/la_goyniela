"""Tests for T-data-2 — APScheduler integration (Quiniwin daily ingest).

The scheduler, the jobs module, the FastAPI lifespan, and the env-driven
Settings are exercised together with the real Postgres DB on port 5433
(see ``tests/conftest.py``). The autouse ``_cleanup_rows`` fixture wipes
the test-created rows after each test.

Tolerance contract (asserted by ``test_job_iterates_jornadas_and_creates_one_fetch_session``):

- One bad jornada (404 / parse error) does NOT abort the others.
- Each tick persists exactly one ``fetch_session`` row (not one per jornada).
- The ``source_stats`` JSONB contains the per-jornada breakdown.

We also pin down that re-running the job is idempotent (no new
jornada/composicion rows) — the only thing that grows is fetch_session.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from app.db.models import Composicion, FetchSession, Jornada, JornadaEscrutinio
from app.db.session import AsyncSessionLocal, engine
from app.main import app
from app.scheduler.config import SchedulerSettings
from app.scheduler.jobs import (
    SOURCE_CODE_QUINIWIN,
    LOSILLA_DAILY_POLL_GRACE_HOURS,
    _discover_forward_window,
    fetch_and_record,
    ingest_today_quiniwin_job,
    losilla_poll_recent_closed_job,
)
from app.scripts.losilla_ingest import IngestSummary
from app.scheduler.runner import SchedulerRunner


# ---------- Cleanup ----------


@pytest_asyncio.fixture(autouse=True)
async def _cleanup_rows():
    """Wipe jornada + composicion + fetch_session + auto/test team rows.

    The slice 9 tests insert ``jornada_escrutinio`` and
    ``jornada_comentario`` rows via the losilla daily poll job. We delete
    them in the right order so the parent ``jornada`` CASCADE on the
    next-to-last step takes care of the children, and any stragglers
    (e.g. rows whose parent was wiped by an earlier test) are gone too.
    """
    yield
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM fetch_session"))
        await conn.execute(text("DELETE FROM jornada_escrutinio"))
        await conn.execute(text("DELETE FROM jornada_comentario"))
        await conn.execute(text("DELETE FROM composicion"))
        await conn.execute(text("DELETE FROM jornada"))
        await conn.execute(
            text(
                "DELETE FROM team_identity_map "
                "WHERE entity_id LIKE 'test-%' OR entity_id LIKE 'auto-%'"
            )
        )


# ---------- Settings defaults ----------


def _clear_scheduler_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip any inherited scheduler env vars so defaults are honest."""
    for k in [
        "SCHEDULER_ENABLED",
        "SCHEDULER_TIMEZONE",
        "SCHEDULER_INGEST_HOUR",
        "SCHEDULER_INGEST_MINUTE",
        "SCHEDULER_FETCH_TIMEOUT_SEC",
        "SCHEDULER_JORNADA_RANGE_START",
        "SCHEDULER_JORNADA_RANGE_END",
        "SCHEDULER_JORNADA_FORWARD_CAP",
        "SCHEDULER_LOSILLA_DAILY_POLL_HOUR",
        "SCHEDULER_LOSILLA_DAILY_POLL_MINUTE",
        "SCHEDULER_LOSILLA_DAILY_POLL_ENABLED",
        "SCHEDULER_AUTORUN_ON_STARTUP",
    ]:
        monkeypatch.delenv(k, raising=False)


def test_scheduler_config_defaults(monkeypatch):
    """Defaults: Europe/Madrid, 09:00, scheduler disabled in test env.

    We construct ``SchedulerSettings`` with a clean env (no inherited
    ``SCHEDULER_*`` vars) so the test does not depend on whatever is in
    ``.env``. The default ``scheduler_enabled`` is False — production
    deployments opt in via ``SCHEDULER_ENABLED=true``.
    """
    _clear_scheduler_env(monkeypatch)

    s = SchedulerSettings()

    assert s.scheduler_enabled is False
    assert s.scheduler_timezone == "Europe/Madrid"
    assert s.scheduler_ingest_hour == 9
    assert s.scheduler_ingest_minute == 0
    assert s.scheduler_fetch_timeout_sec == 20.0
    assert s.scheduler_jornada_range_start == 1
    assert s.scheduler_jornada_range_end == 42
    assert s.scheduler_jornada_forward_cap == 5
    assert s.scheduler_autorun_on_startup is True
    # Slice 9 (§6.4) defaults: 23:00 Madrid, kill switch on by default.
    assert s.scheduler_losilla_daily_poll_hour == 23
    assert s.scheduler_losilla_daily_poll_minute == 0
    assert s.scheduler_losilla_daily_poll_enabled is True
    # Inherited from core: still has database_url
    assert s.database_url.startswith("postgresql+psycopg://")


def test_scheduler_config_rejects_bad_timezone(monkeypatch):
    """A garbage TZ string should fail at construction, not at first tick."""
    _clear_scheduler_env(monkeypatch)
    monkeypatch.setenv("SCHEDULER_TIMEZONE", "Not/A_Real_TZ")

    with pytest.raises(Exception) as ei:
        SchedulerSettings()
    # Pydantic wraps the validation error; assert the message mentions TZ.
    assert "SCHEDULER_TIMEZONE" in str(ei.value) or "timezone" in str(ei.value).lower()


def test_scheduler_config_rejects_hour_out_of_range(monkeypatch):
    """Ge/le constraints on hour and minute kick in early."""
    _clear_scheduler_env(monkeypatch)
    monkeypatch.setenv("SCHEDULER_INGEST_HOUR", "99")
    with pytest.raises(Exception):
        SchedulerSettings()


# ---------- Runner behaviour ----------


@pytest.mark.asyncio
async def test_scheduler_disabled_means_no_jobs(monkeypatch):
    """When SCHEDULER_ENABLED=false, start() is a no-op.

    The runner should return an empty job list, the underlying
    AsyncIOScheduler should never enter its running state, and no thread
    should be spawned.
    """
    _clear_scheduler_env(monkeypatch)
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")

    runner = SchedulerRunner()
    assert runner.is_running is False

    ids = await runner.start()
    assert ids == []
    assert runner.is_running is False
    assert runner.scheduler.get_jobs() == []

    # Shutdown must be safe to call even though nothing was started.
    await runner.shutdown()
    assert runner.is_running is False


@pytest.mark.asyncio
async def test_scheduler_enabled_adds_daily_job(monkeypatch):
    """When enabled, the Quiniwin daily job, the Losilla weekly job,
    AND the Losilla daily poll job (slice 9) are registered (set of
    ids, order not guaranteed)."""
    _clear_scheduler_env(monkeypatch)
    monkeypatch.setenv("SCHEDULER_ENABLED", "true")
    monkeypatch.setenv("SCHEDULER_INGEST_HOUR", "9")
    monkeypatch.setenv("SCHEDULER_INGEST_MINUTE", "0")
    monkeypatch.setenv("SCHEDULER_TIMEZONE", "Europe/Madrid")

    runner = SchedulerRunner()
    ids = await runner.start()
    try:
        assert set(ids) == {
            "ingest_today_quiniwin",
            "losilla_persist_recent",
            "losilla_poll_recent_closed",
        }
        jobs = runner.scheduler.get_jobs()
        assert len(jobs) == 3
        quiniwin_job = next(j for j in jobs if j.id == "ingest_today_quiniwin")
        assert quiniwin_job.id == "ingest_today_quiniwin"
        # Trigger should be a CronTrigger with hour=9, minute=0, tz=Madrid.
        from apscheduler.triggers.cron import CronTrigger

        assert isinstance(quiniwin_job.trigger, CronTrigger)
        # CronTrigger.fields order is
        # [year, month, day, week, day_of_week, hour, minute, second]
        # so hour is index 5 and minute is index 6. The string form of the
        # field carries the configured value.

        fields = {f.name: str(f) for f in quiniwin_job.trigger.fields}
        assert fields["hour"] == "9"
        assert fields["minute"] == "0"

        # The Losilla daily poll job uses hour=23, minute=0 (slice 9
        # default), no day_of_week constraint.
        daily_poll = next(
            j for j in jobs if j.id == "losilla_poll_recent_closed"
        )
        dp_fields = {f.name: str(f) for f in daily_poll.trigger.fields}
        assert dp_fields["hour"] == "23"
        assert dp_fields["minute"] == "0"
        # No day_of_week restriction (cron fires every day).
        assert dp_fields["day_of_week"] == "*"
    finally:
        await runner.shutdown()

    assert runner.is_running is False


@pytest.mark.asyncio
async def test_scheduler_daily_poll_kill_switch_disables_job(monkeypatch):
    """``SCHEDULER_LOSILLA_DAILY_POLL_ENABLED=false`` → daily job NOT
    registered, but the weekly Losilla backstop stays. The Quiniwin job
    is unaffected.
    """
    _clear_scheduler_env(monkeypatch)
    monkeypatch.setenv("SCHEDULER_ENABLED", "true")
    monkeypatch.setenv("SCHEDULER_LOSILLA_DAILY_POLL_ENABLED", "false")

    runner = SchedulerRunner()
    ids = await runner.start()
    try:
        assert "losilla_poll_recent_closed" not in ids
        # Weekly backstop is still here.
        assert "losilla_persist_recent" in ids
        # Quiniwin daily job is unaffected.
        assert "ingest_today_quiniwin" in ids
        assert len(ids) == 2
    finally:
        await runner.shutdown()


@pytest.mark.asyncio
async def test_scheduler_daily_poll_trigger_uses_settings(monkeypatch):
    """The CronTrigger for the daily poll job reads hour/minute from
    the SchedulerSettings (so operators can move it without code)."""
    _clear_scheduler_env(monkeypatch)
    monkeypatch.setenv("SCHEDULER_ENABLED", "true")
    monkeypatch.setenv("SCHEDULER_LOSILLA_DAILY_POLL_HOUR", "22")
    monkeypatch.setenv("SCHEDULER_LOSILLA_DAILY_POLL_MINUTE", "30")
    monkeypatch.setenv("SCHEDULER_TIMEZONE", "Europe/Madrid")

    runner = SchedulerRunner()
    ids = await runner.start()
    try:
        assert "losilla_poll_recent_closed" in ids
        job = next(
            j for j in runner.scheduler.get_jobs()
            if j.id == "losilla_poll_recent_closed"
        )
        fields = {f.name: str(f) for f in job.trigger.fields}
        assert fields["hour"] == "22"
        assert fields["minute"] == "30"
    finally:
        await runner.shutdown()


# ---------- Job tolerance (the core contract) ----------


# Two distinct 15-row payloads so J1 and J3 both have valid XML.
J1_XML = """<?xml version="1.0" encoding="UTF-8"?>
<XML>
  <QuinielaBASE>
    <NumeroJornada valor="1"/>
    <Temporada valor="2026/2027"/>
    <Partidos>
""" + "\n".join(
    f'      <partido numero="{i+1}" pronostico="1" equipos="TeamA{i+1}-TeamB{i+1}"/>'
    for i in range(15)
) + """
    </Partidos>
  </QuinielaBASE>
</XML>
"""

J3_XML = """<?xml version="1.0" encoding="UTF-8"?>
<XML>
  <QuinielaBASE>
    <NumeroJornada valor="3"/>
    <Temporada valor="2026/2027"/>
    <Partidos>
""" + "\n".join(
    f'      <partido numero="{i+1}" pronostico="1" equipos="TeamC{i+1}-TeamD{i+1}"/>'
    for i in range(15)
) + """
    </Partidos>
  </QuinielaBASE>
</XML>
"""


def _url_for(temporada: int, j: int) -> str:
    return f"https://www.quiniwin.com/xml/{temporada}-{temporada + 1}_j{j:02d}.xml"


@pytest.mark.asyncio
async def test_job_iterates_jornadas_and_creates_one_fetch_session(
    monkeypatch, httpx_mock,
):
    """One fetch_session per tick, per-jornada breakdown in source_stats.

    Mocked upstream: J1 → 15 rows, J2 → 404, J3 → 15 rows.
    Expectations:

    - Exactly 2 ingest events succeed (J1 + J3).
    - Exactly ONE ``fetch_session`` row exists (not three).
    - ``source_stats.quiniwin.jornadas`` has 3 entries: 2 ok + 1 error.
    - The error entry carries ``status=error`` for J2.
    - ``ok_count=2``, ``fail_count=1``.
    """
    _clear_scheduler_env(monkeypatch)

    httpx_mock.add_response(url=_url_for(2026, 1), content=J1_XML.encode("utf-8"))
    httpx_mock.add_response(url=_url_for(2026, 2), status_code=404)
    httpx_mock.add_response(url=_url_for(2026, 3), content=J3_XML.encode("utf-8"))

    stats = await ingest_today_quiniwin_job(
        temporada=2026,
        fetch_timeout_sec=20.0,
        jornada_range_start=1,
        jornada_range_end=3,
        forward_cap=5,
        chain_losilla=False,  # tests only mock Quiniwin HTTP
    )

    # The forward window starts at 1 because nothing is in the DB yet,
    # so the loop walks [1, 2, 3]. J2 404s (recorded as failure); J1 and
    # J3 succeed.
    quiniwin = stats["quiniwin"]
    assert quiniwin["status"] == "partial"
    assert quiniwin["ok_count"] == 2
    assert quiniwin["fail_count"] == 1
    assert {row["jornada"] for row in quiniwin["jornadas"]} == {1, 2, 3}
    by_j = {row["jornada"]: row for row in quiniwin["jornadas"]}
    assert by_j[1]["status"] == "ok"
    assert by_j[2]["status"] == "error"
    assert by_j[3]["status"] == "ok"
    # J1 and J3 each ingested 15 composicion rows.
    assert by_j[1]["composicion_inserted"] == 15
    assert by_j[3]["composicion_inserted"] == 15
    # J2: no composicion touched.
    assert by_j[2]["composicion_inserted"] == 0

    # Exactly ONE fetch_session row.
    async with AsyncSessionLocal() as session:
        fs_count = await session.scalar(
            select(func.count()).select_from(FetchSession)
        )
        assert fs_count == 1

        row = (await session.execute(select(FetchSession))).scalars().one()
        assert row.source_stats == stats
        assert row.finished_at is not None
        assert row.started_at <= row.finished_at
        # source_id: we don't seed a 'quiniwin' source row in tests, so
        # the FK stays null (the audit row records "we did the work; we
        # just don't have a Source catalog entry for it yet").
        assert row.source_id is None
        assert row.notes is not None

    # DB-side: J1 and J3 persisted, J2 not.
    async with AsyncSessionLocal() as session:
        nums = (await session.execute(
            select(Jornada.numero).where(Jornada.temporada == 2026)
        )).scalars().all()
    assert set(nums) == {1, 3}


@pytest.mark.asyncio
async def test_job_idempotent_on_repeat_run(monkeypatch, httpx_mock):
    """Running the job twice with the same upstream = no new jornada/composicion.

    The only thing that grows across runs is the ``fetch_session`` table.
    """
    _clear_scheduler_env(monkeypatch)

    # Run 1 will fetch J1 + J2 (max_known is None → window is [1, 2]).
    # Run 2 will NOT fetch anything (max=2 == range_end → empty window).
    httpx_mock.add_response(url=_url_for(2026, 1), content=J1_XML.encode("utf-8"))
    httpx_mock.add_response(url=_url_for(2026, 2), content=J3_XML.encode("utf-8"))

    # Run 1: nothing in DB, so forward window = [1, 2].
    await ingest_today_quiniwin_job(
        temporada=2026, jornada_range_start=1, jornada_range_end=2,
        forward_cap=5,
        chain_losilla=False,  # tests only mock Quiniwin HTTP
    )

    async with AsyncSessionLocal() as session:
        j1_count_after_first = await session.scalar(
            select(func.count()).select_from(Jornada).where(
                Jornada.temporada == 2026,
            )
        )
        comp1_count = await session.scalar(
            select(func.count()).select_from(Composicion)
        )
        fs1_count = await session.scalar(
            select(func.count()).select_from(FetchSession)
        )
    assert j1_count_after_first == 2
    assert comp1_count == 30
    assert fs1_count == 1

    # Run 2: max(numero) = 2 == range_end, so forward window is empty.
    await ingest_today_quiniwin_job(
        temporada=2026, jornada_range_start=1, jornada_range_end=2,
        forward_cap=5,
        chain_losilla=False,  # tests only mock Quiniwin HTTP
    )

    async with AsyncSessionLocal() as session:
        j2_count = await session.scalar(
            select(func.count()).select_from(Jornada).where(
                Jornada.temporada == 2026,
            )
        )
        comp2_count = await session.scalar(
            select(func.count()).select_from(Composicion)
        )
        fs2_count = await session.scalar(
            select(func.count()).select_from(FetchSession)
        )
    # NO new jornada, NO new composicion, ONE new fetch_session.
    assert j2_count == j1_count_after_first
    assert comp2_count == comp1_count
    assert fs2_count == 2


# ---------- Slice 9 (§6.4): Losilla daily poll job ----------


async def _make_jornada_with_kickoff(
    *,
    numero: int,
    temporada: int = 2026,
    kickoff_last_match_at: datetime | None = None,
) -> int:
    """Insert a jornada row with optional kickoff_last_match_at.

    Returns the jornada.id. Mirrors the helper used by
    test_losilla_persistence.py but adds the kickoff field, which the
    daily poll filter relies on.
    """
    async with AsyncSessionLocal() as session:
        j = Jornada(
            temporada=temporada,
            temporada_str=f"{temporada}/{temporada + 1}",
            numero=numero,
            kickoff_last_match_at=kickoff_last_match_at,
        )
        session.add(j)
        await session.commit()
        await session.refresh(j)
        return j.id


def _fake_losilla_run_ingest(
    *, jornada: int, temporada: int,
) -> IngestSummary:
    """Build the "everything worked" IngestSummary for test stubs.

    The daily poll job does ``await losilla_run_ingest(...)``, so the
    test stubs that monkeypatch ``app.scripts.losilla_ingest.run_ingest``
    must be ``async def`` and return an :class:`IngestSummary`. The
    convention in the tests below is to wrap this in a small
    ``async def _spy(...)`` so the failure-injection tests can also
    raise from the same stub.
    """
    return IngestSummary(
        jornada=jornada,
        temporada=temporada,
        escrutinio_inserted=True,
        escrutinio_existed=False,
        comentarios_inserted=0,
        comentarios_updated=0,
        comentarios_existing=0,
    )


@pytest.mark.asyncio
async def test_daily_poll_picks_up_recently_closed_without_escrutinio(
    monkeypatch,
):
    """The job selects jornadas where
    ``kickoff_last_match_at + 3h < now`` AND no ``jornada_escrutinio``.

    Setup (all jornada rows are temporada=2026):

    - J1: kickoff 5h ago  → IN  (5h > 3h, no escrutinio)
    - J2: kickoff 1h ago  → OUT (1h < 3h grace)
    - J3: kickoff 5h ago  → OUT (already has escrutinio)
    - J4: kickoff 5h ago  → IN  (5h > 3h, no escrutinio)
    - J5: kickoff_last_match_at=NULL → OUT (filter requires non-NULL)

    Expected: only J1 and J4 are passed to ``losilla_run_ingest``.
    """
    _clear_scheduler_env(monkeypatch)

    now = datetime(2026, 10, 6, 22, 0, tzinfo=timezone.utc)
    five_h_ago = datetime(2026, 10, 6, 17, 0, tzinfo=timezone.utc)
    one_h_ago = datetime(2026, 10, 6, 21, 0, tzinfo=timezone.utc)

    j1 = await _make_jornada_with_kickoff(
        numero=1, kickoff_last_match_at=five_h_ago,
    )
    await _make_jornada_with_kickoff(
        numero=2, kickoff_last_match_at=one_h_ago,
    )
    j3 = await _make_jornada_with_kickoff(
        numero=3, kickoff_last_match_at=five_h_ago,
    )
    j4 = await _make_jornada_with_kickoff(
        numero=4, kickoff_last_match_at=five_h_ago,
    )
    await _make_jornada_with_kickoff(
        numero=5, kickoff_last_match_at=None,
    )

    # J3 already has an escrutinio row — must be excluded by the IS NULL
    # check.
    async with AsyncSessionLocal() as session:
        session.add(JornadaEscrutinio(
            jornada_id=j3,
            source_id=1,  # arbitrary; the losilla source is seeded in
                          # the losilla test suite, but here we just need
                          # a row to flip the IS NULL check.
            payload={"_test": True},
        ))
        await session.commit()

    # Patch the losilla_run_ingest function so the job doesn't hit the
    # network. Capture which jornadas the job asked us to ingest.
    seen_jornadas: list[int] = []

    async def _spy(*, jornada: int, temporada: int) -> IngestSummary:
        seen_jornadas.append(jornada)
        return _fake_losilla_run_ingest(jornada=jornada, temporada=temporada)

    monkeypatch.setattr(
        "app.scripts.losilla_ingest.run_ingest", _spy,
    )

    stats = await losilla_poll_recent_closed_job(temporada=2026, now=now)

    # The job called losilla_run_ingest exactly for J1 and J4 — not J2
    # (too fresh), not J3 (already has escrutinio), not J5 (no kickoff).
    assert sorted(seen_jornadas) == [1, 4]
    # All 5 jornada rows are still in the DB — the fake didn't touch
    # them and the filter doesn't delete candidates, just excludes them.
    async with AsyncSessionLocal() as session:
        nums = (await session.execute(
            select(Jornada.numero).where(Jornada.temporada == 2026)
            .order_by(Jornada.numero)
        )).scalars().all()
    assert nums == [1, 2, 3, 4, 5]

    # The audit trail: one fetch_session row, losilla section reports
    # 2 jornadas with status=ok.
    assert stats["losilla"]["status"] == "ok"
    assert stats["losilla"]["ok_count"] == 2
    assert stats["losilla"]["fail_count"] == 0
    assert {row["jornada"] for row in stats["losilla"]["jornadas"]} == {1, 4}

    async with AsyncSessionLocal() as session:
        fs_count = await session.scalar(
            select(func.count()).select_from(FetchSession)
        )
    assert fs_count == 1


@pytest.mark.asyncio
async def test_daily_poll_noop_on_empty_candidate_set(monkeypatch):
    """Zero candidates → 0 jornadas found, clean exit, ONE fetch_session.

    No upstream is hit and no error is raised. The fetch_session row
    is still written so the audit trail records the tick.
    """
    _clear_scheduler_env(monkeypatch)
    now = datetime(2026, 10, 6, 22, 0, tzinfo=timezone.utc)

    # Patch losilla_run_ingest to assert the job does NOT call it.
    async def _spy(*, jornada: int, temporada: int) -> IngestSummary:
        raise AssertionError(
            "losilla_run_ingest should not be called when no candidates"
        )

    monkeypatch.setattr(
        "app.scripts.losilla_ingest.run_ingest", _spy,
    )

    stats = await losilla_poll_recent_closed_job(temporada=2026, now=now)

    assert stats["losilla"]["status"] == "noop"
    assert stats["losilla"]["ok_count"] == 0
    assert stats["losilla"]["fail_count"] == 0
    assert stats["losilla"]["jornadas"] == []
    assert stats["losilla"]["grace_hours"] == LOSILLA_DAILY_POLL_GRACE_HOURS

    async with AsyncSessionLocal() as session:
        fs_count = await session.scalar(
            select(func.count()).select_from(FetchSession)
        )
        fs_row = (await session.execute(select(FetchSession))).scalars().one()
    assert fs_count == 1
    # The noop tick still records the grace_hours setting so the audit
    # trail can answer "what window was the job using?".
    assert fs_row.source_stats == stats


@pytest.mark.asyncio
async def test_daily_poll_idempotent_on_repeat_run(monkeypatch):
    """Re-running after a successful tick does not re-ingest anything.

    The first run inserts the (mocked) escrutinio for J1 + J4. On the
    second run, the IS NULL filter excludes them — same as the weekly
    job's idempotency contract.
    """
    _clear_scheduler_env(monkeypatch)
    now = datetime(2026, 10, 6, 22, 0, tzinfo=timezone.utc)
    five_h_ago = datetime(2026, 10, 6, 17, 0, tzinfo=timezone.utc)

    j1 = await _make_jornada_with_kickoff(
        numero=1, kickoff_last_match_at=five_h_ago,
    )
    j4 = await _make_jornada_with_kickoff(
        numero=4, kickoff_last_match_at=five_h_ago,
    )

    # Simulate a previous run that already persisted escrutinio for J1
    # and J4. The losilla source FK is required; we use id=1 — tests
    # don't seed ``source`` rows so this is purely a FK satisfier (we
    # never read it back).
    async with AsyncSessionLocal() as session:
        session.add(JornadaEscrutinio(
            jornada_id=j1, source_id=1, payload={"_test": True},
        ))
        session.add(JornadaEscrutinio(
            jornada_id=j4, source_id=1, payload={"_test": True},
        ))
        await session.commit()

    async def _spy(*, jornada: int, temporada: int) -> IngestSummary:
        raise AssertionError("no candidates expected on this tick")

    monkeypatch.setattr(
        "app.scripts.losilla_ingest.run_ingest", _spy,
    )

    stats = await losilla_poll_recent_closed_job(temporada=2026, now=now)
    assert stats["losilla"]["status"] == "noop"
    assert stats["losilla"]["jornadas"] == []
    # And only ONE fetch_session was written for this tick (the noop row).
    async with AsyncSessionLocal() as session:
        fs_count = await session.scalar(
            select(func.count()).select_from(FetchSession)
        )
    assert fs_count == 1


@pytest.mark.asyncio
async def test_daily_poll_failure_isolation(monkeypatch):
    """A failing ``losilla_run_ingest`` for one jornada does not abort
    the others — same contract as ``ingest_today_quiniwin_job``.
    """
    _clear_scheduler_env(monkeypatch)
    now = datetime(2026, 10, 6, 22, 0, tzinfo=timezone.utc)
    five_h_ago = datetime(2026, 10, 6, 17, 0, tzinfo=timezone.utc)

    await _make_jornada_with_kickoff(
        numero=1, kickoff_last_match_at=five_h_ago,
    )
    await _make_jornada_with_kickoff(
        numero=2, kickoff_last_match_at=five_h_ago,
    )
    await _make_jornada_with_kickoff(
        numero=3, kickoff_last_match_at=five_h_ago,
    )

    async def _failing(*, jornada: int, temporada: int) -> IngestSummary:
        if jornada == 2:
            raise RuntimeError("simulated upstream timeout")
        return _fake_losilla_run_ingest(
            jornada=jornada, temporada=temporada,
        )

    monkeypatch.setattr(
        "app.scripts.losilla_ingest.run_ingest", _failing,
    )

    stats = await losilla_poll_recent_closed_job(temporada=2026, now=now)

    losilla = stats["losilla"]
    assert losilla["status"] == "partial"
    assert losilla["ok_count"] == 2
    assert losilla["fail_count"] == 1
    by_j = {row["jornada"]: row for row in losilla["jornadas"]}
    assert by_j[1]["status"] == "ok"
    assert by_j[2]["status"] == "error"
    assert "simulated upstream timeout" in by_j[2]["error"]
    assert by_j[3]["status"] == "ok"


@pytest.mark.asyncio
async def test_daily_poll_grace_hours_is_configurable(monkeypatch):
    """The ``grace_hours`` kwarg is forwarded to the filter so the
    window is testable without changing the module constant.
    """
    _clear_scheduler_env(monkeypatch)
    now = datetime(2026, 10, 6, 22, 0, tzinfo=timezone.utc)
    two_h_ago = datetime(2026, 10, 6, 20, 0, tzinfo=timezone.utc)

    await _make_jornada_with_kickoff(
        numero=1, kickoff_last_match_at=two_h_ago,
    )

    # With the default 3h grace, 2h ago is too fresh → noop.
    stats_default = await losilla_poll_recent_closed_job(
        temporada=2026, now=now,
    )
    assert stats_default["losilla"]["status"] == "noop"

    # With grace=1h, 2h ago is in window → ingests.
    seen: list[int] = []

    async def _spy(*, jornada: int, temporada: int) -> IngestSummary:
        seen.append(jornada)
        return _fake_losilla_run_ingest(
            jornada=jornada, temporada=temporada,
        )

    monkeypatch.setattr(
        "app.scripts.losilla_ingest.run_ingest", _spy,
    )
    stats_short = await losilla_poll_recent_closed_job(
        temporada=2026, now=now, grace_hours=1,
    )
    assert stats_short["losilla"]["status"] == "ok"
    assert stats_short["losilla"]["ok_count"] == 1
    assert stats_short["losilla"]["grace_hours"] == 1
    assert seen == [1]


# ---------- Lifespan (FastAPI integration) ----------


def test_lifespan_starts_and_stops_scheduler(monkeypatch):
    """``TestClient(app)` activates the lifespan: scheduler runs while up.

    We assert:

    - Inside the lifespan, ``app.state.scheduler.is_running`` is True
      (or False if SCHEDULER_ENABLED=false, but we set it to true here).
    - The ``ingest_today_quiniwin`` job id is in
      ``app.state.scheduler_job_ids``.
    - After leaving the context, the scheduler is no longer running.
    """
    _clear_scheduler_env(monkeypatch)
    monkeypatch.setenv("SCHEDULER_ENABLED", "true")

    with TestClient(app) as client:
        # We do not need any HTTP request to drive startup — TestClient
        # __enter__ runs the lifespan. But we hit /health to be sure the
        # app is fully up.
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}

        # Inspect app.state via the TestClient's transport (TestClient
        # uses ASGITransport internally; the lifespan attaches state).
        scheduler = getattr(app.state, "scheduler", None)
        assert scheduler is not None
        ids = getattr(app.state, "scheduler_job_ids", [])
        assert "ingest_today_quiniwin" in ids
        assert scheduler.is_running is True

    # Out of the TestClient context — scheduler should be shut down.
    assert app.state.scheduler.is_running is False


def test_lifespan_no_jobs_when_disabled(monkeypatch):
    """``SCHEDULER_ENABLED=false`` (default in dev .env) → no thread.

    The runner is reachable on ``app.state`` but ``is_running`` is False and
    the job list is empty. ``/health`` still returns 200 (the existing
    endpoint must keep working under the new lifespan).
    """
    _clear_scheduler_env(monkeypatch)
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")

    with TestClient(app) as client:
        r = client.get("/health")
        assert r.status_code == 200
        scheduler = app.state.scheduler
        assert scheduler.is_running is False
        assert app.state.scheduler_job_ids == []

    assert app.state.scheduler.is_running is False


# ---------- Timezone ----------


def test_scheduler_timezone_is_madrid(monkeypatch):
    """The CronTrigger must use Europe/Madrid, not UTC.

    We construct the trigger the same way the runner does and assert the
    timezone is the configured one. This is the property the user cares
    about: 09:00 must mean 09:00 Madrid local time, not 09:00 UTC.
    """
    _clear_scheduler_env(monkeypatch)
    monkeypatch.setenv("SCHEDULER_ENABLED", "true")
    monkeypatch.setenv("SCHEDULER_TIMEZONE", "Europe/Madrid")
    monkeypatch.setenv("SCHEDULER_INGEST_HOUR", "9")
    monkeypatch.setenv("SCHEDULER_INGEST_MINUTE", "0")

    runner = SchedulerRunner()

    # Pull the trigger kwargs the same way the runner will.
    from apscheduler.triggers.cron import CronTrigger

    trigger = CronTrigger(
        hour=runner.settings.scheduler_ingest_hour,
        minute=runner.settings.scheduler_ingest_minute,
        timezone=runner.settings.scheduler_timezone,
    )
    # CronTrigger stores its timezone on the field; we can introspect it
    # via the next_fire_time computation.
    nrt = trigger.get_next_fire_time(None, datetime.now(tz=timezone.utc))
    assert nrt is not None
    # next_fire_time is timezone-aware; convert to Madrid and verify hour.
    from zoneinfo import ZoneInfo

    madrid = ZoneInfo("Europe/Madrid")
    assert nrt.astimezone(madrid).hour == 9
    assert nrt.astimezone(madrid).minute == 0
    # And it should NOT be 09:00 UTC (winter) or 11:00 UTC (summer DST).
    utc_offset = nrt.utcoffset()
    assert utc_offset is not None
    # Madrid is UTC+1 (winter) or UTC+2 (summer). 09:00 Madrid != 09:00 UTC
    # in winter (Madrid 09:00 = UTC 08:00) and summer (Madrid 09:00 = UTC 07:00).
    # If we were in UTC, nrt.utcoffset() would be zero. Assert non-zero:
    assert utc_offset != __import__("datetime").timedelta(0)


# ---------- fetch_and_record primitive ----------


@pytest.mark.asyncio
async def test_fetch_and_record_writes_one_row(monkeypatch):
    """``fetch_and_record`` always persists exactly one fetch_session row.

    Even when the wrapped coroutine raises. The audit trail is more
    important than the happy path.
    """
    async def _ok() -> dict[str, str]:
        return {"status": "ok", "rows_inserted": 15}

    stats = await fetch_and_record(
        AsyncSessionLocal,
        SOURCE_CODE_QUINIWIN,
        _ok,
        notes="unit-test",
    )
    assert stats == {"status": "ok", "rows_inserted": 15}

    async with AsyncSessionLocal() as session:
        rows = (await session.execute(select(FetchSession))).scalars().all()
        assert len(rows) == 1
        row = rows[0]
        assert row.finished_at is not None
        assert row.source_stats == stats
        assert row.notes == "unit-test"


@pytest.mark.asyncio
async def test_fetch_and_record_handles_caller_exception(monkeypatch):
    """A failing wrapped coroutine still records a fetch_session row."""

    async def _fail() -> dict[str, str]:
        raise RuntimeError("boom")

    stats = await fetch_and_record(
        AsyncSessionLocal,
        SOURCE_CODE_QUINIWIN,
        _fail,
        notes="unit-test-fail",
    )
    assert "error" in stats

    async with AsyncSessionLocal() as session:
        row = (await session.execute(select(FetchSession))).scalars().one()
        assert row.source_stats == stats
        assert row.notes is not None
        assert "boom" in row.notes


# ---------- _discover_forward_window ----------


def test_discover_forward_window_no_existing():
    """When nothing is in the DB, start at range_start."""
    out = _discover_forward_window(
        None, range_start=1, range_end=42, forward_cap=5,
    )
    assert out == [1, 2, 3, 4, 5]


def test_discover_forward_window_resumes_after_max():
    """When max=10, walk 11..15 (capped at forward_cap=5)."""
    out = _discover_forward_window(
        10, range_start=1, range_end=42, forward_cap=5,
    )
    assert out == [11, 12, 13, 14, 15]


def test_discover_forward_window_caps_at_range_end():
    """When max is near the range_end, stop at range_end."""
    out = _discover_forward_window(
        40, range_start=1, range_end=42, forward_cap=5,
    )
    assert out == [41, 42]


def test_discover_forward_window_empty_when_max_at_range_end():
    """When max == range_end, return [] (no work to do)."""
    out = _discover_forward_window(
        42, range_start=1, range_end=42, forward_cap=5,
    )
    assert out == []