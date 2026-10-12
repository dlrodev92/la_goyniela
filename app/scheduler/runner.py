"""AsyncIOScheduler wrapper.

Owns the APScheduler lifecycle: instantiate, attach cron jobs, start the
background thread, shut it down on app teardown. Keeps the rest of the
app free of apscheduler imports.

Design constraints (T-data-2 spec):

- ``SCHEDULER_ENABLED=false`` makes ``start()`` a no-op. No jobs registered,
  no thread spawned. Tests and dev use this to keep behaviour deterministic.
- The scheduled job is registered with ``max_instances=1`` and
  ``coalesce=True`` so a long-running ingest cannot pile up on itself and
  any missed ticks collapse into the next fire time.
- ``misfire_grace_time`` is set to a generous window (default APScheduler
  would be 1s — far too short for a 5-jornada ingest) so transient hiccups
  do not lose ticks. We do NOT set ``misfire_grace_time=None`` because that
  would silently fire every missed tick the moment the process restarts,
  which is the opposite of what we want.
- The timezone of the CronTrigger is the configured scheduler TZ
  (``Europe/Madrid`` by default). The scheduler's own timezone is set to
  UTC because APScheduler requires an aware datetime in ``start()``'s
  reply path; the cron trigger carries the human-facing TZ.
"""
from __future__ import annotations

import logging
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.scheduler.config import SchedulerSettings
from app.scheduler.jobs import (
    ingest_today_quiniwin_job,
    losilla_persist_recent_job,
    losilla_poll_recent_closed_job,
)


LOG = logging.getLogger("scheduler")


# CronTrigger kwargs for the daily Quiniwin ingest job. Module-level so
# tests can introspect it without instantiating the runner.
def _ingest_trigger_kwargs(settings: SchedulerSettings) -> dict[str, Any]:
    return {
        "hour": settings.scheduler_ingest_hour,
        "minute": settings.scheduler_ingest_minute,
        "timezone": settings.scheduler_timezone,
    }


# CronTrigger kwargs for the weekly Losilla persistence job.
def _losilla_trigger_kwargs(settings: SchedulerSettings) -> dict[str, Any]:
    return {
        "day_of_week": settings.scheduler_losilla_day_of_week,
        "hour": settings.scheduler_losilla_hour,
        "minute": settings.scheduler_losilla_minute,
        "timezone": settings.scheduler_timezone,
    }


# CronTrigger kwargs for the daily Losilla poll job (slice 9, §6.4).
# No day_of_week — fires every day at 23:00 Madrid local time.
def _losilla_daily_poll_trigger_kwargs(
    settings: SchedulerSettings,
) -> dict[str, Any]:
    return {
        "hour": settings.scheduler_losilla_daily_poll_hour,
        "minute": settings.scheduler_losilla_daily_poll_minute,
        "timezone": settings.scheduler_timezone,
    }


