"""Pydantic DTOs for team-related responses."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class TeamOut(BaseModel):
    """One canonical team identity."""
    model_config = ConfigDict(from_attributes=True)

    id: int = Field(..., description="Numeric primary key")
    entity_id: str = Field(..., description="Stable slug, e.g. 'club-men-fc-barcelona'")
    competition_id: str = Field(..., description="FK to competition.id")
    canonical_name: str
    short_name: str | None = None
    gender: str | None = Field(None, description="'men' | 'women' | None")
    country: str | None = Field(None, description="3-letter ISO code (e.g. 'ESP')")
    logo_url: str | None = Field(None, description="Relative path or full URL to the team's logo")


class TeamListOut(BaseModel):
    """Paginated team list response."""
    items: list[TeamOut]
    total: int
    limit: int = Field(..., ge=1, le=200)
    offset: int = Field(..., ge=0)