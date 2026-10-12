"""SQLAlchemy ORM models.

Tables defined here:

- ``competition``           — 9 entries (laliga-ea-sports, liga-f, uefa-champions-league, ...) plus the 'unknown' stub
- ``source``                — the data sources we ingest from (Quiniwin, Losilla, API-Football, ...)
- ``team_identity_map``     — canonical team identity (212 clubs + 211 national teams)
- ``team_id_api_football``  — the API-Football numeric ID per canonical team
- ``user`` / ``group`` / ``group_member`` — auth + peña (T3)
- ``jornada``               — Quiniela matchweek lifecycle (T8)
- ``composicion``           — the 15 partidos in a jornada's boleto (T8)
- ``jornada_escrutinio``    — Losilla escrutinio oficial persisted row (T8)
- ``jornada_comentario``    — Losilla editorial comments per partido (T8)
- ``fetch_session``         — audit trail for scheduler batches (T8)
- ``pick``                  — per-member per-jornada picks (MVP-02 voting)

Notes on the schema vs. ``docs/proyecto/modelo-datos.md``:

- The plano proposed ``team_identity_map.competition`` as a Postgres ENUM
  ``('sp1', 'sp2', 'liga_f', 'uefa')``. That ENUM (a) only covered 4 values
  while the data has 9 competitions, and (b) cannot represent the hyphenated
  competition IDs (``laliga-ea-sports``) the JSON uses. We replace it with a
  small ``competition`` table referenced by FK. The plano's ENUM was a v1
  shortcut; this is its data-aware evolution.
- The plano's ``team_identity_map`` was modelled as an alias map (one row
  per ``raw_name`` per source). The JSON ``entities`` block is a canonical
  roster (one row per real-world team). We model the roster here; the alias
  map can be added later without touching this table.
- ``jornada.status`` uses ``native_enum=False`` so it is rendered as a
  ``VARCHAR(16)`` + ``CHECK`` rather than a Postgres ``CREATE TYPE``. Easier
  to autogenerate, easier to extend with new states.
- Dedup invariant for ``team_identity_map``: a functional unique index
  ``(competition_id, lower(unaccent(canonical_name)))`` is created in the
  Alembic migration (autogenerate does not manage functional indexes).
  This prevents two rows with the same normalized name inside the same
  competition (e.g., "Albacete" vs "albacete" vs "ALBACETE" all collide).
"""
from __future__ import annotations

import enum as _enum
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.db import Base  # noqa: E402  — DeclarativeBase re-exported by app.db


class Competition(Base):
    """Top-level competitions the app knows about.

    Loaded from ``data/team-catalog.json``'s ``competitions`` block (and the
    matching metadata in ``data/team-catalog.sources.json``). 9 entries in
    2026/27 (4 Spanish men, 1 women, 1 Spanish women's 2nd tier, 3 UEFA
    club, 1 FIFA associations).
    """
    __tablename__ = "competition"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    country: Mapped[str | None] = mapped_column(String(8), nullable=True)
    gender: Mapped[str | None] = mapped_column(String(8), nullable=True)
    tier: Mapped[int | None] = mapped_column(Integer, nullable=True)
    season: Mapped[str | None] = mapped_column(String(16), nullable=True)
    official_source_url: Mapped[str | None] = mapped_column(Text, nullable=True)


class Source(Base):
    """External data sources we ingest from.

    Distinct from :class:`Competition` — sources are upstream providers
    (Quiniwin XML, Losilla XML, API-Football, The Odds API, ...). One source
    can serve many competitions.
    """
    __tablename__ = "source"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # "api" | "scrape" | "xml" | ...
    base_url: Mapped[str | None] = mapped_column(String(255), nullable=True)


