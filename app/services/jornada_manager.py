"""Lifecycle helper for the ``Jornada`` window.

Slices 2 + 4 of the gestor-jornadas plan (``docs/producto/gestor-jornadas.md``,
sections 2.2, 3, 4). Owns:

- the four derived fields of a Jornada (``compute_window``):
  - ``kickoff_first_match_at``  =  min of ``composicion.kickoff_at``
  - ``kickoff_last_match_at``   =  max of ``composicion.kickoff_at``
  - ``voting_closes_at``        =  kickoff_last - 1h   (HARD RULE)
  - ``escrutinio_expected_at``  =  kickoff_last + 2.5h (HARD RULE)
- the effective phase computation (:func:`compute_effective_phase`).
- the voting-window guard used by the 4 mutation endpoints in
  :mod:`app.api.v1.porras` (:meth:`JornadaManager.assert_jornada_accepts`).

The first two derived fields are taken straight from the composicion rows;
the last two are derived by adding/subtracting the corresponding constants.
The two derived rules are hard-coded: there is no override hook in MVP.
``sale_opens_at`` is editorial and is only set by
:meth:`JornadaManager.configure_dates`.

The module exposes:

- :func:`compute_window` — pure math (no DB). Tests + manager use it.
- :func:`compute_effective_phase` — pure phase computation (no DB).
- :class:`JornadaManager` — async service that talks to the test/dev DB
  via a caller-provided :class:`AsyncSession`. No HTTP, no auth, no
  external dependencies. The only HTTP it raises is ``HTTPException(409)``
  on :meth:`JornadaManager.assert_jornada_accepts`, which is the helper
  the porras endpoints use.

Spec: ``docs/producto/gestor-jornadas.md`` § 2.2, § 3.1, § 4.1.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Literal, TypedDict

from fastapi import HTTPException, status as http_status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Composicion, Jornada, JornadaStatus


LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Hard rules (constants — no override in MVP).
# ---------------------------------------------------------------------------


VOTING_CLOSES_BEFORE_LAST_KICKOFF: timedelta = timedelta(hours=1)
"""Voting closes ``VOTING_CLOSES_BEFORE_LAST_KICKOFF`` before the last
partido kicks off. Hard rule per the design doc (gestor-jornadas § 3.1)."""

ESCRUTINIO_AFTER_LAST_KICKOFF: timedelta = timedelta(hours=2, minutes=30)
"""Escrutinio is expected ``ESCRUTINIO_AFTER_LAST_KICKOFF`` after the
last partido. Hard rule per the design doc (gestor-jornadas § 3.1)."""

PARTIDOS_PER_JORNADA: int = 15
"""Number of partidos in a Quiniela jornada. The CHECK constraint
``posicion BETWEEN 1 AND 15`` on ``composicion`` is the source of
truth; this constant is exposed for clarity in error messages."""


# ---------------------------------------------------------------------------
# Public TypedDict for the helper output.
# ---------------------------------------------------------------------------


class WindowFields(TypedDict):
    """The four derived fields returned by :func:`compute_window`.

    ``kickoff_first_match_at`` and ``kickoff_last_match_at`` come
    straight from the min/max of the input kickoffs. The other two are
    derived by subtracting / adding the corresponding hard-rule
    constants. Any of the four fields may be ``None`` if there are no
    non-null kickoffs in the input (the manager writes zeros to ``None``
    for those cases; slice 4 treats a ``None`` window as "no window").
    """
    kickoff_first_match_at: datetime | None
    kickoff_last_match_at: datetime | None
    voting_closes_at: datetime | None
    escrutinio_expected_at: datetime | None


# ---------------------------------------------------------------------------
# Exceptions.
# ---------------------------------------------------------------------------


class JornadaNotFoundError(LookupError):
    """The jornada id has no row in ``jornada``.

    Raised by :meth:`JornadaManager.configure_dates` and
    :meth:`JornadaManager.recompute_window` when the caller asks for
    a jornada that does not exist.
    """


class IncompleteJornadaError(ValueError):
    """One or more composicion rows are missing for the jornada.

    The manager refuses to compute the window for a jornada that does
    not have all 15 composicion rows present. Admin should ingest the
    boleto from Quiniwin first. Not a real failure in normal flow —
    the design assumes compos rows exist before dates are configured.
    """


# ---------------------------------------------------------------------------
# Pure math: derive the 4 fields from a list of kickoffs.
# ---------------------------------------------------------------------------


def compute_window(
    kickoff_per_partido: list[datetime | None],
) -> WindowFields:
    """Apply the hard rules to a list of kickoff datetimes.

    Accepts a list of any length; entries that are ``None`` are skipped
    for the min/max reduction. If the list is empty (or every entry is
    ``None``), all four fields are ``None``.

    Naive datetimes raise ``ValueError``. The underlying DB columns are
    ``DateTime(timezone=True)``; the manager does not auto-coerce naive
    values to UTC because the timezone is editorial context that
    callers must own.

    Pure function: no DB, no I/O. Both :class:`JornadaManager` methods
    call it; unit tests cover it directly.
    """
    non_null = [k for k in kickoff_per_partido if k is not None]
    for k in non_null:
        if k.tzinfo is None:
            raise ValueError(
                "kickoff_per_partido entries must be timezone-aware",
            )

    if not non_null:
        return WindowFields(
            kickoff_first_match_at=None,
            kickoff_last_match_at=None,
            voting_closes_at=None,
            escrutinio_expected_at=None,
        )

    first = min(non_null)
    last = max(non_null)
    return WindowFields(
        kickoff_first_match_at=first,
        kickoff_last_match_at=last,
        voting_closes_at=last - VOTING_CLOSES_BEFORE_LAST_KICKOFF,
        escrutinio_expected_at=last + ESCRUTINIO_AFTER_LAST_KICKOFF,
    )


# ---------------------------------------------------------------------------
# Effective phase (pure, no DB).
# ---------------------------------------------------------------------------


EffectivePhase = Literal[
    "en_preparacion",
    "lista_abre_pronto",
    "in_window",
    "fuera_de_ventana",
    "escrutinio_pendiente",
    "escrutinio_disponible",
]
"""The six possible values returned by :func:`compute_effective_phase`.

