"""Scheduler-specific settings.

The scheduler env vars live here (not in ``app.core.config``) so the web
process can keep running even when the scheduler is disabled. Both classes
load the same ``.env`` file because ``app.core.config.Settings`` already
loads it; subclassing keeps the inherited fields (``database_url``,
``log_level``, ...) free of duplication.

Env vars (all optional, all have sensible values for dev):

- ``SCHEDULER_ENABLED``               — bool (default ``false``). When false,
  ``SchedulerRunner.start()`` is a no-op and no jobs are registered.
- ``SCHEDULER_TIMEZONE``              — IANA TZ string (default
  ``"Europe/Madrid"``). Used by the CronTrigger so 09:00 means 09:00
  Madrid local time, not UTC.
- ``SCHEDULER_INGEST_HOUR``           — 0..23 (default ``9``).
- ``SCHEDULER_INGEST_MINUTE``         — 0..59 (default ``0``).
- ``SCHEDULER_FETCH_TIMEOUT_SEC``     — httpx timeout (default ``20``).
- ``SCHEDULER_JORNADA_RANGE_START``   — first jornada number to consider
  when resuming ingest (default ``1``). Together with
  ``SCHEDULER_JORNADA_RANGE_END`` (default ``42``) and the per-tick
  discovery (look up ``max(numero)`` already in DB), the job walks
  ``[max+1 .. max+CAP]`` and stops on the first 404.
- ``SCHEDULER_JORNADA_FORWARD_CAP``   — max number of forward-only jornadas
  per tick (default ``5``). One bad jornada must not blow up the batch.
- ``SCHEDULER_AUTORUN_ON_STARTUP``    — bool (default ``true``). When true
  the job also fires once at process startup so a freshly-deployed container
  does not wait until 09:00 to ingest. Tests flip this off to keep
  fixtures deterministic.
"""
from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator

from app.core.config import Settings as CoreSettings


class SchedulerSettings(CoreSettings):
    """Scheduler-specific env vars. Inherits DB / auth / etc. from core."""

    scheduler_enabled: bool = Field(
        default=False,
        description="When false, SchedulerRunner.start() is a no-op.",
    )
    scheduler_timezone: str = Field(
        default="Europe/Madrid",
        description="IANA timezone for the CronTrigger (e.g. 'Europe/Madrid').",
    )
    scheduler_ingest_hour: int = Field(
        default=9, ge=0, le=23,
        description="Hour of day for the daily ingest (0..23, scheduler TZ).",
    )
    scheduler_ingest_minute: int = Field(
        default=0, ge=0, le=59,
        description="Minute of hour for the daily ingest (0..59).",
    )
    scheduler_fetch_timeout_sec: float = Field(
        default=20.0, gt=0,
        description="httpx timeout for Quiniwin fetches.",
    )
    scheduler_jornada_range_start: int = Field(
        default=1, ge=1, le=99,
        description="Lower bound for jornada numbers when discovering 'next'.",
    )
    scheduler_jornada_range_end: int = Field(
        default=42, ge=1, le=99,
        description="Upper bound for jornada numbers (we stop at first 404).",
    )
    scheduler_jornada_forward_cap: int = Field(
        default=5, ge=1, le=42,
        description="Max number of forward-only jornadas to ingest per tick.",
    )
    scheduler_losilla_day_of_week: str = Field(
        default="mon",
        description=(
            "Cron day-of-week for the Losilla persistence job (3-letter "
            "lowercase: 'mon', 'tue', ...). The job persists escrutinio + "
            "comentarios for any closed jornada that doesn't have them yet."
        ),
    )
    scheduler_losilla_hour: int = Field(
        default=9, ge=0, le=23,
        description="Hour of day for the Losilla persistence job (scheduler TZ).",
    )
    scheduler_losilla_minute: int = Field(
        default=0, ge=0, le=59,
        description="Minute of hour for the Losilla persistence job.",
    )
    scheduler_losilla_daily_poll_hour: int = Field(
        default=23, ge=0, le=23,
        description=(
            "Hour of day for the daily Losilla poll job (scheduler TZ). "
            "Picks up any closed jornada whose last match finished more "
            "than 3 hours ago and which still lacks a ``jornada_escrutinio``."
        ),
    )
    scheduler_losilla_daily_poll_minute: int = Field(
        default=0, ge=0, le=59,
        description="Minute of hour for the daily Losilla poll job.",
    )
    scheduler_losilla_daily_poll_enabled: bool = Field(
        default=True,
        description=(
            "Kill switch for the daily Losilla poll job. When false, the "
            "daily 23:00 job is NOT registered; the weekly Monday job "
            "continues to act as a backstop."
        ),
    )
    scheduler_autorun_on_startup: bool = Field(
        default=True,
        description=(
            "When true, the scheduled job also fires once at startup so a "
            "freshly-deployed container does not wait until 09:00 for the "
            "first ingest. Tests flip this off."
        ),
    )

    @field_validator("scheduler_timezone")
    @classmethod
    def _validate_tz(cls, value: str) -> str:
        # Fail loudly at startup if the TZ string is wrong — CronTrigger
        # construction is deferred, so a bad string would surface only at
        # the first scheduled tick. Surface it here instead.
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(
                f"SCHEDULER_TIMEZONE={value!r} is not a valid IANA timezone"
            ) from exc
        return value


def get_settings() -> SchedulerSettings:
    """Build a fresh ``SchedulerSettings`` instance.

    Re-reads env vars on each call so tests that mutate ``os.environ``
    between calls see the new values. Production callers cache the result
    via :data:`settings` if they want one-shot loading.
    """
    return SchedulerSettings()


# Module-level singleton — matches the project pattern in ``app.core.config``.
# Tests that need a different env pass ``_environ={...}`` directly to
# ``SchedulerSettings(**_environ)``.
settings = get_settings()


__all__ = ["SchedulerSettings", "get_settings", "settings"]