class TeamIdentityMap(Base):
    """Canonical team identity (club or national team).

    Each row is one entity in the world of football (e.g., "FC Barcelona",
    "Spain men's national team", "Real Madrid women's team"). 212 clubs +
    211 national teams per ``data/team-catalog.json``'s ``entities`` block.

    ``competition_id`` points to the team's *primary* domestic competition.
    A team participating in multiple competitions (e.g., Real Madrid in
    LaLiga + Champions) lives here once, with ``competition_id`` set to
    their domestic league; additional UEFA appearances will be captured in
    a future ``team_competition`` junction table.
    """
    __tablename__ = "team_identity_map"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entity_id: Mapped[str] = mapped_column(String(80), nullable=False)
    competition_id: Mapped[str] = mapped_column(
        ForeignKey("competition.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    canonical_name: Mapped[str] = mapped_column(String(120), nullable=False)
    short_name: Mapped[str | None] = mapped_column(String(40), nullable=True)
    gender: Mapped[str | None] = mapped_column(String(8), nullable=True)
    gender_known: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true"),
    )
    country: Mapped[str | None] = mapped_column(String(8), nullable=True)
    logo_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    # Soft-delete sentinel for the superadmin-panel sprint (slice 1 +
    # slice 2). Nullable: NULL means "live". Indexed for the public
    # read endpoints that filter ``deleted_at IS NULL``.
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    competition = relationship("Competition", lazy="joined")

    __table_args__ = (
        UniqueConstraint("entity_id", name="uq_team_identity_entity_id"),
    )


class TeamIdApiFootball(Base):
    """API-Football numeric IDs for canonical teams.

    Multiple rows per team are possible (different ``season_seen`` rows) —
    the API sometimes reuses or rotates IDs between seasons. We keep all
    observed IDs to preserve history.
    """
    __tablename__ = "team_id_api_football"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("source.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    team_identity_id: Mapped[int] = mapped_column(
        ForeignKey("team_identity_map.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    api_football_id: Mapped[int] = mapped_column(Integer, nullable=False)
    season_seen: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )

    team_identity = relationship("TeamIdentityMap")
    source = relationship("Source")

    __table_args__ = (
        UniqueConstraint(
            "team_identity_id", "season_seen", name="uq_team_id_af_team_season",
        ),
    )


class User(Base):
    """A peña user account. Can authenticate via local password, Google, or X.

    A user may have multiple auth_providers linked. The account is keyed by
    email (unique). On first sign-in with a new provider, we look up by email
    and either link (if email matches an existing user) or create a new user.
    """
    __tablename__ = "user"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(120), unique=True, nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(60), nullable=False)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    google_id: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True, index=True)
    x_id: Mapped[str | None] = mapped_column(String(32), unique=True, nullable=True, index=True)
    avatar_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(default=True, server_default="true", nullable=False)
    # Slice 1 of the superadmin-panel sprint: a user-level flag that
    # unlocks the admin-only endpoints (slice 2). The owner is
    # bootstrapped to True in the migration; subsequent grants go
    # through the proper endpoint once it ships.
    is_superadmin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )

    groups = relationship("GroupMember", back_populates="user", cascade="all, delete-orphan")
    created_groups = relationship("Group", back_populates="creator", foreign_keys="Group.created_by_user_id")

    __table_args__ = (
        CheckConstraint(
            "password_hash IS NOT NULL OR google_id IS NOT NULL OR x_id IS NOT NULL",
            name="ck_user_has_at_least_one_provider",
        ),
    )


class Group(Base):
    """A peña (group of friends playing La Quiniela together).

    Created by one user (creator) who becomes its first admin. Other users join
    via 6-char alphanumeric join_code (which the creator shares out-of-band).

    ``show_picks`` is an admin toggle (MVP-02 voting): when ``False``,
    members can see *who* has voted but not *what* they voted. When
    ``True``, picks become visible to all members of the group.
    """
    __tablename__ = "group"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_user_id: Mapped[int] = mapped_column(
        ForeignKey("user.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    join_code: Mapped[str] = mapped_column(String(8), unique=True, nullable=False, index=True)
    max_members: Mapped[int] = mapped_column(Integer, default=50, server_default="50", nullable=False)
    show_picks: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )

    creator = relationship("User", back_populates="created_groups", foreign_keys=[created_by_user_id])
    members = relationship("GroupMember", back_populates="group", cascade="all, delete-orphan")


class GroupMember(Base):
    """A user's membership in a group.

    Role is `admin` (creator or promoted) or `member` (joined via code).
    """
    __tablename__ = "group_member"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("group.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    role: Mapped[str] = mapped_column(
        String(16), default="member", server_default="member", nullable=False,
    )
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )

    user = relationship("User", back_populates="groups")
    group = relationship("Group", back_populates="members")

    __table_args__ = (
        UniqueConstraint("group_id", "user_id", name="uq_group_member_group_user"),
        CheckConstraint(
            "role IN ('admin','member')", name="ck_group_member_role_valid",
        ),
    )


