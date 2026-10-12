"""DTOs for the superadmin panel endpoints."""
from __future__ import annotations

from datetime import datetime
from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.db.models import JornadaStatus


# ---------- Users ----------

class UserAdminOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    email: EmailStr
    display_name: str
    is_active: bool
    is_superadmin: bool
    created_at: datetime


class UserPatchIn(BaseModel):
    """Toggle superadmin. All fields optional — only what's sent is updated."""
    model_config = ConfigDict(extra="forbid")
    is_superadmin: bool | None = None
    is_active: bool | None = None


# ---------- Teams ----------

class TeamOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    entity_id: str
    canonical_name: str
    short_name: str | None
    gender: str | None
    gender_known: bool
    country: str | None
    logo_url: str | None
    competition_id: str
    deleted_at: datetime | None


class TeamCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_id: str = Field(..., min_length=1, max_length=80)
    canonical_name: str = Field(..., min_length=1, max_length=120)
    short_name: str | None = None
    gender: str | None = None
    gender_known: bool = True
    country: str | None = None
    logo_url: str | None = None
    competition_id: str


class TeamPatchIn(BaseModel):
    """All fields optional; only provided ones are updated."""
    model_config = ConfigDict(extra="forbid")
    canonical_name: str | None = None
    short_name: str | None = None
    gender: str | None = None
    gender_known: bool | None = None
    country: str | None = None
    logo_url: str | None = None
    competition_id: str | None = None


# ---------- Jornadas ----------

class JornadaAdminOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    temporada: int
    temporada_str: str
    numero: int
    status: JornadaStatus
    sale_opens_at: datetime | None
    voting_closes_at: datetime | None
    kickoff_first_match_at: datetime | None
    kickoff_last_match_at: datetime | None
    escrutinio_expected_at: datetime | None
    fetched_at: datetime
    deleted_at: datetime | None
    # Multi-column default (oct 2026). ``None`` means "no default";
    # the porra's create endpoint then requires ``columnas`` to be
    # supplied explicitly.
    default_columnas: int | None = None


class JornadaPatchIn(BaseModel):
    """All fields optional; only provided ones are updated."""
    model_config = ConfigDict(extra="forbid")
    status: JornadaStatus | None = None
    sale_opens_at: datetime | None = None
    voting_closes_at: datetime | None = None


class JornadaDefaultColumnasIn(BaseModel):
    """Body for ``PATCH /api/v1/admin/jornadas/{id}/default-columnas``.

    ``default_columnas`` is the jornada-level default for porras
    created in ``sencilla`` / ``intuicion`` mode against this jornada.
    Range 2..12 (the same window as ``porra.columnas``). Sending
    ``null`` clears the default (the porra's create endpoint then
    requires ``columnas`` to be supplied explicitly).
    """
    model_config = ConfigDict(extra="forbid")
    default_columnas: int | None = Field(default=None, ge=2, le=12)


# ---------- Composicion ----------

class ComposicionAdminOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    posicion: int
    local_team_id: int
    visitante_team_id: int
    local_raw: str
    visitante_raw: str
    kickoff_at: datetime | None
    competicion_raw: str | None


class ComposicionPatchIn(BaseModel):
    """Corrections to the scraper output. All fields optional."""
    model_config = ConfigDict(extra="forbid")
    local_team_id: int | None = None
    visitante_team_id: int | None = None
    kickoff_at: datetime | None = None
    competicion_raw: str | None = None
