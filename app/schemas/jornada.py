"""Pydantic DTOs for jornada / composicion / escrutinio / comentario.

These are the read-side DTOs returned by the ``/jornadas`` endpoints. The
``JornadaStatus`` here mirrors the SQLAlchemy enum but is independent — keep
them in sync if you add new states.
"""
from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from types import SimpleNamespace

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.db.models import JornadaStatus as DbJornadaStatus
from app.schemas.losilla import PartidoEstimacion
from app.services.jornada_manager import (
    EffectivePhase,
    PARTIDOS_PER_JORNADA,
    compute_effective_phase,
)


class JornadaStatus(str, Enum):
    """Lifecycle state of a Quiniela matchweek (mirrors
    :class:`app.db.models.JornadaStatus`, post superadmin-panel sprint).

    Reduced from 4 states to 3 — see the ORM enum's docstring for the
    rationale. The public API only ever returns one of these three
    strings; the ``closed``/``completed`` distinction is gone.
    """
    draft = "draft"
    active = "active"
    past = "past"


class TeamSummary(BaseModel):
    """Minimal team data embedded in partido responses.

    Subset of ``TeamOut`` (no ``competition_id``) because the picks form
    only needs the display fields (name + logo). If the team row was
    removed (e.g. FK target deleted between bootstrap and read), the
    partido's ``local_team`` / ``visitante_team`` is ``None`` — never
    raises.
    """
    model_config = ConfigDict(from_attributes=True)

    id: int
    entity_id: str
    canonical_name: str
    short_name: str | None = None
    gender: str | None = None
    # Slice 8 of gestor-jornadas.md (§7.2 + §5.2): ``gender_known=True`` for
    # seeded catalog rows and any stub created from a feminine/masculine
    # suffix; ``False`` for stubs the matcher had to guess as 'men' (slice 5
    # of the plan). The column was added in the slice 1 migration with a
    # ``server_default=true`` so all pre-existing rows read as known here;
    # the schema default of ``True`` is a belt-and-braces fallback for any
    # path that constructs ``TeamSummary`` without an ORM row.
    gender_known: bool = True
    country: str | None = None
    logo_url: str | None = None


class ComposicionOut(BaseModel):
    """One partido in the boleto, with team IDs AND embedded team summaries.

    ``local_team`` / ``visitante_team`` are the embedded ``TeamSummary``
    so the client can render the 15-partido boleto without making 30
    follow-up calls to ``/api/v1/teams/{id}``. They are ``None`` only
    when the referenced team row was deleted; ``local_team_id`` /
    ``visitante_team_id`` are still populated so the client can show
    *something* (the raw name, the id, a placeholder).
    """
    model_config = ConfigDict(from_attributes=True)

    posicion: int = Field(..., ge=1, le=15)
    local_team_id: int
    visitante_team_id: int
    local_raw: str
    visitante_raw: str
    kickoff_at: datetime | None = None
    competicion_raw: str | None = None
    # Embedded summaries (None if the team row was deleted).
    local_team: TeamSummary | None = None
    visitante_team: TeamSummary | None = None


class JornadaOut(BaseModel):
    """Jornada summary (list + detail).

    Slice 8 of ``docs/producto/gestor-jornadas.md`` (§7) adds four fields
    on top of the legacy T8 list shape:

    - ``kickoff_last_match_at`` and ``escrutinio_expected_at``: pass-through
      from the DB columns added in the slice 1 migration. Both nullable —
      a draft jornada carries ``None`` until the manager writes the window.
    - ``effective_phase``: derived via
      :func:`app.services.jornada_manager.compute_effective_phase`. Set by
      the ``_compute_effective_phase`` after-validator using the same
      ``now`` exposed on the response (so a client can detect clock skew).
      The default is overwritten in the validator; the literal value is
      required to keep the schema honest when callers bypass it.
    - ``now``: the UTC datetime the server used to compute
      ``effective_phase``. Default ``datetime.now(UTC)``; emitted on the
      list endpoint too (a debugging helper for clock skew).
    """
    model_config = ConfigDict(from_attributes=True)

    id: int
    temporada: int
    temporada_str: str
    numero: int = Field(..., ge=1)
    status: JornadaStatus
    sale_opens_at: datetime | None = None
    voting_closes_at: datetime | None = None
    kickoff_first_match_at: datetime | None = None
    # Slice 8 — pass-throughs from the slice 1 migration.
    kickoff_last_match_at: datetime | None = None
    escrutinio_expected_at: datetime | None = None
    fetched_at: datetime
    # Slice 8 — derived fields. ``effective_phase`` is filled by the
    # after-validator; the literal default is overwritten there.
    effective_phase: EffectivePhase = "en_preparacion"
    now: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description=(
            "UTC datetime used to compute ``effective_phase``. Lets the "
            "client detect clock skew between the server and itself."
        ),
    )

    @model_validator(mode="after")
    def _compute_effective_phase(self) -> "JornadaOut":
        # Compute ``effective_phase`` using the same ``now`` we expose on
        # the response. ``compute_effective_phase`` expects the ORM-level
        # ``JornadaStatus`` (different class from this schema's enum), so
        # we re-wrap the three attributes it reads on a ``SimpleNamespace``
        # rather than coupling the schema's enum to ``app.db.models``.
        fake = SimpleNamespace(
            status=DbJornadaStatus(self.status.value),
            sale_opens_at=self.sale_opens_at,
            voting_closes_at=self.voting_closes_at,
        )
        self.effective_phase = compute_effective_phase(fake, now=self.now)
        return self


class JornadaDetailOut(JornadaOut):
    """Jornada + 15-partidos detail.

    Slice 8 additionally exposes ``estimaciones`` (a list of
    :class:`app.schemas.losilla.PartidoEstimacion`) embedded from the
    jornada's ``jornada_escrutinio.payload.estimaciones`` JSONB. Empty list
    when no escrutinio row exists for the jornada — the detail endpoint
    must NOT 404 in that case, only return ``[]``.
    """
    partidos: list[ComposicionOut]
    estimaciones: list[PartidoEstimacion] = Field(
        default_factory=list,
        description=(
            "Live 1X2 + Pleno al 15 percentages persisted in the jornada's "
            "escrutinio payload (slice 7 of gestor-jornadas.md §6.2). "
            "Empty list when no escrutinio row exists yet."
        ),
    )


class JornadaDatesIn(BaseModel):
    """Request body for ``POST /api/v1/admin/jornadas/{jornada_id}/dates``.

    Slice 3 of the gestor-jornadas plan (``docs/producto/gestor-jornadas.md``
    § 3.2). The endpoint passes these straight to
    :meth:`app.services.jornada_manager.JornadaManager.configure_dates`,
    which is the source of truth for validation (count, tz-awareness).
    Pydantic only enforces the field shape here — the manager raises
    ``ValueError`` on wrong count or naive datetimes and the endpoint maps
    those to 422.

    The list length is enforced here as a fast path so a length error surfaces
    as the same 422 shape Pydantic emits for malformed bodies (rather than
    going through the manager's ``ValueError``).
    """
    kickoff_per_partido: list[datetime] = Field(
        ...,
        min_length=PARTIDOS_PER_JORNADA,
        max_length=PARTIDOS_PER_JORNADA,
        description=(
            f"{PARTIDOS_PER_JORNADA} tz-aware datetimes, one per partido "
            f"(ordered by posicion 1..{PARTIDOS_PER_JORNADA})."
        ),
    )
    sale_opens_at: datetime = Field(
        ..., description="Editorial moment when voting opens (must be tz-aware).",
    )