# --- Jornada / composicion / escrutinio / comentario / fetch_session (T8) ---


class JornadaStatus(str, _enum.Enum):
    """Lifecycle of a Quiniela matchweek (post superadmin-panel sprint).

    Reduced from 4 states (``draft``/``active``/``closed``/``completed``)
    to 3 — the old distinction between "escrutinio published but not yet
    scored" (``closed``) and "fully scored" (``completed``) was editorial
    and added complexity without a clear product use case. The new
    single terminal state ``past`` means "voting is over; the jornada is
    history" — the public API derives the escrutinio sub-state from the
    presence of a ``jornada_escrutinio`` row at response time.

    - ``draft``  — Quiniwin publishes the boleto, but no enriched data yet
    - ``active`` — kickoff_at confirmed, voting open
    - ``past``   — admin closed it; it's history
    """
    draft = "draft"
    active = "active"
    past = "past"


class Jornada(Base):
    """A Quiniela matchweek (1..~42 per season).

    The 15-partido boleto for one jornada. Quiniwin publishes this when the
    week's matches are scheduled. Multiple quinielas can exist per week
    (rare, midweek + weekend) — each one is its own row.

    The per-jornada quiniela-mode config (MVP-04) used to live here as
    ``quini_config`` (JSONB). It was replaced in MVP-05 by the
    per-``(group, jornada)`` :class:`Porra` entity — picks and format are
    now scoped to the peña, not to the jornada. See ``app/api/v1/porras.py``.

    ``default_columnas`` (oct 2026, multi-column sprint) is the admin-set
    default for porras created in ``sencilla`` / ``intuicion`` mode against
    this jornada: the admin configures the matchweek once ("this is a
    4-column jornada") and any peña's porra that doesn't override
    ``columnas`` at create time picks up the default. Nullable so
    ``multiple`` / ``reducida`` porras (which don't use ``columnas``)
    and pre-existing jornadas are unaffected. Range enforced by
    ``ck_jornada_default_columnas_range`` (2..12).
    """
    __tablename__ = "jornada"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    temporada: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    temporada_str: Mapped[str] = mapped_column(String(16), nullable=False)
    numero: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    status: Mapped[JornadaStatus] = mapped_column(
        Enum(JornadaStatus, name="jornada_status", native_enum=False, length=16),
        default=JornadaStatus.draft,
        server_default=JornadaStatus.draft.value,
        nullable=False,
    )
    sale_opens_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    voting_closes_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    kickoff_first_match_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    kickoff_last_match_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    escrutinio_expected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    # Soft-delete sentinel for the superadmin-panel sprint (slice 1 +
    # slice 2). Nullable: NULL means "live". Indexed for the public
    # read endpoints that filter ``deleted_at IS NULL``.
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    # Multi-column default (oct 2026). Used by ``create_porra`` when the
    # admin doesn't pass ``columnas`` and the mode is ``sencilla`` or
    # ``intuicion``. Range 2..12 (the same window as ``porra.columnas``).
    default_columnas: Mapped[int | None] = mapped_column(
        SmallInteger, nullable=True,
    )

    composicion_rows = relationship(
        "Composicion", back_populates="jornada", cascade="all, delete-orphan",
    )
    escrutinio_row = relationship(
        "JornadaEscrutinio", back_populates="jornada",
        uselist=False, cascade="all, delete-orphan",
    )
    comentarios_rows = relationship(
        "JornadaComentario", back_populates="jornada", cascade="all, delete-orphan",
    )

    __table_args__ = (
        UniqueConstraint("temporada", "numero", name="uq_jornada_temporada_numero"),
        Index("idx_jornada_status_kickoff", "status", "kickoff_first_match_at"),
        CheckConstraint(
            "default_columnas IS NULL OR "
            "(default_columnas >= 2 AND default_columnas <= 12)",
            name="ck_jornada_default_columnas_range",
        ),
    )


