"""In-process APScheduler integration for periodic Quiniwin ingest.

Public surface (re-exported for callers):

- :class:`SchedulerRunner` from :mod:`app.scheduler.runner` — owns the
  ``AsyncIOScheduler`` lifecycle (``start``, ``shutdown``, ``add_jobs``).
- :func:`ingest_today_quiniwin_job` from :mod:`app.scheduler.jobs` — the
  scheduled coroutine that drives the daily Quiniwin fetch.
- :class:`SchedulerSettings` from :mod:`app.scheduler.config` — the
  pydantic-settings subclass with the scheduler env vars.
- :func:`lifespan` from :mod:`app.scheduler.lifespan` — the FastAPI
  lifespan context manager wired into ``app.main``.
"""
from __future__ import annotations

from app.scheduler.config import SchedulerSettings
from app.scheduler.jobs import (
    fetch_and_record,
    ingest_today_quiniwin_job,
)
from app.scheduler.lifespan import lifespan
from app.scheduler.runner import SchedulerRunner

__all__ = [
    "SchedulerSettings",
    "SchedulerRunner",
    "ingest_today_quiniwin_job",
    "fetch_and_record",
    "lifespan",
]