class SchedulerRunner:
    """Wraps :class:`AsyncIOScheduler` with our app's job set.

    Lifecycle:

    >>> runner = SchedulerRunner()
    >>> await runner.start()    # registers the daily ingest cron + starts
    >>> # ...app runs...
    >>> await runner.shutdown() # graceful shutdown
    """

    def __init__(
        self,
        settings: SchedulerSettings | None = None,
        *,
        scheduler: AsyncIOScheduler | None = None,
    ) -> None:
        self._settings = settings or SchedulerSettings()
        self._scheduler: AsyncIOScheduler = scheduler or AsyncIOScheduler(
            timezone="UTC",
        )
        self._started = False

    # --- properties ---

    @property
    def is_running(self) -> bool:
        """True when the underlying ``AsyncIOScheduler`` is running."""
        if not self._started:
            return False
        return self._scheduler.running

    @property
    def scheduler(self) -> AsyncIOScheduler:
        """The underlying ``AsyncIOScheduler`` (escape hatch for tests)."""
        return self._scheduler

    @property
    def settings(self) -> SchedulerSettings:
        return self._settings

    # --- lifecycle ---

    def add_jobs(self) -> list[str]:
        """Register all scheduled jobs on the underlying scheduler.

        Returns the list of registered job ids so tests can assert.

        Called automatically by :meth:`start`; exposed separately so tests
        can inspect what would be registered without actually starting
        the thread.
        """
        trigger = CronTrigger(**_ingest_trigger_kwargs(self._settings))
        self._scheduler.add_job(
            ingest_today_quiniwin_job,
            trigger=trigger,
            id="ingest_today_quiniwin",
            name="Daily Quiniwin ingest",
            kwargs={
                "fetch_timeout_sec": self._settings.scheduler_fetch_timeout_sec,
                "jornada_range_start": self._settings.scheduler_jornada_range_start,
                "jornada_range_end": self._settings.scheduler_jornada_range_end,
                "forward_cap": self._settings.scheduler_jornada_forward_cap,
            },
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=3600,
        )
        # Monday morning: persist Losilla escrutinio + comentarios for any
        # closed jornada that doesn't have them yet. Self-healing across
        # re-runs (the job is idempotent and only touches missing rows).
        losilla_trigger = CronTrigger(**_losilla_trigger_kwargs(self._settings))
        self._scheduler.add_job(
            losilla_persist_recent_job,
            trigger=losilla_trigger,
            id="losilla_persist_recent",
            name="Weekly Losilla persistence (post-jornada)",
            kwargs={},
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=3600,
        )
        # Daily 23:00: poll any recently-closed jornada that still lacks
        # its escrutinio (slice 9, §6.4). Closes the gap between Tuesday
        # and Sunday so a mid-week closure does not sit ``closed`` until
        # the Monday backstop above. The weekly job is kept as a backstop
        # for extreme cases. The kill switch
        # ``scheduler_losilla_daily_poll_enabled`` is honoured here —
        # when false, the daily job is NOT registered, just like the
        # other ``scheduler_*_enabled`` flags.
        if self._settings.scheduler_losilla_daily_poll_enabled:
            daily_poll_trigger = CronTrigger(
                **_losilla_daily_poll_trigger_kwargs(self._settings),
            )
            self._scheduler.add_job(
                losilla_poll_recent_closed_job,
                trigger=daily_poll_trigger,
                id="losilla_poll_recent_closed",
                name="Daily Losilla poll (recently-closed jornadas)",
                kwargs={},
                replace_existing=True,
                coalesce=True,
                max_instances=1,
                misfire_grace_time=3600,
            )
        return [j.id for j in self._scheduler.get_jobs()]

    async def start(self) -> list[str]:
        """Register jobs (if enabled) and start the scheduler.

        Returns the list of registered job ids. When the scheduler is
        disabled, returns an empty list and never touches APScheduler's
        thread.
        """
        if self._started:
            LOG.warning("SchedulerRunner.start() called while already running")
            return [j.id for j in self._scheduler.get_jobs()]

        if not self._settings.scheduler_enabled:
            LOG.info(
                "SCHEDULER_ENABLED=false; scheduler not starting",
            )
            return []

        ids = self.add_jobs()
        # ``start()`` is sync in APScheduler 3.x but launches its own
        # background asyncio task runner. We do NOT await it (would block
        # the FastAPI startup).
        self._scheduler.start()
        self._started = True
        LOG.info(
            "Scheduler started: %s jobs=%s tz=%s next_run=%s",
            self._scheduler.running,
            ids,
            self._settings.scheduler_timezone,
            # next_run_time may be None right after start; guard.
            self._get_next_run(),
        )
        return ids

    async def shutdown(self, *, wait: bool = True) -> None:
        """Shut the scheduler down. Safe to call even when not started."""
        if not self._started:
            return
        try:
            self._scheduler.shutdown(wait=wait)
        except Exception:  # pragma: no cover — defensive
            LOG.exception("Scheduler shutdown raised; continuing")
        finally:
            self._started = False
            LOG.info("scheduler shut down")

    # --- internals ---

    def _get_next_run(self) -> str | None:
        jobs = self._scheduler.get_jobs()
        if not jobs:
            return None
        nrt = jobs[0].next_run_time
        return nrt.isoformat() if nrt is not None else None


__all__ = ["SchedulerRunner"]