class Composicion(Base):
    """One of the 15 partidos in a jornada's boleto.

    ``local_team_id`` and ``visitante_team_id`` point to ``team_identity_map``
    (which may be an auto-created stub if Quiniwin emitted a team we do not
    have yet). Raw names are kept in ``local_raw`` / ``visitante_raw`` for
    audit / reconciliation.
    """
    __tablename__ = "composicion"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    jornada_id: Mapped[int] = mapped_column(
        ForeignKey("jornada.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    posicion: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    local_team_id: Mapped[int] = mapped_column(
        ForeignKey("team_identity_map.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    visitante_team_id: Mapped[int] = mapped_column(
        ForeignKey("team_identity_map.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    local_raw: Mapped[str] = mapped_column(String(120), nullable=False)
    visitante_raw: Mapped[str] = mapped_column(String(120), nullable=False)
    kickoff_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    competicion_raw: Mapped[str | None] = mapped_column(String(80), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )

    jornada = relationship("Jornada", back_populates="composicion_rows")
    local_team = relationship("TeamIdentityMap", foreign_keys=[local_team_id])
    visitante_team = relationship("TeamIdentityMap", foreign_keys=[visitante_team_id])

    __table_args__ = (
        UniqueConstraint(
            "jornada_id", "posicion", name="uq_composicion_jornada_posicion",
        ),
        CheckConstraint(
            "posicion >= 1 AND posicion <= 15",
            name="ck_composicion_posicion_range",
        ),
    )


class JornadaEscrutinio(Base):
    """Losilla escrutinio oficial, persisted when the jornada closes.

    One row per jornada. ``payload`` JSONB contains the full
    ``EscrutinioOficial`` Pydantic model (acertantes, premios, bote, etc.).
    Source = ``source.code='losilla'``.
    """
    __tablename__ = "jornada_escrutinio"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    jornada_id: Mapped[int] = mapped_column(
        ForeignKey("jornada.id", ondelete="CASCADE"), unique=True, nullable=False,
    )
    source_id: Mapped[int] = mapped_column(
        ForeignKey("source.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    fetch_session_id: Mapped[Any] = mapped_column(UUID(as_uuid=True), nullable=True)
    payload: Mapped[Any] = mapped_column(JSONB, nullable=False)

    jornada = relationship("Jornada", back_populates="escrutinio_row")
    source = relationship("Source")


class JornadaComentario(Base):
    """Losilla editorial comments, one row per (jornada, partido_orden).

    Partido 15 (Pleno al 15) does NOT carry an editorial comment (per Losilla's
    layout). Expect 14 rows per jornada in normal conditions.
    """
    __tablename__ = "jornada_comentario"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    jornada_id: Mapped[int] = mapped_column(
        ForeignKey("jornada.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    source_id: Mapped[int] = mapped_column(
        ForeignKey("source.id", ondelete="RESTRICT"), nullable=False,
    )
    partido_orden: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    local_raw: Mapped[str] = mapped_column(String(120), nullable=False)
    visitante_raw: Mapped[str] = mapped_column(String(120), nullable=False)
    comentario: Mapped[str] = mapped_column(Text, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    fetch_session_id: Mapped[Any] = mapped_column(UUID(as_uuid=True), nullable=True)

    jornada = relationship("Jornada", back_populates="comentarios_rows")
    source = relationship("Source")

    __table_args__ = (
        UniqueConstraint(
            "jornada_id", "partido_orden",
            name="uq_jornada_comentario_jornada_orden",
        ),
    )


class FetchSession(Base):
    """One row per scheduler batch — full audit trail.

    ``source_stats`` JSONB with per-source results:
    ``{"losilla": {"status": "ok", "rows_inserted": 15}, ...}``.
    """
    __tablename__ = "fetch_session"

    id: Mapped[Any] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid(),
    )
    source_id: Mapped[int | None] = mapped_column(
        ForeignKey("source.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    source_stats: Mapped[Any] = mapped_column(JSONB, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    source = relationship("Source")


# --- pick (MVP-05 voting — porra-scoped, free-form votes + {local, visitante} pleno) ---


class Pick(Base):
    """One member's pick for one partido in one column of a porra.

    The picks table is scoped to a per-``(group, jornada)`` porra via
    :class:`Porra`; the ``porra_id`` column is the authoritative scope.

    **Multi-column shape (oct 2026)**: a member submits N columns per
    porra (``N = porra.columnas`` for ``sencilla`` / ``intuicion``,
    ``N = 1`` for ``multiple`` / ``reducida``). The pick table now holds
    ``N`` rows per ``(porra, user, partido)`` tuple — one per column,
    keyed by ``columna_idx`` (1..N). For partido 15 the Pleno is
    **shared** across all columns of the same submission: there are
    still N rows for partido 15 (one per ``columna_idx``), each carrying
    the same ``{local, visitante}`` values.

    For partidos 1-14: ``signs`` is a JSONB list of 1, 2, or 3 unique
    elements from ``{"1","X","2"}``. Length 1 = simple, 2 = doble, 3 =
    triple. **Length is FREE** — it does not depend on the porra's
    ``dobles`` / ``triples`` format (the format only governs how many
    columns the FINAL boleto plays; member votes are unconstrained).
    For partido 15 (pleno al 15): ``pleno_local`` and ``pleno_visitante``
    each hold a single value in ``{"0","1","2","M"}`` (M = "more than
    3 goals"). ``signs`` is null in this case.
    Exactly one of the two shapes is set per row (enforced in Pydantic +
    a CHECK constraint that splits by ``partido_orden``).

    BREAKING CHANGE (MVP-05, was MVP-04): the ``pleno_signs`` JSONB dict
    (M0/M1/M2 columns) was replaced by the simpler ``{local, visitante}``
    pair that mirrors the Quiniela real layout. Old M0/M1/M2 picks are
    NOT migrated — the porra table is rebuilt fresh (this is dev, no
    production migration concern).

    Idempotency: UNIQUE on ``(porra_id, user_id, partido_orden,
    columna_idx)`` plus a single DELETE+INSERT UPSERT in the API layer
    (one transaction). The legacy MVP-02 unique key on
    ``(group_id, user_id, jornada_id, partido_orden)`` was dropped in the
    multi-column migration — it would block the new multi-row shape.

    Cascade policy:
    - ``porra_id`` → CASCADE (deleting a porra drops its picks).
    - ``user_id`` → RESTRICT (we should never silently delete a peña
      member who has picks; remove their memberships / picks first).
    - ``group_id`` and ``jornada_id`` are denormalised from ``porra``
      but kept for index efficiency + FK-cascade alignment (also
      CASCADE, inherited from the porra FK). Uniqueness is enforced at
      the porra scope, not here.
    """
    __tablename__ = "pick"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("group.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("user.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    jornada_id: Mapped[int] = mapped_column(
        ForeignKey("jornada.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    porra_id: Mapped[int] = mapped_column(
        ForeignKey("porra.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    partido_orden: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    columna_idx: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=1, server_default="1",
    )
    signs: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    pleno_local: Mapped[str | None] = mapped_column(String(2), nullable=True)
    pleno_visitante: Mapped[str | None] = mapped_column(String(2), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "porra_id", "user_id", "partido_orden", "columna_idx",
            name="uq_pick_porra_user_partido_col",
        ),
        CheckConstraint(
            "partido_orden >= 1 AND partido_orden <= 15",
            name="ck_pick_partido_orden_range",
        ),
        CheckConstraint(
            "(partido_orden != 15 AND signs IS NOT NULL "
            "AND pleno_local IS NULL AND pleno_visitante IS NULL) "
            "OR (partido_orden = 15 AND signs IS NULL "
            "AND pleno_local IS NOT NULL AND pleno_visitante IS NOT NULL)",
            name="ck_pick_signs_xor_pleno",
        ),
        CheckConstraint(
            "pleno_local IS NULL OR pleno_local IN ('0','1','2','M')",
            name="ck_pick_pleno_local_valid",
        ),
        CheckConstraint(
            "pleno_visitante IS NULL OR pleno_visitante IN ('0','1','2','M')",
            name="ck_pick_pleno_visitante_valid",
        ),
        Index("ix_pick_group_jornada", "group_id", "jornada_id"),
        Index("ix_pick_porra_user", "porra_id", "user_id"),
    )


# --- porra (MVP-05: per-(group, jornada) peña bet) ---


class PorraStatus(str, _enum.Enum):
    """Lifecycle of a per-peña porra.

    - ``open``   — admin has created the porra, members can vote; admin
      can edit format / budget via PATCH.
    - ``closed`` — admin marked it closed manually; the picks endpoint
      rejects new submissions with 403 (mirrors the ``voting_closes_at``
      hard deadline on ``Jornada``).
    """
    open = "open"
    closed = "closed"


class PorraModo(str, _enum.Enum):
    """How a porra's columns are generated (oct 2026 sprint).

    - ``multiple``  — 1 column with D doubles + T triples (legacy MVP-04/05).
    - ``sencilla``  — N independent columns of simples (TBD).
    - ``reducida``  — 1 column from one of the R1..R6 official reductions
      (TBD).
    - ``intuicion`` — N columns where ``fijos`` parties share the same sign
      and the rest vary (the personal mode). Slice 1 of the
      intuicion + PDF sprint.
    """
    multiple = "multiple"
    sencilla = "sencilla"
    reducida = "reducida"
    intuicion = "intuicion"


class Porra(Base):
    """One peña's per-jornada porra (bet). UNIQUE per (group_id, jornada_id).

    The MVP-05 evolution of the old per-jornada ``Jornada.quini_config``:
    the format (D doubles, T triples) and the budget now live HERE, scoped
    to one peña + one jornada. Each peña can have its own format and
    budget for the same jornada — they no longer share a global config.

    **Boleto lifecycle**: the admin's FINAL boleto (the one the peña
    actually plays) is stored on this row when ``PUT /boleto`` is called:

    - ``boleto_matches`` JSONB: ``[{"partido_orden": int, "signs": [str, str, str]}, ...]``
    - ``pleno_local`` + ``pleno_visitante`` VARCHAR(2): the pleno values.
    - ``boleto_set_at`` + ``boleto_set_by_user_id``: audit fields.

    The ``boleto_matches`` JSONB carries ONE row per partido 1-14 (15
    rows total, in order). Each row's ``signs`` is a list of 1, 2, or
    3 elements from ``{"1","X","2"}` — the per-match kind is implicit
    in the length (1=simple, 2=doble, 3=triple).

    FREE votes: the format (D, T) does NOT constrain how members vote.
    A member can mark any partido with 1, 2, or 3 signs regardless of
    the porra's format. The format only governs the FINAL boleto the
    admin saves after seeing the votes.

    ``precio_apuesta_eur`` comes from Losilla's persisted escrutinio
    (``jornada_escrutinio.payload.coste_apuesta_eur``) when available,
    otherwise defaults to 0.50€.

    **Boleto storage** (oct 2026 sprint, N-column phase): the FINAL
    boleto is stored in two JSONB columns:

    - ``boleto_columns`` — the canonical N-column shape:
      ``[{"matches": [{"partido_orden": int, "signs": [str, ...]}, ...]}, ...]``,
      one entry per column, 14 rows per column. ``None`` until the admin
      saves the boleto via PUT.
    - ``boleto_matches`` — the LEGACY single-column mirror (kept only for
      the GET / PDF paths that predate the N-column phase, and for the
      DB-level CHECK on the count). Mirrors the FIRST column of
      ``boleto_columns`` whenever a boleto is saved, so existing tests
      and the legacy single-column read still work without rewriting
      the renderer.

    **Boleto modes** (oct 2026 sprint): ``modo`` picks how the FINAL
    boleto is generated.

    - ``multiple``  — 1 column with D doubles + T triples (legacy MVP-05).
      ``dobles`` and ``triples`` are meaningful; ``fijos`` and ``columnas``
      are NULL. ``columns`` is computed as ``2^D × 3^T``.
    - ``sencilla``  — N independent columns of simples (every match
      in every column has exactly 1 sign). ``columnas`` carries N.
    - ``reducida``  — 1 column from one of the R1..R6 official reductions.
      ``reduccion`` carries the code (e.g. ``"R4"``); ``dobles``/``triples``
      mirror the reduction's spec (validated at PUT time against the
      canonical table in ``app.services.reducciones``).
    - ``intuicion`` — N columns where ``fijos`` parties share the same
      sign and the rest vary (the personal mode). ``fijos`` + ``columnas``
      are set; ``dobles``/``triples`` are 0. ``columns`` is ``porra.columnas``.
      The 14 rows in every column use exactly 1 sign (no dobles/triples
      in the persisted boleto — the "variation" lives across columns,
      not within a row).

    The CHECK constraint ``ck_porra_reduccion_only_reducida`` enforces
    that ``reduccion`` is non-null only when ``modo='reducida'``.
    """
    __tablename__ = "porra"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("group.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    jornada_id: Mapped[int] = mapped_column(
        ForeignKey("jornada.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    created_by_user_id: Mapped[int] = mapped_column(
        ForeignKey("user.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    budget_eur: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    dobles: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0, server_default="0")
    triples: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0, server_default="0")
    precio_apuesta_eur: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    modo: Mapped[PorraModo] = mapped_column(
        Enum(PorraModo, name="porra_modo", native_enum=False, length=16),
        default=PorraModo.multiple,
        server_default=PorraModo.multiple.value,
        nullable=False,
    )
    # ``fijos`` and ``columnas`` are nullable ints, only meaningful when
    # ``modo == 'intuicion'``. Stored as SmallInteger (—32k to 32k, plenty
    # for the 0..13 and 2..12 ranges). The DB constraint
    # ``ck_porra_intuicion_fields`` enforces "if modo=intuicion then both
    # are NOT NULL".
    fijos: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    columnas: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    status: Mapped[PorraStatus] = mapped_column(
        Enum(PorraStatus, name="porra_status", native_enum=False, length=16),
        default=PorraStatus.open,
        server_default=PorraStatus.open.value,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    # Final boleto (1:1 with the porra — at most one boleto per porra).
    # ``boleto_matches`` is None until the admin saves the boleto via PUT.
    boleto_set_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    boleto_set_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True,
    )
    boleto_matches: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    boleto_columns: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    reduccion: Mapped[str | None] = mapped_column(String(4), nullable=True)
    pleno_local: Mapped[str | None] = mapped_column(String(2), nullable=True)
    pleno_visitante: Mapped[str | None] = mapped_column(String(2), nullable=True)

    __table_args__ = (
        UniqueConstraint("group_id", "jornada_id", name="uq_porra_group_jornada"),
        CheckConstraint("dobles >= 0 AND triples >= 0", name="ck_porra_dt_nonneg"),
        CheckConstraint(
            "dobles + triples <= 14",
            name="ck_porra_dt_within_partidos",
        ),
        CheckConstraint("budget_eur > 0", name="ck_porra_budget_positive"),
        CheckConstraint(
            "precio_apuesta_eur > 0",
            name="ck_porra_precio_positive",
        ),
        CheckConstraint(
            "boleto_matches IS NULL OR jsonb_array_length(boleto_matches) = 14",
            name="ck_porra_boleto_matches_count",
        ),
        CheckConstraint(
            "reduccion IS NULL OR reduccion IN ('R1','R2','R3','R4','R5','R6')",
            name="ck_porra_reduccion_values",
        ),
        CheckConstraint(
            "pleno_local IS NULL OR pleno_local IN ('0','1','2','M')",
            name="ck_porra_pleno_local_valid",
        ),
        CheckConstraint(
            "pleno_visitante IS NULL OR pleno_visitante IN ('0','1','2','M')",
            name="ck_porra_pleno_visitante_valid",
        ),
        Index("ix_porra_group_jornada", "group_id", "jornada_id"),
    )