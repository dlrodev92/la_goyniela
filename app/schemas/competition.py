"""Pydantic DTOs for competition-related responses."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class CompetitionOut(BaseModel):
    """One competition (league, cup, or association grouping)."""
    model_config = ConfigDict(from_attributes=True)

    id: str = Field(..., description="Stable slug, e.g. 'laliga-ea-sports'")
    name: str
    country: str | None = Field(None, description="Country name (e.g. 'Spain') or None for UEFA/FIFA")
    gender: str | None = Field(None, description="'men' | 'women' | None")
    tier: int | None = Field(None, description="Domestic tier (1 = top flight). None for UEFA/FIFA.")
    season: str | None = Field(None, description="e.g. '2026/27'")
    official_source_url: str | None = Field(None, description="URL to the official competition page")


class CompetitionListOut(BaseModel):
    """Paginated competition list response."""
    items: list[CompetitionOut]
    total: int
    limit: int = Field(..., ge=1, le=200)
    offset: int = Field(..., ge=0)