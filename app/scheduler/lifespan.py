"""FastAPI lifespan that wires the scheduler into the app.

Usage from ``app/main.py``::

    ``app = FastAPI(..., lifespan=scheduler.lifespan)``

Behaviours:

- On startup: build a :class:`SchedulerRunner`, call ``await runner.start()``,
  and stash it on ``app.state.scheduler`` for observability.
- On shutdown: ``await runner.shutdown()`` so APScheduler drains the
  currently-running job (if any) before the process exits.
- When ``SCHEDULER_ENABLED=false`` the runner's ``start()`` returns an
  empty job list and never spawns a thread. ``app.state.scheduler`` is
  still attached so tests and ``/health`` can introspect.

This context manager does NOT touch the Losilla HTTP client — that
lifecycle is still handled by ``app.main``'s existing teardown. (If we
later collapse that, we just add the ``_close_client()`` call here.)
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.scheduler.runner import SchedulerRunner


LOG = logging.getLogger("scheduler.lifespan")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start the scheduler on app startup; shut it down on teardown."""
    runner = SchedulerRunner()
    try:
        ids = await runner.start()
        app.state.scheduler = runner
        app.state.scheduler_job_ids = ids
        LOG.info("app.state.scheduler attached: jobs=%s", ids)
        yield
    finally:
        try:
            await runner.shutdown()
        except Exception:  # pragma: no cover — defensive
            LOG.exception("scheduler shutdown failed; continuing teardown")
        # Leave ``app.state.scheduler`` set so post-mortem inspection still
        # works in tests; do not delete.


__all__ = ["lifespan"]