See ``docs/producto/gestor-jornadas.md`` § 2.2 for the table that maps
``status`` + ``now`` to one of these strings.
"""


def compute_effective_phase(
    jornada: Jornada,
    now: datetime | None = None,
) -> EffectivePhase:
    """Compute the effective phase of ``jornada`` at the given ``now``.

    Pure function — no DB, no I/O. Returns one of :data:`EffectivePhase`:

    - ``"en_preparacion"``        : ``status == draft``
    - ``"lista_abre_pronto"``     : ``status == active`` AND ``now < sale_opens_at``
    - ``"in_window"``             : ``status == active`` AND
                                    ``sale_opens_at <= now < voting_closes_at``
    - ``"fuera_de_ventana"``      : ``status == active`` AND
                                    ``now >= voting_closes_at``
    - ``"escrutinio_disponible"`` : ``status == past`` (the closed-but-
                                    not-yet-scored sub-state is no
                                    longer a thing — see ORM enum's
                                    docstring)

    For an ``active`` jornada with one or both window dates missing
    (``sale_opens_at`` or ``voting_closes_at`` is ``None``), the function
    cannot establish the window and returns ``"fuera_de_ventana"``. The
    spec calls for this defensive behaviour; in practice ``active`` rows
    always carry both dates because the manager sets them on
    ``draft → active`` transition.

    The function can't see the ``jornada_escrutinio`` table, so for a
    ``past`` jornada it cannot distinguish "escrutinio not yet
    published" from "escrutinio persisted + scored". The design choice
    is to default to ``"escrutinio_disponible"`` here — admin already
    closed the jornada implies the result path is in motion, and the
    ``/jornadas/{id}`` response overrides this with the actual escrutinio
    presence via a separate DB read.

    ``now`` defaults to ``datetime.now(tz=timezone.utc)``. Naive ``now``
    values are treated as UTC (the underlying DB columns are
    ``DateTime(timezone=True)``).
    """
    if now is None:
        now = datetime.now(tz=timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    sale = jornada.sale_opens_at
    closes = jornada.voting_closes_at
    status = jornada.status

    if status == JornadaStatus.draft:
        return "en_preparacion"

    if status == JornadaStatus.active:
        # Without both dates we can't tell if voting is open — treat as
        # outside the window (safer than reporting "in_window" by mistake).
        if sale is None or closes is None:
            return "fuera_de_ventana"
        if now < sale:
            return "lista_abre_pronto"
        if now < closes:
            return "in_window"
        return "fuera_de_ventana"

    if status == JornadaStatus.past:
        # Caller knows the escrutinio state from the jornada_escrutinio
        # table directly. This function can't query the DB; default to
        # "escrutinio_disponible" for past (admin already closed implies
        # escrutinio is in motion or persisted). The /jornadas/{id}
        # response overrides this with the DB query result.
        return "escrutinio_disponible"

    raise ValueError(f"unknown jornada status: {status!r}")


# ---------------------------------------------------------------------------
# Stable error code for the 409 body.
# ---------------------------------------------------------------------------


def _phase_error_code(phase: str, action: str) -> str:
    """Map a rejected ``(phase, action)`` pair to a stable error code string.

    The code is part of the public API contract (clients pattern-match on
    it). Keep it stable; never change the strings without a deprecation
    cycle.
    """
    if phase == "en_preparacion":
        return "jornada_in_preparation"
    if phase == "lista_abre_pronto":
        return "sale_not_yet_opened"
    if phase == "fuera_de_ventana":
        return "voting_window_closed"
    if phase == "escrutinio_pendiente":
        return "escrutinio_pending"
    if phase == "escrutinio_disponible":
        return "escrutinio_disponible"
    return "phase_not_allowed"


# ---------------------------------------------------------------------------
# Manager.
# ---------------------------------------------------------------------------


class JornadaManager:
    """Service that manages the dates and voting window of a Jornada.

    Stateless — instantiate freely. No HTTP, no auth, no external
    dependencies beyond SQLAlchemy async. Both public methods commit
    before returning; callers should NOT wrap them in a larger
    transaction (the manager owns its own atomic unit of work).

    Lifecycle: ``draft → active`` is owned by :meth:`configure_dates`
    (only). :meth:`JornadaManager.recompute_window` never transitions
    status — status changes are an editorial event, not a derived one.
    """

    # ---------- public API ----------

    async def configure_dates(
        self,
        session: AsyncSession,
        jornada_id: int,
        *,
        kickoff_per_partido: list[datetime],
        sale_opens_at: datetime,
    ) -> Jornada:
        """Persist the 15 kickoffs + ``sale_opens_at``; recompute the window.

        Steps:

        1. Validate ``len(kickoff_per_partido) == 15`` (else
           ``ValueError``). All kickoffs and ``sale_opens_at`` must be
           tz-aware (else ``ValueError``).
        2. Load the jornada (else :class:`JornadaNotFoundError`).
        3. Load the 15 :class:`Composicion` rows ordered by
           ``posicion`` (else :class:`IncompleteJornadaError`).
           Update ``kickoff_at`` on each.
        4. Set ``sale_opens_at`` on the jornada.
        5. Apply :func:`compute_window` to the (just-updated) kickoffs
           and persist the four derived fields on the jornada.
        6. If status is ``draft`` and the window is non-null (i.e. at
           least one kickoff was set), transition to ``active``.
        7. Commit; refresh; return the jornada.

        :raises ValueError: wrong kickoff count, or any naive datetime.
        :raises JornadaNotFoundError: jornada id not in DB.
        :raises IncompleteJornadaError: some composicion rows missing.
        """
        if len(kickoff_per_partido) != PARTIDOS_PER_JORNADA:
            raise ValueError(
                f"kickoff_per_partido must have {PARTIDOS_PER_JORNADA} "
                f"entries, got {len(kickoff_per_partido)}",
            )
        for i, k in enumerate(kickoff_per_partido, start=1):
            if k.tzinfo is None:
                raise ValueError(
                    f"kickoff for partido {i} must be timezone-aware",
                )
        if sale_opens_at.tzinfo is None:
            raise ValueError("sale_opens_at must be timezone-aware")

        j = await self._load_jornada_or_raise(session, jornada_id)
        compos_rows = await self._load_composicion_rows(session, jornada_id)

        # Step 3: set kickoff_at on each composicion (ordered by posicion).
        for compos, kickoff in zip(
            compos_rows, kickoff_per_partido, strict=True,
        ):
            compos.kickoff_at = kickoff

        # Step 4: editorial decision (only place we set this field).
        j.sale_opens_at = sale_opens_at

        # Step 5: derive + persist the 4 window fields.
        window = compute_window(list(kickoff_per_partido))
        j.kickoff_first_match_at = window["kickoff_first_match_at"]
        j.kickoff_last_match_at = window["kickoff_last_match_at"]
        j.voting_closes_at = window["voting_closes_at"]
        j.escrutinio_expected_at = window["escrutinio_expected_at"]

        # Step 6: draft → active. configure_dates always sets
        # sale_opens_at AND 15 kickoffs (validation enforces it), so
        # the only real condition is "voting_closes_at is non-null"
        # (which requires at least one non-null kickoff).
        if (
            j.status == JornadaStatus.draft
            and j.voting_closes_at is not None
        ):
            j.status = JornadaStatus.active

        await session.commit()
        await session.refresh(j)
        LOG.info(
            "configured dates for jornada %s (status=%s)",
            jornada_id, j.status.value,
        )
        return j

    async def recompute_window(
        self,
        session: AsyncSession,
        jornada_id: int,
    ) -> Jornada:
        """Recompute the four derived fields from existing compos rows.

        Reads ``composicion.kickoff_at`` rows for the jornada and
        re-applies :func:`compute_window`. Persists the result on the
        jornada. Does NOT touch ``sale_opens_at`` (editorial decision;
        only :meth:`configure_dates` accepts it). Does NOT transition
        status (status transitions are owned by ``configure_dates``).

        :raises JornadaNotFoundError: jornada id not in DB.
        :raises IncompleteJornadaError: some composicion rows missing.
        """
        j = await self._load_jornada_or_raise(session, jornada_id)
        compos_rows = await self._load_composicion_rows(session, jornada_id)

        kickoff_list = [c.kickoff_at for c in compos_rows]
        window = compute_window(kickoff_list)

        j.kickoff_first_match_at = window["kickoff_first_match_at"]
        j.kickoff_last_match_at = window["kickoff_last_match_at"]
        j.voting_closes_at = window["voting_closes_at"]
        j.escrutinio_expected_at = window["escrutinio_expected_at"]
        # sale_opens_at is intentionally NOT touched.

        await session.commit()
        await session.refresh(j)
        LOG.info(
            "recomputed window for jornada %s (status=%s)",
            jornada_id, j.status.value,
        )
        return j

    async def assert_jornada_accepts(
        self,
        session: AsyncSession,
        jornada: Jornada,
        *,
        action: Literal["bets", "edits", "final_confirmation"],
        now: datetime | None = None,
    ) -> None:
        """Reject the mutation if the jornada is not in the right window.

        The 4 mutation endpoints under ``/groups/{gid}/porras`` use this
        helper as the single source of truth for "is the porra in a state
        where this write makes sense?". The mapping from
        ``action`` → allowed phases is:

        - ``"bets"`` (create_porra, submit_picks): only ``"in_window"``.
        - ``"edits"`` (patch_porra): ``"in_window"`` or
          ``"lista_abre_pronto"`` (admin can patch format before sale
          opens and during voting; blocked once the window is past).
        - ``"final_confirmation"`` (put_boleto): ``"in_window"``,
          ``"lista_abre_pronto"``, ``"fuera_de_ventana"`` or
          ``"escrutinio_pendiente"`` (admin can save the FINAL boleto
          once the jornada is configured: pre-loading the jugada before
          sale opens, while voting is open, in the post-close window
          before escrutinio is published, or after close but before
          escrutinio is published). Blocked only in
          ``"en_preparacion"`` (no partidos ingested yet → no votes to
          confirm) and ``"escrutinio_disponible"`` (game is over).

        On reject, raises ``HTTPException(409)`` whose ``detail`` is a
        JSON object with five fields (the client can pattern-match on
        ``code``):

        .. code-block:: json

            {
              "code": "voting_window_closed",
              "sale_opens_at": "2026-10-08T19:00:00+00:00",
              "voting_closes_at": "2026-10-11T14:00:00+00:00",
              "now": "2026-10-11T15:30:00+00:00",
              "effective_phase": "escrutinio_pendiente"
            }

        ``sale_opens_at`` and ``voting_closes_at`` are ``null`` when the
        jornada does not have them set.

        The ``session`` argument is part of the signature for symmetry
        with the other :class:`JornadaManager` methods (the caller owns
        the :class:`AsyncSession`); the check itself reads only the
        in-memory ``jornada``.

        :param session: Caller-owned :class:`AsyncSession` (unused, kept
            for API consistency with the other manager methods).
        :param jornada: The jornada to validate.
        :param action: Which mutation is being attempted.
        :param now: Override for the current time (defaults to UTC now;
            tests pin this to control clock).

        Spec: ``docs/producto/gestor-jornadas.md`` § 4.1, § 4.3.
        """
        if now is None:
            now = datetime.now(tz=timezone.utc)
        elif now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        phase = compute_effective_phase(jornada, now=now)

        allowed: frozenset[str]
        if action == "bets":
            allowed = frozenset({"in_window"})
        elif action == "edits":
            allowed = frozenset({"in_window", "lista_abre_pronto"})
        elif action == "final_confirmation":
            allowed = frozenset({
                "lista_abre_pronto",
                "in_window",
                "fuera_de_ventana",
                "escrutinio_pendiente",
            })
        else:
            raise ValueError(f"unknown action: {action!r}")

        if phase in allowed:
            return

        detail = {
            "code": _phase_error_code(phase, action),
            "sale_opens_at": (
                jornada.sale_opens_at.isoformat()
                if jornada.sale_opens_at is not None else None
            ),
            "voting_closes_at": (
                jornada.voting_closes_at.isoformat()
                if jornada.voting_closes_at is not None else None
            ),
            "now": now.isoformat(),
            "effective_phase": phase,
        }
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=detail,
        )

    # ---------- internals ----------

    async def _load_jornada_or_raise(
        self,
        session: AsyncSession,
        jornada_id: int,
    ) -> Jornada:
        """Load one jornada by id; raise ``JornadaNotFoundError`` if missing."""
        j = await session.get(Jornada, jornada_id)
        if j is None:
            raise JornadaNotFoundError(f"jornada {jornada_id} not found")
        return j

    async def _load_composicion_rows(
        self,
        session: AsyncSession,
        jornada_id: int,
    ) -> list[Composicion]:
        """Load the 15 compos rows ordered by ``posicion``.

        Raises ``IncompleteJornadaError`` if any posicion in 1..15 is
        missing. The list returned is in posicion order, which is what
        the callers (configure_dates, recompute_window) rely on.
        """
        rows = (
            await session.execute(
                select(Composicion)
                .where(Composicion.jornada_id == jornada_id)
                .order_by(Composicion.posicion)
            )
        ).scalars().all()

        if len(rows) != PARTIDOS_PER_JORNADA:
            seen = {r.posicion for r in rows}
            missing = sorted(
                set(range(1, PARTIDOS_PER_JORNADA + 1)) - seen,
            )
            raise IncompleteJornadaError(
                f"jornada {jornada_id} is missing composicion rows for "
                f"positions {missing}; cannot manage Jornada window",
            )
        return list(rows)


__all__ = [
    "ESCRUTINIO_AFTER_LAST_KICKOFF",
    "EffectivePhase",
    "IncompleteJornadaError",
    "JornadaManager",
    "JornadaNotFoundError",
    "PARTIDOS_PER_JORNADA",
    "VOTING_CLOSES_BEFORE_LAST_KICKOFF",
    "WindowFields",
    "compute_effective_phase",
    "compute_window",
]